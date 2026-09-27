from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.goals import (
    GoalStoreError,
    ProposalSnapshot,
    apply_proposal,
    cancel_proposal,
    create_proposal,
    current_plan,
    get_proposal,
)
from nutrition_bot.adapters.database.schema_goals import target_plans
from nutrition_bot.application.meal_conversation import MealReply
from nutrition_bot.domain.goals import GoalError, estimate_targets, manual_targets


def _help() -> MealReply:
    return MealReply(
        "Goal setup creates a proposal first; only the exact reviewed receipt can activate it.\n"
        "Estimated starting plan:\n"
        "/goal estimate mode=loss weight=80kg height=180cm age=35 "
        "coefficient=+5 activity=1.6 rate=0.4kg/week\n"
        "Choose coefficient +5 or -161 explicitly. Activity 1.4/1.6/1.8 means lower/typical/"
        "higher total activity, including usual training.\n"
        "Manual plan:\n"
        "/goal manual mode=loss weight=80kg rate=0.4kg/week calories=2200 "
        "protein=160g fat=65g\n"
        "Use mode=maintenance and rate=0 for maintenance. Carbohydrate is the calorie remainder."
    )


def _values(text: str, method: str) -> dict[str, str]:
    parts = text.split()[2:]
    values: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            raise GoalError("Use the guided key=value format shown by /goal setup.")
        key, value = part.split("=", 1)
        if not key or not value or key in values:
            raise GoalError("Each goal field must appear once in key=value form.")
        values[key.casefold()] = value.casefold()
    required = (
        {"mode", "weight", "height", "age", "coefficient", "activity", "rate"}
        if method == "estimate"
        else {"mode", "weight", "rate", "calories", "protein", "fat"}
    )
    if set(values) != required:
        missing = ", ".join(sorted(required - set(values))) or "none"
        extra = ", ".join(sorted(set(values) - required)) or "none"
        raise GoalError(f"Goal fields do not match. Missing: {missing}. Extra: {extra}.")
    return values


def _decimal(value: str, suffix: str, label: str) -> Decimal:
    if suffix and not value.endswith(suffix):
        raise GoalError(f"{label} must end in {suffix}.")
    raw = value[: -len(suffix)] if suffix else value
    try:
        result = Decimal(raw)
    except InvalidOperation:
        raise GoalError(f"{label} must be a number.") from None
    if not result.is_finite():
        raise GoalError(f"{label} must be a finite number.")
    return result


def _integer(value: str, suffix: str, label: str) -> int:
    number = _decimal(value, suffix, label)
    if number != number.to_integral_value():
        raise GoalError(f"{label} must be a whole number.")
    return int(number)


def _proposal_receipt(proposal: ProposalSnapshot, lead: str = "Review goal proposal") -> MealReply:
    weight = Decimal(proposal.reference_weight_grams) / 1000
    rate = Decimal(abs(proposal.target_rate_grams_per_week)) / 1000
    direction = "loss" if proposal.target_rate_grams_per_week < 0 else "gain"
    rate_text = (
        "maintenance" if proposal.target_rate_grams_per_week == 0 else f"{rate} kg/week {direction}"
    )
    lines = [
        f"{lead} G{proposal.id} · {proposal.mode} · reference weight {weight} kg",
        f"Intended rate: {rate_text}",
        f"Targets: {proposal.energy_kcal} kcal · P {proposal.protein_grams} g · "
        f"F {proposal.fat_grams} g · C {proposal.carbohydrate_grams} g",
    ]
    if proposal.method == "estimate":
        lines.append(
            f"Estimated TDEE: {proposal.estimated_tdee_kcal} kcal; practical calorie range "
            f"{proposal.energy_range_low_kcal}–{proposal.energy_range_high_kcal} kcal."
        )
        lines.append(
            "This is a population-based starting estimate using your explicit coefficient and "
            "total-activity choice; it will need trend data before adjustment."
        )
    else:
        lines.append("Manual targets: carbohydrate was calculated from the remaining calories.")
    if proposal.state == "open":
        lines.append("Apply starts this plan tomorrow. Cancel keeps current targets unchanged.")
        return MealReply(
            "\n".join(lines),
            "goal_proposal",
            buttons=("apply", "cancel"),
            goal_proposal_id=proposal.id,
        )
    lines.append(f"State: {proposal.state}.")
    return MealReply("\n".join(lines), "goal_result", goal_proposal_id=proposal.id)


