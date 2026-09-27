"""Paged food/supplement comparisons and factual plan adherence; read-only."""

import re
import shlex
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.checkins import food_day_status
from nutrition_bot.adapters.database.daily import daily_totals
from nutrition_bot.adapters.database.schema_supplements import (
    nutrient_reference_sets as reference_sets,
)
from nutrition_bot.adapters.database.schema_supplements import (
    nutrient_reference_values as reference_values,
)
from nutrition_bot.adapters.database.supplement_reports import adherence, exposures
from nutrition_bot.application.meal_conversation import MealReply
from nutrition_bot.domain.daily import DailyTotals
from nutrition_bot.domain.supplement_reports import (
    Amount,
    Exposure,
    UpperLimit,
    check_limit,
    combine,
    converted,
    supplement_sum,
)

HELP = (
    "Use /supplements today or /supplements week for food, supplement and combined totals.\n"
    "Add YYYY-MM-DD to choose the day or week-ending date; add page 2 for more rows.\n"
    "Optional reviewed limits: add reference <set> <group>. "
    "Use /supplements references to list available reviewed groups. "
    "No reference is selected automatically."
)


@dataclass(frozen=True)
class Request:
    end: date
    days: int
    page: int = 1
    reference_id: int | None = None
    group: str | None = None


def parse(text: str, today: date) -> Request | None:
    if not re.match(r"^/supplements?\s+(today|week|references)(?:\s|$)", text.strip(), re.I):
        return None
    try:
        tokens = shlex.split(text)
        mode = tokens[1].casefold()
        end, page, rid, group = today, 1, None, None
        seen: set[str] = set()
        index = 2
        while index < len(tokens):
            token = tokens[index].casefold()
            if token in {"page", "reference"} and token not in seen:
                seen.add(token)
                if token == "page":
                    page = int(tokens[index + 1])
                    index += 2
                else:
                    rid, group = int(tokens[index + 1]), tokens[index + 2]
                    index += 3
            elif "date" not in seen and mode != "references":
                end = date.fromisoformat(token)
                seen.add("date")
                index += 1
            else:
                raise ValueError
        if end > today or page < 1 or (rid is not None and rid < 1):
            raise ValueError
        days = 7 if mode == "week" else 1
        if mode == "references" and seen - {"page"}:
            raise ValueError
        if end.toordinal() < days:
            raise ValueError
        return Request(end, 0 if mode == "references" else days, page, rid, group)
    except (ValueError, IndexError):
        raise ValueError(HELP) from None


def label(value: str, limit: int = 48) -> str:
    return " ".join(value.split())[:limit]


def number(amount: int) -> str:
    with localcontext() as context:
        context.prec = max(50, len(str(amount)) + 10)
        value = Decimal(amount) / 1_000_000
        if abs(value) >= 1_000_000_000:
            return f"{value:.3E}"
        return (
            format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else str(value)
        )


def display(amount: Amount, unit: str) -> str:
    if amount.known is None:
        return "unknown"
    return f"{number(amount.known)} {unit}" + (" known" if amount.partial else "")


def pages(lines: list[str], header: str, command: str, selected: int) -> str:
    chunks: list[list[str]] = [[]]
    size = 0
    for line in lines:
        # All free-form data is bounded by its renderer; no hidden dropped report rows.
        count = len(line.encode("utf-16-le")) // 2 + 1
        if count > 2800:
            raise ValueError("A report row is too long to display.")
        if size + count > 2800:
            chunks.append([])
            size = 0
        chunks[-1].append(line)
        size += count
    if selected > len(chunks):
        return f"Choose page 1–{len(chunks)}.\n{command}"
    footer = f"Page {selected}/{len(chunks)}."
    if selected < len(chunks):
        footer += f" More: {command} page {selected + 1}"
    return header + "\n" + "\n".join(chunks[selected - 1]) + "\n" + footer


def food_amount(days: list[DailyTotals], complete: list[bool], code: str) -> Amount:
    values: list[int] = []
    known_entries = total_entries = missing_days = 0
    for totals, is_complete in zip(days, complete, strict=True):
        nutrient = next(n for n in totals.nutrients if n.code == code)
        total_entries += nutrient.total_items
        known_entries += nutrient.known_items
        if nutrient.known_amount_scaled is not None:
            values.append(nutrient.known_amount_scaled)
        elif not totals.item_count and is_complete:
            values.append(0)
        if not totals.item_count and not is_complete:
            missing_days += 1
    return Amount(sum(values) if values else None, known_entries, total_entries, missing_days)


