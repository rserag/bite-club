from datetime import date, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema_adaptive import adaptive_reviews
from nutrition_bot.application.adaptive_targets import (
    AdaptiveError,
    AdaptiveProposal,
    ReviewResult,
    get_adaptive_proposal,
    pending_proposal,
    resolve_proposal,
    run_review,
)
from nutrition_bot.application.meal_conversation import MealReply

REASONS = {
    "target_not_stable_21_days": "the current target has not been stable for 21 days",
    "fewer_than_12_weight_dates": "fewer than 12 weight dates are available",
    "weight_window_too_short": "the measured weight span is shorter than 14 days",
    "weight_gap_over_7_days": "the weight history contains a gap longer than 7 days",
    "sparse_weekly_weight_block": "at least one seven-day block has fewer than 3 weight dates",
    "food_completeness_below_90_percent": "fewer than 90% of food days are complete",
    "unresolved_meal_drafts": "unresolved meal drafts remain in the review window",
    "incomplete_energy_coverage": "some complete days lack full energy data",
    "missing_historical_targets": "some complete days have no historical calorie target",
    "intake_or_target_average_unavailable": "intake or target averages are unavailable",
    "mean_intake_not_within_10_percent_of_target": "mean intake is not within 10% of targets",
    "weight_rate_unavailable": "a robust weight rate is unavailable",
    "awaiting_second_weekly_confirmation": "the same mismatch must recur next weekly review",
    "proposed_macros_infeasible": "a calorie reduction would make the macro plan infeasible",
    "proposed_outside_reviewed_range": (
        "the change would leave the reviewed calorie range; create a fresh goal plan"
    ),
}


def _rate(value: Decimal | None) -> str:
    if value is None:
        return "unavailable"
    return f"{value / 1000:+.2f} kg/week"


async def _proposal_reply(
    connection: AsyncConnection, proposal: AdaptiveProposal, lead: str = "Calorie proposal"
) -> MealReply:
    review = (
        (
            await connection.execute(
                sa.select(adaptive_reviews).where(adaptive_reviews.c.id == proposal.review_id)
            )
        )
        .mappings()
        .one()
    )
    lines = [
        f"{lead} A{proposal.id} · review ending {review['review_end']}",
        f"Target rate: {_rate(Decimal(review['target_rate_grams_per_week']))}; "
        f"observed: {_rate(Decimal(str(review['observed_rate_grams_per_week'])))}.",
        f"Mean intake: {review['mean_intake_kcal']:.0f} kcal; "
        f"mean target: {review['mean_target_kcal']:.0f} kcal over "
        f"{review['complete_food_days']}/21 complete dates.",
        f"Proposed change: {proposal.proposed_delta_kcal:+d} kcal/day.",
        f"New targets: {proposal.energy_kcal} kcal · P {proposal.protein_grams} g · "
        f"F {proposal.fat_grams} g · C {proposal.carbohydrate_grams} g.",
        "The change is half-damped, rounded to 50 kcal, and capped at 150 kcal/day.",
    ]
    if proposal.state == "pending":
        lines.append("Apply starts tomorrow. Keep target records the decision without changing it.")
        return MealReply(
            "\n".join(lines),
            "adaptive_proposal",
            buttons=("apply", "keep", "review"),
            adaptive_proposal_id=proposal.id,
        )
    lines.append(f"State: {proposal.state}.")
    return MealReply("\n".join(lines), "adaptive_result", adaptive_proposal_id=proposal.id)


def _review_reply(result: ReviewResult) -> MealReply:
    evidence, evaluation = result.evidence, result.evaluation
    lines = [
        f"Adaptive review R{result.review_id} · ending {result.review_end}",
        f"Food coverage: {evidence.complete_food_days}/21 complete dates; "
        f"energy data {evidence.complete_energy_days}/{evidence.complete_food_days}.",
        f"Weight coverage: {evidence.measured_dates} dates over {evidence.weight_span_days} days; "
        f"largest gap {evidence.max_weight_gap_days} days.",
        f"Target rate: {_rate(Decimal(evidence.target_rate_grams_per_week))}; "
        f"observed: {_rate(evidence.observed_rate_grams_per_week)}.",
    ]
    if not evaluation.eligible:
        lines.append("No adjustment evaluation yet:")
        lines.extend(f"• {REASONS.get(reason, reason)}" for reason in evaluation.reasons)
    elif evaluation.mismatch_direction == 0:
        lines.append(
            f"No change suggested; the mismatch is inside the "
            f"{evaluation.deadband_grams_per_week / 1000:.2f} kg/week deadband."
        )
    else:
        lines.extend(f"• {REASONS.get(reason, reason)}" for reason in evaluation.reasons)
    lines.append("No target changed.")
    return MealReply("\n".join(lines), "adaptive_review")


async def handle_adaptive_message(
    connection: AsyncConnection, text: str, *, action_key: str, today: date
) -> MealReply | None:
    normalized = " ".join(text.casefold().split())
    if normalized not in {"/adjust", "review calorie target", "should my calories change"}:
        return None
    try:
        from nutrition_bot.adapters.database.supplement_plans import has_weight_context

        if await has_weight_context(connection, today):
            raise AdaptiveError(
                "Calorie review withheld: creatine context falls in the 28-day "
                "weight window. Scale changes may include fluid changes."
            )
        pending = await pending_proposal(connection)
        if pending is not None:
            return await _proposal_reply(connection, pending, "Pending calorie proposal")
        result = await run_review(connection, today, action_key=action_key)
        if result.proposal_id is not None:
            return await _proposal_reply(
                connection, await get_adaptive_proposal(connection, result.proposal_id)
            )
        return _review_reply(result)
    except AdaptiveError as exc:
        return MealReply(f"{exc}\nNo target changed.", "adaptive_rejected")


async def handle_adaptive_callback(
    connection: AsyncConnection,
    action: str,
    proposal_id: int,
    *,
    action_key: str,
    today: date,
) -> MealReply:
    try:
        proposal = await get_adaptive_proposal(connection, proposal_id)
        if action == "review":
            return await _proposal_reply(connection, proposal, "Proposal evidence")
        proposal, plan_id = await resolve_proposal(
            connection,
            proposal.id,
            action,
            action_key=action_key,
            effective_from=today + timedelta(days=1),
        )
        lead = "Applied calorie proposal" if action == "apply" else "Kept current target"
        reply = await _proposal_reply(connection, proposal, lead)
        if plan_id is not None:
            return MealReply(
                reply.text + f"\nTarget plan T{plan_id} starts {today + timedelta(days=1)}.",
                reply.kind,
                adaptive_proposal_id=proposal.id,
            )
        return reply
    except AdaptiveError as exc:
        return MealReply(f"{exc}\nNo target changed.", "adaptive_rejected")
