from datetime import date

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema_recovery import recovery_checkins, recovery_revisions
from nutrition_bot.adapters.database.schema_training import (
    training_plan_events,
    training_session_revisions,
    training_sessions,
)
from nutrition_bot.domain.training_guidance import RecoverySignal
from nutrition_bot.domain.training_load import (
    TrainingLoadSession,
    TrainingLoadTrend,
    summarize_training_load,
    workload_window_start,
)


async def training_load_inputs(
    connection: AsyncConnection, *, start: date, end: date
) -> tuple[tuple[TrainingLoadSession, ...], frozenset[date]]:
    rows = (
        (
            await connection.execute(
                sa.select(
                    training_session_revisions.c.local_date,
                    training_session_revisions.c.kind,
                    training_session_revisions.c.duration_minutes,
                    training_session_revisions.c.session_rpe_tenths,
                    training_session_revisions.c.rpe_source,
                )
                .join(
                    training_sessions,
                    training_sessions.c.current_revision_id == training_session_revisions.c.id,
                )
                .where(
                    training_session_revisions.c.local_date.between(start, end),
                    training_session_revisions.c.deleted.is_(False),
                )
                .order_by(training_session_revisions.c.local_date, training_session_revisions.c.id)
            )
        )
        .mappings()
        .all()
    )
    sessions = tuple(
        TrainingLoadSession(
            local_date=row["local_date"],
            kind=row["kind"],
            duration_minutes=row["duration_minutes"],
            session_rpe_tenths=row["session_rpe_tenths"],
            rpe_source=row["rpe_source"],
        )
        for row in rows
    )
    event_rows = (
        (
            await connection.execute(
                sa.select(
                    training_plan_events.c.local_date,
                    training_plan_events.c.state,
                    training_plan_events.c.id,
                )
                .where(training_plan_events.c.local_date.between(start, end))
                .order_by(training_plan_events.c.local_date, training_plan_events.c.id.desc())
            )
        )
        .mappings()
        .all()
    )
    latest_states: dict[date, str] = {}
    for row in event_rows:
        latest_states.setdefault(row["local_date"], row["state"])
    rest_days = frozenset(day for day, state in latest_states.items() if state == "rest")
    return sessions, rest_days


async def weekly_training_load(connection: AsyncConnection, *, as_of: date) -> TrainingLoadTrend:
    start = workload_window_start(as_of)
    end = as_of
    sessions, rest_days = await training_load_inputs(connection, start=start, end=end)
    return summarize_training_load(sessions, rest_days, as_of=as_of)


async def recovery_signals(
    connection: AsyncConnection, *, start: date, end: date
) -> tuple[RecoverySignal, ...]:
    rows = (
        (
            await connection.execute(
                sa.select(
                    recovery_checkins.c.local_date,
                    recovery_revisions.c.soreness,
                    recovery_revisions.c.fatigue,
                )
                .join(
                    recovery_revisions,
                    recovery_checkins.c.current_revision_id == recovery_revisions.c.id,
                )
                .where(
                    recovery_checkins.c.local_date.between(start, end),
                    recovery_revisions.c.deleted.is_(False),
                )
                .order_by(recovery_checkins.c.local_date)
            )
        )
        .mappings()
        .all()
    )
    return tuple(RecoverySignal(row["local_date"], row["soreness"], row["fatigue"]) for row in rows)
