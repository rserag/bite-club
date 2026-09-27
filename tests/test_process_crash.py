import os
import subprocess
import sys

import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import actions, inbox, outbox, profile
from tests.helpers import message


async def test_process_death_rolls_back_and_restart_applies_once(service, store, settings):
    await service.accept([message()])
    code = """
import asyncio, os
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.application.service import Service
from nutrition_bot.config import BotSettings

async def main():
    settings = BotSettings(_env_file=None)
    store = Store(settings)
    service = Service(store, settings)
    original = service._reply
    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        os._exit(23)
    service._reply = crash
    await service.process_one()
asyncio.run(main())
"""
    env = os.environ | {
        "DATABASE_URL": settings.database_url,
        "TELEGRAM_BOT_TOKEN": settings.telegram_bot_token.get_secret_value(),
        "ALLOWED_TELEGRAM_USER_ID": "101",
        "ALLOWED_TELEGRAM_CHAT_ID": "101",
    }
    # Run in a real child process so SQLite observes an abruptly closed writer.
    # Cold aiogram imports take about 20 seconds on the smallest target VM;
    # this is a recovery assertion, not a startup performance deadline.
    import asyncio

    result = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", code], env=env, capture_output=True, timeout=60
    )
    assert result.returncode == 23, result.stderr.decode()
    async with store.engine.connect() as connection:
        for table in (profile, actions, outbox):
            assert await connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0
        assert await connection.scalar(sa.select(inbox.c.status)) == "pending"
    assert await service.process_one()
    assert not await service.process_one()
