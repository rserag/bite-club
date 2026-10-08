"""An estimated proposal must fit the ledger before a durable insert is attempted."""

from decimal import Decimal

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import inbox
from nutrition_bot.adapters.database.schema_goals import goal_proposals, target_plans
from nutrition_bot.domain.goals import GoalError, estimate_targets
from tests.helpers import message
from tests.test_telegram_meals import process


def estimate(*, mode="loss", weight="80", rate="0.4", height="180", age=35, activity="1.6"):
    return estimate_targets(
        mode=mode,
        weight_kg=Decimal(weight),
        height_cm=Decimal(height),
        age_years=age,
        rmr_coefficient=5,
        activity_factor=Decimal(activity),
        rate_kg_per_week=Decimal(rate),
    )


@pytest.mark.parametrize("mode,rate", [("loss", "1.5"), ("maintenance", "0"), ("gain", "0.6")])
def test_extreme_valid_inputs_reject_unpersistable_estimated_macros(mode, rate):
    with pytest.raises(GoalError, match="supported plan range"):
        estimate(mode=mode, weight="300", rate=rate, height="230", age=18, activity="1.8")


def test_estimate_at_supported_protein_boundary_and_existing_values_are_unchanged():
    boundary = estimate(weight="250", rate="0.625", height="230", age=18, activity="1.8")
    assert (
        boundary.energy_kcal,
        boundary.protein_grams,
        boundary.fat_grams,
        boundary.carbohydrate_grams,
    ) == (6250, 500, 200, 615)
    original = estimate()
    assert (
        original.estimated_tdee_kcal,
        original.energy_kcal,
        original.protein_grams,
        original.fat_grams,
        original.carbohydrate_grams,
    ) == (2800, 2350, 160, 65, 280)


@pytest.mark.parametrize("mode,rate", [("loss", "1.5kg/week"), ("maintenance", "0")])
async def test_direct_estimate_rejection_finishes_update_without_poisoning_proposal(
    service, store, mode, rate
):
    result = await process(
        service,
        store,
        message(
            1,
            f"/goal estimate mode={mode} weight=300kg height=230cm age=18 "
            f"coefficient=+5 activity=1.8 rate={rate}",
        ),
    )
    assert "supported plan range" in result["payload"]["text"]
    assert "Nothing changed" in result["payload"]["text"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(inbox.c.status)) == "done"
        assert await connection.scalar(sa.select(sa.func.count()).select_from(goal_proposals)) == 0
        assert await connection.scalar(sa.select(sa.func.count()).select_from(target_plans)) == 0
    assert not await service.process_one()
    valid = await process(
        service,
        store,
        message(
            2,
            "/goal estimate mode=loss weight=80kg height=180cm age=35 "
            "coefficient=+5 activity=1.6 rate=0.4kg/week",
        ),
    )
    assert "2350 kcal" in valid["payload"]["text"]
    assert "goal_proposal_id" in valid["payload"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(goal_proposals)) == 1
        assert await connection.scalar(sa.select(sa.func.count()).select_from(target_plans)) == 0
