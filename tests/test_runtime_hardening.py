import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from nutrition_bot.adapters.database.schema import actions, inbox, outbox, profile
from nutrition_bot.application.service import QueueFullError
from nutrition_bot.cli import migrate
from nutrition_bot.runtime.lock import database_lock
from nutrition_bot.runtime.watchdog import WatchState, check, decision
from tests.helpers import message


async def rows(store, table):
    async with store.engine.connect() as connection:
        return (await connection.execute(sa.select(table))).mappings().all()


async def test_inbox_limit_rolls_back_entire_batch_and_cursor(service, store, monkeypatch):
    monkeypatch.setattr("nutrition_bot.application.service.MAX_PENDING_UPDATES", 2)
    await service.accept([message(1)])
    with pytest.raises(QueueFullError):
        await service.accept([message(2), message(3)])
    assert await service.offset() == 2
    assert [row["update_id"] for row in await rows(store, inbox)] == [1]
    await service.accept([message(1), message(2)])
    assert await service.offset() == 3
    await service.process_one()
    await service.accept([message(3)])
    assert await service.offset() == 4


async def test_outbox_backpressure_leaves_action_pending_until_drain(service, store, monkeypatch):
    monkeypatch.setattr("nutrition_bot.application.service.MAX_PENDING_REPLIES", 2)
    await service.accept([message(1), message(2)])
    assert await service.process_one()
    assert not await service.process_one()
    assert len(await rows(store, actions)) == 1
    async with store.write() as connection:
        await connection.execute(sa.update(outbox).values(status="sent"))
    assert await service.process_one()
    assert len(await rows(store, actions)) == 2


async def test_real_sqlite_full_rolls_back_and_replay_is_once(service, store, monkeypatch):
    await service.accept([message(1)])
    original = service._reply

    async def exhaust(connection, *args, **kwargs):
        await original(connection, *args, **kwargs)
        await connection.exec_driver_sql("CREATE TABLE synthetic_space_probe (data BLOB)")
        pages = await connection.scalar(sa.text("PRAGMA page_count"))
        await connection.exec_driver_sql(f"PRAGMA max_page_count={pages}")
        # Real SQLITE_FULL, without filling the developer's disk.
        await connection.exec_driver_sql(
            "INSERT INTO synthetic_space_probe VALUES (zeroblob(1048576))"
        )

    monkeypatch.setattr(service, "_reply", exhaust)
    with pytest.raises(OperationalError, match="full"):
        await service.process_one()
    assert not await rows(store, actions)
    assert not await rows(store, profile)
    assert not await rows(store, outbox)
    assert (await rows(store, inbox))[0]["status"] == "pending"
    async with store.write() as connection:
        await connection.exec_driver_sql("PRAGMA max_page_count=1073741823")
    monkeypatch.setattr(service, "_reply", original)
    assert await service.process_one()
    assert not await service.process_one()
    assert len(await rows(store, actions)) == 1


@pytest.mark.parametrize("revision", ["0018_recovery_checkins", "0019_supplement_foundation"])
def test_previous_schema_migration_preserves_rows_and_snapshot(settings, tmp_path, revision):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    config.attributes["database_url"] = settings.resolved_database_url
    command.upgrade(config, revision)
    backup = tmp_path / "before.sqlite3"
    with closing(sqlite3.connect(settings.database_path)) as db:
        db.execute("CREATE TABLE synthetic_migration_probe (id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO synthetic_migration_probe VALUES (1, 'synthetic')")
        db.commit()
        with closing(sqlite3.connect(backup)) as copied:
            db.backup(copied)
    migrate(settings)
    migrate(settings)
    for path in (settings.database_path, backup):
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.execute("SELECT * FROM synthetic_migration_probe").fetchall() == [
                (1, "synthetic")
            ]


def test_stall_requires_consecutive_elapsed_checks_and_cooldown():
    state = WatchState()
    for now in (1000.0, 1060.0, 1120.0):
        state, result = decision(
            state, now=now, generation="one", running=True, reason="worker_stale"
        )
        assert result == "observe_stall"
    state, result = decision(
        state, now=1180.0, generation="one", running=True, reason="worker_stale"
    )
    assert result == "restart_needed"
    state.restarted_at = 1180.0
    _, result = decision(state, now=1240.0, generation="one", running=True, reason="worker_stale")
    assert result == "restart_cooldown"
    # A long gap or clock reversal restarts the observation window.
    for now in (100.0, 2000.0):
        _, result = decision(state, now=now, generation="one", running=True, reason="worker_stale")
        assert result == "observe_stall"


@pytest.mark.parametrize(
    "reason", ["running", "disk_low", "database_unreadable", "worker_stopped", "schema_mismatch"]
)
def test_only_local_stalls_allow_restart(reason):
    state = WatchState(generation="one", first_stale=1000.0, checks=4, checked_at=1180.0)
    next_state, result = decision(state, now=1240.0, generation="one", running=True, reason=reason)
    assert result == "no_restart" and next_state.checks == 0


def test_watchdog_respects_deploy_lock_and_stopped_container(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    deployment = tmp_path / "deployment"
    calls = []

    def docker(args):
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps({"Running": False, "Restarting": False, "StartedAt": "synthetic"}),
            "",
        )

    monkeypatch.setattr("nutrition_bot.runtime.watchdog.docker", docker)
    with database_lock(deployment), pytest.raises(RuntimeError):
        check("synthetic-bot", state, deployment, restart=True)
    assert not calls
    assert check("synthetic-bot", state, deployment, restart=True) == "no_restart"
    assert len(calls) == 1 and calls[0][0] == "inspect"


def test_restart_failure_reserves_cooldown(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(
        WatchState(
            generation="synthetic", first_stale=1000.0, checks=3, checked_at=1120.0
        ).model_dump_json()
    )
    calls = []

    def docker(args):
        calls.append(args)
        if args[0] == "inspect":
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"Running": True, "Restarting": False, "StartedAt": "synthetic"}),
                "",
            )
        if args[0] == "exec":
            return subprocess.CompletedProcess(args, 1, '{"reason":"worker_stale"}', "")
        return subprocess.CompletedProcess(args, 1, "", "synthetic failure")

    monkeypatch.setattr("nutrition_bot.runtime.watchdog.docker", docker)
    monkeypatch.setattr("nutrition_bot.runtime.watchdog.time.time", lambda: 1180.0)
    with pytest.raises(RuntimeError, match="cooldown"):
        check("synthetic-bot", state, tmp_path / "deploy", restart=True)
    assert WatchState.model_validate_json(state.read_text()).restarted_at == 1180.0


def test_corrupt_watchdog_clock_state_fails_closed():
    with pytest.raises(ValidationError):
        WatchState.model_validate_json('{"restarted_at": NaN}')
