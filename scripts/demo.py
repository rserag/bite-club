"""Exercise the real service offline, with synthetic values and temporary storage."""

import asyncio
import tempfile
import time
from pathlib import Path

import sqlalchemy as sa
from aiogram.types import Update

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.schema import outbox
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.application.service import Service
from nutrition_bot.cli import migrate
from nutrition_bot.config import BotSettings
from nutrition_bot.domain.food import NutrientInput, ReviewedFoodInput

USER = {"id": 101, "is_bot": False, "first_name": "Demo"}
CHAT = {"id": 101, "type": "private"}


async def walkthrough(settings: BotSettings) -> None:
    store = Store(settings)
    service = Service(store, settings)
    sequence = 0
    timestamp = int(time.time())

    async def dispatch(payload):
        nonlocal sequence
        sequence += 1
        update = Update.model_validate({"update_id": sequence, **payload})
        await service.accept([update])
        assert await service.process_one()
        key = (
            f"callback:{update.callback_query.id}"
            if update.callback_query
            else f"update:{sequence}"
        )
        async with store.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(outbox).where(
                            outbox.c.action_key == key, outbox.c.kind == "message"
                        )
                    )
                )
                .mappings()
                .one()
            )
        mid = 1000 + row["id"]
        # Simulate Telegram acknowledging delivery. No gateway/network is used.
        await service.finish_reply(row["id"], "sent", message_id=mid)
        print("bot>", row["payload"]["text"], "\n")
        return row, mid

    async def send(text):
        print("you>", text)
        return await dispatch(
            {
                "message": {
                    "message_id": sequence + 1,
                    "date": timestamp,
                    "from": USER,
                    "chat": CHAT,
                    "text": text,
                }
            }
        )

    try:
        async with store.write() as conn:
            await publish_reviewed_food(
                conn,
                ReviewedFoodInput(
                    name="rice",
                    preparation="cooked",
                    source_reference="Synthetic demo values, not a real food record",
                    source_license="Synthetic fixture",
                    nutrients=(
                        NutrientInput(code="energy", amount="100", unit="kcal"),
                        NutrientInput(code="protein", amount="10", unit="g"),
                        NutrientInput(code="fat", amount="0", unit="g"),
                        NutrientInput(code="fiber", amount="1", unit="g"),
                    ),
                ),
            )
        await send("150g rice")
        draft, mid = await send("about 120g rice")
        approval = next(b for b in draft["payload"]["buttons"] if b["text"] == "Approve estimate")
        print("you> [tap Approve estimate]")
        await dispatch(
            {
                "callback_query": {
                    "id": "demo-approval",
                    "chat_instance": "synthetic-demo",
                    "from": USER,
                    "data": approval["callback_data"],
                    "message": {
                        "message_id": mid,
                        "date": timestamp,
                        "chat": CHAT,
                        "from": {"id": 123456, "is_bot": True, "first_name": "Bite Club demo"},
                        "text": draft["payload"]["text"],
                    },
                }
            }
        )
        await send("/today short")
    finally:
        await store.close()


def main() -> None:
    print("BITE CLUB — first rule: log your meals.")
    print("Offline demo. All food values and identities below are synthetic.\n")
    with tempfile.TemporaryDirectory(prefix="bite-club-demo-") as directory:
        settings = BotSettings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{Path(directory) / 'demo.sqlite3'}",
            app_timezone="UTC",
            telegram_bot_token="123456:synthetic_token_for_offline_tests_only",
            allowed_telegram_user_id=101,
            allowed_telegram_chat_id=101,
        )
        migrate(settings)
        asyncio.run(walkthrough(settings))
    print("Demo finished. Temporary database removed; no Telegram messages sent.")


if __name__ == "__main__":
    main()
