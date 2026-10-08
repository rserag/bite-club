"""Deterministic local-time preferences; environment values only seed new profiles."""

from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.bjj_plans import day_plan
from nutrition_bot.adapters.database.schema import actions, outbox, profile
from nutrition_bot.adapters.database.schema_recovery import recovery_checkins, recovery_revisions
from nutrition_bot.adapters.database.schema_settings import (
    notification_settings,
    reminder_deliveries,
    reminder_jobs,
    schedule_rules,
    settings_events,
)
from nutrition_bot.adapters.database.schema_weights import body_weight_revisions, body_weights

CATEGORIES = ("weight", "recovery", "evening", "weekly", "training_pre", "training_post")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class SettingsError(ValueError):
    """An invalid preference; safe to display without echoing arbitrary input."""


@dataclass(frozen=True)
class ScheduleRule:
    category: str
    enabled: bool = False
    local_minute: int | None = None
    weekday: int | None = None
    offset_minutes: int | None = None


@dataclass(frozen=True)
class Preferences:
    timezone: str
    revision: int = 0
    report_length: str = "short"
    quiet_start_minute: int | None = None
    quiet_end_minute: int | None = None
    updated_at: float = 0
    rules: tuple[ScheduleRule, ...] = ()

    def rule(self, category: str) -> ScheduleRule:
        return next(
            (rule for rule in self.rules if rule.category == category),
            ScheduleRule(
                category,
                offset_minutes=-120
                if category == "training_pre"
                else 15
                if category == "training_post"
                else None,
            ),
        )


def parse_minute(value: str) -> int:
    parts = value.split(":")
    if (
        len(parts) != 2
        or len(parts[0]) != 2
        or len(parts[1]) != 2
        or not all(part.isascii() and part.isdigit() for part in parts)
        or not 0 <= int(parts[0]) <= 23
        or not 0 <= int(parts[1]) <= 59
    ):
        raise SettingsError("Use an unambiguous 24-hour time, HH:MM, such as 08:00 or 20:30.")
    return int(parts[0]) * 60 + int(parts[1])


def clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise SettingsError("Use an IANA timezone, such as Europe/Paris or UTC.") from None
    return value


def wall_time(day: date, minute: int, timezone: str) -> datetime:
    """One occurrence per logical date: earliest fold; spring gaps move forward."""
    zone = ZoneInfo(timezone)
    naive = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minute)
    for _ in range(181):
        selected = naive.replace(tzinfo=zone, fold=0)
        if selected.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive:
            return selected.astimezone(UTC)
        naive += timedelta(minutes=1)
    raise SettingsError("That local clock time cannot be resolved in this timezone.")


def in_quiet_hours(preferences: Preferences, instant: float) -> bool:
    start, end = preferences.quiet_start_minute, preferences.quiet_end_minute
    if start is None or end is None:
        return False
    local = datetime.fromtimestamp(instant, ZoneInfo(preferences.timezone))
    minute = local.hour * 60 + local.minute
    return start <= minute < end if start < end else minute >= start or minute < end


