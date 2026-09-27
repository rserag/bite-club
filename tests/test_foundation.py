import asyncio
import json
import logging
import sqlite3
import time
from contextlib import closing

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pydantic import ValidationError

from nutrition_bot.adapters.database.schema import heartbeat, metadata, profile
from nutrition_bot.cli import migrate
from nutrition_bot.config import BotSettings, StorageSettings
from nutrition_bot.logging import configure_logging
from nutrition_bot.runtime.health import COMPONENTS, check_health
from nutrition_bot.runtime.lock import database_lock
from nutrition_bot.runtime.worker import run_worker
from nutrition_bot.telegram.gateway import FatalGatewayError
from tests.helpers import FakeGateway


def test_missing_auth_fails_without_secret_disclosure(monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "ALLOWED_TELEGRAM_USER_ID", "ALLOWED_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError):
        BotSettings(_env_file=None)
    secret = "do-not-print-this-secret"
    with pytest.raises(ValidationError) as caught:
        BotSettings(
            _env_file=None,
            telegram_bot_token=secret,
            allowed_telegram_user_id=101,
            allowed_telegram_chat_id=101,
        )
    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///:memory:",
        "postgresql://localhost/db",
        "sqlite+aiosqlite:///file.db?mode=ro",
    ],
)
def test_nonlocal_or_volatile_database_rejected(url):
    with pytest.raises(ValidationError):
        StorageSettings(_env_file=None, database_url=url)


def test_invalid_timezone_rejected():
    with pytest.raises(ValidationError):
        StorageSettings(_env_file=None, app_timezone="invalid/not-a-zone")


def test_migration_is_repeatable_and_health_is_read_only(migrated):
    migrate(migrated)
    assert not check_health(migrated).healthy
    with closing(sqlite3.connect(migrated.database_path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("SELECT count(*) FROM runtime_heartbeat").fetchone()[0] == 0


def test_lock_excludes_worker_and_migration(settings):
    with database_lock(settings.database_path):
        with pytest.raises(RuntimeError):
            with database_lock(settings.database_path):
                pytest.fail("second lock acquired")
        with pytest.raises(RuntimeError):
            migrate(settings)


async def test_metadata_matches_frozen_migration_and_pragmas(store):
    async with store.engine.connect() as connection:
        differences = await connection.run_sync(
            lambda sync: compare_metadata(MigrationContext.configure(sync), metadata)
        )
        assert differences == []
        assert await connection.scalar(sa.text("PRAGMA foreign_keys")) == 1
        assert await connection.scalar(sa.text("PRAGMA synchronous")) == 2


async def test_profile_singleton_constraint(store):
    with pytest.raises(sa.exc.IntegrityError):
        async with store.write() as connection:
            await connection.execute(sa.insert(profile).values(id=2, timezone="UTC", created_at=0))


async def test_health_distinguishes_api_outage_from_dead_worker(service, store, settings):
    for component in COMPONENTS:
        await service.pulse(component)
    await service.pulse("receiver", "degraded")
    health = check_health(settings)
    assert health.healthy and health.degraded == ("receiver",)
    await service.pulse("receiver")
    assert check_health(settings).degraded == ("receiver",)
    async with store.write() as connection:
        await connection.execute(
            sa.update(heartbeat)
            .where(heartbeat.c.component == "processor")
            .values(touched_at=time.time() - 120)
        )
    assert not check_health(settings).healthy


async def test_fatal_loop_failure_stops_other_loops(service, settings):
    class FatalGateway(FakeGateway):
        async def poll(self, offset):
            raise FatalGatewayError()

    with pytest.raises(ExceptionGroup):
        await asyncio.wait_for(run_worker(service, FatalGateway(), asyncio.Event()), timeout=5)
    assert not check_health(settings).healthy


async def test_graceful_stop_leaves_unhealthy_heartbeat(service, settings):
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(service, FakeGateway(), stop))
    for _ in range(100):
        if check_health(settings).healthy:
            break
        await asyncio.sleep(0.01)
    assert check_health(settings).healthy
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert not check_health(settings).healthy


def test_logs_do_not_render_exception_content(capsys):
    configure_logging("DEBUG")
    try:
        raise RuntimeError("secret-token-and-private-meal")
    except RuntimeError:
        logging.getLogger("nutrition_bot.test").exception(
            "synthetic_failure", extra={"error_type": "RuntimeError"}
        )
    logging.getLogger("aiogram").error("another-secret")
    output = capsys.readouterr().err
    assert "secret" not in output
    assert json.loads(output)["event"] == "synthetic_failure"
