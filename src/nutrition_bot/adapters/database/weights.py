import time
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import actions
from nutrition_bot.adapters.database.schema_weights import body_weight_revisions, body_weights
from nutrition_bot.domain.weight_trends import DailyWeight


class WeightError(ValueError):
    pass


@dataclass(frozen=True)
class WeightSnapshot:
    id: int
    revision_id: int
    revision_number: int
    local_date: date
    measured_at: float
    timezone: str
    weight_grams: int
    timing: str
    deleted: bool
    operation: str


def _snapshot(row: sa.RowMapping) -> WeightSnapshot:
    return WeightSnapshot(
        id=row["body_weight_id"],
        revision_id=row["id"],
        revision_number=row["revision_number"],
        local_date=row["local_date"],
        measured_at=row["measured_at"],
        timezone=row["timezone"],
        weight_grams=row["weight_grams"],
        timing=row["timing"],
        deleted=row["deleted"],
        operation=row["operation"],
    )


async def _validate_action(connection: AsyncConnection, action_key: str) -> None:
    if await connection.scalar(sa.select(actions.c.key).where(actions.c.key == action_key)) is None:
        raise WeightError("This action is unavailable; send the weight again.")
    if await connection.scalar(
        sa.select(body_weight_revisions.c.id).where(
            body_weight_revisions.c.action_key == action_key
        )
    ):
        raise WeightError("This action was already applied; open the latest weight receipt.")


