import time
from dataclasses import dataclass
from datetime import date
from statistics import median

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import actions
from nutrition_bot.adapters.database.schema_training import (
    bjj_session_details,
    gym_exercise_occurrences,
    gym_workout_sets,
    training_session_revisions,
    training_sessions,
)


class TrainingError(ValueError):
    pass


@dataclass(frozen=True)
class WorkoutSet:
    set_index: int
    reps: int | None = None
    load_grams: int | None = None
    load_convention: str | None = None
    set_rpe_tenths: int | None = None
    set_type: str | None = None


@dataclass(frozen=True)
class GymExercise:
    occurrence_index: int
    name: str
    sets: tuple[WorkoutSet, ...]


@dataclass(frozen=True)
class BJJDetails:
    warmup_minutes: int | None = None
    technical_minutes: int | None = None
    positional_rounds: int | None = None
    positional_round_minutes: int | None = None
    sparring_rounds: int | None = None
    sparring_round_minutes: int | None = None


@dataclass(frozen=True)
class TrainingSnapshot:
    id: int
    revision_id: int
    revision_number: int
    kind: str
    local_date: date
    occurred_at: float
    timezone: str
    duration_minutes: int
    focus: str | None
    intensity: str | None
    session_rpe_tenths: int
    rpe_source: str
    deleted: bool
    operation: str
    exercises: tuple[GymExercise, ...]
    bjj_details: BJJDetails | None


def _snapshot(
    row: RowMapping, exercises: tuple[GymExercise, ...], bjj_details: BJJDetails | None
) -> TrainingSnapshot:
    return TrainingSnapshot(
        id=row["training_session_id"],
        revision_id=row["id"],
        revision_number=row["revision_number"],
        kind=row["kind"],
        local_date=row["local_date"],
        occurred_at=row["occurred_at"],
        timezone=row["timezone"],
        duration_minutes=row["duration_minutes"],
        focus=row["focus"],
        intensity=row["intensity"],
        session_rpe_tenths=row["session_rpe_tenths"],
        rpe_source=row["rpe_source"],
        deleted=row["deleted"],
        operation=row["operation"],
        exercises=exercises,
        bjj_details=bjj_details,
    )


async def _validate_action(connection: AsyncConnection, action_key: str) -> None:
    if await connection.scalar(sa.select(actions.c.key).where(actions.c.key == action_key)) is None:
        raise TrainingError("This action is unavailable; send the training log again.")
    if await connection.scalar(
        sa.select(training_session_revisions.c.id).where(
            training_session_revisions.c.action_key == action_key
        )
    ):
        raise TrainingError("This action was already applied; open the latest training receipt.")


