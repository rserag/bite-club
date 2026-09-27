"""Run scenario assertions against the real service via an offline Telegram double.

This checks scenario correctness; it is not evidence of live Telegram acceptance.
User and bot message IDs intentionally differ, as they can in private chats.
"""

from types import SimpleNamespace

import pytest
from aiogram.types import Update

from nutrition_bot.adapters.database.store import Store
from nutrition_bot.application.service import Service
from nutrition_bot.config import BotSettings
from nutrition_bot.runtime.worker import send_one
from tools.telegram_e2e import scenarios
from tools.telegram_e2e.config import Settings
from tools.telegram_e2e.driver import Driver
from tools.telegram_e2e.environment import Environment
from tools.telegram_e2e.reporting import Recorder


class TelegramDouble:
    def __init__(self, settings, env):
        self.settings = settings
        self.sequence = 0
        self.message_number = 100
        self.messages = {}
        self.driver = None
        storage = BotSettings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{env.database}",
            telegram_bot_token=settings.bot_token,
            allowed_telegram_user_id=settings.user_id,
            allowed_telegram_chat_id=settings.user_id,
        )
        self.store = Store(storage)
        self.service = Service(self.store, storage)
        self.ack = None

    async def dispatch(self, payload):
        self.sequence += 1
        await self.service.accept([Update.model_validate({"update_id": self.sequence, **payload})])
        assert await self.service.process_one()
        while await send_one(self.service, self):
            pass

    async def send_message(self, peer, text, *args, reply_to=None, parse_mode=None):
        self.message_number += 1
        mid = self.message_number
        if args:  # Gateway send: peer is chat ID, args is button token and optional buttons.
            token = args[0]
            buttons = args[1] if len(args) > 1 else None
            buttons = buttons or (
                [{"text": "Refresh status", "callback_data": f"status:{token}"}] if token else []
            )
            raw = SimpleNamespace(
                id=mid + 20000,
                raw_text=text,
                buttons=[[SimpleNamespace(text=b["text"]) for b in buttons]],
            )

            async def click(*, text):
                data = next(b["callback_data"] for b in buttons if b["text"] == text)
                self.ack = None
                await self.dispatch(
                    {
                        "callback_query": {
                            "id": f"callback-{self.sequence + 1}",
                            "chat_instance": "synthetic",
                            "from": self.user(),
                            "data": data,
                            "message": self.message(mid, raw.raw_text, bot=True),
                        }
                    }
                )
                return SimpleNamespace(message=self.ack)

            raw.click = click
            self.messages[raw.id] = self.message(mid, text, bot=True)
            self.driver.collector.feed(
                self.settings.bot_id, self.settings.bot_id, self.driver.convert(raw)
            )
            return mid
        raw = SimpleNamespace(id=mid + 10000, raw_text=text, buttons=[])
        payload = self.message(mid, text)
        self.messages[raw.id] = payload
        if reply_to is not None:
            payload["reply_to_message"] = self.messages[reply_to]
        await self.dispatch({"message": payload})
        return raw

    def user(self):
        return {"id": self.settings.user_id, "is_bot": False, "first_name": "Synthetic"}

    def message(self, mid, text, *, bot=False):
        import time

        return {
            "message_id": mid,
            "date": int(time.time()),
            "text": text,
            "chat": {"id": self.settings.user_id, "type": "private"},
            "from": {"id": self.settings.bot_id, "is_bot": True, "first_name": "Bot"}
            if bot
            else self.user(),
        }

    async def edit_message(self, peer, mid, text, **kwargs):
        payload = dict(self.messages[mid], text=text)
        await self.dispatch({"edited_message": payload})

    async def answer_callback(self, callback_id, text):
        self.ack = text


@pytest.mark.parametrize(
    "scenario",
    [
        scenarios.test_smoke_start_status,
        scenarios.test_smoke_measured_and_unknown,
        scenarios.test_rough_revision_and_repeat_approval,
        scenarios.test_reply_correction,
        scenarios.test_original_edit_guidance,
        scenarios.test_delete_and_undo,
        scenarios.test_supplement_reporting,
        scenarios.test_supplement_adherence,
        scenarios.test_nutrient_coverage,
    ],
)
async def test_scenario_contract(tmp_path, scenario):
    settings = Settings(
        dedicated_test_bot=True,
        api_id=12345,
        api_hash="a" * 32,
        bot_token="678901:synthetic_token_for_offline_tests_only",
        bot_id=678901,
        bot_username="synthetic_test_bot",
        user_id=54321,
    )
    # Pacing alone is disabled for the fully offline double.
    settings = settings.model_copy(update={"pace_seconds": 0})
    env = Environment(settings, tmp_path / "state", tmp_path)
    actor = None
    try:
        await env.prepare(0)
        actor = TelegramDouble(settings, env)
        driver = Driver(settings, actor, Recorder(settings))
        actor.driver = driver
        await scenario((driver, env))
        driver.assert_idle()
    finally:
        if actor:
            await actor.store.close()
        await env.close()