async def _revision(connection: AsyncConnection, revision_id: int) -> WeightSnapshot:
    row = (
        (
            await connection.execute(
                sa.select(body_weight_revisions).where(body_weight_revisions.c.id == revision_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise WeightError("That weight entry is unavailable.")
    return _snapshot(row)


async def get_weight(connection: AsyncConnection, weight_id: int) -> WeightSnapshot:
    revision_id = await connection.scalar(
        sa.select(body_weights.c.current_revision_id).where(body_weights.c.id == weight_id)
    )
    if revision_id is None:
        raise WeightError("That weight entry is unavailable. Use /weight history.")
    return await _revision(connection, revision_id)


async def _publish(
    connection: AsyncConnection,
    *,
    weight_id: int,
    current: WeightSnapshot | None,
    action_key: str,
    local_date: date,
    measured_at: float,
    timezone: str,
    weight_grams: int,
    timing: str,
    deleted: bool,
    operation: str,
) -> WeightSnapshot:
    revision_id = (
        await connection.execute(
            sa.insert(body_weight_revisions)
            .values(
                body_weight_id=weight_id,
                revision_number=current.revision_number + 1 if current else 1,
                previous_revision_id=current.revision_id if current else None,
                action_key=action_key,
                local_date=local_date,
                measured_at=measured_at,
                timezone=timezone,
                weight_grams=weight_grams,
                timing=timing,
                deleted=deleted,
                operation=operation,
                created_at=time.time(),
            )
            .returning(body_weight_revisions.c.id)
        )
    ).scalar_one()
    await connection.execute(
        sa.update(body_weights)
        .where(body_weights.c.id == weight_id)
        .values(current_revision_id=revision_id)
    )
    return await _revision(connection, revision_id)


async def create_weight(
    connection: AsyncConnection,
    *,
    action_key: str,
    source_chat_id: int,
    source_message_id: int,
    local_date: date,
    measured_at: float,
    timezone: str,
    weight_grams: int,
    timing: str,
) -> WeightSnapshot:
    await _validate_action(connection, action_key)
    if not 20000 <= weight_grams <= 500000 or timing not in {"morning", "unspecified"}:
        raise WeightError("Weight must be between 20 and 500 kg.")
    if await connection.scalar(
        sa.select(body_weights.c.id).where(
            body_weights.c.source_chat_id == source_chat_id,
            body_weights.c.source_message_id == source_message_id,
        )
    ):
        raise WeightError("That message already has a weight entry.")
    weight_id = (
        await connection.execute(
            sa.insert(body_weights)
            .values(
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                created_at=time.time(),
            )
            .returning(body_weights.c.id)
        )
    ).scalar_one()
    return await _publish(
        connection,
        weight_id=weight_id,
        current=None,
        action_key=action_key,
        local_date=local_date,
        measured_at=measured_at,
        timezone=timezone,
        weight_grams=weight_grams,
        timing=timing,
        deleted=False,
        operation="create",
    )


async def revise_weight(
    connection: AsyncConnection,
    weight_id: int,
    expected_revision_id: int,
    *,
    action_key: str,
    weight_grams: int | None = None,
    local_date: date | None = None,
    timing: str | None = None,
    delete: bool = False,
) -> WeightSnapshot:
    current = await get_weight(connection, weight_id)
    if current.revision_id != expected_revision_id:
        raise WeightError("That receipt is old. Open the latest entry with /weight history.")
    await _validate_action(connection, action_key)
    if delete and current.deleted:
        raise WeightError("That entry is already deleted; use Undo.")
    chosen_weight = current.weight_grams if weight_grams is None else weight_grams
    chosen_timing = current.timing if timing is None else timing
    if not 20000 <= chosen_weight <= 500000 or chosen_timing not in {"morning", "unspecified"}:
        raise WeightError("Weight must be between 20 and 500 kg.")
    return await _publish(
        connection,
        weight_id=weight_id,
        current=current,
        action_key=action_key,
        local_date=current.local_date if local_date is None else local_date,
        measured_at=current.measured_at,
        timezone=current.timezone,
        weight_grams=chosen_weight,
        timing=chosen_timing,
        deleted=True if delete else current.deleted,
        operation="delete" if delete else "edit",
    )


async def undo_weight(
    connection: AsyncConnection, weight_id: int, expected_revision_id: int, *, action_key: str
) -> WeightSnapshot:
    current = await get_weight(connection, weight_id)
    if current.revision_id != expected_revision_id:
        raise WeightError("That receipt is old. Open the latest entry with /weight history.")
    await _validate_action(connection, action_key)
    previous_id = await connection.scalar(
        sa.select(body_weight_revisions.c.previous_revision_id).where(
            body_weight_revisions.c.id == current.revision_id
        )
    )
    previous = current if previous_id is None else await _revision(connection, previous_id)
    return await _publish(
        connection,
        weight_id=weight_id,
        current=current,
        action_key=action_key,
        local_date=previous.local_date,
        measured_at=previous.measured_at,
        timezone=previous.timezone,
        weight_grams=previous.weight_grams,
        timing=previous.timing,
        deleted=True if previous_id is None else previous.deleted,
        operation="undo",
    )


async def daily_weights(
    connection: AsyncConnection, *, start: date, end: date
) -> list[DailyWeight]:
    rows = (
        (
            await connection.execute(
                sa.select(body_weight_revisions)
                .join(
                    body_weights, body_weights.c.current_revision_id == body_weight_revisions.c.id
                )
                .where(
                    body_weight_revisions.c.deleted.is_(False),
                    body_weight_revisions.c.local_date.between(start, end),
                )
                .order_by(body_weight_revisions.c.local_date, body_weight_revisions.c.id)
            )
        )
        .mappings()
        .all()
    )
    result: list[DailyWeight] = []
    for day in sorted({row["local_date"] for row in rows}):
        day_rows = [row for row in rows if row["local_date"] == day]
        morning = [row for row in day_rows if row["timing"] == "morning"]
        chosen = morning or day_rows
        values = sorted(row["weight_grams"] for row in chosen)
        middle = len(values) // 2
        grams = (
            Fraction(values[middle])
            if len(values) % 2
            else Fraction(values[middle - 1] + values[middle], 2)
        )
        result.append(DailyWeight(day, grams, len(day_rows), bool(morning)))
    return result


async def recent_weights(connection: AsyncConnection, limit: int = 10) -> list[WeightSnapshot]:
    ids = (
        (
            await connection.execute(
                sa.select(body_weights.c.id).order_by(body_weights.c.id.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [await get_weight(connection, weight_id) for weight_id in ids]
