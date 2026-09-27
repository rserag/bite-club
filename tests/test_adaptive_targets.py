from dataclasses import replace
from decimal import Decimal

import pytest

from nutrition_bot.domain.adaptive_targets import AdjustmentEvidence, evaluate_adjustment


@pytest.fixture
def complete_evidence() -> AdjustmentEvidence:
    return AdjustmentEvidence(
        days_since_target_change=28,
        measured_dates=15,
        weight_span_days=25,
        max_weight_gap_days=3,
        block_measurement_counts=(4, 4, 4),
        complete_food_days=19,
        total_food_days=21,
        unresolved_drafts=0,
        complete_energy_days=19,
        targeted_energy_days=19,
        mean_intake_kcal=Decimal("2200"),
        mean_target_kcal=Decimal("2200"),
        target_rate_grams_per_week=-400,
        observed_rate_grams_per_week=Decimal("-100"),
        prior_eligible_direction=-1,
        prior_review_days_ago=7,
    )


def test_confirmed_mismatch_is_damped_rounded_and_clamped(
    complete_evidence: AdjustmentEvidence,
):
    result = evaluate_adjustment(complete_evidence)
    assert result.eligible
    assert result.deadband_grams_per_week == 100
    assert result.mismatch_direction == -1
    assert result.raw_delta_kcal == Decimal("-330")
    assert result.proposed_delta_kcal == -150


def test_first_directional_review_only_establishes_confirmation(
    complete_evidence: AdjustmentEvidence,
):
    result = evaluate_adjustment(
        replace(
            complete_evidence,
            prior_eligible_direction=None,
            prior_review_days_ago=None,
        )
    )
    assert result.eligible
    assert result.reasons == ("awaiting_second_weekly_confirmation",)
    assert result.proposed_delta_kcal is None


def test_deadband_ignores_small_rate_difference(complete_evidence: AdjustmentEvidence):
    result = evaluate_adjustment(
        replace(complete_evidence, observed_rate_grams_per_week=Decimal("-310"))
    )
    assert result.eligible
    assert result.mismatch_direction == 0
    assert result.proposed_delta_kcal is None


def test_opposite_or_nonweekly_previous_review_does_not_confirm(
    complete_evidence: AdjustmentEvidence,
):
    opposite = evaluate_adjustment(replace(complete_evidence, prior_eligible_direction=1))
    wrong_date = evaluate_adjustment(replace(complete_evidence, prior_review_days_ago=8))
    assert opposite.proposed_delta_kcal is None
    assert wrong_date.proposed_delta_kcal is None


def test_every_evidence_gate_is_reported_together(complete_evidence: AdjustmentEvidence):
    result = evaluate_adjustment(
        replace(
            complete_evidence,
            days_since_target_change=20,
            measured_dates=11,
            weight_span_days=13,
            max_weight_gap_days=8,
            block_measurement_counts=(2, 4, 4),
            complete_food_days=18,
            unresolved_drafts=1,
            complete_energy_days=17,
            targeted_energy_days=17,
            mean_intake_kcal=Decimal("2500"),
            observed_rate_grams_per_week=None,
        )
    )
    assert not result.eligible
    assert set(result.reasons) == {
        "target_not_stable_21_days",
        "fewer_than_12_weight_dates",
        "weight_window_too_short",
        "weight_gap_over_7_days",
        "sparse_weekly_weight_block",
        "food_completeness_below_90_percent",
        "unresolved_meal_drafts",
        "incomplete_energy_coverage",
        "missing_historical_targets",
        "mean_intake_not_within_10_percent_of_target",
        "weight_rate_unavailable",
    }
    assert result.proposed_delta_kcal is None
