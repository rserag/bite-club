import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.checkins import FoodDayStatus, food_day_status
from nutrition_bot.adapters.database.daily import daily_totals
from nutrition_bot.adapters.database.goals import TargetPlanSnapshot, current_plan
from nutrition_bot.adapters.database.weights import daily_weights
from nutrition_bot.domain.daily import DailyNutrient, DailyTotals
from nutrition_bot.domain.food import NUTRIENT_SCALE
from nutrition_bot.domain.weight_trends import DailyWeight, summarize_weight

MICROS = (
    "fiber",
    "sodium",
    "potassium",
    "calcium",
    "magnesium",
    "iron",
    "zinc",
    "vitamin_d",
    "vitamin_b12",
)
MACROS = (
    ("energy", "Energy", "kcal"),
    ("protein", "P", "g"),
    ("fat", "F", "g"),
    ("carbohydrate", "C", "g"),
)


class WeeklyRequestError(ValueError):
    pass


@dataclass(frozen=True)
class WeeklyRequest:
    end: date
    short: bool


@dataclass(frozen=True)
class WeeklyDay:
    day: date
    totals: DailyTotals
    status: FoodDayStatus
    target: TargetPlanSnapshot | None


def parse_week_request(text: str, *, today: date) -> WeeklyRequest | None:
    normalized = " ".join(text.casefold().replace("’", "'").split())
    if normalized.rstrip("?!. ") in {"show this week's calories", "weekly report"}:
        return WeeklyRequest(today, False)
    if normalized == "short weekly report":
        return WeeklyRequest(today, True)
    tokens = normalized.split()
    if not tokens or tokens[0] != "/week":
        return None
    short = False
    end = today
    saw_view = False
    saw_date = False
    for token in tokens[1:]:
        if token in {"short", "full"}:
            if saw_view:
                raise WeeklyRequestError("Use /week, /week short, or /week YYYY-MM-DD.")
            saw_view = True
            short = token == "short"
        elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", token) and not saw_date:
            try:
                end = date.fromisoformat(token)
            except ValueError:
                raise WeeklyRequestError(
                    "Use a valid week-ending date in YYYY-MM-DD form."
                ) from None
            saw_date = True
        else:
            raise WeeklyRequestError("Use /week, /week short, or /week YYYY-MM-DD.")
    if end > today:
        raise WeeklyRequestError("The week-ending date cannot be in the future.")
    return WeeklyRequest(end, short)


def _nutrient(day: WeeklyDay, code: str) -> DailyNutrient | None:
    return next((value for value in day.totals.nutrients if value.code == code), None)


def _complete_amount(day: WeeklyDay, code: str) -> Decimal | None:
    if day.status.state != "complete":
        return None
    if not day.totals.item_count:
        return Decimal(0)
    nutrient = _nutrient(day, code)
    if (
        nutrient is None
        or nutrient.known_amount_scaled is None
        or nutrient.known_items != nutrient.total_items
    ):
        return None
    return Decimal(nutrient.known_amount_scaled) / NUTRIENT_SCALE


def _target_value(target: TargetPlanSnapshot, code: str) -> int:
    return {
        "energy": target.energy_kcal,
        "protein": target.protein_grams,
        "fat": target.fat_grams,
        "carbohydrate": target.carbohydrate_grams,
    }[code]


def _number(value: Decimal, code: str) -> str:
    step = Decimal("1" if code == "energy" else "0.1")
    return format(value.quantize(step, rounding=ROUND_HALF_UP), "f")


def _status_label(status: FoodDayStatus) -> str:
    return {"complete": "complete", "incomplete": "incomplete", "unknown": "unknown"}[status.state]


def _date_rows(days: tuple[WeeklyDay, ...]) -> list[str]:
    rows = ["Dates:"]
    for value in days:
        amount = _complete_amount(value, "energy")
        energy = f"{_number(amount, 'energy')} kcal" if amount is not None else "energy excluded"
        target = f" / target {value.target.energy_kcal}" if value.target else " / no target"
        rows.append(f"{value.day} · {_status_label(value.status)} · {energy}{target}")
    return rows


