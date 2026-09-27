from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from aiogram.types import Update

from nutrition_bot.adapters.database.schema_goals import goal_proposals, goals, target_plans
from nutrition_bot.domain.goals import GoalError, estimate_targets, manual_targets
from tests.helpers import callback, message
from tests.test_telegram_meals import process


def goal_press(receipt, operation="apply", *, update_id=2, callback_id=None):
    value = callback(
        receipt["button_token"],
        update_id=update_id,
        callback_id=callback_id or f"synthetic-goal-{update_id}",
        message_id=receipt["telegram_message_id"],
    ).model_dump(mode="json", exclude_none=True)
    value["callback_query"]["data"] = f"goal:{operation}:{receipt['button_token']}"
    return Update.model_validate(value)


def test_estimated_targets_are_deterministic_and_conservatively_bounded():
    result = estimate_targets(
        mode="loss",
        weight_kg=Decimal("80"),
        height_cm=Decimal("180"),
        age_years=35,
        rmr_coefficient=5,
        activity_factor=Decimal("1.6"),
        rate_kg_per_week=Decimal("0.4"),
    )
    assert result.estimated_tdee_kcal == 2800
    assert (result.energy_kcal, result.protein_grams, result.fat_grams) == (2350, 160, 65)
    assert result.carbohydrate_grams == 280
    assert result.target_rate_grams_per_week == -400
    assert (result.energy_range_low_kcal, result.energy_range_high_kcal) == (2250, 2450)


@pytest.mark.parametrize(
    ("mode", "rate"), [("loss", "0.1"), ("gain", "0.4"), ("maintenance", "0.1")]
)
def test_goal_rates_outside_reviewed_ranges_are_rejected(mode, rate):
    with pytest.raises(GoalError):
        manual_targets(
            mode=mode,
            weight_kg=Decimal("80"),
            rate_kg_per_week=Decimal(rate),
            energy_kcal=2400,
            protein_grams=150,
            fat_grams=70,
        )


def test_manual_targets_do_not_invent_a_universal_calorie_floor():
    result = manual_targets(
        mode="maintenance",
        weight_kg=Decimal("30"),
        rate_kg_per_week=Decimal("0"),
        energy_kcal=400,
        protein_grams=20,
        fat_grams=20,
    )
    assert result.carbohydrate_grams == 35


async def test_guided_estimate_requires_explicit_apply_and_starts_tomorrow(service, store):
    setup = await process(
        service,
        store,
        message(
            1,
            "/goal estimate mode=loss weight=80kg height=180cm age=35 "
            "coefficient=+5 activity=1.6 rate=0.4kg/week",
        ),
    )
    assert setup["payload"]["goal_proposal_id"] == 1
    assert {button["text"] for button in setup["payload"]["buttons"]} == {
        "Apply tomorrow",
        "Cancel",
    }
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(target_plans)) == 0

    applied = await process(service, store, goal_press(setup))
    async with store.engine.connect() as connection:
        proposal = (await connection.execute(sa.select(goal_proposals))).mappings().one()
        plan = (await connection.execute(sa.select(target_plans))).mappings().one()
        assert proposal["state"] == "applied"
        assert plan["effective_from"] == date.today() + timedelta(days=1)
        assert plan["energy_kcal"] == 2350
        assert f"starts {plan['effective_from']}" in applied["payload"]["text"]

    repeated = await process(
        service,
        store,
        goal_press(setup, update_id=3, callback_id="second-valid-press"),
    )
    assert "Applied" in repeated["payload"]["text"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(target_plans)) == 1


async def test_manual_proposal_can_be_cancelled_without_changing_targets(service, store):
    setup = await process(
        service,
        store,
        message(
            1,
            "/goal manual mode=maintenance weight=80kg rate=0 calories=2400 protein=145g fat=70g",
        ),
    )
    cancelled = await process(service, store, goal_press(setup, "cancel"))
    assert "cancelled" in cancelled["payload"]["text"].lower()
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(goals)) == 0
        assert await connection.scalar(sa.select(sa.func.count()).select_from(target_plans)) == 0


async def test_new_goal_keeps_history_and_is_the_only_active_goal(service, store):
    first = await process(
        service,
        store,
        message(
            1,
            "/goal manual mode=maintenance weight=80kg rate=0 calories=2400 protein=145g fat=70g",
        ),
    )
    await process(service, store, goal_press(first, update_id=2))
    second = await process(
        service,
        store,
        message(
            3,
            "/goal manual mode=loss weight=80kg rate=0.4kg/week calories=2200 protein=160g fat=65g",
        ),
    )
    await process(service, store, goal_press(second, update_id=4))
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(goals)) == 2
        assert await connection.scalar(sa.select(sa.func.count()).select_from(target_plans)) == 2
        assert (
            await connection.scalar(
                sa.select(sa.func.count()).select_from(goals).where(goals.c.ended_on.is_(None))
            )
            == 1
        )
        with pytest.raises(sa.exc.IntegrityError):
            await connection.execute(sa.update(target_plans).values(energy_kcal=999))


async def test_goal_help_and_invalid_fields_do_not_create_proposals(service, store):
    help_result = await process(service, store, message(1, "/goal setup"))
    assert "/goal estimate" in help_result["payload"]["text"]
    rejected = await process(
        service,
        store,
        message(2, "/goal estimate mode=loss weight=80kg rate=0.4kg/week"),
    )
    assert "Nothing changed" in rejected["payload"]["text"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(goal_proposals)) == 0
