import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from nutrition_bot.adapters.database.drafts import (
    close_draft,
    create_draft,
    expire_drafts,
    revise_draft,
)
from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.meals import MealItemInput, create_meal
from nutrition_bot.adapters.database.schema import heartbeat, metadata
from nutrition_bot.adapters.database.schema_ai import ai_plan_invocations, ai_requests
from nutrition_bot.adapters.database.schema_ai_metrics import ai_metrics
from nutrition_bot.adapters.database.schema_drafts import meal_drafts
from nutrition_bot.application.ai_metrics import link_draft, metrics_summary, record_metric
from nutrition_bot.cli import main, migrate
from nutrition_bot.runtime.lock import database_lock
from tests.test_draft_storage import DAY, NOW, action, proposed
from tests.test_food_storage import reviewed_food


async def measured(connection, key="synthetic-request", **overrides):
    values = dict(
        request_key=key,
        role="meal_text",
        source="normal",
        handling="ai",
        status="ready",
        created_at=NOW,
        completed_at=NOW + 1,
        model="synthetic/model",
        prompt_version="meal-extraction-v2",
        schema_version="meal-proposal-v1",
        reasoning_effort="default",
        auth_ms=5,
        model_catalog_ms=10,
        inference_ms=985,
        inference_sent=True,
    )
    values.update(overrides)
    return await record_metric(connection, **values)


async def test_measurement_replay_preserves_first_usage_and_no_content_columns(store):
    async with store.write() as connection:
        assert await measured(connection, input_tokens=90, output_tokens=30, reasoning_tokens=0)
        assert await measured(connection, input_tokens=500, output_tokens=300, status="unknown")
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(ai_metrics))).mappings().one()
        assert row["input_tokens"] == 90 and row["output_tokens"] == 30
        assert row["reasoning_tokens"] == 0 and row["cached_tokens"] is None
        assert row["status"] == "ready" and row["elapsed_ms"] == 1000
        assert row["attempts_sent"] == 1
    assert {"content", "payload", "outcome", "text", "photo", "generation_id"}.isdisjoint(
        ai_metrics.c.keys()
    )


async def test_unsent_ai_denial_upgrades_once_then_preserves_first_sent_measurement(store):
    async with store.write() as connection:
        assert await measured(connection, status="disabled", inference_sent=False)
        assert await measured(connection, status="ready", input_tokens=90, output_tokens=30)
        assert await measured(connection, status="unknown", input_tokens=500, output_tokens=300)
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(ai_metrics))).mappings().one()
        assert row["status"] == "ready" and row["inference_sent"]
        assert row["attempts_sent"] == 1
        assert row["input_tokens"] == 90 and row["output_tokens"] == 30


@pytest.mark.parametrize(
    "initial", [{"handling": "local", "status": "handled"}, {"source": "evaluation"}]
)
async def test_unsent_local_and_different_source_records_cannot_upgrade(store, initial):
    async with store.write() as connection:
        assert await measured(connection, inference_sent=False, **initial)
        assert await measured(connection, input_tokens=90, output_tokens=30)
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(ai_metrics))).mappings().one()
        assert not row["inference_sent"] and row["input_tokens"] is None
        assert all(row[field] == value for field, value in initial.items())


async def test_linked_unsent_record_cannot_upgrade(store):
    async with store.write() as connection:
        food = await publish_reviewed_food(connection, reviewed_food())
        await action(connection, "synthetic-create")
        draft = await create_draft(
            connection, proposed(food), action_key="synthetic-create", now=NOW
        )
        assert await measured(
            connection, inference_sent=False, draft_id=draft.id, initial_revision=draft.revision
        )
        assert await measured(connection, input_tokens=90, output_tokens=30)
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(ai_metrics))).mappings().one()
        assert not row["inference_sent"] and row["input_tokens"] is None
        assert row["draft_id"] == draft.id and row["initial_revision"] == 1


async def test_database_measurement_failure_rolls_back_savepoint_preserves_core_write(
    store, caplog
):
    async with store.write() as connection:
        await connection.exec_driver_sql(
            "CREATE TRIGGER synthetic_metric_failure BEFORE INSERT ON ai_metrics "
            "BEGIN SELECT RAISE(ABORT, 'PRIVATE provider response'); END"
        )
        assert not await measured(connection)
        await connection.execute(
            sa.insert(heartbeat).values(component="synthetic", touched_at=NOW, state="ok")
        )
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(heartbeat)) == 1
        assert await connection.scalar(sa.select(sa.func.count()).select_from(ai_metrics)) == 0
    assert "ai_metric_unavailable" in caplog.text
    assert "PRIVATE" not in caplog.text and "synthetic-request" not in caplog.text


