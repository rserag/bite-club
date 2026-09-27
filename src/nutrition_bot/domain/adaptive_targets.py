from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class AdjustmentEvidence:
    days_since_target_change: int
    measured_dates: int
    weight_span_days: int
    max_weight_gap_days: int
    block_measurement_counts: tuple[int, int, int]
    complete_food_days: int
    total_food_days: int
    unresolved_drafts: int
    complete_energy_days: int
    targeted_energy_days: int
    mean_intake_kcal: Decimal | None
    mean_target_kcal: Decimal | None
    target_rate_grams_per_week: int
    observed_rate_grams_per_week: Decimal | None
    prior_eligible_direction: int | None
    prior_review_days_ago: int | None


@dataclass(frozen=True)
class AdjustmentEvaluation:
    eligible: bool
    reasons: tuple[str, ...]
    mismatch_direction: int
    deadband_grams_per_week: Decimal
    raw_delta_kcal: Decimal | None
    proposed_delta_kcal: int | None


def _round_50(value: Decimal) -> int:
    return int((value / 50).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * 50


def evaluate_adjustment(evidence: AdjustmentEvidence) -> AdjustmentEvaluation:
    reasons: list[str] = []
    if evidence.days_since_target_change < 21:
        reasons.append("target_not_stable_21_days")
    if evidence.measured_dates < 12:
        reasons.append("fewer_than_12_weight_dates")
    if evidence.weight_span_days < 14:
        reasons.append("weight_window_too_short")
    if evidence.max_weight_gap_days > 7:
        reasons.append("weight_gap_over_7_days")
    if any(count < 3 for count in evidence.block_measurement_counts):
        reasons.append("sparse_weekly_weight_block")
    required_complete = (evidence.total_food_days * 9 + 9) // 10
    if evidence.complete_food_days < required_complete:
        reasons.append("food_completeness_below_90_percent")
    if evidence.unresolved_drafts:
        reasons.append("unresolved_meal_drafts")
    if evidence.complete_energy_days != evidence.complete_food_days:
        reasons.append("incomplete_energy_coverage")
    if evidence.targeted_energy_days != evidence.complete_food_days:
        reasons.append("missing_historical_targets")
    if evidence.mean_intake_kcal is None or evidence.mean_target_kcal is None:
        reasons.append("intake_or_target_average_unavailable")
    elif evidence.mean_target_kcal <= 0 or (
        abs(evidence.mean_intake_kcal - evidence.mean_target_kcal) / evidence.mean_target_kcal
        > Decimal("0.10")
    ):
        reasons.append("mean_intake_not_within_10_percent_of_target")
    if evidence.observed_rate_grams_per_week is None:
        reasons.append("weight_rate_unavailable")
    deadband = max(
        Decimal(100), Decimal(abs(evidence.target_rate_grams_per_week)) * Decimal("0.25")
    )
    if reasons:
        return AdjustmentEvaluation(False, tuple(reasons), 0, deadband, None, None)
    assert evidence.observed_rate_grams_per_week is not None
    mismatch = Decimal(evidence.target_rate_grams_per_week) - evidence.observed_rate_grams_per_week
    direction = 0 if abs(mismatch) <= deadband else (1 if mismatch > 0 else -1)
    if direction == 0:
        return AdjustmentEvaluation(True, (), 0, deadband, Decimal(0), None)
    raw = mismatch * Decimal("7.7") / 7
    confirmed = (
        evidence.prior_eligible_direction == direction and evidence.prior_review_days_ago == 7
    )
    if not confirmed:
        return AdjustmentEvaluation(
            True, ("awaiting_second_weekly_confirmation",), direction, deadband, raw, None
        )
    damped = raw * Decimal("0.5")
    bounded = max(Decimal(-150), min(Decimal(150), damped))
    proposal = _round_50(bounded)
    return AdjustmentEvaluation(True, (), direction, deadband, raw, proposal or None)
