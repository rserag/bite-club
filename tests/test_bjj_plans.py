import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from nutrition_bot.adapters.database.schema_training import (
    bjj_session_details,
    bjj_template_revisions,
    training_plan_events,
    training_schedule_rules,
    training_session_revisions,
    training_sessions,
)
from tests.helpers import message
from tests.test_telegram_meals import process

MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")


async def test_template_is_estimated_context_and_never_fills_completed_session(service, store):
    template = await process(
        service,
        store,
        message(
            1,
            "/bjj template set duration=60 warmup=8-12 technical=20-30 "
            "positional=1-2x3 sparring=2-4x5",
        ),
    )
    assert "estimates only" in template["payload"]["text"]
    assert "never fill a completed session" in template["payload"]["text"]

    saved = await process(service, store, message(2, "BJJ 60 min medium"))
    assert "No exercises, sets or BJJ rounds were inferred" in saved["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(bjj_session_details))
            == 0
        )
        row = (await connection.execute(sa.select(bjj_template_revisions))).mappings().one()
    assert row["positional_min_rounds"] == 1
    assert row["positional_max_rounds"] == 2
    assert row["positional_round_minutes"] == 3


async def test_reported_bjj_details_are_partial_immutable_and_not_added_to_total(service, store):
    await process(service, store, message(1, "BJJ 60 min hard rpe 8"))
    detailed = await process(
        service,
        store,
        message(2, "/bjj details T1r1 warmup=10 positional=2x3 sparring=3x5"),
    )
    text = detailed["payload"]["text"]
    assert "Updated BJJ details T1r2" in text
    assert "Warm-up: 10 min" in text
    assert "Technical:" not in text
    assert "Sparring: 3 × 5 min" in text
    assert "Stage minutes are not added to the reported total" in text
    assert "60 min · hard" in text

    edited = await process(service, store, message(3, "/bjj edit T1r2 55 min medium rpe 7"))
    assert "Sparring: 3 × 5 min" in edited["payload"]["text"]
    async with store.engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    sa.select(bjj_session_details).order_by(
                        bjj_session_details.c.training_revision_id
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 2
    assert rows[0]["technical_minutes"] is None


async def test_bjj_detail_rejects_known_stage_sum_beyond_reported_total(service, store):
    await process(service, store, message(1, "BJJ 30 min medium"))
    rejected = await process(
        service,
        store,
        message(2, "/bjj details T1r1 warmup=10 technical=10 sparring=3x5"),
    )
    assert "Reported stage time is 35 min" in rejected["payload"]["text"]
    assert "No training session changed" in rejected["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(bjj_session_details))
            == 0
        )


async def test_bjj_summary_cannot_be_reduced_below_preserved_stage_sum(service, store):
    await process(service, store, message(1, "BJJ 60 min medium"))
    await process(
        service,
        store,
        message(2, "/bjj details T1r1 warmup=10 technical=20 sparring=3x5"),
    )
    rejected = await process(service, store, message(3, "/bjj edit T1r2 40 min medium"))
    assert "stages total 45 min" in rejected["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(sa.func.count()).select_from(training_session_revisions)
            )
            == 2
        )


async def test_weekly_plan_and_override_never_create_attendance(service, store):
    scheduled = await process(
        service,
        store,
        message(1, "/plan weekly bjj tue thu 60 min at 18:30"),
    )
    assert "never records attendance" in scheduled["payload"]["text"]
    await process(service, store, message(2, "/plan 2023-11-16 cancel"))
    await process(service, store, message(3, "/plan 2023-11-17 rest"))
    week = await process(service, store, message(4, "/plan week"))
    text = week["payload"]["text"]
    assert "2023-11-14 · planned BJJ 60 min at 18:30 (weekly schedule) · completed: none" in text
    assert "2023-11-16 · cancelled (date override) · completed: none" in text
    assert "2023-11-17 · rest (date override) · completed: none" in text
    assert "2023-11-15 · unknown (no plan) · completed: none" in text
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(training_sessions)) == 0
        )
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(training_schedule_rules))
            == 2
        )
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(training_plan_events))
            == 2
        )


async def test_completed_session_is_displayed_separately_from_plan(service, store):
    await process(service, store, message(1, "/plan 2023-11-14 bjj 60 min"))
    await process(service, store, message(2, "BJJ 55 min medium"))
    week = await process(service, store, message(3, "/plan week"))
    assert "planned BJJ 60 min" in week["payload"]["text"]
    assert "completed: BJJ" in week["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(training_plan_events))
            == 1
        )
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(training_sessions)) == 1
        )
        assert (
            await connection.scalar(
                sa.select(sa.func.count()).select_from(training_session_revisions)
            )
            == 1
        )


async def test_weekly_plan_can_show_two_planned_activities_on_one_day(service, store):
    await process(service, store, message(1, "/plan weekly bjj tue 60 min at 18:30"))
    await process(service, store, message(2, "/plan weekly gym tue 45 min at 07:00"))
    week = await process(service, store, message(3, "/plan week"))
    line = next(
        item for item in week["payload"]["text"].splitlines() if item.startswith("2023-11-14")
    )
    assert "GYM 45 min at 07:00" in line
    assert "BJJ 60 min at 18:30" in line
    assert "completed: none" in line


async def test_downgrade_refuses_to_discard_private_bjj_or_plan_history(service, store, settings):
    await process(service, store, message(1, "/bjj template set duration=60 sparring=2-3x5"))
    await store.close()
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    config.attributes["database_url"] = settings.resolved_database_url
    with pytest.raises(RuntimeError, match="BJJ template/detail or plan history exists"):
        await asyncio.to_thread(command.downgrade, config, "0016_gym_details")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0017_bjj_plans_details",
        )
        assert connection.execute("SELECT count(*) FROM bjj_template_revisions").fetchone() == (1,)
