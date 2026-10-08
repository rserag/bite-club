"""Explicit preference changes without model calls or guessed personal schedules."""

import re
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.application.meal_conversation import MealReply
from nutrition_bot.application.settings_service import (
    CATEGORIES,
    WEEKDAYS,
    Preferences,
    ScheduleRule,
    SettingsError,
    load_preferences,
    next_notifications,
    parse_minute,
    save_preferences,
    settings_receipt,
    validate_timezone,
)

HELP = (
    "Choose what suits your day; each reminder starts off.\n"
    "• /settings timezone Europe/Paris\n"
    "• /settings report short (or full)\n"
    "• /settings weight 08:00 on\n"
    "• /settings recovery 08:05 on\n"
    "• /settings evening 20:30 on\n"
    "• /settings weekly Monday 09:00 on\n"
    "• /settings training_pre on 120 (minutes before planned start)\n"
    "• /settings training_post on 15 (minutes after planned end)\n"
    "• /settings quiet 23:00 07:00 (or quiet off)\n"
    "Disable one category: /settings weight off. Disable all: /settings reminders off.\n"
    "You can also say 'set timezone Europe/Paris', 'remind me to weigh at 08:00', "
    "or 'disable recovery reminders'. Training times are editable through /plan weekly "
    "and /plan; weekday-only plans do not imply a start time. "
    "All times here are examples, not your saved preferences."
)


def _tokens(text: str) -> list[str] | None:
    normalized = " ".join(text.strip().split())
    tokens = normalized.split()
    if tokens and tokens[0].casefold() == "/settings":
        return tokens[1:]
    patterns = (
        (r"set (?:my )?timezone (\S+)", "timezone"),
        (r"(?:set (?:my )?)?reports? (short|full)", "report"),
        (r"remind me to weigh at (\S+)", "weight"),
        (r"remind me (?:to check )?recovery at (\S+)", "recovery"),
        (r"(?:set )?evening summary at (\S+)", "evening"),
    )
    for pattern, category in patterns:
        match = re.fullmatch(pattern, normalized, re.IGNORECASE)
        if match:
            return [category, match[1]] + (["on"] if category in CATEGORIES else [])
    match = re.fullmatch(
        r"disable (weight|recovery|evening|weekly|training_pre|training_post) reminders",
        normalized,
        re.IGNORECASE,
    )
    if match:
        return [match[1].casefold(), "off"]
    match = re.fullmatch(
        r"(?:set )?weekly (?:summary|review) (\S+) (?:at )?(\S+)",
        normalized,
        re.IGNORECASE,
    )
    if match:
        return ["weekly", match[1], match[2], "on"]
    match = re.fullmatch(r"(?:set )?quiet hours (\S+) (?:to )?(\S+)", normalized, re.IGNORECASE)
    if match:
        return ["quiet", match[1], match[2]]
    if normalized.casefold() in {"disable all reminders", "turn off all reminders"}:
        return ["reminders", "off"]
    if normalized.casefold() in {"show my settings", "my settings", "settings"}:
        return []
    return None


def _changed(preferences: Preferences, tokens: list[str]) -> Preferences:
    category, *arguments = tokens
    category = category.casefold().replace("-", "_")
    if category == "timezone" and len(arguments) == 1:
        return replace(preferences, timezone=validate_timezone(arguments[0]))
    if category in {"report", "reports"} and len(arguments) == 1:
        if arguments[0].casefold() not in {"short", "full"}:
            raise SettingsError("Choose short or full reports.")
        return replace(preferences, report_length=arguments[0].casefold())
    if category == "quiet":
        if len(arguments) == 1 and arguments[0].casefold() == "off":
            return replace(preferences, quiet_start_minute=None, quiet_end_minute=None)
        if len(arguments) == 2:
            start, end = (parse_minute(value) for value in arguments)
            if start == end:
                raise SettingsError("Quiet hours need two distinct times, or off.")
            return replace(preferences, quiet_start_minute=start, quiet_end_minute=end)
    if category == "reminders" and [value.casefold() for value in arguments] == ["off"]:
        return replace(
            preferences,
            rules=tuple(replace(preferences.rule(name), enabled=False) for name in CATEGORIES),
        )
    if category not in CATEGORIES:
        raise SettingsError("Choose a setting from /settings setup.")
    previous = preferences.rule(category)
    lowered = [value.casefold() for value in arguments]
    rule: ScheduleRule
    if lowered == ["off"]:
        rule = replace(previous, enabled=False)
    elif lowered == ["on"]:
        rule = replace(previous, enabled=True)
    elif category.startswith("training_") and len(lowered) == 2 and lowered[0] in {"on", "off"}:
        if not lowered[1].isascii() or not lowered[1].isdigit():
            raise SettingsError("Choose a whole number of minutes for the training offset.")
        offset = int(lowered[1]) * (-1 if category == "training_pre" else 1)
        rule = replace(previous, enabled=lowered[0] == "on", offset_minutes=offset)
    elif category == "weekly" and len(lowered) in {2, 3}:
        weekdays = {name.casefold(): index for index, name in enumerate(WEEKDAYS)}
        if lowered[0] not in weekdays or (len(lowered) == 3 and lowered[2] not in {"on", "off"}):
            raise SettingsError(
                "Use a full weekday and 24-hour time: /settings weekly Monday 09:00 on."
            )
        rule = replace(
            previous,
            weekday=weekdays[lowered[0]],
            local_minute=parse_minute(lowered[1]),
            enabled=len(lowered) == 2 or lowered[2] == "on",
        )
    elif category in {"weight", "recovery", "evening"} and len(lowered) in {1, 2}:
        if len(lowered) == 2 and lowered[1] not in {"on", "off"}:
            raise SettingsError("Use a 24-hour time followed by on or off.")
        rule = replace(
            previous,
            local_minute=parse_minute(lowered[0]),
            enabled=len(lowered) == 1 or lowered[1] == "on",
        )
    else:
        raise SettingsError("Use /settings setup for that option's exact format.")
    return replace(
        preferences,
        rules=tuple([item for item in preferences.rules if item.category != category] + [rule]),
    )


async def handle_settings_message(
    connection: AsyncConnection,
    text: str,
    *,
    action_key: str,
    now: float,
    default_timezone: str = "UTC",
) -> MealReply | None:
    tokens = _tokens(text)
    if tokens is None:
        return None
    preferences = await load_preferences(connection, default_timezone=default_timezone)
    if not tokens:
        return MealReply(
            settings_receipt(
                preferences,
                now=now,
                next_due_by_category=await next_notifications(connection, preferences, now=now),
            ),
            "settings_view",
        )
    if len(tokens) == 1 and tokens[0].casefold() in {"setup", "help"}:
        return MealReply(HELP, "settings_help")
    try:
        changed = _changed(preferences, tokens)
        saved = await save_preferences(connection, changed, action_key=action_key, now=now)
    except SettingsError as exc:
        return MealReply(
            f"{exc}\nNothing changed. Use /settings setup for the options.", "settings_rejected"
        )
    return MealReply(
        settings_receipt(
            saved,
            now=now,
            saved=True,
            next_due_by_category=await next_notifications(connection, saved, now=now),
        ),
        "settings_saved",
    )