def _macro_lines(days: tuple[WeeklyDay, ...]) -> list[str]:
    complete_count = sum(day.status.state == "complete" for day in days)
    lines = [f"Complete-day averages use {complete_count}/7 dates:"]
    for code, label, unit in MACROS:
        amounts = [amount for day in days if (amount := _complete_amount(day, code)) is not None]
        eligible = [
            (amount, Decimal(_target_value(day.target, code)))
            for day in days
            if day.target is not None and (amount := _complete_amount(day, code)) is not None
        ]
        if not amounts:
            lines.append(f"{label}: unavailable · 0/{complete_count} complete dates have full data")
            continue
        average = sum(amounts, Decimal()) / len(amounts)
        detail = f"{_number(average, code)} {unit} · data {len(amounts)}/{complete_count}"
        if eligible:
            intake = sum((item[0] for item in eligible), Decimal()) / len(eligible)
            target = sum((item[1] for item in eligible), Decimal()) / len(eligible)
            delta = intake - target
            sign = "+" if delta > 0 else ""
            detail += (
                f" · avg difference vs target {sign}{_number(delta, code)} · {len(eligible)} dates"
            )
        lines.append(f"{label}: {detail}")
    return lines


def _micronutrient_lines(days: tuple[WeeklyDay, ...]) -> list[str]:
    complete = [day for day in days if day.status.state == "complete"]
    lines = ["Micronutrient data (known contribution across complete dates):"]
    for code in MICROS:
        definition = next(
            (
                nutrient
                for day in days
                for nutrient in day.totals.nutrients
                if nutrient.code == code
            ),
            None,
        )
        if definition is None:
            continue
        known = 0
        items = 0
        total = 0
        for day in complete:
            nutrient = _nutrient(day, code)
            if nutrient:
                known += nutrient.known_amount_scaled or 0
                items += nutrient.known_items
                total += nutrient.total_items
        coverage = Decimal(items * 100) / total if total else None
        average = Decimal(known) / NUTRIENT_SCALE / len(complete) if complete else None
        amount = (
            _number(average, code) + f" {definition.unit}" if average is not None else "unavailable"
        )
        coverage_text = (
            f"{coverage.quantize(Decimal('1'))}% entries" if coverage is not None else "no entries"
        )
        lines.append(f"{definition.name}: {amount} known avg · {coverage_text}")
    if len(complete) < 5:
        lines.append("Possible-gap screening withheld: fewer than 5 complete dates.")
    else:
        lines.append(
            "Possible-gap screening withheld: no reviewed micronutrient reference set is "
            "configured; "
            "two eligible weeks are also required."
        )
    return lines


def _weight_lines(weight_points: list[DailyWeight], *, end: date) -> list[str]:
    trend = summarize_weight(weight_points, as_of=end)
    if trend.latest is None:
        return ["Weight trend: unavailable · no measurements in the 28-day window."]
    if trend.weekly_rate_grams is None:
        return [
            f"Weight trend: unavailable · {trend.trend_dates} measured dates over "
            f"{trend.trend_span_days} days."
        ]
    rate = Decimal(trend.weekly_rate_grams.numerator) / trend.weekly_rate_grams.denominator / 1000
    sign = "+" if rate > 0 else ""
    return [
        f"Weight trend: {sign}{rate.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} kg/week · "
        f"{trend.trend_dates} measured dates over {trend.trend_span_days} days."
    ]


def render_weekly(
    days: tuple[WeeklyDay, ...],
    weight_points: list[DailyWeight],
    *,
    short: bool,
) -> str:
    start, end = days[0].day, days[-1].day
    counts = {
        state: sum(day.status.state == state for day in days)
        for state in ("complete", "incomplete", "unknown")
    }
    lines = [
        f"Weekly report · {start} to {end}",
        f"Coverage: complete {counts['complete']} · incomplete {counts['incomplete']} · "
        f"unknown {counts['unknown']}.",
    ]
    lines.extend(_date_rows(days))
    lines.extend(_macro_lines(days))
    lines.extend(_weight_lines(weight_points, end=end))
    if not short:
        lines.extend(_micronutrient_lines(days))
    lines.append("Incomplete and unknown dates are excluded rather than counted as zero.")
    lines.append(f"Full report: /week {end} full" if short else f"Short report: /week {end} short")
    return "\n".join(lines)


async def build_weekly_report(connection: AsyncConnection, request: WeeklyRequest) -> str:
    start = request.end - timedelta(days=6)
    days = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        days.append(
            WeeklyDay(
                day,
                await daily_totals(connection, day),
                await food_day_status(connection, day),
                await current_plan(connection, on_date=day),
            )
        )
    weights = await daily_weights(
        connection, start=request.end - timedelta(days=27), end=request.end
    )
    return render_weekly(tuple(days), weights, short=request.short)