async def load_preferences(
    connection: AsyncConnection, *, default_timezone: str = "UTC"
) -> Preferences:
    timezone = await connection.scalar(sa.select(profile.c.timezone).where(profile.c.id == 1))
    row = (
        (
            await connection.execute(
                sa.select(notification_settings).where(notification_settings.c.id == 1)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return Preferences(timezone or default_timezone)
    rules = await connection.execute(sa.select(schedule_rules).order_by(schedule_rules.c.category))
    return Preferences(
        timezone or default_timezone,
        revision=row["revision"],
        report_length=row["report_length"],
        quiet_start_minute=row["quiet_start_minute"],
        quiet_end_minute=row["quiet_end_minute"],
        updated_at=row["updated_at"],
        rules=tuple(ScheduleRule(**dict(rule)) for rule in rules.mappings()),
    )


async def save_preferences(
    connection: AsyncConnection,
    preferences: Preferences,
    *,
    action_key: str,
    now: float,
) -> Preferences:
    if await connection.scalar(sa.select(actions.c.key).where(actions.c.key == action_key)) is None:
        raise SettingsError("Send that settings change again; the original action is unavailable.")
    validate_timezone(preferences.timezone)
    if preferences.report_length not in {"short", "full"}:
        raise SettingsError("Choose short or full reports.")
    start, end = preferences.quiet_start_minute, preferences.quiet_end_minute
    if (start is None) != (end is None) or (
        start is not None
        and end is not None
        and (not 0 <= start <= 1439 or not 0 <= end <= 1439 or start == end)
    ):
        raise SettingsError("Quiet hours need two distinct 24-hour times, or off.")
    if len({rule.category for rule in preferences.rules}) != len(preferences.rules):
        raise SettingsError("Choose one schedule for each reminder category.")
    for rule in preferences.rules:
        _validate_rule(rule)
    previous = await load_preferences(connection)
    saved = replace(preferences, revision=previous.revision + 1, updated_at=now)
    await connection.execute(
        insert(profile)
        .values(id=1, timezone=saved.timezone, created_at=now)
        .on_conflict_do_update(index_elements=["id"], set_={"timezone": saved.timezone})
    )
    values = {
        key: value for key, value in asdict(saved).items() if key not in {"timezone", "rules"}
    }
    await connection.execute(
        insert(notification_settings)
        .values(id=1, **values)
        .on_conflict_do_update(index_elements=["id"], set_=values)
    )
    await connection.execute(sa.delete(schedule_rules))
    if saved.rules:
        await connection.execute(sa.insert(schedule_rules), [asdict(rule) for rule in saved.rules])
    await connection.execute(
        sa.insert(settings_events).values(
            revision=saved.revision, action_key=action_key, snapshot=asdict(saved), created_at=now
        )
    )
    await connection.execute(
        sa.update(reminder_jobs)
        .where(
            reminder_jobs.c.logical_key.in_(
                sa.select(reminder_deliveries.c.logical_key)
                .join(outbox, outbox.c.id == reminder_deliveries.c.outbox_id)
                .where(outbox.c.status == "sent")
            )
        )
        .values(status="sent", reason=None)
    )
    # Keep sent identities permanently. Old unsent payloads must never escape a preference edit.
    affected = sa.select(reminder_jobs.c.outbox_id).where(
        reminder_jobs.c.status.in_(["scheduled", "queued"]),
        reminder_jobs.c.outbox_id.is_not(None),
    )
    await connection.execute(
        sa.update(outbox)
        .where(outbox.c.id.in_(affected), outbox.c.status.in_(["queued", "sending"]))
        .values(status="failed", payload=None, error_type="SettingsChanged")
    )
    await connection.execute(
        sa.update(reminder_jobs)
        .where(reminder_jobs.c.status.in_(["scheduled", "queued"]))
        .values(status="suppressed", reason="SettingsChanged")
    )
    return saved


def _validate_rule(rule: ScheduleRule) -> None:
    if rule.category not in CATEGORIES or type(rule.enabled) is not bool:
        raise SettingsError(
            "Choose weight, recovery, evening, weekly, training_pre or training_post."
        )
    if rule.local_minute is not None and not 0 <= rule.local_minute <= 1439:
        raise SettingsError("Use a valid 24-hour reminder time.")
    if rule.weekday is not None and not 0 <= rule.weekday <= 6:
        raise SettingsError("Choose a weekday from Monday to Sunday.")
    if rule.category.startswith("training_"):
        if rule.local_minute is not None or rule.weekday is not None or rule.offset_minutes is None:
            raise SettingsError("Training reminders use an offset from a known planned session.")
        if rule.category == "training_pre" and not -1440 <= rule.offset_minutes <= -1:
            raise SettingsError(
                "Preparation reminders need 1–1440 minutes before the planned start."
            )
        if rule.category == "training_post" and not 0 <= rule.offset_minutes <= 1440:
            raise SettingsError("Logging reminders need 0–1440 minutes after the planned end.")
    elif rule.offset_minutes is not None or (
        rule.category != "weekly" and rule.weekday is not None
    ):
        raise SettingsError("That reminder needs a local clock time.")
    elif rule.enabled and (
        rule.local_minute is None or (rule.category == "weekly" and rule.weekday is None)
    ):
        raise SettingsError(
            "Set a clock time (and a weekday for weekly reviews) before enabling it."
        )


def next_clock_notification(
    preferences: Preferences, rule: ScheduleRule, now: float
) -> datetime | None:
    if not rule.enabled or rule.local_minute is None:
        return None
    day = datetime.fromtimestamp(now, ZoneInfo(preferences.timezone)).date()
    for offset in range(9):
        selected_day = day + timedelta(days=offset)
        if rule.weekday is not None and selected_day.weekday() != rule.weekday:
            continue
        candidate = wall_time(selected_day, rule.local_minute, preferences.timezone)
        if candidate.timestamp() >= now and not in_quiet_hours(preferences, candidate.timestamp()):
            return candidate
    return None


async def next_notifications(
    connection: AsyncConnection, preferences: Preferences, *, now: float
) -> dict[str, datetime | None]:
    result: dict[str, datetime | None] = {}
    day = datetime.fromtimestamp(now, ZoneInfo(preferences.timezone)).date()
    delivered: set[str] = set(
        (
            await connection.execute(
                sa.select(reminder_jobs.c.logical_key)
                .outerjoin(
                    reminder_deliveries,
                    reminder_deliveries.c.logical_key == reminder_jobs.c.logical_key,
                )
                .outerjoin(outbox, outbox.c.id == reminder_deliveries.c.outbox_id)
                .where(
                    sa.or_(reminder_jobs.c.status == "sent", outbox.c.status == "sent"),
                    reminder_jobs.c.local_date >= day - timedelta(days=1),
                    reminder_jobs.c.local_date <= day + timedelta(days=8),
                )
            )
        ).scalars()
    )
    completed_days: dict[str, set[date]] = {
        "weight": set(
            (
                await connection.execute(
                    sa.select(body_weight_revisions.c.local_date)
                    .join(
                        body_weights,
                        body_weights.c.current_revision_id == body_weight_revisions.c.id,
                    )
                    .where(
                        body_weight_revisions.c.deleted.is_(False),
                        body_weight_revisions.c.local_date >= day,
                        body_weight_revisions.c.local_date <= day + timedelta(days=8),
                    )
                )
            ).scalars()
        ),
        "recovery": set(
            (
                await connection.execute(
                    sa.select(recovery_checkins.c.local_date)
                    .join(
                        recovery_revisions,
                        recovery_checkins.c.current_revision_id == recovery_revisions.c.id,
                    )
                    .where(
                        recovery_revisions.c.deleted.is_(False),
                        recovery_checkins.c.local_date >= day,
                        recovery_checkins.c.local_date <= day + timedelta(days=8),
                    )
                )
            ).scalars()
        ),
    }
    for rule in preferences.rules:
        if not rule.enabled or rule.local_minute is None:
            continue
        upcoming_clock = []
        for offset in range(9):
            selected = day + timedelta(days=offset)
            if (
                (rule.weekday is not None and selected.weekday() != rule.weekday)
                or f"{rule.category}:{selected.isoformat()}" in delivered
                or selected in completed_days.get(rule.category, set())
            ):
                continue
            due = wall_time(selected, rule.local_minute, preferences.timezone)
            if due.timestamp() >= now and not in_quiet_hours(preferences, due.timestamp()):
                upcoming_clock.append(due)
        result[rule.category] = min(upcoming_clock) if upcoming_clock else None
    for category in ("training_pre", "training_post"):
        rule = preferences.rule(category)
        if not rule.enabled or rule.offset_minutes is None:
            continue
        upcoming: list[datetime] = []
        for offset in range(-1, 9):
            planned_day = day + timedelta(days=offset)
            plan = await day_plan(connection, planned_day)
            if plan.state != "planned":
                continue
            for activity in plan.activities:
                if (
                    activity.start_minute is None
                    or activity.duration_minutes is None
                    or activity.kind in plan.completed
                    or f"{category}:{planned_day.isoformat()}:{activity.kind}" in delivered
                ):
                    continue
                start = wall_time(planned_day, activity.start_minute, preferences.timezone)
                due = start + timedelta(minutes=rule.offset_minutes)
                if category == "training_post":
                    due += timedelta(minutes=activity.duration_minutes)
                if due.timestamp() >= now and not in_quiet_hours(preferences, due.timestamp()):
                    upcoming.append(due)
        result[category] = min(upcoming) if upcoming else None
    return result


def settings_receipt(
    preferences: Preferences,
    *,
    now: float,
    saved: bool = False,
    next_due_by_category: dict[str, datetime | None] | None = None,
) -> str:
    lines = [
        "Settings saved." if saved else "Your settings",
        f"Timezone: {preferences.timezone}",
        f"Reports: {preferences.report_length}",
    ]
    if preferences.quiet_start_minute is None or preferences.quiet_end_minute is None:
        lines.append("Quiet hours: off")
    else:
        lines.append(
            f"Quiet hours: {clock(preferences.quiet_start_minute)}–"
            f"{clock(preferences.quiet_end_minute)}"
        )
    for category in CATEGORIES:
        rule = preferences.rule(category)
        label = category.replace("_", " ")
        description = "off"
        if rule.enabled:
            if rule.offset_minutes is not None:
                anchor = (
                    "before planned start" if category == "training_pre" else "after planned end"
                )
                description = (
                    f"{abs(rule.offset_minutes)} min {anchor}; only when a start time is known"
                )
                if next_due_by_category is not None:
                    next_training = next_due_by_category.get(category)
                    if next_training is not None:
                        local = next_training.astimezone(ZoneInfo(preferences.timezone))
                        description += f" · next {local:%a %d %b %H:%M %Z}"
                    else:
                        description += " · no eligible timed plan in the next eight days"
            else:
                assert rule.local_minute is not None
                description = clock(rule.local_minute)
                if rule.weekday is not None:
                    description = f"{WEEKDAYS[rule.weekday]} {description}"
                next_due = (
                    next_due_by_category.get(category)
                    if next_due_by_category is not None
                    else next_clock_notification(preferences, rule, now)
                )
                if next_due:
                    next_local = next_due.astimezone(ZoneInfo(preferences.timezone))
                    description += f" · next {next_local:%a %d %b %H:%M %Z}"
                else:
                    description += " · blocked by quiet hours"
        lines.append(f"{label.capitalize()}: {description}")
    lines.append("Use /settings setup for editable options. Reminders never create diary entries.")
    if saved:
        lines.append(
            "Unsent reminders were recomputed; historical dates and timestamps stay unchanged."
        )
    return "\n".join(lines)
