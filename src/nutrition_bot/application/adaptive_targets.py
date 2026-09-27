import time
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.checkins import food_day_status
from nutrition_bot.adapters.database.daily import daily_totals
from nutrition_bot.adapters.database.goals import current_plan
from nutrition_bot.adapters.database.schema_adaptive import adaptive_proposals, adaptive_reviews
from nutrition_bot.adapters.database.schema_goals import goals, target_plans
from nutrition_bot.adapters.database.weights import daily_weights
from nutrition_bot.domain.adaptive_targets import (
    AdjustmentEvaluation,
    AdjustmentEvidence,
    evaluate_adjustment,
)
from nutrition_bot.domain.daily import DailyTotals
from nutrition_bot.domain.food import NUTRIENT_SCALE
from nutrition_bot.domain.weight_trends import summarize_weight


class AdaptiveError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewResult:
    review_id: int
    review_end: date
    evaluation: AdjustmentEvaluation
    evidence: AdjustmentEvidence
    proposal_id: int | None


@dataclass(frozen=True)
class AdaptiveProposal:
    id: int
    review_id: int
    current_target_plan_id: int
    proposed_delta_kcal: int
    energy_kcal: int
    protein_grams: int
    fat_grams: int
    carbohydrate_grams: int
    state: str
    applied_target_plan_id: int | None


def _proposal(row: RowMapping) -> AdaptiveProposal:
    return AdaptiveProposal(
        id=row["id"],
        review_id=row["review_id"],
        current_target_plan_id=row["current_target_plan_id"],
        proposed_delta_kcal=row["proposed_delta_kcal"],
        energy_kcal=row["energy_kcal"],
        protein_grams=row["protein_grams"],
        fat_grams=row["fat_grams"],
        carbohydrate_grams=row["carbohydrate_grams"],
        state=row["state"],
        applied_target_plan_id=row["applied_target_plan_id"],
    )


