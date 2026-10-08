import time
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema_supplement_plans import (
    plan_dose_marks,
    plan_proposals,
    plan_resolutions,
)
from nutrition_bot.adapters.database.schema_supplements import (
    health_context_events,
    supplement_phase_doses,
    supplement_regimen_phases,
    supplement_regimen_revisions,
    supplement_regimens,
)
from nutrition_bot.adapters.database.supplements import (
    SupplementError,
    create_direct_supplement_intake,
    ensure_creatine_substance,
    get_supplement_intake,
    revise_supplement_intake,
)
from nutrition_bot.domain.supplement_plans import CreatinePlan


async def propose(connection: AsyncConnection, plan: CreatinePlan, key: str) -> int:
    value = (
        await connection.execute(
            sa.insert(plan_proposals)
            .values(
                action_key=key,
                plan=plan.model_dump(mode="json"),
                created_at=time.time(),
            )
            .returning(plan_proposals.c.id)
        )
    ).scalar_one()
    return int(value)


async def proposal(connection: AsyncConnection, proposal_id: int) -> CreatinePlan:
    value = await connection.scalar(
        sa.select(plan_proposals.c.plan).where(plan_proposals.c.id == proposal_id)
    )
    if value is None:
        raise SupplementError("Plan preview is unavailable.")
    return CreatinePlan.model_validate(value)


