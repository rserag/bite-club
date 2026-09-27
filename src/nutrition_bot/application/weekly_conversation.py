from datetime import date

from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.application.meal_conversation import MealReply
from nutrition_bot.application.weekly_report import (
    WeeklyRequest,
    WeeklyRequestError,
    build_weekly_report,
    parse_week_request,
)


def _reply(text: str, request: WeeklyRequest) -> MealReply:
    return MealReply(
        text,
        "weekly_report",
        buttons=("full",) if request.short else ("short",),
        weekly_end=request.end.isoformat(),
    )


async def handle_weekly_message(
    connection: AsyncConnection, text: str, *, today: date
) -> MealReply | None:
    try:
        request = parse_week_request(text, today=today)
    except WeeklyRequestError as exc:
        return MealReply(str(exc), "weekly_report_help")
    if request is None:
        return None
    return _reply(await build_weekly_report(connection, request), request)


async def handle_weekly_callback(
    connection: AsyncConnection, action: str, end_text: str
) -> MealReply:
    try:
        end = date.fromisoformat(end_text)
    except ValueError:
        return MealReply("That report date is invalid. Open /week again.", "weekly_report_help")
    request = WeeklyRequest(end, short=action == "short")
    return _reply(await build_weekly_report(connection, request), request)
