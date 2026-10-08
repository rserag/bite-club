"""Opt-in local schedules with stable identities, bounded expiry and sender-time checks."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.bjj_plans import day_plan
from nutrition_bot.adapters.database.schema import actions, inbox, outbox
from nutrition_bot.adapters.database.schema_recovery import recovery_checkins, recovery_revisions
from nutrition_bot.adapters.database.schema_settings import reminder_deliveries, reminder_jobs
from nutrition_bot.adapters.database.schema_weights import body_weight_revisions, body_weights
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.application.daily_report import report_for_day
from nutrition_bot.application.settings_service import (
    Preferences,
    ScheduleRule,
    in_quiet_hours,
    load_preferences,
    wall_time,
)
from nutrition_bot.application.weekly_report import WeeklyRequest, build_weekly_report
from nutrition_bot.config import BotSettings

FRESHNESS_SECONDS = {
    "weight": 3600,
    "recovery": 3600,
    "evening": 7200,
    "weekly": 21600,
    "training_pre": 1800,
    "training_post": 3600,
}


@dataclass(frozen=True)
class Candidate:
    logical_key: str
    category: str
    local_date: date
    due_at: float
    expires_at: float
    session_kind: str | None = None
    plan_fingerprint: str | None = None


async def candidates_for_day(
    connection: AsyncConnection, preferences: Preferences, day: date
) -> tuple[Candidate, ...]:
    result: list[Candidate] = []
    for rule in preferences.rules:
        if not rule.enabled:
            continue
        if rule.category.startswith("training_"):
            result.extend(await _training_candidates(connection, preferences, rule, day))
            continue
        if rule.local_minute is None or (
            rule.weekday is not None and day.weekday() != rule.weekday
        ):
            continue
        due = wall_time(day, rule.local_minute, preferences.timezone).timestamp()
        result.append(
            Candidate(
                f"{rule.category}:{day.isoformat()}",
                rule.category,
                day,
                due,
                due + FRESHNESS_SECONDS[rule.category],
            )
        )
    return tuple(result)


async def _training_candidates(
    connection: AsyncConnection, preferences: Preferences, rule: ScheduleRule, day: date
) -> list[Candidate]:
    plan = await day_plan(connection, day)
    if plan.state != "planned" or rule.offset_minutes is None:
        return []
    result = []
    for activity in plan.activities:
        if (
            activity.kind is None
            or activity.start_minute is None
            or activity.duration_minutes is None
        ):
            continue
        start = wall_time(day, activity.start_minute, preferences.timezone).timestamp()
        anchor = (
            start if rule.category == "training_pre" else start + activity.duration_minutes * 60
        )
        due = anchor + rule.offset_minutes * 60
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "timezone": preferences.timezone,
                    "kind": activity.kind,
                    "start": activity.start_minute,
                    "duration": activity.duration_minutes,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        expires = due + FRESHNESS_SECONDS[rule.category]
        if rule.category == "training_pre":
            expires = min(expires, start)
        result.append(
            Candidate(
                f"{rule.category}:{day.isoformat()}:{activity.kind}",
                rule.category,
                day,
                due,
                expires,
                activity.kind,
                fingerprint,
            )
        )
    return result


async def _completed(connection: AsyncConnection, candidate: Candidate) -> bool:
    if candidate.category == "weight":
        return bool(
            await connection.scalar(
                sa.select(body_weight_revisions.c.id)
                .join(
                    body_weights, body_weights.c.current_revision_id == body_weight_revisions.c.id
                )
                .where(
                    body_weight_revisions.c.local_date == candidate.local_date,
                    body_weight_revisions.c.deleted.is_(False),
                )
                .limit(1)
            )
        )
    if candidate.category == "recovery":
        return bool(
            await connection.scalar(
                sa.select(recovery_revisions.c.id)
                .join(
                    recovery_checkins,
                    recovery_checkins.c.current_revision_id == recovery_revisions.c.id,
                )
                .where(
                    recovery_checkins.c.local_date == candidate.local_date,
                    recovery_revisions.c.deleted.is_(False),
                )
                .limit(1)
            )
        )
    if candidate.category.startswith("training_"):
        plan = await day_plan(connection, candidate.local_date)
        return candidate.session_kind in plan.completed
    return False


async def render_notification(
    connection: AsyncConnection, candidate: Candidate, preferences: Preferences
) -> str:
    if candidate.category == "weight":
        return (
            f"Weight check-in · {candidate.local_date:%d %b}\n"
            "If you measured your weight, send 'weight 79.4' with your actual value. "
            "Skip if you did not measure. /settings changes this reminder."
        )
    if candidate.category == "recovery":
        return (
            f"Recovery check-in · {candidate.local_date:%d %b}\n"
            "How was your sleep and readiness? Use /recovery to log only what you know. "
            "/settings changes this reminder."
        )
    if candidate.category == "evening":
        return "Evening summary\n" + await report_for_day(
            connection,
            candidate.local_date,
            timezone=preferences.timezone,
            short=preferences.report_length == "short",
        )
    if candidate.category == "weekly":
        # The previous completed Monday–Sunday week, regardless of the chosen review weekday.
        end = candidate.local_date - timedelta(days=candidate.local_date.weekday() + 1)
        return "Weekly review · previous completed local week\n" + await build_weekly_report(
            connection, WeeklyRequest(end, short=preferences.report_length == "short")
        )
    kind = (candidate.session_kind or "training").upper()
    if candidate.category == "training_pre":
        return (
            f"{kind} preparation · {candidate.local_date:%d %b}\n"
            "A session is planned soon. Check /today for recorded intake "
            "and your reviewed targets. "
            "The plan does not confirm attendance. /settings changes this reminder."
        )
    plan = await day_plan(connection, candidate.local_date)
    recovery_done = await _completed(
        connection, Candidate("", "recovery", candidate.local_date, 0, 1)
    )
    recovery = "" if recovery_done else " You can also record how you feel with /recovery."
    return (
        f"{kind} logging check-in · {candidate.local_date:%d %b}\n"
        f"Did the planned session happen? Use /{(candidate.session_kind or 'bjj')} "
        "with your actual "
        "duration and effort, or update /plan if it was cancelled. "
        f"Planned end times are approximate.{recovery} "
        f"{'' if plan.completed else 'Nothing has been logged by this reminder. '}"
        "/settings changes it."
    )


def _candidate(row: RowMapping) -> Candidate:
    return Candidate(
        row["logical_key"],
        row["category"],
        row["local_date"],
        row["due_at"],
        row["expires_at"],
        row["session_kind"],
        row["plan_fingerprint"],
    )


async def _reason(
    connection: AsyncConnection,
    row: RowMapping,
    preferences: Preferences,
    settings: BotSettings,
    now: float,
) -> str | None:
    if (
        row["owner_user_id"] != settings.allowed_telegram_user_id
        or row["chat_id"] != settings.allowed_telegram_chat_id
    ):
        return "AuthorizationChanged"
    if row["settings_revision"] != preferences.revision:
        return "SettingsChanged"
    if row["expires_at"] <= now:
        return "ExpiredReminder"
    if not preferences.rule(row["category"]).enabled:
        return "CategoryDisabled"
    if in_quiet_hours(preferences, now):
        return "QuietHours"
    candidate = _candidate(row)
    current = next(
        (
            value
            for value in await candidates_for_day(connection, preferences, candidate.local_date)
            if value.logical_key == candidate.logical_key
        ),
        None,
    )
    if (
        current is None
        or current.due_at != candidate.due_at
        or current.plan_fingerprint != candidate.plan_fingerprint
    ):
        return "PlanChanged"
    if await _completed(connection, candidate):
        return "AlreadyCompleted"
    return None


async def _suppress(connection: AsyncConnection, row: RowMapping, reason: str) -> None:
    if row["status"] != "sent":
        await connection.execute(
            sa.update(reminder_jobs)
            .where(reminder_jobs.c.logical_key == row["logical_key"])
            .values(
                status="expired" if reason == "ExpiredReminder" else "suppressed", reason=reason
            )
        )
    if row["outbox_id"] is not None:
        await connection.execute(
            sa.update(outbox)
            .where(outbox.c.id == row["outbox_id"], outbox.c.status.in_(["queued", "sending"]))
            .values(status="failed", payload=None, error_type=reason)
        )


async def guard_scheduled_reply(
    connection: AsyncConnection, reply: RowMapping, settings: BotSettings, *, now: float
) -> bool:
    """Call again immediately before a request; refresh the returned outbox row afterward."""
    row = (
        (
            await connection.execute(
                sa.select(reminder_jobs).where(reminder_jobs.c.outbox_id == reply["id"])
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        payload = reply["payload"]
        if isinstance(payload, dict) and "reminder_key" in payload:
            await connection.execute(
                sa.update(outbox)
                .where(outbox.c.id == reply["id"])
                .values(status="failed", payload=None, error_type="MissingReminderIdentity")
            )
            return False
        return True
    if row["status"] == "sent":
        await _suppress(connection, row, "AlreadyDelivered")
        return False
    preferences = await load_preferences(connection, default_timezone=settings.app_timezone)
    reason = await _reason(connection, row, preferences, settings, now)
    if row["status"] != "queued" or reason or row["due_at"] > now:
        await _suppress(connection, row, reason or "InactiveReminder")
        return False
    text = await render_notification(connection, _candidate(row), preferences)
    await connection.execute(
        sa.update(outbox)
        .where(outbox.c.id == reply["id"])
        .values(payload={"text": text, "reminder_key": row["logical_key"]})
    )
    return True


async def scheduler_tick(store: Store, settings: BotSettings, *, now: float) -> int:
    """One bounded local transaction; sender performs the external request later."""
    async with store.write() as connection:
        preferences = await load_preferences(connection, default_timezone=settings.app_timezone)
        if preferences.revision == 0:
            return 0
        # Reconcile sends after a restart before revisiting logical identities.
        for status in ("sent", "failed"):
            delivered = reminder_jobs.c.logical_key.in_(
                sa.select(reminder_deliveries.c.logical_key)
                .join(outbox, outbox.c.id == reminder_deliveries.c.outbox_id)
                .where(outbox.c.status == "sent")
            )
            await connection.execute(
                sa.update(reminder_jobs)
                .where(
                    delivered
                    if status == "sent"
                    else reminder_jobs.c.outbox_id.in_(
                        sa.select(outbox.c.id).where(outbox.c.status == status)
                    ),
                    sa.true() if status == "sent" else reminder_jobs.c.status == "queued",
                )
                .values(status=status, reason=None if status == "sent" else "SendFailed")
            )
        local_day = datetime.fromtimestamp(now, ZoneInfo(preferences.timezone)).date()
        for offset in range(-1, 9):
            for candidate in await candidates_for_day(
                connection, preferences, local_day + timedelta(days=offset)
            ):
                if candidate.due_at < preferences.updated_at or candidate.expires_at <= now:
                    continue
                values: dict[str, Any] = {
                    **candidate.__dict__,
                    "settings_revision": preferences.revision,
                    "status": "scheduled",
                    "outbox_id": None,
                    "owner_user_id": settings.allowed_telegram_user_id,
                    "chat_id": settings.allowed_telegram_chat_id,
                    "created_at": now,
                    "reason": None,
                }
                old = (
                    (
                        await connection.execute(
                            sa.select(reminder_jobs).where(
                                reminder_jobs.c.logical_key == candidate.logical_key
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if old is None:
                    await connection.execute(sa.insert(reminder_jobs).values(**values))
                elif (
                    old["status"] != "sent"
                    and candidate.due_at >= now
                    and (
                        old["settings_revision"] != preferences.revision
                        or old["plan_fingerprint"] != candidate.plan_fingerprint
                    )
                ):
                    # Recompute only future unsent work; a delivered local identity cannot replay.
                    if old["outbox_id"] is not None:
                        await _suppress(connection, old, "PlanChanged")
                    await connection.execute(
                        sa.update(reminder_jobs)
                        .where(reminder_jobs.c.logical_key == candidate.logical_key)
                        .values(**values)
                    )
        queued_count = int(
            await connection.scalar(
                sa.select(sa.func.count())
                .select_from(outbox)
                .where(outbox.c.status.in_(["queued", "sending"]))
            )
            or 0
        )
        ready = (
            (
                await connection.execute(
                    sa.select(reminder_jobs)
                    .where(reminder_jobs.c.status == "scheduled", reminder_jobs.c.due_at <= now)
                    .order_by(reminder_jobs.c.due_at, reminder_jobs.c.logical_key)
                    .limit(20)
                )
            )
            .mappings()
            .all()
        )
        queued = 0
        for row in ready:
            reason = await _reason(connection, row, preferences, settings, now)
            if reason:
                await _suppress(connection, row, reason)
                continue
            if queued_count + queued >= 1000:
                break
            candidate = _candidate(row)
            key = (
                f"reminder:{candidate.logical_key}:{preferences.revision}:"
                f"{candidate.plan_fingerprint or 'clock'}"
            )
            # Separate from real Telegram IDs and the Mini App reserved range.
            update_id = -(1 << 59) - (
                int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") & ((1 << 59) - 1)
            )
            await connection.execute(
                insert(inbox)
                .values(
                    update_id=update_id,
                    payload=None,
                    status="done",
                    received_at=now,
                    processed_at=now,
                )
                .on_conflict_do_nothing(index_elements=["update_id"])
            )
            await connection.execute(
                insert(actions)
                .values(key=key, update_id=update_id, kind="scheduled_notification", created_at=now)
                .on_conflict_do_nothing(index_elements=["key"])
            )
            payload = {
                "text": await render_notification(connection, candidate, preferences),
                "reminder_key": candidate.logical_key,
            }
            reply_id = (
                await connection.execute(
                    insert(outbox)
                    .values(
                        action_key=key,
                        kind="message",
                        chat_id=settings.allowed_telegram_chat_id,
                        owner_user_id=settings.allowed_telegram_user_id,
                        payload=payload,
                        status="queued",
                        attempts=0,
                        next_attempt_at=now,
                        created_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["action_key", "kind"],
                        set_={
                            "payload": payload,
                            "status": "queued",
                            "attempts": 0,
                            "next_attempt_at": now,
                            "error_type": None,
                        },
                    )
                    .returning(outbox.c.id)
                )
            ).scalar_one()
            await connection.execute(
                sa.update(reminder_jobs)
                .where(reminder_jobs.c.logical_key == candidate.logical_key)
                .values(status="queued", outbox_id=reply_id)
            )
            await connection.execute(
                insert(reminder_deliveries)
                .values(outbox_id=reply_id, logical_key=candidate.logical_key, created_at=now)
                .on_conflict_do_nothing(index_elements=["outbox_id"])
            )
            queued += 1
        # Expire future rows from obsolete dates as well, without waiting for them to be due again.
        await connection.execute(
            sa.update(reminder_jobs)
            .where(reminder_jobs.c.status == "scheduled", reminder_jobs.c.expires_at <= now)
            .values(status="expired", reason="ExpiredReminder")
        )
        return queued