@pytest.mark.parametrize(
    "changes",
    [
        {"input_tokens": -1},
        {"output_tokens": True},
        {"cached_tokens": 1.5},
        {"created_at": float("nan")},
        {"status": "private meal content"},
        {"model": "https://private-provider.invalid/account"},
        {"prompt_version": "private meal text"},
        {"failure_category": "private provider response"},
        {"draft_id": 999, "initial_revision": 1},
        {"draft_id": 1},
        {"attempts_sent": 3},
        {"attempts_sent": 0},
    ],
)
async def test_invalid_measurement_is_optional_and_never_persisted(store, changes):
    async with store.write() as connection:
        assert not await measured(connection, **changes)
        await connection.execute(
            sa.insert(heartbeat).values(component="synthetic", touched_at=NOW, state="ok")
        )
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(heartbeat)) == 1
        assert await connection.scalar(sa.select(sa.func.count()).select_from(ai_metrics)) == 0


async def test_report_separates_sources_windows_usage_and_preflight_failures(store, settings):
    async with store.write() as connection:
        assert await measured(
            connection,
            "normal-one",
            input_tokens=100,
            output_tokens=50,
            reasoning_tokens=0,
            cached_tokens=20,
            charged_micro_usd=150,
        )
        assert await measured(
            connection,
            "normal-timeout",
            status="unavailable",
            failure_category="timeout",
            elapsed_ms=30000,
            inference_ms=29900,
            attempts_sent=2,
        )
        assert await measured(
            connection,
            "normal-preflight",
            status="unavailable",
            failure_category="auth",
            inference_sent=False,
            auth_ms=600,
            model_catalog_ms=None,
            inference_ms=None,
        )
        assert await measured(
            connection,
            "normal-local",
            handling="local",
            status="handled",
            inference_sent=False,
            model=None,
            elapsed_ms=5,
        )
        assert await measured(connection, "eval:synthetic:one", source="evaluation", input_tokens=7)
        assert await measured(connection, "old-request", created_at=NOW - 4 * 86400)
        assert await measured(connection, "future-request", created_at=NOW + 5)
        for key in ("normal-one", "eval:synthetic:one"):
            await connection.execute(
                sa.insert(ai_requests).values(
                    request_key=key,
                    role="meal_text",
                    state="done",
                    created_at=NOW,
                )
            )
            await connection.execute(
                sa.insert(ai_plan_invocations).values(
                    request_key=key,
                    day=datetime.fromtimestamp(NOW, UTC).date().isoformat(),
                    state="done",
                    created_at=NOW,
                )
            )
    report = metrics_summary(settings.database_path, days=3, now=NOW + 2)
    normal = report["sources"]["normal"]
    assert normal["requests"] == 4 and normal["handling"] == {"local": 1, "ai": 3}
    assert normal["inference_sent"] == 2 and normal["attempts_sent"] == 3
    assert normal["latency"]["ai"] == {
        "known": 3,
        "unknown": 0,
        "median_ms": 1000,
        "p95_ms": 30000,
    }
    assert normal["usage"]["input_tokens"] == {"known": 1, "unknown": 1, "total": 100}
    assert normal["usage"]["reasoning_tokens"] == {"known": 1, "unknown": 1, "total": 0}
    assert normal["usage"]["charged_micro_usd"] == {"known": 1, "unknown": 1, "total": 150}
    assert normal["failures"] == {"auth": 1, "timeout": 1}
    assert report["sources"]["evaluation"]["requests"] == 1
    assert report["sources"]["evaluation"]["usage"]["input_tokens"]["total"] == 7
    assert report["subscription_invocations"] == [
        {"day": "2024-01-15", "normal": 1, "evaluation": 1, "total": 2}
    ]
    output = json.dumps(report)
    for private_field in ("request_key", "draft_id", "normal-one", "eval:synthetic:one"):
        assert private_field not in output


async def test_subscription_counts_include_full_overlapping_utc_days(store, settings):
    start_day = NOW - 3 * 86400
    async with store.write() as connection:
        for key, when in (
            ("normal-before-window-same-day", start_day - 3600),
            ("normal-earlier-day", start_day - 86400),
            ("eval:synthetic:inside", NOW),
        ):
            await connection.execute(
                sa.insert(ai_requests).values(
                    request_key=key,
                    role="meal_text",
                    state="done",
                    created_at=when,
                )
            )
            await connection.execute(
                sa.insert(ai_plan_invocations).values(
                    request_key=key,
                    day=datetime.fromtimestamp(when, UTC).date().isoformat(),
                    state="done",
                    created_at=when,
                )
            )
    report = metrics_summary(settings.database_path, days=3, now=NOW)
    assert report["subscription_invocations_scope"] == "full_utc_days_overlapping_window"
    assert report["subscription_invocations"] == [
        {"day": "2024-01-12", "normal": 1, "evaluation": 0, "total": 1},
        {"day": "2024-01-15", "normal": 0, "evaluation": 1, "total": 1},
    ]


