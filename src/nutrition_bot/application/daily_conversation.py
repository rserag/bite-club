import re
from datetime import date

from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.checkins import CheckinError, mark_food_day
from nutrition_bot.application.daily_report import (
    DailyRequestError,
    parse_request,
    report_for_day,
)
from nutrition_bot.application.meal_conversation import MealReply
from nutrition_bot.application.settings_service import load_preferences


def _reply(text: str, day: date, *, short: bool) -> MealReply:
    return MealReply(
        text,
        "daily_report",
        buttons=("full" if short else "short", "complete", "incomplete", "add"),
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
    preferences = await load_preferences(connection, default_timezone=timezone)
    default_short = preferences.report_length == "short"
    normalized = " ".join(text.casefold().split())
    selected: str | None = None
    if normalized in {"all food logged", "mark today complete", "/today complete"}:
        selected = "complete"
    elif normalized in {"today is incomplete", "mark today incomplete", "/today incomplete"}:
        selected = "incomplete"
    if selected:
        try:
            await mark_food_day(connection, today, selected, action_key=action_key)
            return _reply(
                await report_for_day(connection, today, timezone=timezone, short=default_short),
                today,
                short=default_short,
            )
        except CheckinError as exc:
            return MealReply(f"{exc}\nNothing changed.", "daily_rejected")
    try:
        request = parse_request(text, today=today, default_short=default_short)
    except DailyRequestError as exc:
        return MealReply(str(exc), "daily_report_help")
    if request is None:
        return None
    rendered = await report_for_day(connection, request.day, timezone=timezone, short=request.short)
    return _reply(rendered, request.day, short=request.short)


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
        if action in {"full", "short"}:
            short = action == "short"
            return _reply(
                await report_for_day(connection, day, timezone=timezone, short=short),
                day,
                short=short,
            )
        if action == "add":
            return MealReply(
                "Send a measured meal, use /foods to find an exact food, or /drafts to resume "
                "an unresolved entry. The day status was not changed.",
                "daily_add_help",
            )
        await mark_food_day(connection, day, action, action_key=action_key)
        short = (
            await load_preferences(connection, default_timezone=timezone)
        ).report_length == "short"
        return _reply(
            await report_for_day(connection, day, timezone=timezone, short=short), day, short=short
        )
    except (CheckinError, ValueError) as exc:
        return MealReply(f"{exc}\nNothing changed.", "daily_rejected")