async def _revision(connection: AsyncConnection, revision_id: int) -> TrainingSnapshot:
    row = (
        (
            await connection.execute(
                sa.select(training_session_revisions).where(
                    training_session_revisions.c.id == revision_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise TrainingError("That training session is unavailable.")
    exercise_rows = (
        (
            await connection.execute(
                sa.select(gym_exercise_occurrences)
                .where(gym_exercise_occurrences.c.training_revision_id == revision_id)
                .order_by(gym_exercise_occurrences.c.occurrence_index)
            )
        )
        .mappings()
        .all()
    )
    exercises: list[GymExercise] = []
    for exercise in exercise_rows:
        set_rows = (
            (
                await connection.execute(
                    sa.select(gym_workout_sets)
                    .where(gym_workout_sets.c.exercise_occurrence_id == exercise["id"])
                    .order_by(gym_workout_sets.c.set_index)
                )
            )
            .mappings()
            .all()
        )
        exercises.append(
            GymExercise(
                occurrence_index=exercise["occurrence_index"],
                name=exercise["name"],
                sets=tuple(
                    WorkoutSet(
                        set_index=item["set_index"],
                        reps=item["reps"],
                        load_grams=item["load_grams"],
                        load_convention=item["load_convention"],
                        set_rpe_tenths=item["set_rpe_tenths"],
                        set_type=item["set_type"],
                    )
                    for item in set_rows
                ),
            )
        )
    detail_row = (
        (
            await connection.execute(
                sa.select(bjj_session_details).where(
                    bjj_session_details.c.training_revision_id == revision_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    details = (
        BJJDetails(
            warmup_minutes=detail_row["warmup_minutes"],
            technical_minutes=detail_row["technical_minutes"],
            positional_rounds=detail_row["positional_rounds"],
            positional_round_minutes=detail_row["positional_round_minutes"],
            sparring_rounds=detail_row["sparring_rounds"],
            sparring_round_minutes=detail_row["sparring_round_minutes"],
        )
        if detail_row is not None
        else None
    )
    return _snapshot(row, tuple(exercises), details)


async def get_training(connection: AsyncConnection, session_id: int) -> TrainingSnapshot:
    revision_id = await connection.scalar(
        sa.select(training_sessions.c.current_revision_id).where(
            training_sessions.c.id == session_id
        )
    )
    if revision_id is None:
        raise TrainingError("That training session is unavailable. Use /training history.")
    return await _revision(connection, revision_id)


async def inferred_rpe(
    connection: AsyncConnection, *, kind: str, focus: str | None, intensity: str | None
) -> tuple[int, str]:
    conditions = [
        training_session_revisions.c.kind == kind,
        training_session_revisions.c.rpe_source == "reported",
        training_session_revisions.c.deleted.is_(False),
    ]
    if kind == "gym":
        conditions.append(
            sa.func.lower(training_session_revisions.c.focus) == (focus or "").lower()
        )
    values = (
        (
            await connection.execute(
                sa.select(training_session_revisions.c.session_rpe_tenths)
                .join(
                    training_sessions,
                    training_sessions.c.current_revision_id == training_session_revisions.c.id,
                )
                .where(*conditions)
                .order_by(training_session_revisions.c.local_date.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    if len(values) >= 3:
        return int(median(values)), "personal_history"
    if kind == "bjj" and intensity is not None:
        return {"easy": 30, "medium": 50, "hard": 80}[intensity], "intensity_map"
    return (60, "system_default") if kind == "gym" else (50, "system_default")


_KEEP_BJJ_DETAILS = object()


def _bjj_known_minutes(details: BJJDetails | None) -> int:
    if details is None:
        return 0
    total = (details.warmup_minutes or 0) + (details.technical_minutes or 0)
    if details.positional_rounds is not None:
        total += details.positional_rounds * (details.positional_round_minutes or 0)
    if details.sparring_rounds is not None:
        total += details.sparring_rounds * (details.sparring_round_minutes or 0)
    return total


async def _publish(
    connection: AsyncConnection,
    *,
    session_id: int,
    current: TrainingSnapshot | None,
    action_key: str,
    kind: str,
    local_date: date,
    occurred_at: float,
    timezone: str,
    duration_minutes: int,
    focus: str | None,
    intensity: str | None,
    session_rpe_tenths: int,
    rpe_source: str,
    deleted: bool,
    operation: str,
    exercises: tuple[GymExercise, ...] | None = None,
    bjj_details: BJJDetails | None | object = _KEEP_BJJ_DETAILS,
) -> TrainingSnapshot:
    revision_id = (
        await connection.execute(
            sa.insert(training_session_revisions)
            .values(
                training_session_id=session_id,
                revision_number=current.revision_number + 1 if current else 1,
                previous_revision_id=current.revision_id if current else None,
                action_key=action_key,
                kind=kind,
                local_date=local_date,
                occurred_at=occurred_at,
                timezone=timezone,
                duration_minutes=duration_minutes,
                focus=focus,
                intensity=intensity,
                session_rpe_tenths=session_rpe_tenths,
                rpe_source=rpe_source,
                deleted=deleted,
                operation=operation,
                created_at=time.time(),
            )
            .returning(training_session_revisions.c.id)
        )
    ).scalar_one()
    chosen_exercises = current.exercises if exercises is None and current else exercises or ()
    for exercise in chosen_exercises:
        occurrence_id = (
            await connection.execute(
                sa.insert(gym_exercise_occurrences)
                .values(
                    training_revision_id=revision_id,
                    occurrence_index=exercise.occurrence_index,
                    name=exercise.name,
                )
                .returning(gym_exercise_occurrences.c.id)
            )
        ).scalar_one()
        if exercise.sets:
            await connection.execute(
                sa.insert(gym_workout_sets),
                [
                    {
                        "exercise_occurrence_id": occurrence_id,
                        "set_index": item.set_index,
                        "reps": item.reps,
                        "load_grams": item.load_grams,
                        "load_convention": item.load_convention,
                        "set_rpe_tenths": item.set_rpe_tenths,
                        "set_type": item.set_type,
                    }
                    for item in exercise.sets
                ],
            )
    chosen_bjj = (
        current.bjj_details if bjj_details is _KEEP_BJJ_DETAILS and current else bjj_details
    )
    if isinstance(chosen_bjj, BJJDetails):
        await connection.execute(
            sa.insert(bjj_session_details).values(
                training_revision_id=revision_id,
                warmup_minutes=chosen_bjj.warmup_minutes,
                technical_minutes=chosen_bjj.technical_minutes,
                positional_rounds=chosen_bjj.positional_rounds,
                positional_round_minutes=chosen_bjj.positional_round_minutes,
                sparring_rounds=chosen_bjj.sparring_rounds,
                sparring_round_minutes=chosen_bjj.sparring_round_minutes,
            )
        )
    await connection.execute(
        sa.update(training_sessions)
        .where(training_sessions.c.id == session_id)
        .values(current_revision_id=revision_id)
    )
    return await _revision(connection, revision_id)


async def create_training(
    connection: AsyncConnection,
    *,
    action_key: str,
    source_chat_id: int,
    source_message_id: int,
    kind: str,
    local_date: date,
    occurred_at: float,
    timezone: str,
    duration_minutes: int,
    focus: str | None,
    intensity: str | None,
    session_rpe_tenths: int | None,
) -> TrainingSnapshot:
    await _validate_action(connection, action_key)
    if await connection.scalar(
        sa.select(training_sessions.c.id).where(
            training_sessions.c.source_chat_id == source_chat_id,
            training_sessions.c.source_message_id == source_message_id,
        )
    ):
        raise TrainingError("That message already has a training session.")
    if session_rpe_tenths is None:
        session_rpe_tenths, source = await inferred_rpe(
            connection, kind=kind, focus=focus, intensity=intensity
        )
    else:
        source = "reported"
    session_id = (
        await connection.execute(
            sa.insert(training_sessions)
            .values(
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                created_at=time.time(),
            )
            .returning(training_sessions.c.id)
        )
    ).scalar_one()
    return await _publish(
        connection,
        session_id=session_id,
        current=None,
        action_key=action_key,
        kind=kind,
        local_date=local_date,
        occurred_at=occurred_at,
        timezone=timezone,
        duration_minutes=duration_minutes,
        focus=focus,
        intensity=intensity,
        session_rpe_tenths=session_rpe_tenths,
        rpe_source=source,
        deleted=False,
        operation="create",
        exercises=(),
        bjj_details=None,
    )


async def revise_training(
    connection: AsyncConnection,
    session_id: int,
    expected_revision_id: int,
    *,
    action_key: str,
    duration_minutes: int | None = None,
    focus: str | None = None,
    intensity: str | None = None,
    session_rpe_tenths: int | None = None,
    rpe_source: str | None = None,
    delete: bool = False,
) -> TrainingSnapshot:
    current = await get_training(connection, session_id)
    if current.revision_id != expected_revision_id:
        raise TrainingError("That receipt is old. Open /training history for the latest one.")
    await _validate_action(connection, action_key)
    if delete and current.deleted:
        raise TrainingError("That session is already deleted; use Undo.")
    next_duration = duration_minutes or current.duration_minutes
    known_bjj_minutes = _bjj_known_minutes(current.bjj_details)
    if current.kind == "bjj" and known_bjj_minutes > next_duration:
        raise TrainingError(
            f"Reported BJJ stages total {known_bjj_minutes} min; update or clear details "
            f"before reducing the session to {next_duration} min."
        )
    return await _publish(
        connection,
        session_id=session_id,
        current=current,
        action_key=action_key,
        kind=current.kind,
        local_date=current.local_date,
        occurred_at=current.occurred_at,
        timezone=current.timezone,
        duration_minutes=next_duration,
        focus=current.focus if focus is None else focus,
        intensity=current.intensity if intensity is None else intensity,
        session_rpe_tenths=(
            current.session_rpe_tenths if session_rpe_tenths is None else session_rpe_tenths
        ),
        rpe_source=(current.rpe_source if session_rpe_tenths is None else rpe_source or "reported"),
        deleted=True if delete else current.deleted,
        operation="delete" if delete else "edit",
    )


async def undo_training(
    connection: AsyncConnection, session_id: int, expected_revision_id: int, *, action_key: str
) -> TrainingSnapshot:
    current = await get_training(connection, session_id)
    if current.revision_id != expected_revision_id:
        raise TrainingError("That receipt is old. Open /training history for the latest one.")
    await _validate_action(connection, action_key)
    previous_id = await connection.scalar(
        sa.select(training_session_revisions.c.previous_revision_id).where(
            training_session_revisions.c.id == current.revision_id
        )
    )
    previous = current if previous_id is None else await _revision(connection, previous_id)
    return await _publish(
        connection,
        session_id=session_id,
        current=current,
        action_key=action_key,
        kind=previous.kind,
        local_date=previous.local_date,
        occurred_at=previous.occurred_at,
        timezone=previous.timezone,
        duration_minutes=previous.duration_minutes,
        focus=previous.focus,
        intensity=previous.intensity,
        session_rpe_tenths=previous.session_rpe_tenths,
        rpe_source=previous.rpe_source,
        deleted=True if previous_id is None else previous.deleted,
        operation="undo",
        exercises=previous.exercises,
        bjj_details=previous.bjj_details,
    )


async def revise_gym_details(
    connection: AsyncConnection,
    session_id: int,
    expected_revision_id: int,
    exercises: tuple[GymExercise, ...],
    *,
    action_key: str,
) -> TrainingSnapshot:
    current = await get_training(connection, session_id)
    if current.revision_id != expected_revision_id:
        raise TrainingError("That receipt is old. Open /training history for the latest one.")
    if current.kind != "gym" or current.deleted:
        raise TrainingError("Gym details require a current, non-deleted gym session.")
    await _validate_action(connection, action_key)
    return await _publish(
        connection,
        session_id=session_id,
        current=current,
        action_key=action_key,
        kind=current.kind,
        local_date=current.local_date,
        occurred_at=current.occurred_at,
        timezone=current.timezone,
        duration_minutes=current.duration_minutes,
        focus=current.focus,
        intensity=current.intensity,
        session_rpe_tenths=current.session_rpe_tenths,
        rpe_source=current.rpe_source,
        deleted=False,
        operation="edit",
        exercises=exercises,
    )


async def revise_bjj_details(
    connection: AsyncConnection,
    session_id: int,
    expected_revision_id: int,
    details: BJJDetails,
    *,
    action_key: str,
) -> TrainingSnapshot:
    current = await get_training(connection, session_id)
    if current.revision_id != expected_revision_id:
        raise TrainingError("That receipt is old. Open /training history for the latest one.")
    if current.kind != "bjj" or current.deleted:
        raise TrainingError("BJJ details require a current, non-deleted BJJ session.")
    known_minutes = _bjj_known_minutes(details)
    if known_minutes > current.duration_minutes:
        raise TrainingError(
            f"Reported stage time is {known_minutes} min, longer than the "
            f"{current.duration_minutes} min session."
        )
    await _validate_action(connection, action_key)
    return await _publish(
        connection,
        session_id=session_id,
        current=current,
        action_key=action_key,
        kind=current.kind,
        local_date=current.local_date,
        occurred_at=current.occurred_at,
        timezone=current.timezone,
        duration_minutes=current.duration_minutes,
        focus=current.focus,
        intensity=current.intensity,
        session_rpe_tenths=current.session_rpe_tenths,
        rpe_source=current.rpe_source,
        deleted=False,
        operation="edit",
        bjj_details=details,
    )


async def recent_training(connection: AsyncConnection, limit: int = 10) -> list[TrainingSnapshot]:
    ids = (
        (
            await connection.execute(
                sa.select(training_sessions.c.id)
                .order_by(training_sessions.c.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [await get_training(connection, session_id) for session_id in ids]
