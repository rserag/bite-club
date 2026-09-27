"""Read current dose snapshots; retain their recorded dates and immutable labels."""

import time as clock
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema_supplement_plans import plan_dose_marks
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_intake_components as components,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_intake_revisions as revisions,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_intakes as intakes,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_phase_doses as doses,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_product_components as labels,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_product_versions as products,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_regimen_phases as phases,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_regimen_revisions as plans,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_substances as substances,
)
from nutrition_bot.domain.supplement_reports import Exposure


async def exposures(connection: AsyncConnection, start: date, end: date) -> list[Exposure]:
    rows = (
        (
            await connection.execute(
                sa.select(
                    revisions.c.local_date,
                    intakes.c.id.label("intake_id"),
                    products.c.product_id,
                    components.c.amount_scaled,
                    components.c.substance_code,
                    substances.c.name,
                    substances.c.nutrient_code,
                    substances.c.canonical_unit,
                    labels.c.chemical_form,
                    labels.c.comparison,
                    labels.c.conversion_version,
                    products.c.sealed.label("label_sealed"),
                    revisions.c.product_version_id,
                )
                .select_from(
                    intakes.join(revisions, intakes.c.current_revision_id == revisions.c.id)
                    .join(components, components.c.intake_revision_id == revisions.c.id)
                    .join(substances, substances.c.code == components.c.substance_code)
                    .outerjoin(products, products.c.id == revisions.c.product_version_id)
                    .outerjoin(
                        labels,
                        sa.and_(
                            labels.c.product_version_id == revisions.c.product_version_id,
                            labels.c.component_index == components.c.component_index,
                            labels.c.substance_code == components.c.substance_code,
                        ),
                    )
                )
                .where(
                    revisions.c.local_date.between(start, end),
                    revisions.c.sealed.is_(True),
                    revisions.c.deleted.is_(False),
                    revisions.c.status == "taken",
                )
                .order_by(revisions.c.local_date, intakes.c.id, components.c.component_index)
            )
        )
        .mappings()
        .all()
    )
    return [
        Exposure(
            row["local_date"],
            row["intake_id"],
            row["product_id"],
            row["substance_code"],
            row["name"],
            row["nutrient_code"],
            row["canonical_unit"],
            row["amount_scaled"]
            if row["product_version_id"] is None
            or (row["label_sealed"] and row["conversion_version"])
            else None,
            row["chemical_form"],
            (row["comparison"] or "unknown") if row["product_version_id"] is not None else "exact",
        )
        for row in rows
    ]


async def adherence(
    connection: AsyncConnection, start: date, end: date, reference: datetime
) -> list[str]:
    # Telegram timestamps have second precision. Use observation time for current
    # corrections, so a plan approved earlier in the same second is not omitted.
    observed_at = max(reference.timestamp(), clock.time())
    # All revisions are needed to reconstruct pauses/stops without rewriting history.
    history = (
        (
            await connection.execute(
                sa.select(plans)
                .where(plans.c.sealed.is_(True))
                .order_by(plans.c.regimen_id, plans.c.created_at, plans.c.id)
            )
        )
        .mappings()
        .all()
    )
    groups: dict[int, list[sa.RowMapping]] = defaultdict(list)
    for row in history:
        groups[row["regimen_id"]].append(row)
    phase_rows = (await connection.execute(sa.select(phases))).mappings().all()
    dose_rows = (await connection.execute(sa.select(doses))).mappings().all()
    marks = await connection.execute(
        sa.select(plan_dose_marks)
        .where(plan_dose_marks.c.day.between(start, end))
        .order_by(plan_dose_marks.c.id)
    )
    latest = {(r["regimen_id"], r["day"], r["slot"]): r for r in marks.mappings()}
    live = (
        (
            await connection.execute(
                sa.select(revisions).join(intakes, intakes.c.current_revision_id == revisions.c.id)
            )
        )
        .mappings()
        .all()
    )
    current = {r["intake_id"]: r for r in live}
    lines: list[str] = []
    for regimen, history_rows in groups.items():
        scheduled = taken = skipped = unconfirmed = matched = 0
        zone = ZoneInfo(history_rows[0]["timezone"])
        for day_offset in range((end - start).days + 1):
            day = start + timedelta(days=day_offset)
            beginning = datetime.combine(day, time.min, zone).timestamp()
            ending = min(
                datetime.combine(day + timedelta(days=1), time.min, zone).timestamp(),
                observed_at,
            )
            slots: dict[int, int] = {}
            if ending < beginning:
                continue
            for index, revision in enumerate(history_rows):
                next_at = (
                    history_rows[index + 1]["created_at"]
                    if index + 1 < len(history_rows)
                    else float("inf")
                )
                if (
                    revision["state"] != "active"
                    or revision["deleted"]
                    or revision["created_at"] > ending
                    or next_at <= beginning
                ):
                    continue
                offset = (day - revision["start_date"]).days
                if revision["end_date"] and day > revision["end_date"]:
                    continue
                for phase in phase_rows:
                    if (
                        phase["regimen_revision_id"] == revision["id"]
                        and offset >= phase["start_day_offset"]
                        and (
                            phase["duration_days"] is None
                            or offset < phase["start_day_offset"] + phase["duration_days"]
                        )
                    ):
                        slots.update(
                            {
                                d["slot_index"]: d["amount_scaled"]
                                for d in dose_rows
                                if d["regimen_revision_id"] == revision["id"]
                                and d["phase_index"] == phase["phase_index"]
                            }
                        )
            scheduled += len(slots)
            for slot, expected in slots.items():
                mark = latest.get((regimen, day, slot))
                actual = current.get(mark["intake_id"]) if mark else None
                if (
                    mark
                    and mark["status"] == "taken"
                    and actual
                    and not actual["deleted"]
                    and actual["sealed"]
                    and actual["status"] == "taken"
                    and actual["local_date"] == day
                ):
                    taken += 1
                    if actual["amount_scaled"] == expected:
                        matched += 1
                elif mark and mark["status"] == "skipped":
                    skipped += 1
                else:
                    unconfirmed += 1
        if scheduled:
            lines.append(
                f"R{regimen} · {zone.key}: {scheduled} scheduled; {taken} taken "
                f"({matched} at planned amount); {skipped} skipped; {unconfirmed} unconfirmed."
            )
    return lines
