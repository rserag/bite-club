import time
from dataclasses import dataclass, replace
from datetime import date

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema_goals import goal_proposals, goals, target_plans
from nutrition_bot.domain.goals import CALCULATION_VERSION, TargetProposal


class GoalStoreError(ValueError):
    pass


@dataclass(frozen=True)
class ProposalSnapshot:
    id: int
    method: str
    mode: str
    target_rate_grams_per_week: int
    reference_weight_grams: int
    inputs: dict[str, object]
    estimated_tdee_kcal: int | None
    energy_kcal: int
    protein_grams: int
    fat_grams: int
    carbohydrate_grams: int
    energy_range_low_kcal: int | None
    energy_range_high_kcal: int | None
    state: str


@dataclass(frozen=True)
class TargetPlanSnapshot:
    id: int
    goal_id: int
    effective_from: date
    energy_kcal: int
    protein_grams: int
    fat_grams: int
    carbohydrate_grams: int
    energy_range_low_kcal: int | None
    energy_range_high_kcal: int | None
    source: str
    allocation_id: int | None = None


def _proposal(row: sa.RowMapping) -> ProposalSnapshot:
    return ProposalSnapshot(
        id=row["id"],
        method=row["method"],
        mode=row["mode"],
        target_rate_grams_per_week=row["target_rate_grams_per_week"],
        reference_weight_grams=row["reference_weight_grams"],
        inputs=dict(row["inputs"]),
        estimated_tdee_kcal=row["estimated_tdee_kcal"],
        energy_kcal=row["energy_kcal"],
        protein_grams=row["protein_grams"],
        fat_grams=row["fat_grams"],
        carbohydrate_grams=row["carbohydrate_grams"],
        energy_range_low_kcal=row["energy_range_low_kcal"],
        energy_range_high_kcal=row["energy_range_high_kcal"],
        state=row["state"],
    )


def _plan(row: sa.RowMapping) -> TargetPlanSnapshot:
    return TargetPlanSnapshot(
        id=row["id"],
        goal_id=row["goal_id"],
        effective_from=row["effective_from"],
        energy_kcal=row["energy_kcal"],
        protein_grams=row["protein_grams"],
        fat_grams=row["fat_grams"],
        carbohydrate_grams=row["carbohydrate_grams"],
        energy_range_low_kcal=row["energy_range_low_kcal"],
        energy_range_high_kcal=row["energy_range_high_kcal"],
        source=row["source"],
    )


async def create_proposal(
    connection: AsyncConnection, proposal: TargetProposal, *, action_key: str
) -> ProposalSnapshot:
    await connection.execute(
        sa.insert(goal_proposals).values(
            action_key=action_key,
            method=proposal.method,
            mode=proposal.mode,
            target_rate_grams_per_week=proposal.target_rate_grams_per_week,
            reference_weight_grams=proposal.reference_weight_grams,
            inputs=proposal.inputs,
            estimated_tdee_kcal=proposal.estimated_tdee_kcal,
            energy_kcal=proposal.energy_kcal,
            protein_grams=proposal.protein_grams,
            fat_grams=proposal.fat_grams,
            carbohydrate_grams=proposal.carbohydrate_grams,
            energy_range_low_kcal=proposal.energy_range_low_kcal,
            energy_range_high_kcal=proposal.energy_range_high_kcal,
            calculation_version=CALCULATION_VERSION,
            state="open",
            created_at=time.time(),
        )
    )
    proposal_id = await connection.scalar(
        sa.select(goal_proposals.c.id).where(goal_proposals.c.action_key == action_key)
    )
    assert isinstance(proposal_id, int)
    return await get_proposal(connection, proposal_id)


