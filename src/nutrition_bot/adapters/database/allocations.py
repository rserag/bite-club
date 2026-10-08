import time
from collections.abc import Sequence
from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema_allocations import allocation_events as events
from nutrition_bot.adapters.database.schema_allocations import allocation_proposals as proposals
from nutrition_bot.domain.training_allocation import calorie_deltas


async def latest_event(connection: AsyncConnection, week: date) -> RowMapping | None:
    return (
        (
            await connection.execute(
                sa.select(proposals, events.c.kind, events.c.id.label("event_id"))
                .join(events, events.c.proposal_id == proposals.c.id)
                .where(proposals.c.week_start == week)
                .order_by(events.c.id.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )


async def active_allocation(connection: AsyncConnection, day: date) -> RowMapping | None:
    week = day - timedelta(days=day.weekday())
    event = await latest_event(connection, week)
    return event if event is not None and event["kind"] == "apply" else None


async def target_change_blocked(connection: AsyncConnection, effective_from: date) -> bool:
    weeks: Sequence[date] = (
        (
            await connection.execute(
                sa.select(proposals.c.week_start)
                .distinct()
                .where(proposals.c.week_start >= effective_from - timedelta(days=6))
            )
        )
        .scalars()
        .all()
    )
    for week in weeks:
        if await active_allocation(connection, week) is not None:
            return True
    return False


async def create_allocation(
    connection: AsyncConnection,
    *,
    week: date,
    today: date,
    kinds: list[str],
    step_kcal: int,
    action_key: str,
) -> RowMapping:
    from nutrition_bot.adapters.database.goals import current_plan

    if week.weekday() != 0 or not today < week <= today + timedelta(days=365):
        raise ValueError("Choose a future Monday within the next year; past days cannot change.")
    deltas = calorie_deltas(kinds, step_kcal)
    plans = [
        await current_plan(connection, on_date=week + timedelta(days=i), include_allocation=False)
        for i in range(7)
    ]
    plan = plans[0]
    if plan is None or any(p is None or p.id != plan.id for p in plans):
        raise ValueError("The full week needs one reviewed baseline target plan.")
    for delta in deltas:
        if (
            not 1 <= plan.energy_kcal + delta <= 10000
            or not 0 <= plan.carbohydrate_grams + delta // 4 <= 1200
        ):
            raise ValueError("This shift exceeds the calorie or carbohydrate bounds; choose less.")
    identifier = (
        await connection.execute(
            sa.insert(proposals)
            .values(
                action_key=action_key,
                week_start=week,
                base_plan_id=plan.id,
                kinds=kinds,
                deltas=deltas,
                step_kcal=step_kcal,
                calculation_version="weekly-allocation-v1",
                created_at=time.time(),
            )
            .returning(proposals.c.id)
        )
    ).scalar_one()
    return await get_allocation(connection, identifier)


async def get_allocation(connection: AsyncConnection, identifier: int) -> RowMapping:
    row = (
        (await connection.execute(sa.select(proposals).where(proposals.c.id == identifier)))
        .mappings()
        .first()
    )
    if row is None:
        raise ValueError("Unknown allocation proposal.")
    return row


async def resolve_allocation(
    connection: AsyncConnection, identifier: int, action: str, *, today: date, action_key: str
) -> str:
    from nutrition_bot.adapters.database.goals import current_plan

    if action not in {"apply", "cancel"}:
        raise ValueError("Use apply or cancel with the exact proposal ID.")
    proposal = await get_allocation(connection, identifier)
    if await connection.scalar(
        sa.select(events.c.id).where(events.c.proposal_id == identifier, events.c.kind == action)
    ):
        return "Already processed; nothing changed."
    if proposal["week_start"] <= today:
        raise ValueError("This week has started; its approved allocation cannot be rewritten.")
    latest = await connection.scalar(
        sa.select(sa.func.max(proposals.c.id)).where(
            proposals.c.week_start == proposal["week_start"]
        )
    )
    current = await latest_event(connection, proposal["week_start"])
    if action == "apply":
        if identifier != latest:
            raise ValueError("A newer draft exists. Review and approve its exact ID.")
        if await connection.scalar(
            sa.select(events.c.id).where(
                events.c.proposal_id == identifier, events.c.kind == "cancel"
            )
        ):
            raise ValueError("That draft was cancelled. Create a fresh proposal.")
        for offset in range(7):
            baseline = await current_plan(
                connection,
                on_date=proposal["week_start"] + timedelta(days=offset),
                include_allocation=False,
            )
            if baseline is None or baseline.id != proposal["base_plan_id"]:
                raise ValueError("Baseline targets changed. Create and review a fresh proposal.")
    elif current is not None and current["id"] != identifier:
        raise ValueError("Cancel the currently applied allocation ID first.")
    await connection.execute(
        sa.insert(events).values(
            proposal_id=identifier, action_key=action_key, kind=action, created_at=time.time()
        )
    )
    return (
        "Allocation applied."
        if action == "apply"
        else "Allocation cancelled; baseline targets apply."
    )
