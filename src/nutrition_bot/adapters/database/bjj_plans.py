import time
from dataclasses import dataclass
from datetime import date

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import actions
from nutrition_bot.adapters.database.schema_training import (
    bjj_template_revisions,
    training_plan_events,
    training_schedule_revisions,
    training_schedule_rules,
    training_session_revisions,
    training_sessions,
)
from nutrition_bot.adapters.database.training import TrainingError


@dataclass(frozen=True)
class BJJTemplate:
    revision_number: int
    typical_duration_minutes: int
    warmup_min_minutes: int | None
    warmup_max_minutes: int | None
    technical_min_minutes: int | None
    technical_max_minutes: int | None
    positional_min_rounds: int | None
    positional_max_rounds: int | None
    positional_round_minutes: int | None
    sparring_min_rounds: int | None
    sparring_max_rounds: int | None
    sparring_round_minutes: int | None


@dataclass(frozen=True)
class ScheduleRule:
    weekday: int
    kind: str
    duration_minutes: int
    start_minute: int | None = None


@dataclass(frozen=True)
class PlannedActivity:
    kind: str | None
    duration_minutes: int | None
    start_minute: int | None


@dataclass(frozen=True)
class DayPlan:
    local_date: date
    state: str
    activities: tuple[PlannedActivity, ...]
    source: str
    completed: tuple[str, ...]


async def _check_action(connection: AsyncConnection, action_key: str) -> None:
    if await connection.scalar(sa.select(actions.c.key).where(actions.c.key == action_key)) is None:
        raise TrainingError("This action is unavailable; send the command again.")


async def current_template(connection: AsyncConnection) -> BJJTemplate | None:
    row = (
        (
            await connection.execute(
                sa.select(bjj_template_revisions)
                .order_by(bjj_template_revisions.c.revision_number.desc())
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["deleted"]:
        return None
    return BJJTemplate(**{field: row[field] for field in BJJTemplate.__dataclass_fields__})


async def save_template(
    connection: AsyncConnection, template: BJJTemplate, *, action_key: str
) -> BJJTemplate:
    await _check_action(connection, action_key)
    previous = (
        (
            await connection.execute(
                sa.select(bjj_template_revisions.c.id, bjj_template_revisions.c.revision_number)
                .order_by(bjj_template_revisions.c.revision_number.desc())
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    revision = 1 if previous is None else previous["revision_number"] + 1
    values = {field: getattr(template, field) for field in BJJTemplate.__dataclass_fields__}
    values["revision_number"] = revision
    await connection.execute(
        sa.insert(bjj_template_revisions).values(
            **values,
            previous_revision_id=previous["id"] if previous else None,
            action_key=action_key,
            deleted=False,
            created_at=time.time(),
        )
    )
    return BJJTemplate(**values)


async def clear_template(connection: AsyncConnection, *, action_key: str) -> None:
    current = await current_template(connection)
    if current is None:
        raise TrainingError("No BJJ template is set.")
    await _check_action(connection, action_key)
    previous = (
        (
            await connection.execute(
                sa.select(bjj_template_revisions.c.id, bjj_template_revisions.c.revision_number)
                .order_by(bjj_template_revisions.c.revision_number.desc())
                .limit(1)
            )
        )
        .mappings()
        .one()
    )
    values = {field: getattr(current, field) for field in BJJTemplate.__dataclass_fields__}
    values["revision_number"] = previous["revision_number"] + 1
    await connection.execute(
        sa.insert(bjj_template_revisions).values(
            **values,
            previous_revision_id=previous["id"],
            action_key=action_key,
            deleted=True,
            created_at=time.time(),
        )
    )


async def current_schedule(connection: AsyncConnection) -> tuple[ScheduleRule, ...]:
    revision_id = await connection.scalar(
        sa.select(training_schedule_revisions.c.id)
        .order_by(training_schedule_revisions.c.revision_number.desc())
        .limit(1)
    )
    if revision_id is None:
        return ()
    rows = (
        (
            await connection.execute(
                sa.select(training_schedule_rules)
                .where(training_schedule_rules.c.schedule_revision_id == revision_id)
                .order_by(training_schedule_rules.c.weekday, training_schedule_rules.c.kind)
            )
        )
        .mappings()
        .all()
    )
    return tuple(
        ScheduleRule(row["weekday"], row["kind"], row["duration_minutes"], row["start_minute"])
        for row in rows
    )


async def replace_schedule(
    connection: AsyncConnection,
    rules: tuple[ScheduleRule, ...],
    *,
    timezone: str,
    action_key: str,
) -> tuple[ScheduleRule, ...]:
    await _check_action(connection, action_key)
    previous = (
        (
            await connection.execute(
                sa.select(
                    training_schedule_revisions.c.id, training_schedule_revisions.c.revision_number
                )
                .order_by(training_schedule_revisions.c.revision_number.desc())
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    revision_id = (
        await connection.execute(
            sa.insert(training_schedule_revisions)
            .values(
                revision_number=1 if previous is None else previous["revision_number"] + 1,
                previous_revision_id=previous["id"] if previous else None,
                action_key=action_key,
                timezone=timezone,
                created_at=time.time(),
            )
            .returning(training_schedule_revisions.c.id)
        )
    ).scalar_one()
    if rules:
        await connection.execute(
            sa.insert(training_schedule_rules),
            [
                {
                    "schedule_revision_id": revision_id,
                    "weekday": rule.weekday,
                    "kind": rule.kind,
                    "duration_minutes": rule.duration_minutes,
                    "start_minute": rule.start_minute,
                }
                for rule in rules
            ],
        )
    return rules


async def add_plan_event(
    connection: AsyncConnection,
    *,
    action_key: str,
    local_date: date,
    state: str,
    timezone: str,
    kind: str | None = None,
    duration_minutes: int | None = None,
    start_minute: int | None = None,
) -> None:
    await _check_action(connection, action_key)
    await connection.execute(
        sa.insert(training_plan_events).values(
            action_key=action_key,
            local_date=local_date,
            state=state,
            kind=kind,
            duration_minutes=duration_minutes,
            start_minute=start_minute,
            timezone=timezone,
            created_at=time.time(),
        )
    )


async def day_plan(connection: AsyncConnection, day: date) -> DayPlan:
    override = (
        (
            await connection.execute(
                sa.select(training_plan_events)
                .where(training_plan_events.c.local_date == day)
                .order_by(training_plan_events.c.id.desc())
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    rules = tuple(
        rule for rule in await current_schedule(connection) if rule.weekday == day.weekday()
    )
    activities: tuple[PlannedActivity, ...]
    if override is not None and override["state"] != "clear":
        state = override["state"]
        activities = (
            (
                PlannedActivity(
                    override["kind"], override["duration_minutes"], override["start_minute"]
                ),
            )
            if state == "planned"
            else ()
        )
        source = "date override"
    elif rules:
        state, activities, source = (
            "planned",
            tuple(
                PlannedActivity(rule.kind, rule.duration_minutes, rule.start_minute)
                for rule in rules
            ),
            "weekly schedule",
        )
    else:
        state, activities, source = "unknown", (), "no plan"
    completed: tuple[str, ...] = tuple(
        (
            await connection.execute(
                sa.select(training_session_revisions.c.kind)
                .join(
                    training_sessions,
                    training_sessions.c.current_revision_id == training_session_revisions.c.id,
                )
                .where(
                    training_session_revisions.c.local_date == day,
                    training_session_revisions.c.deleted.is_(False),
                )
                .order_by(training_session_revisions.c.id)
            )
        )
        .scalars()
        .all()
    )
    return DayPlan(day, state, activities, source, completed)
