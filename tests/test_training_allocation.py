from datetime import UTC, datetime, timedelta
from itertools import product

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from nutrition_bot.adapters.database.allocations import resolve_allocation
from nutrition_bot.adapters.database.goals import current_plan
from nutrition_bot.adapters.database.schema_allocations import (
    allocation_events,
    allocation_proposals,
)
from nutrition_bot.domain.training_allocation import calorie_deltas
from tests.helpers import message
from tests.test_goals import goal_press
from tests.test_telegram_meals import process


def next_monday():
    today = datetime.now(UTC).date()
    return today + timedelta(days=7 - today.weekday())


async def setup_goal(service, store):
    proposal = await process(
        service,
        store,
        message(
            1, "/goal manual mode=maintenance weight=80kg rate=0 calories=2400 protein=150g fat=60g"
        ),
    )
    await process(service, store, goal_press(proposal, update_id=2))


async def preview(service, store, *, update_id=3, shift=100):
    return await process(
        service,
        store,
        message(
            update_id,
            f"/allocation {next_monday()} rest gym bjj rest double unknown rest shift={shift}",
        ),
    )


def test_allocation_preserves_budget_and_unknowns_for_varied_plans():
    for first in product(("rest", "gym", "bjj", "double", "unknown"), repeat=3):
        kinds = [*first, "unknown", "gym", "rest", "double"]
        for step in (0, 4, 100, 300):
            result = calorie_deltas(kinds, step)
            assert sum(result) == 0
            assert result[3] == 0
            assert all(value % 4 == 0 for value in result)
            assert result == calorie_deltas(kinds, step)
    assert calorie_deltas(["unknown"] * 7, 100) == [0] * 7
    assert calorie_deltas(["gym"] * 7, 100) == [0] * 7


@pytest.mark.parametrize("step", [-4, 1, 304, True, 4.0])
def test_invalid_allocation_step(step):
    with pytest.raises(ValueError):
        calorie_deltas(["rest"] * 7, step)


async def test_preview_approval_reports_and_duplicate_are_exact(service, store):
    await setup_goal(service, store)
    receipt = await preview(service, store)
    assert "Weekly calories: 16800 → 16800" in receipt["payload"]["text"]
    assert "/allocation apply A1" in receipt["payload"]["text"]
    week = next_monday()
    async with store.engine.connect() as connection:
        baseline = await current_plan(connection, on_date=week)
        assert baseline.energy_kcal == 2400 and baseline.allocation_id is None
    applied = await process(service, store, message(4, "/allocation apply A1"))
    assert "Allocation applied" in applied["payload"]["text"]
    repeated = await process(service, store, message(5, "/allocation apply A1"))
    assert "Already processed" in repeated["payload"]["text"]
    async with store.engine.connect() as connection:
        targets = [
            await current_plan(connection, on_date=week + timedelta(days=i)) for i in range(7)
        ]
        assert sum(plan.energy_kcal for plan in targets) == 16800
        assert all(plan.protein_grams == 150 and plan.fat_grams == 60 for plan in targets)
        assert targets[5].energy_kcal == 2400
        assert all(plan.allocation_id == 1 for plan in targets)
        assert (
            await current_plan(connection, on_date=week + timedelta(days=7))
        ).allocation_id is None
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(allocation_events)) == 1
        )


async def test_new_draft_invalidates_old_approval_and_cancel_does_not_revive_it(service, store):
    await setup_goal(service, store)
    await preview(service, store)
    await preview(service, store, update_id=4, shift=200)
    rejected = await process(service, store, message(5, "/allocation apply A1"))
    assert "newer draft" in rejected["payload"]["text"]
    await process(service, store, message(6, "/allocation apply A2"))
    await process(service, store, message(7, "/allocation cancel A2"))
    repeat = await process(service, store, message(8, "/allocation apply A2"))
    assert "Already processed" in repeat["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (await current_plan(connection, on_date=next_monday())).allocation_id is None


async def test_started_week_is_immutable_and_baseline_changes_are_blocked(service, store):
    await setup_goal(service, store)
    await preview(service, store)
    await process(service, store, message(4, "/allocation apply A1"))
    async with store.write() as connection:
        with pytest.raises(ValueError, match="started"):
            await resolve_allocation(
                connection, 1, "cancel", today=next_monday(), action_key="unused"
            )
    replacement = await process(
        service,
        store,
        message(
            5, "/goal manual mode=maintenance weight=80kg rate=0 calories=2200 protein=150g fat=60g"
        ),
    )
    rejected = await process(service, store, goal_press(replacement, update_id=6))
    assert "allocation overlaps" in rejected["payload"]["text"]
    async with store.write() as connection:
        with pytest.raises(IntegrityError, match="immutable"):
            await connection.execute(sa.update(allocation_proposals).values(step_kcal=0))


async def test_baseline_change_after_preview_requires_new_approval(service, store):
    await setup_goal(service, store)
    await preview(service, store)
    replacement = await process(
        service,
        store,
        message(
            4, "/goal manual mode=maintenance weight=80kg rate=0 calories=2200 protein=150g fat=60g"
        ),
    )
    await process(service, store, goal_press(replacement, update_id=5))
    rejected = await process(service, store, message(6, "/allocation apply A1"))
    assert "Baseline targets changed" in rejected["payload"]["text"]


async def test_old_message_timestamp_cannot_create_past_allocation(service, store):
    await setup_goal(service, store)
    result = await process(
        service,
        store,
        message(3, "/allocation 2023-11-20 rest gym bjj rest double unknown rest shift=100"),
    )
    assert "future Monday" in result["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(allocation_proposals))
            == 0
        )


async def test_unauthorized_approval_and_edited_command_do_not_apply(service, store):
    await setup_goal(service, store)
    await preview(service, store)
    await service.accept([message(4, "/allocation apply A1", user=202)])
    assert not await service.process_one()
    await process(service, store, message(5, "/allocation apply A1", edited=True))
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(allocation_events)) == 0
        )