async def get_adaptive_proposal(connection: AsyncConnection, proposal_id: int) -> AdaptiveProposal:
    row = (
        (
            await connection.execute(
                sa.select(adaptive_proposals).where(adaptive_proposals.c.id == proposal_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AdaptiveError("That calorie proposal is unavailable.")
    return _proposal(row)


async def pending_proposal(connection: AsyncConnection) -> AdaptiveProposal | None:
    row = (
        (
            await connection.execute(
                sa.select(adaptive_proposals)
                .where(adaptive_proposals.c.state == "pending")
                .order_by(adaptive_proposals.c.id.desc())
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    return _proposal(row) if row is not None else None


def _energy_amount(totals: DailyTotals) -> Decimal | None:
    if not totals.item_count:
        return Decimal(0)
    nutrient = next((item for item in totals.nutrients if item.code == "energy"), None)
    if (
        nutrient is None
        or nutrient.known_amount_scaled is None
        or nutrient.known_items != nutrient.total_items
    ):
        return None
    return Decimal(nutrient.known_amount_scaled) / NUTRIENT_SCALE


async def _evidence(
    connection: AsyncConnection, review_end: date
) -> tuple[AdjustmentEvidence, RowMapping, RowMapping]:
    plan = await current_plan(connection, on_date=review_end)
    if plan is None:
        raise AdaptiveError("No target plan is active. Set one with /goal setup first.")
    plan_row = (
        (await connection.execute(sa.select(target_plans).where(target_plans.c.id == plan.id)))
        .mappings()
        .one()
    )
    goal = (
        (await connection.execute(sa.select(goals).where(goals.c.id == plan.goal_id)))
        .mappings()
        .one()
    )
    weight_start = review_end - timedelta(days=27)
    weights = await daily_weights(connection, start=weight_start, end=review_end)
    trend = summarize_weight(weights, as_of=review_end)
    dates = [point.day for point in weights]
    gaps = (
        [(dates[0] - weight_start).days, (review_end - dates[-1]).days]
        + [(right - left).days for left, right in zip(dates, dates[1:], strict=False)]
        if dates
        else [28]
    )
    counts = tuple(
        sum(
            review_end - timedelta(days=20 - block * 7)
            <= point.day
            <= review_end - timedelta(days=14 - block * 7)
            for point in weights
        )
        for block in range(3)
    )
    block_counts = (counts[0], counts[1], counts[2])
    complete_days = 0
    unresolved = 0
    energy_values: list[Decimal] = []
    target_values: list[Decimal] = []
    window_start = review_end - timedelta(days=20)
    for offset in range(21):
        day = window_start + timedelta(days=offset)
        status = await food_day_status(connection, day)
        unresolved += status.unresolved_drafts
        if status.state != "complete":
            continue
        complete_days += 1
        amount = _energy_amount(await daily_totals(connection, day))
        if amount is not None:
            energy_values.append(amount)
        historical = await current_plan(connection, on_date=day)
        if historical is not None:
            target_values.append(Decimal(historical.energy_kcal))
    previous = (
        (
            await connection.execute(
                sa.select(adaptive_reviews)
                .where(
                    adaptive_reviews.c.review_end < review_end,
                    adaptive_reviews.c.eligible.is_(True),
                )
                .order_by(adaptive_reviews.c.review_end.desc())
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    observed = (
        Decimal(trend.weekly_rate_grams.numerator) / trend.weekly_rate_grams.denominator
        if trend.weekly_rate_grams is not None
        else None
    )
    evidence = AdjustmentEvidence(
        days_since_target_change=(review_end - plan.effective_from).days,
        measured_dates=len(weights),
        weight_span_days=trend.trend_span_days,
        max_weight_gap_days=max(gaps),
        block_measurement_counts=block_counts,
        complete_food_days=complete_days,
        total_food_days=21,
        unresolved_drafts=unresolved,
        complete_energy_days=len(energy_values),
        targeted_energy_days=len(target_values),
        mean_intake_kcal=(
            sum(energy_values, Decimal()) / len(energy_values) if energy_values else None
        ),
        mean_target_kcal=(
            sum(target_values, Decimal()) / len(target_values) if target_values else None
        ),
        target_rate_grams_per_week=goal["target_rate_grams_per_week"],
        observed_rate_grams_per_week=observed,
        prior_eligible_direction=previous["mismatch_direction"] if previous else None,
        prior_review_days_ago=(review_end - previous["review_end"]).days if previous else None,
    )
    return evidence, plan_row, goal


async def run_review(
    connection: AsyncConnection, review_end: date, *, action_key: str
) -> ReviewResult:
    from nutrition_bot.adapters.database.supplement_plans import has_weight_context

    if await has_weight_context(connection, review_end):
        raise AdaptiveError(
            "Calorie review withheld: a creatine start, stop, restart or scheduled "
            "phase transition falls in the 28-day weight window. Scale changes "
            "may include fluid changes; this does not establish their cause."
        )
    existing = (
        (
            await connection.execute(
                sa.select(adaptive_reviews).where(adaptive_reviews.c.review_end == review_end)
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        proposal_id = await connection.scalar(
            sa.select(adaptive_proposals.c.id).where(
                adaptive_proposals.c.review_id == existing["id"]
            )
        )
        stored_counts = existing["block_measurement_counts"]
        block_counts = (stored_counts[0], stored_counts[1], stored_counts[2])
        evidence = AdjustmentEvidence(
            days_since_target_change=existing["days_since_target_change"],
            measured_dates=existing["measured_dates"],
            weight_span_days=existing["weight_span_days"],
            max_weight_gap_days=existing["max_weight_gap_days"],
            block_measurement_counts=block_counts,
            complete_food_days=existing["complete_food_days"],
            total_food_days=21,
            unresolved_drafts=existing["unresolved_drafts"],
            complete_energy_days=existing["complete_energy_days"],
            targeted_energy_days=existing["targeted_energy_days"],
            mean_intake_kcal=(
                Decimal(str(existing["mean_intake_kcal"]))
                if existing["mean_intake_kcal"] is not None
                else None
            ),
            mean_target_kcal=(
                Decimal(str(existing["mean_target_kcal"]))
                if existing["mean_target_kcal"] is not None
                else None
            ),
            target_rate_grams_per_week=existing["target_rate_grams_per_week"],
            observed_rate_grams_per_week=(
                Decimal(str(existing["observed_rate_grams_per_week"]))
                if existing["observed_rate_grams_per_week"] is not None
                else None
            ),
            prior_eligible_direction=None,
            prior_review_days_ago=None,
        )
        evaluation = AdjustmentEvaluation(
            existing["eligible"],
            tuple(existing["reason_codes"]),
            existing["mismatch_direction"],
            max(
                Decimal(100),
                Decimal(abs(evidence.target_rate_grams_per_week)) * Decimal("0.25"),
            ),
            (
                Decimal(str(existing["raw_delta_kcal"]))
                if existing["raw_delta_kcal"] is not None
                else None
            ),
            None,
        )
        return ReviewResult(existing["id"], review_end, evaluation, evidence, proposal_id)
    evidence, plan, goal = await _evidence(connection, review_end)
    evaluation = evaluate_adjustment(evidence)
    proposed = evaluation.proposed_delta_kcal
    carbohydrate = None
    if proposed is not None:
        remaining = (
            plan["energy_kcal"] + proposed - plan["protein_grams"] * 4 - plan["fat_grams"] * 9
        )
        carbohydrate = int((Decimal(remaining) / 4).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        proposed_energy = plan["energy_kcal"] + proposed
        outside_reviewed_range = (
            plan["energy_range_low_kcal"] is not None
            and proposed_energy < plan["energy_range_low_kcal"]
        ) or (
            plan["energy_range_high_kcal"] is not None
            and proposed_energy > plan["energy_range_high_kcal"]
        )
        if outside_reviewed_range:
            evaluation = replace(
                evaluation,
                reasons=("proposed_outside_reviewed_range",),
                proposed_delta_kcal=None,
            )
            proposed = None
        elif not 1 <= proposed_energy <= 10_000 or not 0 <= carbohydrate <= 1_200:
            evaluation = replace(
                evaluation,
                reasons=("proposed_macros_infeasible",),
                proposed_delta_kcal=None,
            )
            proposed = None
    review_id = (
        await connection.execute(
            sa.insert(adaptive_reviews)
            .values(
                review_end=review_end,
                window_start=review_end - timedelta(days=20),
                eligible=evaluation.eligible,
                reason_codes=list(evaluation.reasons),
                days_since_target_change=evidence.days_since_target_change,
                measured_dates=evidence.measured_dates,
                weight_span_days=evidence.weight_span_days,
                max_weight_gap_days=evidence.max_weight_gap_days,
                block_measurement_counts=list(evidence.block_measurement_counts),
                complete_food_days=evidence.complete_food_days,
                unresolved_drafts=evidence.unresolved_drafts,
                complete_energy_days=evidence.complete_energy_days,
                targeted_energy_days=evidence.targeted_energy_days,
                mean_intake_kcal=(
                    float(evidence.mean_intake_kcal)
                    if evidence.mean_intake_kcal is not None
                    else None
                ),
                mean_target_kcal=(
                    float(evidence.mean_target_kcal)
                    if evidence.mean_target_kcal is not None
                    else None
                ),
                target_rate_grams_per_week=evidence.target_rate_grams_per_week,
                observed_rate_grams_per_week=(
                    float(evidence.observed_rate_grams_per_week)
                    if evidence.observed_rate_grams_per_week is not None
                    else None
                ),
                mismatch_direction=evaluation.mismatch_direction,
                raw_delta_kcal=(
                    float(evaluation.raw_delta_kcal)
                    if evaluation.raw_delta_kcal is not None
                    else None
                ),
                action_key=action_key,
                created_at=time.time(),
            )
            .returning(adaptive_reviews.c.id)
        )
    ).scalar_one()
    proposal_id = None
    if proposed is not None:
        assert carbohydrate is not None
        proposal_id = (
            await connection.execute(
                sa.insert(adaptive_proposals)
                .values(
                    review_id=review_id,
                    goal_id=goal["id"],
                    current_target_plan_id=plan["id"],
                    proposed_delta_kcal=proposed,
                    energy_kcal=plan["energy_kcal"] + proposed,
                    protein_grams=plan["protein_grams"],
                    fat_grams=plan["fat_grams"],
                    carbohydrate_grams=carbohydrate,
                    state="pending",
                    created_at=time.time(),
                )
                .returning(adaptive_proposals.c.id)
            )
        ).scalar_one()
    return ReviewResult(review_id, review_end, evaluation, evidence, proposal_id)


async def resolve_proposal(
    connection: AsyncConnection,
    proposal_id: int,
    action: str,
    *,
    action_key: str,
    effective_from: date,
) -> tuple[AdaptiveProposal, int | None]:
    proposal = await get_adaptive_proposal(connection, proposal_id)
    if proposal.state == "applied":
        return proposal, proposal.applied_target_plan_id
    if proposal.state == "kept":
        raise AdaptiveError(
            "That proposal was already declined; the current target remains active."
        )
    if action == "keep":
        await connection.execute(
            sa.update(adaptive_proposals)
            .where(adaptive_proposals.c.id == proposal.id)
            .values(state="kept", resolved_action_key=action_key, resolved_at=time.time())
        )
        return await get_adaptive_proposal(connection, proposal.id), None
    if action != "apply":
        raise AdaptiveError("Choose Apply or Keep target.")
    from nutrition_bot.adapters.database.supplement_plans import has_weight_context

    if await has_weight_context(connection, effective_from - timedelta(days=1)):
        raise AdaptiveError(
            "Creatine context changed; calorie adjustment is withheld. "
            "Keep the target and review later."
        )
    current = await current_plan(connection, on_date=effective_from - timedelta(days=1))
    if current is None or current.id != proposal.current_target_plan_id:
        raise AdaptiveError("Targets changed after this proposal. Run /adjust again.")
    future_plan = await connection.scalar(
        sa.select(target_plans.c.id)
        .where(target_plans.c.effective_from >= effective_from)
        .order_by(target_plans.c.effective_from, target_plans.c.id)
        .limit(1)
    )
    if future_plan is not None:
        raise AdaptiveError("A newer target plan is already scheduled. Run /adjust again.")
    current_row = (
        (await connection.execute(sa.select(target_plans).where(target_plans.c.id == current.id)))
        .mappings()
        .one()
    )
    if (
        current_row["energy_range_low_kcal"] is not None
        and proposal.energy_kcal < current_row["energy_range_low_kcal"]
    ) or (
        current_row["energy_range_high_kcal"] is not None
        and proposal.energy_kcal > current_row["energy_range_high_kcal"]
    ):
        raise AdaptiveError(
            "This change is outside the reviewed calorie range. Create a fresh goal plan."
        )
    plan_id = (
        await connection.execute(
            sa.insert(target_plans)
            .values(
                goal_id=current.goal_id,
                effective_from=effective_from,
                energy_kcal=proposal.energy_kcal,
                protein_grams=proposal.protein_grams,
                fat_grams=proposal.fat_grams,
                carbohydrate_grams=proposal.carbohydrate_grams,
                energy_range_low_kcal=current_row["energy_range_low_kcal"],
                energy_range_high_kcal=current_row["energy_range_high_kcal"],
                reference_weight_grams=current_row["reference_weight_grams"],
                estimated_tdee_kcal=current_row["estimated_tdee_kcal"],
                source="adaptive",
                calculation_version="adaptive-targets-v1",
                proposal_id=None,
                action_key=action_key,
                created_at=time.time(),
            )
            .returning(target_plans.c.id)
        )
    ).scalar_one()
    await connection.execute(
        sa.update(adaptive_proposals)
        .where(adaptive_proposals.c.id == proposal.id)
        .values(
            state="applied",
            resolved_action_key=action_key,
            applied_target_plan_id=plan_id,
            resolved_at=time.time(),
        )
    )
    return await get_adaptive_proposal(connection, proposal.id), plan_id
