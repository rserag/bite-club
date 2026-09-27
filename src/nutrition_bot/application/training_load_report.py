from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.training_analytics import (
    recovery_signals,
    weekly_training_load,
)
from nutrition_bot.application.meal_conversation import MealReply
from nutrition_bot.domain.training_guidance import RecoverySignal, training_guidance
from nutrition_bot.domain.training_load import TrainingLoadTrend


def _load(value: int | Fraction) -> str:
    amount = (
        Decimal(value.numerator) / Decimal(value.denominator) / 10
        if isinstance(value, Fraction)
        else Decimal(value) / 10
    )
    return f"{amount.quantize(Decimal('0.1')).normalize()} AU"


def _percent(value: Fraction) -> str:
    rounded = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return f"{rounded:+}"


def render_training_load_report(
    trend: TrainingLoadTrend, recovery: tuple[RecoverySignal, ...], *, short: bool = False
) -> MealReply:
    week = trend.latest
    guidance = training_guidance(trend, recovery)
    lines = [
        f"Training load · {week.start} to {week.end}",
        f"Total {_load(week.total_load_tenths)} · Gym {_load(week.gym_load_tenths)} · "
        f"BJJ {_load(week.bjj_load_tenths)}",
        f"Coverage: {week.covered_days}/7 dates confirmed · {week.session_count} sessions",
        f"Effort: {week.reported_session_count} reported · {week.inferred_session_count} estimated "
        f"({_load(week.inferred_load_tenths)} estimated load)",
    ]
    if trend.comparison_status == "available":
        assert trend.baseline_median_load_tenths is not None
        assert trend.change_percent is not None
        lines.append(
            f"Baseline: {_load(trend.baseline_median_load_tenths)} median from "
            f"{trend.valid_reference_weeks} complete reference weeks · "
            f"change {_percent(trend.change_percent)}%"
        )
    elif trend.comparison_status == "latest_week_incomplete":
        lines.append("No relative comparison: the latest week has unknown training/rest dates.")
    elif trend.comparison_status == "insufficient_reference_weeks":
        lines.append(
            "No relative comparison: fewer than 3 of the preceding 4 weeks have complete "
            "training/rest coverage."
        )
    else:
        lines.append("Starting or resuming training: the reference baseline is zero.")

    if guidance.total_jump_prompt:
        lines.append(
            "Workload prompt: this week is at least 30% above the eligible baseline. "
            "Review sleep, soreness, fatigue and readiness; this is not an injury-risk threshold."
        )
    if guidance.lighter_gym_option:
        lines.append(
            f"Optional next-gym adjustment: BJJ load is at least 30% above its baseline and "
            f"{guidance.elevated_recovery_days} dates report soreness or fatigue at 4–5/5. "
            "Consider removing one working set per exercise, avoiding grinding sets, or moving "
            "the session."
        )
    elif not short:
        lines.append(
            "No lighter-gym suggestion: it requires both unusually high BJJ load and at least "
            "2 elevated recovery check-ins."
        )
    lines.append("No workout or plan was changed.")
    return MealReply("\n".join(lines), "training_load_report")


async def build_training_load_report(
    connection: AsyncConnection, *, as_of: date, short: bool = False
) -> MealReply:
    trend = await weekly_training_load(connection, as_of=as_of)
    recovery = await recovery_signals(connection, start=trend.latest.start, end=trend.latest.end)
    return render_training_load_report(trend, recovery, short=short)


async def handle_training_load_message(
    connection: AsyncConnection, text: str, *, today: date
) -> MealReply | None:
    normalized = " ".join(text.strip().casefold().split())
    if normalized in {"/load", "/training load", "show this week's training load"}:
        return await build_training_load_report(connection, as_of=today)
    if normalized in {"/load short", "/training load short", "short training load"}:
        return await build_training_load_report(connection, as_of=today, short=True)
    return None
