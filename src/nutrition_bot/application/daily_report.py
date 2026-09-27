"""Bounded deterministic views of recorded intake and historical daily targets."""

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext

from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.checkins import FoodDayStatus, food_day_status
from nutrition_bot.adapters.database.daily import daily_totals
from nutrition_bot.adapters.database.goals import TargetPlanSnapshot, current_plan
from nutrition_bot.domain.daily import DailyNutrient, DailyTotals
from nutrition_bot.domain.food import NUTRIENT_SCALE

HELP = (
    "Use /today for the full report or /today short for a quick view.\n"
    "For an earlier date: /today yesterday or /today 2026-01-15 short. "
    "Choose at most one date and one view (full or short). Nothing was changed."
)
MACROS = {"energy", "protein", "carbohydrate", "fat", "fiber"}
SOURCE_NAMES = {"manual_reviewed": "reviewed manual", "usda": "USDA"}
QUALITY_NAMES = {"manual_reviewed": "manually reviewed", "source_reported": "source-reported"}


class DailyRequestError(ValueError):
    """An unsupported report request, safe to display without changing the diary."""


@dataclass(frozen=True, slots=True)
class DailyRequest:
    day: date
    short: bool = False


def parse_request(text: str, *, today: date) -> DailyRequest | None:
    normalized = " ".join(text.casefold().replace("’", "'").split())
    if normalized.rstrip("?!. ") in {
        "how am i doing today",
        "show today's totals",
        "today",
    }:
        return DailyRequest(today)
    tokens = normalized.split()
    if not tokens or tokens[0] != "/today":
        return None
    selected_day = None
    view = None
    for token in tokens[1:]:
        if token in {"full", "short"}:
            if view is not None:
                raise DailyRequestError(HELP)
            view = token
            continue
        if selected_day is not None:
            raise DailyRequestError(HELP)
        try:
            if token == "today":
                selected_day = today
            elif token == "yesterday":
                selected_day = today - timedelta(days=1)
            elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", token):
                selected_day = date.fromisoformat(token)
            else:
                raise ValueError
        except (ValueError, OverflowError):
            raise DailyRequestError(HELP) from None
        if selected_day > today:
            raise DailyRequestError("Choose today or an earlier date.\n" + HELP)
    return DailyRequest(selected_day or today, short=view == "short")


