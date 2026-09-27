import time
from dataclasses import dataclass
from datetime import date

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import actions
from nutrition_bot.adapters.database.schema_recovery import recovery_checkins, recovery_revisions


class RecoveryError(ValueError):
    pass


@dataclass(frozen=True)
class RecoverySnapshot:
    id: int
    revision_id: int
    revision_number: int
    local_date: date
    sleep_minutes: int | None
    soreness: int | None
    fatigue: int | None
    readiness: int | None
    deleted: bool
    operation: str


def _snapshot(row: sa.RowMapping) -> RecoverySnapshot:
    return RecoverySnapshot(
        id=row["checkin_id"],
        revision_id=row["id"],
        revision_number=row["revision_number"],
        local_date=row["local_date"],
        sleep_minutes=row["sleep_minutes"],
        soreness=row["soreness"],
        fatigue=row["fatigue"],
        readiness=row["readiness"],
        deleted=row["deleted"],
        operation=row["operation"],
    )


async def get_recovery(connection: AsyncConnection, checkin_id: int) -> RecoverySnapshot:
    row = (
        (
            await connection.execute(
                sa.select(recovery_revisions, recovery_checkins.c.local_date)
                .join(
                    recovery_checkins,
                    recovery_checkins.c.current_revision_id == recovery_revisions.c.id,
                )
                .where(recovery_checkins.c.id == checkin_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RecoveryError("That recovery check-in is unavailable.")
    return _snapshot(row)


async def _validate(connection: AsyncConnection, action_key: str) -> None:
    if await connection.scalar(sa.select(actions.c.key).where(actions.c.key == action_key)) is None:
        raise RecoveryError("This action is unavailable; send the check-in again.")
    if await connection.scalar(
        sa.select(recovery_revisions.c.id).where(recovery_revisions.c.action_key == action_key)
    ):
        raise RecoveryError("This action was already applied; open the latest recovery receipt.")


async def _publish(
    connection: AsyncConnection,
    *,
    checkin_id: int,
    current: RecoverySnapshot | None,
    action_key: str,
    local_date: date,
    sleep_minutes: int | None,
    soreness: int | None,
    fatigue: int | None,
    readiness: int | None,
    deleted: bool,
    operation: str,
) -> RecoverySnapshot:
    revision_id = (
        await connection.execute(
            sa.insert(recovery_revisions)
            .values(
                checkin_id=checkin_id,
                revision_number=current.revision_number + 1 if current else 1,
                previous_revision_id=current.revision_id if current else None,
                action_key=action_key,
                sleep_minutes=sleep_minutes,
                soreness=soreness,
                fatigue=fatigue,
                readiness=readiness,
                deleted=deleted,
                operation=operation,
                created_at=time.time(),
            )
            .returning(recovery_revisions.c.id)
        )
    ).scalar_one()
    await connection.execute(
        sa.update(recovery_checkins)
        .where(recovery_checkins.c.id == checkin_id)
        .values(current_revision_id=revision_id)
    )
    return await get_recovery(connection, checkin_id)


async def create_recovery(
    connection: AsyncConnection,
    *,
    action_key: str,
    local_date: date,
    sleep_minutes: int | None,
    soreness: int | None,
    fatigue: int | None,
    readiness: int | None,
) -> RecoverySnapshot:
    await _validate(connection, action_key)
    if await connection.scalar(
        sa.select(recovery_checkins.c.id).where(recovery_checkins.c.local_date == local_date)
    ):
        raise RecoveryError("That date already has a recovery check-in; edit its current receipt.")
    checkin_id = (
        await connection.execute(
            sa.insert(recovery_checkins)
            .values(local_date=local_date, created_at=time.time())
            .returning(recovery_checkins.c.id)
        )
    ).scalar_one()
    return await _publish(
        connection,
        checkin_id=checkin_id,
        current=None,
        action_key=action_key,
        local_date=local_date,
        sleep_minutes=sleep_minutes,
        soreness=soreness,
        fatigue=fatigue,
        readiness=readiness,
        deleted=False,
        operation="create",
    )


async def revise_recovery(
    connection: AsyncConnection,
    checkin_id: int,
    expected_revision_id: int,
    *,
    action_key: str,
    sleep_minutes: int | None = None,
    soreness: int | None = None,
    fatigue: int | None = None,
    readiness: int | None = None,
    delete: bool = False,
) -> RecoverySnapshot:
    current = await get_recovery(connection, checkin_id)
    if current.revision_id != expected_revision_id:
        raise RecoveryError("That receipt is old. Open /recovery history for the latest one.")
    await _validate(connection, action_key)
    if delete and current.deleted:
        raise RecoveryError("That check-in is already deleted; use Undo.")
    return await _publish(
        connection,
        checkin_id=checkin_id,
        current=current,
        action_key=action_key,
        local_date=current.local_date,
        sleep_minutes=current.sleep_minutes if delete else sleep_minutes,
        soreness=current.soreness if delete else soreness,
        fatigue=current.fatigue if delete else fatigue,
        readiness=current.readiness if delete else readiness,
        deleted=True if delete else current.deleted,
        operation="delete" if delete else "edit",
    )


async def undo_recovery(
    connection: AsyncConnection, checkin_id: int, expected_revision_id: int, *, action_key: str
) -> RecoverySnapshot:
    current = await get_recovery(connection, checkin_id)
    if current.revision_id != expected_revision_id:
        raise RecoveryError("That receipt is old. Open /recovery history for the latest one.")
    await _validate(connection, action_key)
    previous_id = await connection.scalar(
        sa.select(recovery_revisions.c.previous_revision_id).where(
            recovery_revisions.c.id == current.revision_id
        )
    )
    row = (
        None
        if previous_id is None
        else (
            await connection.execute(
                sa.select(recovery_revisions).where(recovery_revisions.c.id == previous_id)
            )
        )
        .mappings()
        .one()
    )
    return await _publish(
        connection,
        checkin_id=checkin_id,
        current=current,
        action_key=action_key,
        local_date=current.local_date,
        sleep_minutes=current.sleep_minutes if row is None else row["sleep_minutes"],
        soreness=current.soreness if row is None else row["soreness"],
        fatigue=current.fatigue if row is None else row["fatigue"],
        readiness=current.readiness if row is None else row["readiness"],
        deleted=True if row is None else row["deleted"],
        operation="undo",
    )


async def recent_recovery(connection: AsyncConnection, limit: int = 10) -> list[RecoverySnapshot]:
    ids = (
        (
            await connection.execute(
                sa.select(recovery_checkins.c.id)
                .order_by(recovery_checkins.c.local_date.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [await get_recovery(connection, value) for value in ids]
