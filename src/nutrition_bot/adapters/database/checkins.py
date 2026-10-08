import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import actions, meal_revisions, meals
from nutrition_bot.adapters.database.schema_checkins import daily_checkin_events
from nutrition_bot.adapters.database.schema_drafts import meal_drafts


class CheckinError(ValueError):
    pass


@dataclass(frozen=True)
class FoodDayStatus:
    state: str
    event_id: int | None
    unresolved_drafts: int
    changed_since_complete: bool


async def _open_drafts(connection: AsyncConnection, local_date: date) -> int:
    rows: Sequence[object] = (
        (
            await connection.execute(
                sa.select(meal_drafts.c.content).where(meal_drafts.c.state == "open")
            )
        )
        .scalars()
        .all()
    )
    return sum(
        1
        for content in rows
        if isinstance(content, dict) and content.get("local_date") == local_date.isoformat()
    )


async def food_day_status(connection: AsyncConnection, local_date: date) -> FoodDayStatus:
    event = (
        (
            await connection.execute(
                sa.select(daily_checkin_events)
                .where(daily_checkin_events.c.local_date == local_date)
                .order_by(daily_checkin_events.c.id.desc())
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    drafts = await _open_drafts(connection, local_date)
    if event is None:
        return FoodDayStatus("unknown", None, drafts, False)
    changed = False
    if event["food_status"] == "complete":
        changed = bool(
            await connection.scalar(
                sa.select(sa.func.count())
                .select_from(
                    meals.join(
                        meal_revisions, meals.c.current_revision_id == meal_revisions.c.id
                    ).join(actions, actions.c.key == meal_revisions.c.action_key)
                )
                .where(
                    meal_revisions.c.local_date == local_date,
                    actions.c.created_at > event["created_at"],
                )
            )
        )
    state = event["food_status"]
    if state == "complete" and (drafts or changed):
        state = "unknown"
    return FoodDayStatus(state, event["id"], drafts, changed)


async def mark_food_day(
    connection: AsyncConnection,
    local_date: date,
    status: str,
    *,
    action_key: str,
) -> FoodDayStatus:
    if status not in {"complete", "incomplete"}:
        raise CheckinError("Choose complete or incomplete.")
    if await connection.scalar(sa.select(actions.c.key).where(actions.c.key == action_key)) is None:
        raise CheckinError("This action is unavailable; open /today again.")
    if status == "complete" and await _open_drafts(connection, local_date):
        raise CheckinError(
            "This date has an unresolved meal draft. Resolve or cancel it before marking "
            "all food logged."
        )
    await connection.execute(
        sa.insert(daily_checkin_events).values(
            local_date=local_date,
            food_status=status,
            action_key=action_key,
            created_at=time.time(),
        )
    )
    return await food_day_status(connection, local_date)