def _label(value: str, limit: int = 32) -> str:
    # Database labels are content, not formatting; keep arbitrary Unicode bounded.
    clean = " ".join(value.split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _amount(nutrient: DailyNutrient) -> str:
    assert nutrient.known_amount_scaled is not None
    with localcontext() as context:
        context.prec = max(50, len(str(nutrient.known_amount_scaled)) + 8)
        amount = Decimal(nutrient.known_amount_scaled) / NUTRIENT_SCALE
        step = Decimal("1" if nutrient.code == "energy" else "0.1")
        rounded = amount.quantize(step, rounding=ROUND_HALF_UP)
        if amount > 0 and rounded == 0:
            return f"<{step}"
        # Unusually large source inputs must not break Telegram's message limit.
        if rounded >= Decimal("1000000000000"):
            return format(rounded, ".3E")
        return format(rounded, "f")


def _nutrient_line(nutrient: DailyNutrient) -> str:
    prefix = f"{_label(nutrient.name)}: "
    coverage = f"data {nutrient.known_items}/{nutrient.total_items} foods"
    if nutrient.known_amount_scaled is None:
        return f"{prefix}unknown · {coverage}"
    suffix = " known" if nutrient.missing_items else ""
    return f"{prefix}{_amount(nutrient)} {nutrient.unit}{suffix} · {coverage}"


def _target_lines(
    totals: DailyTotals,
    target: TargetPlanSnapshot | None,
    status: FoodDayStatus,
) -> list[str]:
    if status.state == "complete":
        state = "complete · explicitly marked all food logged"
    elif status.state == "incomplete":
        state = "incomplete · excluded from complete-day averages"
    elif status.unresolved_drafts:
        state = f"unknown · {status.unresolved_drafts} unresolved meal draft(s)"
    elif status.changed_since_complete:
        state = "unknown · food log changed after the complete marker"
    else:
        state = "unknown · choose All food logged or Incomplete"
    lines = [f"Food log status: {state}."]
    if target is None:
        lines.append("No calorie or macro target was effective on this date.")
        return lines
    lines.append(
        f"Target T{target.id}: {target.energy_kcal} kcal · P {target.protein_grams} g · "
        f"F {target.fat_grams} g · C {target.carbohydrate_grams} g."
    )
    if target.allocation_id is not None:
        lines.append(
            f"Approved training allocation A{target.allocation_id}; weekly calories unchanged."
        )
    if status.unresolved_drafts:
        lines.append(
            "Remaining targets withheld until unresolved meal drafts are resolved or cancelled."
        )
        return lines
    if not totals.item_count and status.state != "complete":
        lines.append("Remaining targets withheld because recorded intake is unknown, not zero.")
        return lines
    if not totals.item_count:
        lines.append(
            f"From recorded foods: Energy {target.energy_kcal} kcal remaining · "
            f"P {target.protein_grams}.0 g remaining · F {target.fat_grams}.0 g remaining · "
            f"C {target.carbohydrate_grams}.0 g remaining."
        )
        lines.append("Training does not add calories back to this target.")
        return lines
    nutrients = {nutrient.code: nutrient for nutrient in totals.nutrients}
    values: list[str] = []
    for code, label, target_value, unit in (
        ("energy", "Energy", target.energy_kcal, "kcal"),
        ("protein", "P", target.protein_grams, "g"),
        ("fat", "F", target.fat_grams, "g"),
        ("carbohydrate", "C", target.carbohydrate_grams, "g"),
    ):
        nutrient = nutrients.get(code)
        if (
            nutrient is None
            or nutrient.known_amount_scaled is None
            or nutrient.known_items != nutrient.total_items
        ):
            values.append(f"{label} unknown")
            continue
        consumed = Decimal(nutrient.known_amount_scaled) / NUTRIENT_SCALE
        difference = Decimal(target_value) - consumed
        step = Decimal("1" if code == "energy" else "0.1")
        amount = abs(difference).quantize(step, rounding=ROUND_HALF_UP)
        values.append(
            f"{label} {format(amount, 'f')} {unit} remaining"
            if difference >= 0
            else f"{label} {format(amount, 'f')} {unit} over"
        )
    lines.append("From recorded foods: " + " · ".join(values) + ".")
    if status.state != "complete":
        lines.append("These are provisional because the day is not marked complete.")
    lines.append("Training does not add calories back to this target.")
    return lines


def _counts(counts: tuple[tuple[str, int], ...], names: dict[str, str]) -> str:
    shown = [f"{_label(names.get(key, key), 24)} {count}" for key, count in counts[:3]]
    if len(counts) > 3:
        shown.append(f"+{sum(count for _, count in counts[3:])} in other categories")
    return "; ".join(shown)


def _render_daily(
    totals: DailyTotals,
    *,
    timezone: str,
    short: bool,
    nutrient_limit: int,
    meal_limit: int,
    target: TargetPlanSnapshot | None,
    status: FoodDayStatus,
) -> str:
    lines = [f"Daily food log · {totals.local_date.isoformat()}"]
    lines.append(f"Date requested in {_label(timezone, 40)}; meals keep their recorded dates.")
    if not totals.item_count:
        lines.append(
            "No meals logged; explicitly marked complete, so recorded intake is zero."
            if status.state == "complete"
            else "No meals logged for this date. Intake is unknown, not zero."
        )
        lines.append("Find a saved food with /foods, then log a measured amount.")
        lines.extend(_target_lines(totals, target, status))
        return "\n".join(lines)
    lines.append(f"{len(totals.meals)} meals · {totals.item_count} food entries")
    selected = tuple(n for n in totals.nutrients if not short or n.code in MACROS)
    displayed = selected[:nutrient_limit]
    lines.extend(_nutrient_line(nutrient) for nutrient in displayed)
    if len(selected) > len(displayed):
        lines.append(
            f"{len(selected) - len(displayed)} additional registered nutrients are not displayed."
        )
    lines.append("'Known' is a partial sum when other foods lack data.")
    lines.append("Coverage counts logged food entries; it does not measure day completeness.")
    lines.append("Food sources (entries): " + _counts(totals.source_counts, SOURCE_NAMES))
    lines.append(
        "Portions (entries): "
        + _counts(
            totals.quantity_counts,
            {
                "approved_estimate": "approved estimate",
                "calculated_recipe": "calculated recipe ingredient",
            },
        )
    )
    if any(method == "approved_estimate" and count for method, count in totals.quantity_counts):
        lines.append(
            "Includes approved portion estimates; their nutrient contributions are approximate."
        )
    if not short:
        quality_counts: Counter[str] = Counter()
        for nutrient in totals.nutrients:
            quality_counts.update(dict(nutrient.quality_counts))
        if quality_counts:
            lines.append(
                "Known nutrient values: "
                + _counts(tuple(sorted(quality_counts.items())), QUALITY_NAMES)
                + ". These describe data origin, not lab measurements of your meal."
            )
        lines.append("Meals (open a receipt with /meal M<number>):")
        lines.extend(
            f"M{meal.id}r{meal.revision_number} · {_label(meal.label, 32)}"
            for meal in totals.meals[:meal_limit]
        )
        if len(totals.meals) > meal_limit:
            lines.append(f"+{len(totals.meals) - meal_limit} other meals included in every total.")
        if any(zone != timezone for zone in totals.recorded_timezones):
            lines.append("Some meals were recorded in another timezone; their dates are unchanged.")
    lines.extend(_target_lines(totals, target, status))
    command = f"/today {totals.local_date.isoformat()}"
    lines.append(f"Full report: {command}" if short else f"Quick view: {command} short")
    return "\n".join(lines)


def render_daily(
    totals: DailyTotals,
    *,
    timezone: str,
    short: bool = False,
    target: TargetPlanSnapshot | None = None,
    status: FoodDayStatus | None = None,
) -> str:
    status = status or FoodDayStatus("unknown", None, 0, False)
    result = _render_daily(
        totals,
        timezone=timezone,
        short=short,
        nutrient_limit=20,
        meal_limit=5,
        target=target,
        status=status,
    )
    if len(result.encode("utf-16-le")) // 2 > 3900:
        # Preserve all 14 core nutrients; disclose omitted extension/meal display rows.
        # Limits also account for astral Unicode and maximum stored integer lengths.
        result = _render_daily(
            totals,
            timezone=timezone,
            short=short,
            nutrient_limit=14,
            meal_limit=3,
            target=target,
            status=status,
        )
    return result


async def report_for_message(
    connection: AsyncConnection, text: str, *, today: date, timezone: str
) -> str | None:
    request = parse_request(text, today=today)
    if request is None:
        return None
    return await report_for_day(connection, request.day, timezone=timezone, short=request.short)


async def report_for_day(
    connection: AsyncConnection,
    day: date,
    *,
    timezone: str,
    short: bool = False,
) -> str:
    totals = await daily_totals(connection, day)
    target = await current_plan(connection, on_date=day)
    status = await food_day_status(connection, day)
    return render_daily(totals, timezone=timezone, short=short, target=target, status=status)
