"""Explicit reference choices and two-week, coverage-aware nutrient review."""

import shlex
from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.checkins import food_day_status
from nutrition_bot.adapters.database.nutrient_gaps import food_values
from nutrition_bot.adapters.database.nutrient_references import install_bundled
from nutrition_bot.adapters.database.schema_supplements import nutrient_reference_sets as sets
from nutrition_bot.adapters.database.schema_supplements import nutrient_reference_values as values
from nutrition_bot.adapters.database.supplement_reports import exposures
from nutrition_bot.application.meal_conversation import MealReply
from nutrition_bot.application.supplement_report import label, number, pages
from nutrition_bot.domain.nutrient_gaps import Week, screen, summarize
from nutrition_bot.domain.nutrient_references import GROUPS, SCOPE, SOURCE, VERSION

HELP = (
    "Use /nutrients references to install/list the bundled adult DRI choices.\n"
    "Then /nutrients values <set> <group> to inspect values and sources, or "
    "/nutrients review <set> <group> [YYYY-MM-DD] to review two completed weeks. "
    "An optional date is the Sunday ending the latest week and must be before today. "
    "Add page 2 for more. No group is selected automatically."
)


def average(value: int | None, days: int) -> str:
    return "unknown" if value is None or days == 0 else number(value // days)


def week_line(week: Week, index: int, unit: str) -> str:
    combined = None if week.observed is None else week.observed + (week.supplement or 0)
    return (
        f"W{index}: F {average(week.observed, week.complete_days)}; "
        f"S {average(week.supplement, week.complete_days)}; "
        f"C {average(combined, week.complete_days)} {unit}/complete day (recorded subtotals). "
        f"Non-imputed food coverage {week.known_entries}/{week.entries} entries; "
        f"imputed/other subtotal {average(week.imputed, week.complete_days)} {unit}/day excluded; "
        f"{week.estimated_entries} approved portion estimate(s)."
        + (" Supplement amounts/conversions uncertain." if week.uncertain_supplements else "")
    )


async def render_review(
    connection: AsyncConnection,
    rid: int,
    group: str,
    end: date,
    page: int,
    refs: list[sa.RowMapping],
) -> str:
    start = end - timedelta(days=13)
    dates = [start + timedelta(days=i) for i in range(14)]
    statuses = [await food_day_status(connection, day) for day in dates]
    complete = {d for d, s in zip(dates, statuses, strict=True) if s.state == "complete"}
    doses = await exposures(connection, start, end)
    lines = [
        SCOPE,
        "Food status: "
        + "; ".join(f"{d}: {s.state}" for d, s in zip(dates[:7], statuses[:7], strict=True)),
        "Food status: "
        + "; ".join(f"{d}: {s.state}" for d, s in zip(dates[7:], statuses[7:], strict=True)),
        f"Complete days W1={len(complete.intersection(dates[:7]))}/7, "
        f"W2={len(complete.intersection(dates[7:]))}/7. "
        f"Unresolved drafts: {sum(s.unresolved_drafts for s in statuses)}.",
        "F=non-imputed food, S=explicitly logged supplements, C=F+S. "
        "Only complete food days enter averages; blank days are never zero. "
        "Food completeness does not establish supplement completeness.",
        "Rule: 5+ complete days in EACH week, 90%+ non-imputed entry coverage, "
        "no potentially material unknown foods/doses, and BOTH averages below 80% RDA. "
        "This is a product reminder, not a clinical threshold.",
    ]
    messages = {
        "not_gap_reference": "AI/limit/UL: not used to infer an intake gap or an amount to fill.",
        "unsupported_reference": "Reference source/form not supported for gap screening.",
        "insufficient_days": "Insufficient complete days; no gap assessment.",
        "coverage_uncertain": "Coverage uncertain; no gap assessment.",
        "possible_intake_gap": (
            "Possible intake gap in recorded intake across both weeks; "
            "review food choices and logging."
        ),
        "no_repeated_trigger": "No repeated below-80% trigger; this does not establish adequacy.",
    }
    for ref in refs:
        if ref["kind"] == "UL":
            continue  # Daily ULs are in the existing supplement report, never weekly averages.
        code, unit = ref["nutrient_code"], ref["unit"]
        foods = await food_values(connection, start, end, code, unit)
        relevant = [d for d in doses if d.nutrient == code]
        weeks = (
            summarize(complete.intersection(dates[:7]), foods, relevant, unit),
            summarize(complete.intersection(dates[7:]), foods, relevant, unit),
        )
        result = screen(
            weeks,
            code=code,
            kind=ref["kind"],
            target=ref["amount_scaled"],
            scope=ref["applicability"],
            form=ref["chemical_form"],
        )
        lines.append(
            f"{code} · {ref['kind']} {number(ref['amount_scaled'])} {unit}/day "
            f"· {ref['applicability']}: {messages[result]}"
        )
        lines.extend(week_line(w, i, unit) for i, w in enumerate(weeks, 1))
        for name in sorted(set(weeks[0].unknown_foods + weeks[1].unknown_foods)):
            lines.append(f"{code}: unknown/imputed food value — {label(name, 100)}")
    lines.extend(
        [
            "Approved portion estimates retain their uncertainty. Unlogged supplements, "
            "absorption and blood status are not inferred. No supplement or dose is recommended.",
            f"Daily upper-limit comparisons: /supplements week {end} "
            f"reference {rid} {shlex.quote(group)}",
            f"Reference source: {label(refs[0]['source_url'], 250)}",
        ]
    )
    header = (
        f"Nutrient review · {start} → {end} · set {rid} · {label(group, 120)} "
        f"· {label(refs[0]['version'])}\n"
        "Recorded intake only; not a diagnosis or safety assessment."
    )
    return pages(lines, header, f"/nutrients review {rid} {shlex.quote(group)} {end}", page)


async def handle_nutrient_message(
    connection: AsyncConnection, text: str, *, today: date
) -> MealReply | None:
    if not text.strip() or text.split(maxsplit=1)[0].casefold() != "/nutrients":
        return None
    try:
        tokens = shlex.split(text)[1:]
        page = 1
        if len(tokens) >= 2 and tokens[-2].casefold() == "page":
            page = int(tokens[-1])
            tokens = tokens[:-2]
        if page < 1:
            raise ValueError(HELP)
        if tokens == ["references"]:
            rid = await install_bundled(connection)
            lines = [
                SCOPE,
                f"Bundled set {rid}: US/Canadian adult DRIs · {VERSION}",
                "Selection applies only to the requested report; no profile is changed.",
            ]
            lines.extend(f"/nutrients values {rid} {group}" for group in GROUPS)
            lines.extend([f"Review example: /nutrients review {rid} male-31-50", SOURCE])
            return MealReply(
                pages(lines, "Nutrient reference choices", "/nutrients references", page),
                "nutrient_references",
            )
        if len(tokens) not in {3, 4} or tokens[0] not in {"review", "values"}:
            raise ValueError(HELP)
        mode, rid_text, group = tokens[:3]
        rid = int(rid_text)
        if not 1 <= rid <= 2**63 - 1 or (mode == "values" and len(tokens) != 3):
            raise ValueError(HELP)
        # Last completed Sunday; a Sunday request still excludes that ongoing day.
        end = today - timedelta(days=today.weekday() + 1)
        if len(tokens) == 4:
            end = date.fromisoformat(tokens[3])
        if end >= today or end.weekday() != 6 or end.toordinal() <= 13:
            raise ValueError(HELP)
        refs = list(
            (
                await connection.execute(
                    sa.select(values, sets.c.version, sets.c.source_url)
                    .join(
                        sets,
                        sets.c.id == values.c.reference_set_id,
                    )
                    .where(
                        sets.c.sealed.is_(True), sets.c.id == rid, values.c.reference_group == group
                    )
                    .order_by(values.c.nutrient_code, values.c.kind)
                )
            )
            .mappings()
            .all()
        )
        if not refs:
            raise ValueError("That reviewed group is unavailable. Use /nutrients references.")
        if refs[0]["version"] == VERSION:
            await install_bundled(connection)  # Validate persisted content, never rewrite it.
        if mode == "values":
            lines = [
                SCOPE,
                "RDA=recommended allowance; AI=adequate intake; UL=upper limit. "
                "Sodium limit is CDRR, not UL. Missing UL is not proof of safety.",
            ]
            lines.extend(
                f"{r['nutrient_code']} · {r['kind']} · {number(r['amount_scaled'])} "
                f"{r['unit']}/day · {r['applicability']} {r['chemical_form']}\n{r['note'] or ''}"
                for r in refs
            )
            result = pages(
                lines,
                f"Set {rid} · {label(group, 120)} · {label(refs[0]['version'])}",
                f"/nutrients values {rid} {shlex.quote(group)}",
                page,
            )
        else:
            result = await render_review(connection, rid, group, end, page, refs)
        return MealReply(result, "nutrient_review")
    except (ValueError, IndexError) as exc:
        detail = str(exc)
        if not detail.startswith(("That reviewed group", "Bundled nutrient reference integrity")):
            detail = HELP
        return MealReply(detail, "nutrient_help")