async def current(connection: AsyncConnection, regimen_id: int) -> sa.RowMapping:
    row = (
        (
            await connection.execute(
                sa.select(supplement_regimen_revisions)
                .join(
                    supplement_regimens,
                    supplement_regimens.c.current_revision_id == supplement_regimen_revisions.c.id,
                )
                .where(supplement_regimens.c.id == regimen_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise SupplementError("Plan is unavailable.")
    return row


async def plan_for(connection: AsyncConnection, regimen_id: int) -> CreatinePlan:
    pid = await connection.scalar(
        sa.select(plan_resolutions.c.proposal_id).where(plan_resolutions.c.regimen_id == regimen_id)
    )
    if pid is None:
        raise SupplementError("Plan preview is unavailable.")
    return await proposal(connection, pid)


async def _revision(
    connection: AsyncConnection,
    regimen_id: int,
    plan: CreatinePlan,
    key: str,
    state: str,
    operation: str,
    previous: sa.RowMapping | None = None,
) -> int:
    rid = (
        await connection.execute(
            sa.insert(supplement_regimen_revisions)
            .values(
                regimen_id=regimen_id,
                revision_number=1 if previous is None else previous["revision_number"] + 1,
                previous_revision_id=None if previous is None else previous["id"],
                action_key=key,
                product_version_id=None,
                substance_code="creatine_monohydrate",
                name="Creatine monohydrate",
                purpose=None,
                start_date=plan.start,
                end_date=None,
                timezone=plan.timezone,
                state=state,
                provenance="user_selected",
                protocol_code=plan.option,
                protocol_version=plan.protocol_version,
                reference_weight_grams=plan.weight_grams,
                sealed=False,
                deleted=False,
                operation=operation,
                created_at=time.time(),
            )
            .returning(supplement_regimen_revisions.c.id)
        )
    ).scalar_one()
    for index, phase in enumerate(plan.phases(), 1):
        await connection.execute(
            sa.insert(supplement_regimen_phases).values(
                regimen_revision_id=rid,
                phase_index=index,
                name=phase.name,
                start_day_offset=phase.offset,
                duration_days=phase.days,
                daily_target_scaled=phase.dose_mg * phase.frequency * 1_000_000,
                frequency=phase.frequency,
            )
        )
        for slot in range(1, phase.frequency + 1):
            await connection.execute(
                sa.insert(supplement_phase_doses).values(
                    regimen_revision_id=rid,
                    phase_index=index,
                    slot_index=slot,
                    amount_scaled=phase.dose_mg * 1_000_000,
                    reminder_minute=None,
                )
            )
    await connection.execute(
        sa.update(supplement_regimen_revisions)
        .where(supplement_regimen_revisions.c.id == rid)
        .values(sealed=True)
    )
    await connection.execute(
        sa.update(supplement_regimens)
        .where(supplement_regimens.c.id == regimen_id)
        .values(current_revision_id=rid)
    )
    return int(rid)


async def context_event(
    connection: AsyncConnection, key: str, day: date, kind: str, rid: int
) -> None:
    await connection.execute(
        sa.insert(health_context_events).values(
            action_key=key,
            local_date=day,
            kind=kind,
            regimen_revision_id=rid,
            created_at=time.time(),
        )
    )


async def resolve(
    connection: AsyncConnection, pid: int, action: str, key: str, today: date
) -> int | None:
    plan = await proposal(connection, pid)
    if (
        await connection.scalar(
            sa.select(plan_resolutions.c.proposal_id).where(plan_resolutions.c.proposal_id == pid)
        )
        is not None
    ):
        raise SupplementError("This preview was already resolved. Open /supplement plans.")
    regimen_id = None
    if action == "approve":
        if plan.start < today:
            raise SupplementError("The start date has passed. Create a fresh plan preview.")
        active = await connection.scalar(
            sa.select(supplement_regimens.c.id)
            .join(
                supplement_regimen_revisions,
                supplement_regimens.c.current_revision_id == supplement_regimen_revisions.c.id,
            )
            .where(supplement_regimen_revisions.c.state != "stopped")
        )
        if active:
            raise SupplementError("Stop the existing creatine plan before approving another.")
        await ensure_creatine_substance(connection)
        regimen_id = (
            await connection.execute(
                sa.insert(supplement_regimens)
                .values(created_at=time.time())
                .returning(supplement_regimens.c.id)
            )
        ).scalar_one()
        rid = await _revision(connection, regimen_id, plan, key, "active", "create")
        await context_event(
            connection,
            key,
            plan.start,
            "creatine_start" if plan.option == "steady" else "creatine_loading_start",
            rid,
        )
    elif action != "cancel":
        raise SupplementError("Choose Approve plan or Cancel.")
    await connection.execute(
        sa.insert(plan_resolutions).values(
            proposal_id=pid,
            action_key=key,
            state="approved" if regimen_id else "cancelled",
            regimen_id=regimen_id,
        )
    )
    return regimen_id


async def change_state(
    connection: AsyncConnection,
    regimen_id: int,
    expected: int,
    operation: str,
    key: str,
    today: date,
) -> None:
    old = await current(connection, regimen_id)
    if old["revision_number"] != expected:
        raise SupplementError("Old plan reference; open /supplement plans.")
    transitions = {
        ("active", "pause"): "paused",
        ("active", "stop"): "stopped",
        ("paused", "resume"): "active",
        ("paused", "stop"): "stopped",
    }
    state = transitions.get((old["state"], operation))
    if state is None:
        raise SupplementError("That state change is unavailable. Stopped plans cannot restart.")
    plan = await plan_for(connection, regimen_id)
    rid = await _revision(connection, regimen_id, plan, key, state, operation, old)
    await context_event(
        connection,
        key,
        today,
        "creatine_restart" if operation == "resume" else "creatine_stop",
        rid,
    )


async def mark_dose(
    connection: AsyncConnection,
    regimen_id: int,
    expected: int,
    day: date,
    slot: int,
    status: str,
    key: str,
    reference: datetime,
    chat_id: int,
    message_id: int,
) -> None:
    old = await current(connection, regimen_id)
    plan = await plan_for(connection, regimen_id)
    reference = reference.astimezone(ZoneInfo(plan.timezone))
    day = reference.date()
    phase = plan.phase_on(day)
    if old["revision_number"] != expected or old["state"] != "active":
        raise SupplementError("Use the current active plan reference from /supplement plans.")
    if day != reference.date():
        raise SupplementError(
            "Plan dose marking currently supports today only; use exact logs for past intake."
        )
    if phase is None or not 1 <= slot <= phase[1].frequency:
        raise SupplementError("That dose is outside this phase.")
    previous = (
        (
            await connection.execute(
                sa.select(plan_dose_marks)
                .where(
                    plan_dose_marks.c.regimen_id == regimen_id,
                    plan_dose_marks.c.day == day,
                    plan_dose_marks.c.slot == slot,
                )
                .order_by(plan_dose_marks.c.id.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    if previous and previous["status"] != "unconfirmed":
        if status != "unconfirmed":
            raise SupplementError(
                "Dose already marked; clear it before correcting. No duplicate intake saved."
            )
        if previous["intake_id"]:
            intake = await get_supplement_intake(connection, previous["intake_id"])
            if not intake.deleted:
                await revise_supplement_intake(
                    connection, intake.id, intake.revision_id, action_key=key, delete=True
                )
    intake_id = None
    if status == "taken":
        intake = await create_direct_supplement_intake(
            connection,
            action_key=key,
            source_chat_id=chat_id,
            source_message_id=message_id,
            local_date=day,
            consumed_at=reference.timestamp(),
            timezone=plan.timezone,
            substance_code="creatine_monohydrate",
            amount_scaled=phase[1].dose_mg * 1_000_000,
        )
        intake_id = intake.id
    await connection.execute(
        sa.insert(plan_dose_marks).values(
            action_key=key,
            regimen_id=regimen_id,
            revision_id=old["id"],
            day=day,
            slot=slot,
            status=status,
            intake_id=intake_id,
        )
    )


async def describe(connection: AsyncConnection, regimen_id: int, today: date | datetime) -> str:
    row = await current(connection, regimen_id)
    plan = await plan_for(connection, regimen_id)
    if isinstance(today, datetime):
        today = today.astimezone(ZoneInfo(plan.timezone)).date()
    lines = [f"R{regimen_id}r{row['revision_number']} · {row['state']}", plan.preview()]
    phase = plan.phase_on(today)
    if phase and row["state"] == "active":
        for slot in range(1, phase[1].frequency + 1):
            mark = (
                (
                    await connection.execute(
                        sa.select(plan_dose_marks)
                        .where(
                            plan_dose_marks.c.regimen_id == regimen_id,
                            plan_dose_marks.c.day == today,
                            plan_dose_marks.c.slot == slot,
                        )
                        .order_by(plan_dose_marks.c.id.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            status = "unconfirmed" if mark is None else mark["status"]
            if mark and mark["intake_id"]:
                intake = await get_supplement_intake(connection, mark["intake_id"])
                status = (
                    "unconfirmed (intake deleted)"
                    if intake.deleted
                    else (f"taken · actual {intake.component_amount_scaled / 1_000_000_000:g} g")
                )
            lines.append(f"Today dose {slot}: {status}")
    return "\n".join(lines)


async def has_weight_context(connection: AsyncConnection, end: date) -> bool:
    start = end - timedelta(days=27)
    if await connection.scalar(
        sa.select(health_context_events.c.id)
        .where(health_context_events.c.local_date.between(start, end))
        .limit(1)
    ):
        return True
    # A scheduled transition is context, not evidence that any dose was consumed.
    rows: Sequence[object] = (
        (
            await connection.execute(
                sa.select(plan_proposals.c.plan)
                .join(
                    plan_resolutions,
                    plan_resolutions.c.proposal_id == plan_proposals.c.id,
                )
                .where(plan_resolutions.c.state == "approved")
            )
        )
        .scalars()
        .all()
    )
    return any(
        p.loading_days and start <= p.start + timedelta(days=p.loading_days) <= end
        for p in (CreatinePlan.model_validate(row) for row in rows)
    )
