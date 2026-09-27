from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from nutrition_bot.domain.training_load import TrainingLoadTrend


@dataclass(frozen=True)
class RecoverySignal:
    local_date: date
    soreness: int | None
    fatigue: int | None

    @property
    def elevated(self) -> bool:
        return (self.soreness or 0) >= 4 or (self.fatigue or 0) >= 4


@dataclass(frozen=True)
class TrainingGuidance:
    total_jump_prompt: bool
    bjj_jump_supported: bool
    elevated_recovery_days: int
    lighter_gym_option: bool


def _increase_at_least_30(latest: int, baseline: Fraction | None) -> bool:
    return (
        baseline is not None and baseline > 0 and Fraction(latest, 1) >= baseline * Fraction(13, 10)
    )


def training_guidance(
    trend: TrainingLoadTrend, recovery: tuple[RecoverySignal, ...]
) -> TrainingGuidance:
    comparison_available = trend.comparison_status == "available"
    total_jump = (
        comparison_available and trend.change_percent is not None and trend.change_percent >= 30
    )
    bjj_jump = comparison_available and _increase_at_least_30(
        trend.latest.bjj_load_tenths, trend.baseline_median_bjj_load_tenths
    )
    elevated_days = len({item.local_date for item in recovery if item.elevated})
    return TrainingGuidance(total_jump, bjj_jump, elevated_days, bjj_jump and elevated_days >= 2)