async def test_draft_outcomes_use_original_revision_and_survive_content_purge(store, settings):
    async with store.write() as connection:
        food = await publish_reviewed_food(connection, reviewed_food())
        for index, state in enumerate(("saved", "saved_edited", "cancelled", "expired", "open")):
            key = f"synthetic-{state}"
            await action(connection, key)
            content = proposed(food, source_message_id=100 + index)
            draft = await create_draft(connection, content, action_key=key, now=NOW)
            assert await measured(connection, key)
            assert await link_draft(connection, key, draft.id, draft.revision)
            if state == "saved_edited":
                await action(connection, key + "-edit")
                draft = await revise_draft(
                    connection,
                    draft.id,
                    draft.revision,
                    proposed(food, source_message_id=100 + index, label="Changed"),
                    action_key=key + "-edit",
                    now=NOW + 1,
                )
                # A replay or later display cannot replace the original baseline.
                assert await link_draft(connection, key, draft.id, draft.revision)
            if state.startswith("saved"):
                await action(connection, key + "-save")
                meal = await create_meal(
                    connection,
                    items=(MealItemInput(food.version_id, 125000, "125", "g"),),
                    label="Synthetic lunch",
                    local_date=DAY,
                    timezone="UTC",
                    consumed_at=NOW,
                    action_key=key + "-save",
                    source_chat_id=101,
                    source_message_id=100 + index,
                )
                await close_draft(
                    connection,
                    draft.id,
                    draft.revision,
                    state="saved",
                    action_key=key + "-save",
                    now=NOW + 2,
                    saved_meal_id=meal.id,
                )
            elif state == "cancelled":
                await action(connection, key + "-cancel")
                await close_draft(
                    connection,
                    draft.id,
                    draft.revision,
                    state="cancelled",
                    action_key=key + "-cancel",
                    now=NOW + 2,
                )
            elif state == "expired":
                # Exercise actual expiry while retaining another still-open draft below.
                await expire_drafts(connection, now=NOW + 7 * 86400)
        assert await measured(connection, "synthetic-link-failure")
        assert not await link_draft(connection, "synthetic-link-failure", 999, 1)
    report = metrics_summary(settings.database_path, days=8, now=NOW + 7 * 86400 + 1)
    assert report["sources"]["normal"]["drafts"] == {
        "linked": 5,
        "open": 1,
        "saved": 2,
        "cancelled": 1,
        "expired": 1,
        "removed": 0,
        "saved_unchanged": 1,
        "saved_edited": 1,
    }
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(ai_metrics.c.initial_revision).where(
                    ai_metrics.c.request_key == "synthetic-saved_edited"
                )
            )
            == 1
        )
        assert (
            await connection.scalar(
                sa.select(meal_drafts.c.content).where(meal_drafts.c.state == "saved")
            )
            is None
        )


async def test_report_is_read_only_while_worker_lock_is_held(store, settings):
    async with store.write() as connection:
        assert await measured(connection)
    with database_lock(settings.database_path):
        with closing(sqlite3.connect(settings.database_path)) as db:
            before = db.execute("SELECT * FROM ai_metrics").fetchall()
        assert (
            metrics_summary(settings.database_path, now=NOW + 1)["sources"]["normal"]["requests"]
            == 1
        )
        with closing(sqlite3.connect(settings.database_path)) as db:
            assert db.execute("SELECT * FROM ai_metrics").fetchall() == before


@pytest.mark.parametrize("days", [0, 31, True, 1.5])
def test_report_rejects_unbounded_windows(settings, days):
    with pytest.raises(ValueError):
        metrics_summary(settings.database_path, days=days)
    assert not settings.database_path.exists()


def test_report_missing_database_cannot_create_it(settings):
    with pytest.raises(sqlite3.OperationalError):
        metrics_summary(settings.database_path)
    assert not settings.database_path.exists()


def test_cli_metrics_needs_no_telegram_secrets_and_does_not_take_worker_lock(
    migrated, monkeypatch, capsys
):
    monkeypatch.setenv("DATABASE_URL", migrated.resolved_database_url)
    monkeypatch.setattr("sys.argv", ["nutrition-bot", "ai-metrics", "--days", "3"])
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with database_lock(migrated.database_path):
        main()
    output = json.loads(capsys.readouterr().out)
    assert output["days"] == 3
    assert output["sources"]["normal"]["requests"] == 0
    assert output["sources"]["normal"]["usage"]["input_tokens"]["total"] is None


def test_metrics_migration_upgrade_from_previous_schema_and_downgrade(settings):
    config = Config()
    config.set_main_option("script_location", "migrations")
    config.attributes["database_url"] = settings.resolved_database_url
    command.upgrade(config, "0024_guided_flows")
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path)) as db:
        assert db.execute("SELECT count(*) FROM ai_metrics").fetchone() == (0,)
    command.downgrade(config, "0024_guided_flows")
    with closing(sqlite3.connect(settings.database_path)) as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name = 'ai_metrics'").fetchone()
            is None
        )
    assert "ai_metrics" in metadata.tables