async def render(connection: AsyncConnection, request: Request, reference: datetime) -> str:
    if request.days == 0:
        reference_rows = (
            (
                await connection.execute(
                    sa.select(
                        reference_sets.c.id,
                        reference_sets.c.name,
                        reference_sets.c.version,
                        reference_values.c.reference_group,
                    )
                    .join(
                        reference_values, reference_values.c.reference_set_id == reference_sets.c.id
                    )
                    .where(reference_sets.c.sealed.is_(True))
                    .distinct()
                    .order_by(reference_sets.c.id, reference_values.c.reference_group)
                )
            )
            .mappings()
            .all()
        )
        lines = [
            f"Set {r['id']} · {label(r['name'])} · {label(r['version'])} "
            f"· group {label(r['reference_group'], 120)}"
            for r in reference_rows
        ]
        return pages(
            lines or ["No reviewed nutrient reference groups are installed."],
            "Reviewed reference choices (select only an applicable group).",
            "/supplements references",
            request.page,
        )
    start = request.end - timedelta(days=request.days - 1)
    dates = [start + timedelta(days=i) for i in range(request.days)]
    food = [await daily_totals(connection, day) for day in dates]
    statuses = [await food_day_status(connection, day) for day in dates]
    complete = [s.state == "complete" for s in statuses]
    logged = await exposures(connection, start, request.end)
    lines = [
        f"Food log complete: {sum(complete)}/{len(dates)} days; unresolved drafts: "
        f"{sum(s.unresolved_drafts for s in statuses)}.",
        "Food data coverage counts entries, not day completeness. 'Known' means a partial sum.",
        "F = food; S = logged supplement nutrients; C = combined recorded amounts.",
    ]
    estimates = sum(dict(day.quantity_counts).get("approved_estimate", 0) for day in food)
    if estimates:
        lines.append(
            f"Includes {estimates} approved portion estimate(s); food and combined "
            "totals inherit their uncertainty."
        )
    by_nutrient: dict[str, list[Exposure]] = defaultdict(list)
    actives: dict[str, list[Exposure]] = defaultdict(list)
    for row in logged:
        if row.nutrient is None:
            actives[row.substance].append(row)
        else:
            by_nutrient[row.nutrient].append(row)
    for nutrient in food[0].nutrients:
        f = food_amount(food, complete, nutrient.code)
        rows = by_nutrient[nutrient.code]
        s = supplement_sum(rows, nutrient.unit)
        c = combine(f, s)
        supp = display(s, nutrient.unit) if rows else "none logged"
        lines.append(
            f"{label(nutrient.name, 32)} · F {display(f, nutrient.unit)} "
            f"({f.known_entries}/{f.entries} entries); S {supp} "
            f"({s.known_entries}/{s.entries}); C {display(c, nutrient.unit)}"
        )
    lines.append(
        "Supplements are label claims or exact reported doses. Plans never count as intake."
    )
    lines.append("Active ingredients (separate from nutrient gaps; no invented calories):")
    if not actives:
        lines.append("No active-ingredient doses logged in this period.")
    for rows in actives.values():
        unit = rows[0].unit
        total = supplement_sum(rows, unit)
        lines.append(
            f"{label(rows[0].name)}: {display(total, unit)} "
            f"· {len({r.intake_id for r in rows})} dose(s)."
        )
    duplicates: dict[tuple[date, str], set[int]] = defaultdict(set)
    names: dict[str, str] = {}
    for row in logged:
        names[row.substance] = row.name
        if row.product_id is not None:
            duplicates[row.day, row.substance].add(row.product_id)
    stack = [
        (day, substance, products)
        for (day, substance), products in duplicates.items()
        if len(products) > 1
    ]
    lines.append("Duplicate ingredients across products:")
    lines.extend(
        f"{day}: {label(names[substance])} occurs in {len(products)} logged products "
        f"({', '.join('P' + str(p) for p in sorted(products))[:150]})."
        for day, substance, products in stack
    )
    if not stack:
        lines.append("None in the recorded product components; this is not an interaction check.")
    lines.extend(await limit_lines(connection, request, dates, food, complete, logged))
    lines.append("Plan adherence (calendar dates in each plan's original timezone):")
    lines.extend(
        await adherence(connection, start, request.end, reference)
        or ["No scheduled slots in this period."]
    )
    lines.append(
        "Days active for any part of the day include their slots. Unconfirmed is not skipped. "
        "Direct logs are not auto-matched; no catch-up doses are suggested."
    )
    mode = "week" if request.days == 7 else "today"
    command = f"/supplements {mode} {request.end}"
    if request.reference_id is not None:
        command += f" reference {request.reference_id} {shlex.quote(request.group or '')}"
    header = f"Supplement nutrition · {start} → {request.end}\n"
    header += (
        "Recorded sums, not complete intake, a safety clearance or a diagnosis. "
        "ULs are limits, not targets."
    )
    return pages(lines, header, command, request.page)


