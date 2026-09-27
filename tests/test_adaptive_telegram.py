import asyncio
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from aiogram.types import Update
from alembic import command
from alembic.config import Config

from nutrition_bot.adapters.database.goals import apply_proposal, create_proposal
from nutrition_bot.adapters.database.schema_adaptive import adaptive_proposals, adaptive_reviews
from nutrition_bot.adapters.database.schema_goals import goals, target_plans
from nutrition_bot.domain.goals import manual_targets
from tests.helpers import callback, message
from tests.test_goals import goal_press
from tests.test_telegram_meals import process

MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")


def adaptive_press(receipt, operation, *, update_id=904, callback_id=None, message_id=None):
    value = callback(
        receipt["button_token"],
        update_id=update_id,
        callback_id=callback_id or f"synthetic-adaptive-{update_id}",
        message_id=message_id or receipt["telegram_message_id"],
    ).model_dump(mode="json", exclude_none=True)
    value["callback_query"]["data"] = f"adaptive:{operation}:{receipt['button_token']}"
    return Update.model_validate(value)


async def seed_pending_proposal(service, store):
    for update_id in (900, 901, 902):
        await process(service, store, message(update_id, "/status"))
    starting = manual_targets(
        mode="loss",
        weight_kg=Decimal("80"),
        rate_kg_per_week=Decimal("0.4"),
        energy_kcal=2200,
        protein_grams=160,
        fat_grams=65,
    )
    async with store.write() as connection:
        saved = await create_proposal(connection, starting, action_key="update:900")
        _, plan = await apply_proposal(
            connection,
            saved.id,
            action_key="update:901",
            effective_from=date(2023, 1, 1),
        )
        goal_id = await connection.scalar(sa.select(goals.c.id))
        review_id = (
            await connection.execute(
                sa.insert(adaptive_reviews)
                .values(
                    review_end=date(2023, 11, 14),
                    window_start=date(2023, 10, 25),
                    eligible=True,
                    reason_codes=[],
                    days_since_target_change=317,
                    measured_dates=15,
                    weight_span_days=25,
                    max_weight_gap_days=3,
                    block_measurement_counts=[4, 4, 4],
                    complete_food_days=19,
                    unresolved_drafts=0,
                    complete_energy_days=19,
                    targeted_energy_days=19,
                    mean_intake_kcal=2200.0,
                    mean_target_kcal=2200.0,
                    target_rate_grams_per_week=-400,
                    observed_rate_grams_per_week=-100.0,
                    mismatch_direction=-1,
                    raw_delta_kcal=-330.0,
                    action_key="update:902",
                    created_at=0.0,
                )
                .returning(adaptive_reviews.c.id)
            )
        ).scalar_one()
        proposal_id = (
            await connection.execute(
                sa.insert(adaptive_proposals)
                .values(
                    review_id=review_id,
                    goal_id=goal_id,
                    current_target_plan_id=plan.id,
                    proposed_delta_kcal=-150,
                    energy_kcal=2050,
                    protein_grams=160,
                    fat_grams=65,
                    carbohydrate_grams=206,
                    state="pending",
                    created_at=0.0,
                )
                .returning(adaptive_proposals.c.id)
            )
        ).scalar_one()
    return proposal_id


async def test_pending_proposal_requires_exact_button_and_applies_future_plan(service, store):
    proposal_id = await seed_pending_proposal(service, store)
    receipt = await process(service, store, message(903, "/adjust"))
    assert receipt["payload"]["adaptive_proposal_id"] == proposal_id
    assert {button["text"] for button in receipt["payload"]["buttons"]} == {
        "Apply tomorrow",
        "Keep target",
        "Review evidence",
    }

    applied = await process(service, store, adaptive_press(receipt, "apply"))
    assert "Applied calorie proposal" in applied["payload"]["text"]
    async with store.engine.connect() as connection:
        proposal = (await connection.execute(sa.select(adaptive_proposals))).mappings().one()
        plans = (
            (
                await connection.execute(
                    sa.select(target_plans).order_by(target_plans.c.effective_from)
                )
            )
            .mappings()
            .all()
        )
    assert proposal["state"] == "applied"
    assert proposal["applied_target_plan_id"] == plans[-1]["id"]
    assert len(plans) == 2
    assert plans[0]["energy_kcal"] == 2200
    assert plans[-1]["source"] == "adaptive"
    assert plans[-1]["effective_from"] == datetime.now(ZoneInfo("UTC")).date() + timedelta(days=1)
    assert plans[-1]["energy_kcal"] == 2050
    assert plans[-1]["proposal_id"] is None

    repeated = await process(
        service,
        store,
        adaptive_press(receipt, "apply", update_id=905, callback_id="adaptive-repeat"),
    )
    assert "State: applied" in repeated["payload"]["text"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(target_plans)) == 2


async def test_keep_records_decision_without_new_target_plan(service, store):
    await seed_pending_proposal(service, store)
    receipt = await process(service, store, message(903, "/adjust"))
    kept = await process(service, store, adaptive_press(receipt, "keep"))
    assert "Kept current target" in kept["payload"]["text"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(adaptive_proposals.c.state)) == "kept"
        assert await connection.scalar(sa.select(sa.func.count()).select_from(target_plans)) == 1


async def test_adjust_without_active_target_changes_nothing(service, store):
    result = await process(service, store, message(1, "/adjust"))
    assert "Set one with /goal setup first" in result["payload"]["text"]
    assert "No target changed" in result["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(adaptive_reviews)) == 0
        )


async def test_upgrade_backfills_reviewed_energy_range_on_existing_estimated_plan(
    service, store, settings
):
    setup = await process(
        service,
        store,
        message(
            1,
            "/goal estimate mode=loss weight=80kg height=180cm age=35 "
            "coefficient=+5 activity=1.6 rate=0.4kg/week",
        ),
    )
    await process(service, store, goal_press(setup))
    await store.close()
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    config.attributes["database_url"] = settings.resolved_database_url
    await asyncio.to_thread(command.downgrade, config, "0013_daily_checkins")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(target_plans)")}
        assert "energy_range_low_kcal" not in columns
    await asyncio.to_thread(command.upgrade, config, "head")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert connection.execute(
            "SELECT energy_range_low_kcal, energy_range_high_kcal FROM target_plans"
        ).fetchone() == (2250, 2450)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