async def get_proposal(connection: AsyncConnection, proposal_id: int) -> ProposalSnapshot:
    row = (
        (
            await connection.execute(
                sa.select(goal_proposals).where(goal_proposals.c.id == proposal_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise GoalStoreError("That goal proposal no longer exists.")
    return _proposal(row)


async def apply_proposal(
    connection: AsyncConnection,
    proposal_id: int,
    *,
    action_key: str,
    effective_from: date,
) -> tuple[ProposalSnapshot, TargetPlanSnapshot]:
    proposal = await get_proposal(connection, proposal_id)
    existing = (
        (
            await connection.execute(
                sa.select(target_plans).where(target_plans.c.proposal_id == proposal_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        return proposal, _plan(existing)
    if proposal.state != "open":
        raise GoalStoreError("That proposal was cancelled. Create a new one with /goal.")
    from nutrition_bot.adapters.database.allocations import target_change_blocked

    if await target_change_blocked(connection, effective_from):
        raise GoalStoreError(
            "An approved training allocation overlaps future targets. Cancel its future week "
            "with /allocation first, or wait until an already-started week finishes."
        )
    await connection.execute(
        sa.update(goals)
        .where(goals.c.active_slot == 1)
        .values(ended_on=effective_from, active_slot=None)
    )
    await connection.execute(
        sa.insert(goals).values(
            mode=proposal.mode,
            target_rate_grams_per_week=proposal.target_rate_grams_per_week,
            started_on=effective_from,
            active_slot=1,
            proposal_id=proposal.id,
            action_key=action_key,
            created_at=time.time(),
        )
    )
    goal_id = await connection.scalar(sa.select(goals.c.id).where(goals.c.action_key == action_key))
    assert isinstance(goal_id, int)
    await connection.execute(
        sa.insert(target_plans).values(
            goal_id=goal_id,
            effective_from=effective_from,
            energy_kcal=proposal.energy_kcal,
            protein_grams=proposal.protein_grams,
            fat_grams=proposal.fat_grams,
            carbohydrate_grams=proposal.carbohydrate_grams,
            energy_range_low_kcal=proposal.energy_range_low_kcal,
            energy_range_high_kcal=proposal.energy_range_high_kcal,
            reference_weight_grams=proposal.reference_weight_grams,
            estimated_tdee_kcal=proposal.estimated_tdee_kcal,
            source="initial_estimate" if proposal.method == "estimate" else "manual",
            calculation_version=CALCULATION_VERSION,
            proposal_id=proposal.id,
            action_key=action_key,
            created_at=time.time(),
        )
    )
    await connection.execute(
        sa.update(goal_proposals)
        .where(goal_proposals.c.id == proposal.id, goal_proposals.c.state == "open")
        .values(state="applied", resolved_action_key=action_key, resolved_at=time.time())
    )
    plan_id = await connection.scalar(
        sa.select(target_plans.c.id).where(target_plans.c.action_key == action_key)
    )
    assert isinstance(plan_id, int)
    return await get_proposal(connection, proposal.id), _plan(
        (await connection.execute(sa.select(target_plans).where(target_plans.c.id == plan_id)))
        .mappings()
        .one()
    )


async def cancel_proposal(
    connection: AsyncConnection, proposal_id: int, *, action_key: str
) -> ProposalSnapshot:
    proposal = await get_proposal(connection, proposal_id)
    if proposal.state == "open":
        await connection.execute(
            sa.update(goal_proposals)
            .where(goal_proposals.c.id == proposal_id)
            .values(state="cancelled", resolved_action_key=action_key, resolved_at=time.time())
        )
    return await get_proposal(connection, proposal_id)


async def current_plan(
    connection: AsyncConnection, *, on_date: date, include_allocation: bool = True
) -> TargetPlanSnapshot | None:
    row = (
        (
            await connection.execute(
                sa.select(target_plans)
                .where(target_plans.c.effective_from <= on_date)
                .order_by(target_plans.c.effective_from.desc(), target_plans.c.id.desc())
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    plan = _plan(row)
    if not include_allocation:
        return plan
    from nutrition_bot.adapters.database.allocations import active_allocation

    allocation = await active_allocation(connection, on_date)
    if allocation is None or allocation["base_plan_id"] != plan.id:
        return plan
    delta = allocation["deltas"][(on_date - allocation["week_start"]).days]
    return replace(
        plan,
        energy_kcal=plan.energy_kcal + delta,
        carbohydrate_grams=plan.carbohydrate_grams + delta // 4,
        energy_range_low_kcal=(
            max(1, plan.energy_range_low_kcal + delta)
            if plan.energy_range_low_kcal is not None
            else None
        ),
        energy_range_high_kcal=(
            min(10000, plan.energy_range_high_kcal + delta)
            if plan.energy_range_high_kcal is not None
            else None
        ),
        allocation_id=allocation["id"],
    )