async def limit_lines(
    connection: AsyncConnection,
    request: Request,
    dates: list[date],
    food: list[DailyTotals],
    complete: list[bool],
    logged: list[Exposure],
) -> list[str]:
    if request.reference_id is None:
        return [
            "Upper limits not assessed: no reviewed reference group selected. "
            "/supplements references"
        ]
    rows = (
        (
            await connection.execute(
                sa.select(reference_values, reference_sets.c.version, reference_sets.c.source_url)
                .join(reference_sets, reference_sets.c.id == reference_values.c.reference_set_id)
                .where(
                    reference_sets.c.sealed.is_(True),
                    reference_sets.c.id == request.reference_id,
                    reference_values.c.reference_group == request.group,
                )
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        raise ValueError(
            "That reviewed reference group is unavailable. Use /supplements references."
        )
    limits = [r for r in rows if r["kind"] == "UL"]
    lines = [
        f"Daily UL comparisons · set {request.reference_id} · {label(request.group or '', 120)} "
        f"· {label(rows[0]['version'])}. Weekly averaging never hides a daily exceedance.",
        f"Reference source: {label(rows[0]['source_url'], 250)}",
    ]
    if not limits:
        lines.append("No UL values in this group; RDA, AI and other limits were not used as ULs.")
    for row in limits:
        limit = UpperLimit(
            row["nutrient_code"],
            row["unit"],
            row["amount_scaled"],
            row["applicability"],
            row["chemical_form"],
        )
        for index, day in enumerate(dates):
            value = food_amount([food[index]], [complete[index]], limit.nutrient)
            unit = next(n.unit for n in food[index].nutrients if n.code == limit.nutrient)
            converted_food = converted(value.known, unit, limit.unit)
            value = Amount(
                converted_food,
                value.known_entries if converted_food is not None else 0,
                value.entries,
                value.missing_days,
            )
            result = check_limit(
                limit,
                value,
                [e for e in logged if e.day == day and e.nutrient == limit.nutrient],
                food_complete=complete[index],
            )
            status = {
                "exceeds": "recorded amount exceeds UL",
                "at_limit": "recorded amount equals UL",
                "below_recorded": "recorded subtotal below UL",
                "indeterminate": "comparison indeterminate",
            }[result.status]
            amount = "unknown" if result.known is None else number(result.known)
            lines.append(
                f"{day} {label(limit.nutrient)} · {limit.scope}"
                f"{(' / ' + label(limit.form)) if limit.form else ''}: {amount} / "
                f"{number(limit.amount)} {limit.unit}; {status}"
                + ("; amounts/forms/source coverage incomplete." if result.incomplete else ".")
            )
    lines.append(
        "Unlogged intake is not assessed. Food chemical forms/fortification are unknown; "
        "unknown label amounts and unreviewed conversions are never assumed zero."
    )
    return lines


async def handle_report(
    connection: AsyncConnection, text: str, reference: datetime
) -> MealReply | None:
    try:
        request = parse(text, reference.date())
        if request is None:
            return None
        return MealReply(await render(connection, request, reference), "supplement_report")
    except ValueError as exc:
        return MealReply(str(exc), "supplement_report_help")
