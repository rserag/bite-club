from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from typing import Literal

TrainingKind = Literal["gym", "bjj"]
ComparisonStatus = Literal[
    "available",
    "latest_week_incomplete",
    "insufficient_reference_weeks",
    "zero_baseline",
]


@dataclass(frozen=True)
class TrainingLoadSession:
    local_date: date
    kind: TrainingKind
    duration_minutes: int
    session_rpe_tenths: int
    rpe_source: str

    @property
    def load_tenths(self) -> int:
        """Session-RPE load in tenths of an arbitrary unit."""
        return self.duration_minutes * self.session_rpe_tenths

    @property
    def reported_effort(self) -> bool:
        return self.rpe_source == "reported"


@dataclass(frozen=True)
class WeeklyTrainingLoad:
    start: date
    end: date
    total_load_tenths: int
    gym_load_tenths: int
    bjj_load_tenths: int
    session_count: int
    reported_session_count: int
    inferred_session_count: int
    reported_load_tenths: int
    inferred_load_tenths: int
    covered_days: int

    @property
    def coverage_complete(self) -> bool:
        return self.covered_days == 7


@dataclass(frozen=True)
class TrainingLoadTrend:
    latest: WeeklyTrainingLoad
    reference_weeks: tuple[WeeklyTrainingLoad, ...]
    valid_reference_weeks: int
    baseline_median_load_tenths: Fraction | None
    baseline_median_gym_load_tenths: Fraction | None
    baseline_median_bjj_load_tenths: Fraction | None
    change_percent: Fraction | None
    comparison_status: ComparisonStatus


def latest_completed_week_start(as_of: date) -> date:
    """Return the Monday of the last Monday-Sunday week ending before as_of."""
    return as_of - timedelta(days=as_of.weekday() + 7)


def workload_window_start(as_of: date) -> date:
    """Start of the latest week plus its four calendar reference weeks."""
    return latest_completed_week_start(as_of) - timedelta(weeks=4)


def _median(values: list[int]) -> Fraction:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return Fraction(ordered[middle])
    return Fraction(ordered[middle - 1] + ordered[middle], 2)


def _week(
    start: date,
    sessions: tuple[TrainingLoadSession, ...],
    rest_days: frozenset[date],
) -> WeeklyTrainingLoad:
    end = start + timedelta(days=6)
    selected = tuple(item for item in sessions if start <= item.local_date <= end)
    session_days = {item.local_date for item in selected}
    reported = tuple(item for item in selected if item.reported_effort)
    inferred = tuple(item for item in selected if not item.reported_effort)
    gym = sum(item.load_tenths for item in selected if item.kind == "gym")
    bjj = sum(item.load_tenths for item in selected if item.kind == "bjj")
    covered = session_days | {day for day in rest_days if start <= day <= end}
    return WeeklyTrainingLoad(
        start=start,
        end=end,
        total_load_tenths=gym + bjj,
        gym_load_tenths=gym,
        bjj_load_tenths=bjj,
        session_count=len(selected),
        reported_session_count=len(reported),
        inferred_session_count=len(inferred),
        reported_load_tenths=sum(item.load_tenths for item in reported),
        inferred_load_tenths=sum(item.load_tenths for item in inferred),
        covered_days=len(covered),
    )


def summarize_training_load(
    sessions: tuple[TrainingLoadSession, ...],
    rest_days: frozenset[date],
    *,
    as_of: date,
) -> TrainingLoadTrend:
    """Summarize the latest completed week against up to four preceding weeks.

    A day is covered only by a current completed session or an explicit rest
    record. Plans and cancellations are intentionally not evidence of what
    happened. Derived totals are calculated on demand and are never persisted.
    """
    latest_start = latest_completed_week_start(as_of)
    latest = _week(latest_start, sessions, rest_days)
    references = tuple(
        _week(latest_start - timedelta(weeks=offset), sessions, rest_days) for offset in range(1, 5)
    )
    valid = tuple(item for item in references if item.coverage_complete)
    if not latest.coverage_complete:
        return TrainingLoadTrend(
            latest, references, len(valid), None, None, None, None, "latest_week_incomplete"
        )
    if len(valid) < 3:
        return TrainingLoadTrend(
            latest, references, len(valid), None, None, None, None, "insufficient_reference_weeks"
        )
    baseline = _median([item.total_load_tenths for item in valid])
    gym_baseline = _median([item.gym_load_tenths for item in valid])
    bjj_baseline = _median([item.bjj_load_tenths for item in valid])
    if baseline == 0:
        return TrainingLoadTrend(
            latest,
            references,
            len(valid),
            baseline,
            gym_baseline,
            bjj_baseline,
            None,
            "zero_baseline",
        )
    change = Fraction(latest.total_load_tenths, 1) - baseline
    return TrainingLoadTrend(
        latest,
        references,
        len(valid),
        baseline,
        gym_baseline,
        bjj_baseline,
        change * 100 / baseline,
        "available",
    )
