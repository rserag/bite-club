import asyncio
import json
import os
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from aiogram import Bot

from nutrition_bot.config import StorageSettings
from nutrition_bot.runtime.health import check_health
from tools.telegram_e2e.config import ROOT, InfrastructureError, Settings, SetupError, confined
from tools.telegram_e2e.driver import Message


async def preflight(settings: Settings) -> None:
    async with Bot(settings.bot_token.get_secret_value()) as bot:
        try:
            async with asyncio.timeout(settings.step_timeout):
                me = await bot.get_me()
                if (
                    me.id != settings.bot_id
                    or not me.username
                    or me.username.lower() != settings.bot_username.lower()
                ):
                    raise SetupError("bot_token_identity_mismatch")
                if (await bot.get_webhook_info()).url:
                    raise SetupError("test_bot_has_webhook")
        except SetupError:
            raise
        except Exception:
            raise InfrastructureError("bot_preflight_failed") from None


async def drain(settings: Settings) -> int:
    """Exclusive setup poller, closed before the worker starts. Bounded, never production."""
    offset = 0
    async with Bot(settings.bot_token.get_secret_value()) as bot:
        try:
            async with asyncio.timeout(settings.step_timeout):
                for _ in range(100):
                    updates = await bot.get_updates(
                        offset=offset,
                        timeout=0,
                        limit=100,
                        allowed_updates=["message", "edited_message", "callback_query"],
                    )
                    if not updates:
                        return offset
                    offset = max(update.update_id for update in updates) + 1
        except Exception:
            raise InfrastructureError("test_backlog_drain_failed") from None
    raise InfrastructureError("test_backlog_drain_limit")


class Environment:
    def __init__(self, settings: Settings, path: Path, root: Path) -> None:
        self.settings = settings
        self.path = confined(path, root)
        self.root = root
        self.database = self.path / "app.sqlite3"
        self.process: asyncio.subprocess.Process | None = None
        self.owned = False

    def child_env(self) -> dict[str, str]:
        # No inherited .env/provider secrets. Worker cwd is the disposable directory.
        values = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
        values.update(
            {
                "PYTHONPATH": str(ROOT / "src"),
                "PYTHONUNBUFFERED": "1",
                "DATABASE_URL": f"sqlite+aiosqlite:///{self.database}",
                "TELEGRAM_BOT_TOKEN": self.settings.bot_token.get_secret_value(),
                "ALLOWED_TELEGRAM_USER_ID": str(self.settings.user_id),
                "ALLOWED_TELEGRAM_CHAT_ID": str(self.settings.user_id),
                "APP_TIMEZONE": "UTC",
                "LOG_LEVEL": "ERROR",
            }
        )
        return values

    async def command(self, name: str) -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "nutrition_bot",
            name,
            cwd=self.path,
            env=self.child_env(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(60):
                if await process.wait() != 0:
                    raise InfrastructureError("environment_command_failed")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def prepare(self, offset: int) -> None:
        self.path.mkdir(mode=0o700)  # Exclusive creation; never reset an existing directory.
        self.owned = True
        (self.path / ".e2e-owned").write_text("disposable-telegram-e2e-v1")
        await self.command("migrate")
        from nutrition_bot.adapters.database.foods import publish_reviewed_food
        from nutrition_bot.adapters.database.store import Store
        from nutrition_bot.domain.food import NutrientInput, ReviewedFoodInput

        storage = StorageSettings(
            _env_file=None, database_url=f"sqlite+aiosqlite:///{self.database}"
        )
        store = Store(storage)
        try:
            async with store.write() as connection:
                for name, energy, protein in (("rice", "100", "10"), ("chicken", "200", "20")):
                    record = ReviewedFoodInput(
                        name=name,
                        preparation="cooked",
                        source_reference="Synthetic E2E fixture, not dietary reference data",
                        source_license="Synthetic test data",
                        nutrients=(
                            NutrientInput(code="energy", amount=energy, unit="kcal"),
                            NutrientInput(code="protein", amount=protein, unit="g"),
                            NutrientInput(code="fat", amount="0", unit="g"),
                            NutrientInput(code="fiber", amount=None, unit="g"),
                        ),
                    )
                    await publish_reviewed_food(connection, record)
            # Setup only: establish the transport cursor before the worker exists.
            with closing(sqlite3.connect(self.database)) as db:
                db.execute(
                    "INSERT OR REPLACE INTO telegram_cursor(id,next_offset) VALUES(1,?)", (offset,)
                )
                db.commit()
        finally:
            await store.close()

    def read(self, query: str, parameters: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            return list(db.execute(query, parameters))

    async def start(self) -> None:
        if self.process is not None:
            raise SetupError("worker_already_started")
        started_at = time.time()
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "nutrition_bot",
            "run",
            cwd=self.path,
            env=self.child_env(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        storage = StorageSettings(
            _env_file=None, database_url=f"sqlite+aiosqlite:///{self.database}"
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.returncode is not None:
                raise InfrastructureError("worker_exited")
            health = check_health(storage)
            successes = self.read(
                "SELECT last_success_at FROM runtime_heartbeat WHERE component='receiver'"
            )
            if (
                health.healthy
                and not health.degraded
                and successes
                and successes[0][0]
                and successes[0][0] >= started_at
            ):
                return
            await asyncio.sleep(0.2)
        raise InfrastructureError("worker_health_timeout")

    async def settled(self) -> None:
        deadline = time.monotonic() + self.settings.step_timeout
        while time.monotonic() < deadline:
            if self.process is None or self.process.returncode is not None:
                raise InfrastructureError("worker_not_running")
            if self.read("SELECT count(*) FROM outbox WHERE status='failed'")[0][0]:
                raise InfrastructureError("worker_failed_reply")
            if self.read("SELECT count(*) FROM telegram_inbox WHERE status='failed'")[0][0]:
                raise InfrastructureError("worker_failed_input")
            pending = self.read("SELECT count(*) FROM outbox WHERE status IN ('queued','sending')")[
                0
            ][0]
            pending += self.read("SELECT count(*) FROM telegram_inbox WHERE status='pending'")[0][0]
            if not pending:
                return
            await asyncio.sleep(0.1)
        raise InfrastructureError("worker_drain_timeout")

    async def delivered(self, message: Message) -> None:
        # Private-chat message IDs are account-local: MTProto user IDs must not
        # be compared with the bot's Bot API message IDs. Match the issued payload.
        deadline = time.monotonic() + self.settings.step_timeout
        while time.monotonic() < deadline:
            for payload, status in self.read(
                "SELECT payload,status FROM outbox WHERE kind='message' ORDER BY id DESC"
            ):
                if json.loads(payload).get("text") == message.text:
                    if status == "sent":
                        return
                    break
            await asyncio.sleep(0.05)
        raise InfrastructureError("receipt_delivery_not_committed")

    async def stop(self) -> None:
        if self.process is not None:
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    async with asyncio.timeout(30):
                        await self.process.wait()
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            self.process = None

    async def close(self) -> None:
        await self.stop()
        if self.owned:
            confined(self.path, self.root)
            marker = confined(self.path / ".e2e-owned", self.root)
            if marker.read_text() != "disposable-telegram-e2e-v1":
                raise SetupError("disposable_marker_mismatch")
            shutil.rmtree(self.path)
            self.owned = False