async def handle_goal_message(
    connection: AsyncConnection, text: str, *, action_key: str, today: date
) -> MealReply | None:
    lowered = text.casefold()
    if not (lowered == "/goal" or lowered.startswith("/goal ")):
        return None
    try:
        if lowered in {"/goal setup", "/goal help"}:
            return _help()
        if lowered == "/goal":
            active = await current_plan(connection, on_date=today)
            future = (
                (
                    await connection.execute(
                        sa.select(target_plans)
                        .where(target_plans.c.effective_from > today)
                        .order_by(target_plans.c.effective_from, target_plans.c.id.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            lines = []
            if active:
                lines.append(
                    f"Current targets: {active.energy_kcal} kcal · P {active.protein_grams} g · "
                    f"F {active.fat_grams} g · C {active.carbohydrate_grams} g."
                )
            else:
                lines.append("No target plan is active today.")
            if future:
                lines.append(
                    f"Next plan starts {future['effective_from']}: {future['energy_kcal']} kcal · "
                    f"P {future['protein_grams']} g · F {future['fat_grams']} g · "
                    f"C {future['carbohydrate_grams']} g."
                )
            lines.append("Use /goal setup to create or replace a plan.")
            return MealReply("\n".join(lines), "goal_status")
        method = text.split(maxsplit=2)[1].casefold() if len(text.split()) > 1 else ""
        if method not in {"estimate", "manual"}:
            return _help()
        values = _values(text, method)
        mode = values["mode"]
        weight = _decimal(values["weight"], "kg", "Weight")
        rate = Decimal(0) if values["rate"] == "0" else _decimal(values["rate"], "kg/week", "Rate")
        proposal = (
            estimate_targets(
                mode=mode,
                weight_kg=weight,
                height_cm=_decimal(values["height"], "cm", "Height"),
                age_years=_integer(values["age"], "", "Age"),
                rmr_coefficient=_integer(values["coefficient"], "", "Coefficient"),
                activity_factor=_decimal(values["activity"], "", "Activity"),
                rate_kg_per_week=rate,
            )
            if method == "estimate"
            else manual_targets(
                mode=mode,
                weight_kg=weight,
                rate_kg_per_week=rate,
                energy_kcal=_integer(values["calories"], "", "Calories"),
                protein_grams=_integer(values["protein"], "g", "Protein"),
                fat_grams=_integer(values["fat"], "g", "Fat"),
            )
        )
        return _proposal_receipt(await create_proposal(connection, proposal, action_key=action_key))
    except (GoalError, GoalStoreError) as exc:
        return MealReply(
            f"{exc}\nNothing changed. Use /goal setup for the guided format.", "goal_rejected"
        )


async def handle_goal_callback(
    connection: AsyncConnection,
    action: str,
    proposal_id: int,
    *,
    action_key: str,
    today: date,
) -> MealReply:
    try:
        proposal = await get_proposal(connection, proposal_id)
        if action == "apply":
            proposal, plan = await apply_proposal(
                connection,
                proposal.id,
                action_key=action_key,
                effective_from=today + timedelta(days=1),
            )
            result = _proposal_receipt(proposal, "Applied goal proposal")
            return MealReply(
                result.text + f"\nPlan T{plan.id} starts {plan.effective_from}.",
                result.kind,
                goal_proposal_id=proposal.id,
            )
        if action == "cancel":
            proposal = await cancel_proposal(connection, proposal.id, action_key=action_key)
            return _proposal_receipt(proposal, "Cancelled goal proposal")
        raise GoalStoreError("Unknown goal action.")
    except GoalStoreError as exc:
        return MealReply(f"{exc}\nNothing changed.", "goal_rejected")
