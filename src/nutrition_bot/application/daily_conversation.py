import re
from datetime import date

from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.checkins import CheckinError, mark_food_day
from nutrition_bot.application.daily_report import (
    DailyRequestError,
    parse_request,
    report_for_day,
    report_for_message,
)
from nutrition_bot.application.meal_conversation import MealReply


def _reply(text: str, day: date) -> MealReply:
    return MealReply(
        text,
        "daily_report",
        buttons=("complete", "incomplete", "add"),
        daily_date=day.isoformat(),
    )


async def handle_daily_message(
    connection: AsyncConnection,
    text: str,
    *,
    action_key: str,
    today: date,
    timezone: str,
) -> MealReply | None:
    normalized = " ".join(text.casefold().split())
    selected: str | None = None
    if normalized in {"all food logged", "mark today complete", "/today complete"}:
        selected = "complete"
    elif normalized in {"today is incomplete", "mark today incomplete", "/today incomplete"}:
        selected = "incomplete"
    if selected:
        try:
            await mark_food_day(connection, today, selected, action_key=action_key)
            return _reply(await report_for_day(connection, today, timezone=timezone), today)
        except CheckinError as exc:
            return MealReply(f"{exc}\nNothing changed.", "daily_rejected")
    try:
        request = parse_request(text, today=today)
        rendered = await report_for_message(connection, text, today=today, timezone=timezone)
    except DailyRequestError as exc:
        return MealReply(str(exc), "daily_report_help")
    if rendered is None or request is None:
        return None
    return _reply(rendered, request.day)


async def handle_daily_callback(
    connection: AsyncConnection,
    action: str,
    day_text: str,
    *,
    action_key: str,
    timezone: str,
) -> MealReply:
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day_text) is None:
            raise CheckinError("That report date is invalid. Open /today again.")
        day = date.fromisoformat(day_text)
        if action == "add":
            return MealReply(
                "Send a measured meal, use /foods to find an exact food, or /drafts to resume "
                "an unresolved entry. The day status was not changed.",
                "daily_add_help",
            )
        await mark_food_day(connection, day, action, action_key=action_key)
        return _reply(await report_for_day(connection, day, timezone=timezone), day)
    except (CheckinError, ValueError) as exc:
        return MealReply(f"{exc}\nNothing changed.", "daily_rejected")
