from pathlib import Path

import pytest

from nutrition_bot.adapters.database.store import Store
from nutrition_bot.application.service import Service
from nutrition_bot.cli import migrate
from nutrition_bot.config import BotSettings


@pytest.fixture
def settings(tmp_path: Path) -> BotSettings:
    return BotSettings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite3'}",
        telegram_bot_token="123456:synthetic_token_for_offline_tests_only",
        allowed_telegram_user_id=101,
        allowed_telegram_chat_id=101,
    )


@pytest.fixture
def migrated(settings):
    migrate(settings)
    return settings


@pytest.fixture
async def store(migrated):
    value = Store(migrated)
    yield value
    await value.close()


@pytest.fixture
def service(store, settings):
    return Service(store, settings)
