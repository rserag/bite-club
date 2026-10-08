"""Derived manual targets must fit the same ledger bounds as explicit inputs."""

from decimal import Decimal

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import inbox
from nutrition_bot.adapters.database.schema_goals import goal_proposals, target_plans
from nutrition_bot.domain.goals import GoalError, manual_targets
from tests.helpers import message
from tests.test_goals import goal_press
from tests.test_telegram_meals import process


def manual(energy):
    return manual_targets(
        mode="maintenance",
        weight_kg=Decimal("80"),
        rate_kg_per_week=Decimal("0"),
        energy_kcal=energy,
        protein_grams=20,
        fat_grams=20,
    )


@pytest.mark.parametrize("energy", [5080, 10000])
def test_manual_rejects_derived_carbohydrate_outside_plan_range(energy):
    with pytest.raises(GoalError, match="Carbohydrate must be 0–1200 g"):
        manual(energy)


def test_manual_supported_carbohydrate_boundaries_and_existing_numbers_are_unchanged():
    lower = manual(260)
    upper = manual(5060)
    assert (lower.energy_kcal, lower.carbohydrate_grams) == (260, 0)
    assert (upper.energy_kcal, upper.carbohydrate_grams) == (5060, 1200)
    normal = manual_targets(
        mode="maintenance",
        weight_kg=Decimal("80"),
        rate_kg_per_week=Decimal("0"),
        energy_kcal=2400,
        protein_grams=145,
        fat_grams=70,
    )
    assert (normal.energy_kcal, normal.protein_grams, normal.fat_grams) == (2400, 145, 70)
    assert normal.carbohydrate_grams == 300


async def test_direct_manual_overflow_finishes_inbox_and_supported_boundary_can_apply(
    service, store
):
    rejected = await process(
        service,
        store,
        message(
            1,
            "/goal manual mode=maintenance weight=80kg rate=0 calories=10000 protein=20g fat=20g",
        ),
    )
    assert "Carbohydrate must be 0–1200 g" in rejected["payload"]["text"]
    assert "Nothing changed" in rejected["payload"]["text"]
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
            "/goal manual mode=maintenance weight=80kg rate=0 calories=5060 protein=20g fat=20g",
        ),
    )
    assert "5060 kcal" in valid["payload"]["text"]
    assert "goal_proposal_id" in valid["payload"]
    await process(service, store, goal_press(valid, update_id=3))
    async with store.engine.connect() as connection:
        plan = (await connection.execute(sa.select(target_plans))).mappings().one()
        assert (plan["energy_kcal"], plan["carbohydrate_grams"]) == (5060, 1200)
