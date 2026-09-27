from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction


@dataclass(frozen=True)
class DailyWeight:
    day: date
    grams: Fraction
    measurement_count: int
    used_morning: bool


@dataclass(frozen=True)
class WeightTrend:
    latest: DailyWeight | None
    rolling_7d_grams: Fraction | None
    rolling_dates: int
    weekly_rate_grams: Fraction | None
    trend_dates: int
    trend_span_days: int


def median(values: list[Fraction]) -> Fraction:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def summarize_weight(points: list[DailyWeight], *, as_of: date) -> WeightTrend:
    eligible = sorted(
        (point for point in points if point.day <= as_of), key=lambda point: point.day
    )
    latest = eligible[-1] if eligible else None
    recent_7 = [point for point in eligible if point.day >= as_of - timedelta(days=6)]
    rolling = (
        sum((point.grams for point in recent_7), Fraction()) / len(recent_7)
        if len(recent_7) >= 4
        else None
    )
    recent_28 = [point for point in eligible if point.day >= as_of - timedelta(days=27)]
    span = (recent_28[-1].day - recent_28[0].day).days if len(recent_28) >= 2 else 0
    slopes: list[Fraction] = []
    if len(recent_28) >= 7 and span >= 14:
        for index, left in enumerate(recent_28):
            for right in recent_28[index + 1 :]:
                days = (right.day - left.day).days
                if days >= 7:
                    slopes.append((right.grams - left.grams) * 7 / days)
    rate = median(slopes) if slopes else None
    return WeightTrend(latest, rolling, len(recent_7), rate, len(recent_28), span)
