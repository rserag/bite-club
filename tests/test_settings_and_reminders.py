import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from nutrition_bot.adapters.database.bjj_plans import add_plan_event
from nutrition_bot.adapters.database.recovery import create_recovery
from nutrition_bot.adapters.database.schema import actions, cursor, inbox, outbox, profile
from nutrition_bot.adapters.database.schema_settings import (
    notification_settings,
    reminder_jobs,
    schedule_rules,
    settings_events,
)
from nutrition_bot.adapters.database.schema_training import training_sessions
from nutrition_bot.adapters.database.schema_weights import body_weight_revisions
from nutrition_bot.adapters.database.training import create_training
from nutrition_bot.adapters.database.weights import create_weight, revise_weight
from nutrition_bot.application.settings_conversation import handle_settings_message
from nutrition_bot.application.settings_service import (
    Preferences,
    ScheduleRule,
    SettingsError,
    in_quiet_hours,
    load_preferences,
    parse_minute,
    save_preferences,
    wall_time,
)
from nutrition_bot.runtime.scheduler import (
    Candidate,
    candidates_for_day,
    guard_scheduled_reply,
    render_notification,
    scheduler_tick,
)
from nutrition_bot.runtime.worker import send_one
from tests.helpers import FakeGateway, message
from tests.test_telegram_meals import process

DAY = date(2026, 10, 8)
BEFORE = datetime(2026, 10, 8, 7, tzinfo=UTC).timestamp()
DUE = BEFORE + 3600
MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")


async def action(connection):
    update_id = int(await connection.scalar(sa.select(sa.func.max(inbox.c.update_id))) or 0) + 1
    update_id = max(1, update_id)
    key = f"message:{update_id}"
    await connection.execute(
        sa.insert(inbox).values(
            update_id=update_id,
            payload=None,
            status="done",
            received_at=BEFORE,
            processed_at=BEFORE,
        )
    )
    await connection.execute(
        sa.insert(actions).values(key=key, update_id=update_id, kind="synthetic", created_at=BEFORE)
    )
    return key


async def edit(store, text, *, now=BEFORE):
    async with store.write() as connection:
        return await handle_settings_message(
            connection, text, action_key=await action(connection), now=now
        )


async def first_reply(store):
    async with store.engine.connect() as connection:
        return (
            (await connection.execute(sa.select(outbox).order_by(outbox.c.id))).mappings().first()
        )


async def weight(connection, *, day=DAY):
    return await create_weight(
        connection,
        action_key=await action(connection),
        source_chat_id=101,
        source_message_id=int(
            await connection.scalar(sa.select(sa.func.count()).select_from(actions)) or 0
        ),
        local_date=day,
        measured_at=DUE,
        timezone="UTC",
        weight_grams=79400,
        timing="morning",
    )


async def recovery(connection):
    return await create_recovery(
        connection,
        action_key=await action(connection),
        local_date=DAY,
        sleep_minutes=450,
        soreness=None,
        fatigue=None,
        readiness=None,
    )


async def plan(connection, *, state="planned", start=10 * 60, duration=60, day=DAY):
    await add_plan_event(
        connection,
        action_key=await action(connection),
        local_date=day,
        state=state,
        timezone="UTC",
        kind="bjj" if state == "planned" else None,
        duration_minutes=duration if state == "planned" else None,
        start_minute=start if state == "planned" else None,
    )


async def test_defaults_are_off_and_view_does_not_create_preferences(store, settings):
    async with store.engine.connect() as connection:
        prefs = await load_preferences(connection)
        assert prefs.revision == 0
        assert prefs.report_length == "short"
        assert all(
            not prefs.rule(name).enabled
            for name in ("weight", "recovery", "evening", "weekly", "training_pre", "training_post")
        )
        reply = await handle_settings_message(
            connection, "/settings", action_key="unused", now=BEFORE
        )
        assert "Weight: off" in reply.text
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(notification_settings))
            == 0
        )
    assert await scheduler_tick(store, settings, now=DUE) == 0


async def test_explicit_setting_and_restart_preserve_timezone_and_clock(store):
    assert (await edit(store, "set timezone Europe/Paris")).kind == "settings_saved"
    reply = await edit(store, "remind me to weigh at 08:00")
    assert "Weight: 08:00" in reply.text
    assert "next Fri 09 Oct 08:00 CEST" in reply.text
    async with store.engine.connect() as connection:
        prefs = await load_preferences(connection, default_timezone="America/New_York")
        assert prefs.timezone == "Europe/Paris"
        assert prefs.rule("weight").enabled
        assert prefs.rule("weight").local_minute == 480
        assert await connection.scalar(sa.select(sa.func.count()).select_from(settings_events)) == 2


@pytest.mark.parametrize(
    "text",
    [
        "/settings timezone Missing/Zone",
        "/settings report medium",
        "/settings weight 8am on",
        "/settings weight 24:00 on",
        "/settings weight 8:00 on",
        "/settings weight 08:60 on",
        "/settings weight on",
        "/settings weekly Monday on",
        "/settings weekly Someday 08:00 on",
        "/settings quiet 23:00 23:00",
        "/settings quiet 23:00",
        "/settings training_pre on 0",
        "/settings training_pre on 1441",
        "/settings training_post on -10",
        "/settings unknown on",
    ],
)
async def test_invalid_settings_are_non_mutating(store, text):
    reply = await edit(store, text)
    assert reply.kind == "settings_rejected"
    assert "Nothing changed" in reply.text
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(settings_events)) == 0
        assert await connection.scalar(sa.select(sa.func.count()).select_from(schedule_rules)) == 0


@pytest.mark.parametrize("value", ["８:00", "08:٠٠", "08:00:00", " 08:00", "08:00 ", "-1:00"])
def test_times_require_ascii_unambiguous_clock(value):
    with pytest.raises(SettingsError):
        parse_minute(value)


async def test_timezone_edits_preserve_historical_weight_values(store):
    async with store.write() as connection:
        saved = await weight(connection)
    await edit(store, "/settings timezone America/New_York")
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(body_weight_revisions))).mappings().one()
        assert row["local_date"] == saved.local_date
        assert row["measured_at"] == saved.measured_at
        assert row["timezone"] == "UTC"
        assert await connection.scalar(sa.select(profile.c.timezone)) == "America/New_York"


async def test_settings_receipts_are_immutable(store):
    await edit(store, "/settings report full")
    async with store.write() as connection:
        with pytest.raises(IntegrityError, match="immutable settings history"):
            async with connection.begin_nested():
                await connection.execute(sa.update(settings_events).values(snapshot={}))
        with pytest.raises(IntegrityError, match="immutable settings history"):
            async with connection.begin_nested():
                await connection.execute(sa.delete(settings_events))


def test_dst_gap_moves_to_first_valid_minute_and_fold_uses_earliest_occurrence():
    assert wall_time(date(2026, 3, 8), 150, "America/New_York") == datetime(
        2026, 3, 8, 7, tzinfo=UTC
    )
    assert wall_time(date(2026, 11, 1), 90, "America/New_York") == datetime(
        2026, 11, 1, 5, 30, tzinfo=UTC
    )


def test_quiet_hours_cross_midnight_and_end_is_exclusive():
    prefs = Preferences("UTC", quiet_start_minute=23 * 60, quiet_end_minute=7 * 60)
    assert in_quiet_hours(prefs, BEFORE - 1)
    assert not in_quiet_hours(prefs, BEFORE)
    assert in_quiet_hours(prefs, BEFORE + 16 * 3600)


async def test_due_notification_queues_once_and_does_not_advance_telegram_cursor(store, settings):
    await edit(store, "/settings weight 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE - 1) == 0
    assert await scheduler_tick(store, settings, now=DUE) == 1
    assert await scheduler_tick(store, settings, now=DUE + 1) == 0
    reply = await first_reply(store)
    assert reply["owner_user_id"] == settings.allowed_telegram_user_id
    assert "your actual value" in reply["payload"]["text"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(cursor.c.next_offset)) is None
        scheduled_id = await connection.scalar(
            sa.select(actions.c.update_id).where(actions.c.kind == "scheduled_notification")
        )
        assert -(1 << 60) <= scheduled_id <= -(1 << 59)
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(body_weight_revisions))
            == 0
        )
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(training_sessions)) == 0
        )


async def test_reboot_expires_old_jobs_without_replaying_stale_backlog(store, settings):
    await edit(store, "/settings weight 08:00 on")
    assert await scheduler_tick(store, settings, now=BEFORE) == 0
    assert await scheduler_tick(store, settings, now=DUE + 2 * 86400 + 7200) == 0
    assert await first_reply(store) is None
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(reminder_jobs.c.status).where(
                    reminder_jobs.c.logical_key == "weight:2026-10-08"
                )
            )
            == "expired"
        )


async def test_settings_edit_invalidates_queued_payload_and_recomputes_future_job(store, settings):
    await edit(store, "/settings weight 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    old_reply = await first_reply(store)
    await edit(store, "/settings weight 09:00 on", now=DUE + 1)
    async with store.engine.connect() as connection:
        old = (
            (await connection.execute(sa.select(outbox).where(outbox.c.id == old_reply["id"])))
            .mappings()
            .one()
        )
        assert old["status"] == "failed"
        assert old["payload"] is None
    assert await scheduler_tick(store, settings, now=DUE + 1) == 0
    assert await scheduler_tick(store, settings, now=DUE + 3600) == 1
    async with store.engine.connect() as connection:
        job = (
            (
                await connection.execute(
                    sa.select(reminder_jobs).where(
                        reminder_jobs.c.logical_key == "weight:2026-10-08"
                    )
                )
            )
            .mappings()
            .one()
        )
        assert job["settings_revision"] == 2
        assert job["outbox_id"] != old_reply["id"]


async def test_enable_after_today_due_does_not_replay_today(store, settings):
    await edit(store, "/settings weight 08:00 on", now=DUE + 60)
    assert await scheduler_tick(store, settings, now=DUE + 60) == 0
    assert await scheduler_tick(store, settings, now=DUE + 86400) == 1


@pytest.mark.parametrize("category,complete", [("weight", weight), ("recovery", recovery)])
async def test_sender_rechecks_completed_checkin_after_queueing(
    store, settings, category, complete
):
    await edit(store, f"/settings {category} 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    reply = await first_reply(store)
    async with store.write() as connection:
        await complete(connection)
        assert not await guard_scheduled_reply(connection, reply, settings, now=DUE + 1)
        updated = (
            (await connection.execute(sa.select(outbox).where(outbox.c.id == reply["id"])))
            .mappings()
            .one()
        )
        assert updated["payload"] is None
        assert updated["error_type"] == "AlreadyCompleted"


async def test_deleted_weight_does_not_suppress_prompt(store, settings):
    async with store.write() as connection:
        saved = await weight(connection)
        await revise_weight(
            connection,
            saved.id,
            saved.revision_id,
            action_key=await action(connection),
            delete=True,
        )
    await edit(store, "/settings weight 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1


async def test_quiet_hours_suppress_due_work_instead_of_replaying_it_later(store, settings):
    await edit(store, "/settings weight 08:00 on")
    await edit(store, "/settings quiet 07:30 09:00")
    assert await scheduler_tick(store, settings, now=DUE) == 0
    assert await scheduler_tick(store, settings, now=DUE + 3601) == 0
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(reminder_jobs.c.reason).where(
                    reminder_jobs.c.logical_key == "weight:2026-10-08"
                )
            )
            == "QuietHours"
        )


async def test_sent_local_identity_cannot_replay_after_timezone_change(store, settings):
    await edit(store, "/settings weight 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    reply = await first_reply(store)
    async with store.write() as connection:
        await connection.execute(
            sa.update(outbox).where(outbox.c.id == reply["id"]).values(status="sent", sent_at=DUE)
        )
    await edit(store, "/settings timezone America/New_York", now=DUE + 10)
    assert await scheduler_tick(store, settings, now=DUE + 4 * 3600) == 0
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(reminder_jobs.c.status).where(
                    reminder_jobs.c.logical_key == "weight:2026-10-08"
                )
            )
            == "sent"
        )


async def test_sender_rechecks_expiry_and_owner(store, settings):
    await edit(store, "/settings weight 08:00 on")
    await scheduler_tick(store, settings, now=DUE)
    reply = await first_reply(store)
    async with store.write() as connection:
        assert not await guard_scheduled_reply(connection, reply, settings, now=DUE + 3600)
        assert (
            await connection.scalar(
                sa.select(reminder_jobs.c.reason).where(
                    reminder_jobs.c.logical_key == "weight:2026-10-08"
                )
            )
            == "ExpiredReminder"
        )


async def test_sender_rejects_changed_allowlist(store, settings):
    await edit(store, "/settings weight 08:00 on")
    await scheduler_tick(store, settings, now=DUE)
    reply = await first_reply(store)
    changed = settings.model_copy(update={"allowed_telegram_user_id": 202})
    async with store.write() as connection:
        assert not await guard_scheduled_reply(connection, reply, changed, now=DUE)


async def test_training_requires_known_time_and_cancelled_plan_is_rechecked(store, settings):
    await edit(store, "/settings training_pre on")
    async with store.write() as connection:
        await plan(connection, start=None)
        assert not await candidates_for_day(connection, await load_preferences(connection), DAY)
        await plan(connection)
    assert await scheduler_tick(store, settings, now=DUE) == 1
    reply = await first_reply(store)
    async with store.write() as connection:
        await plan(connection, state="cancelled")
        assert not await guard_scheduled_reply(connection, reply, settings, now=DUE + 1)
        assert (
            await connection.scalar(
                sa.select(reminder_jobs.c.reason).where(
                    reminder_jobs.c.logical_key == "training_pre:2026-10-08:bjj"
                )
            )
            == "PlanChanged"
        )


async def test_training_time_edit_recomputes_unsent_identity(store, settings):
    await edit(store, "/settings training_pre on")
    async with store.write() as connection:
        await plan(connection)
    assert await scheduler_tick(store, settings, now=DUE) == 1
    async with store.write() as connection:
        await plan(connection, start=11 * 60)
    assert await scheduler_tick(store, settings, now=DUE + 60) == 0
    assert await scheduler_tick(store, settings, now=DUE + 3600) == 1
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(sa.func.count())
                .select_from(reminder_jobs)
                .where(reminder_jobs.c.logical_key == "training_pre:2026-10-08:bjj")
            )
            == 1
        )


async def test_training_post_midnight_uses_planned_session_date(store, settings):
    await edit(store, "/settings training_post on")
    async with store.write() as connection:
        await plan(connection, start=23 * 60 + 30, duration=60)
    midnight_due = datetime(2026, 10, 9, 0, 45, tzinfo=UTC).timestamp()
    assert await scheduler_tick(store, settings, now=midnight_due) == 1
    reply = await first_reply(store)
    assert "08 Oct" in reply["payload"]["text"]
    assert "Planned end times are approximate" in reply["payload"]["text"]


async def test_weekly_review_always_uses_preceding_completed_local_week(store, settings):
    await edit(store, "/settings weekly Thursday 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    reply = await first_reply(store)
    assert "previous completed local week" in reply["payload"]["text"]
    assert "2026-09-28" in reply["payload"]["text"]
    assert "2026-10-04" in reply["payload"]["text"]
    assert "2026-10-08" not in reply["payload"]["text"]


async def test_evening_summary_preserves_unknown_intake_and_default_short_view(store, settings):
    await edit(store, "/settings evening 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    text = (await first_reply(store))["payload"]["text"]
    assert "Evening summary" in text
    assert "unknown" in text
    assert "0 kcal" not in text


async def test_dst_fold_queues_only_one_logical_occurrence(store, settings):
    before = datetime(2026, 10, 31, tzinfo=UTC).timestamp()
    await edit(store, "/settings timezone America/New_York", now=before)
    await edit(store, "/settings weight 01:30 on", now=before)
    first = datetime(2026, 11, 1, 5, 30, tzinfo=UTC).timestamp()
    assert await scheduler_tick(store, settings, now=first) == 1
    assert await scheduler_tick(store, settings, now=first + 3600) == 0


async def test_all_categories_can_be_disabled_independently(store, settings):
    for command in (
        "weight 08:00 on",
        "recovery 08:00 on",
        "evening 08:00 on",
        "weekly Thursday 08:00 on",
        "training_pre on",
        "training_post on",
    ):
        await edit(store, "/settings " + command)
    await edit(store, "disable weight reminders")
    async with store.engine.connect() as connection:
        prefs = await load_preferences(connection)
        assert not prefs.rule("weight").enabled
        assert prefs.rule("recovery").enabled
    await edit(store, "disable all reminders")
    assert await scheduler_tick(store, settings, now=DUE) == 0


async def test_schedule_transaction_failure_does_not_leave_partial_queue(
    store, settings, monkeypatch
):
    await edit(store, "/settings weight 08:00 on")

    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic render failure")

    monkeypatch.setattr("nutrition_bot.runtime.scheduler.render_notification", broken)
    with pytest.raises(RuntimeError, match="synthetic render failure"):
        await scheduler_tick(store, settings, now=DUE)
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(reminder_jobs)) == 0
        assert await connection.scalar(sa.select(sa.func.count()).select_from(outbox)) == 0


async def test_unknown_preferences_never_pass_validation(store):
    async with store.write() as connection:
        with pytest.raises(SettingsError):
            await save_preferences(
                connection,
                Preferences("UTC", rules=(ScheduleRule("weight", True),)),
                action_key=await action(connection),
                now=BEFORE,
            )


async def test_post_training_avoids_repeating_completed_recovery_question(store):
    async with store.write() as connection:
        await recovery(connection)
        text = await render_notification(
            connection,
            Candidate("synthetic", "training_post", DAY, DUE, DUE + 3600, "bjj"),
            Preferences("UTC"),
        )
    assert "how you feel" not in text
    assert "Did the planned session happen?" in text


async def test_completed_training_is_rechecked_after_reminder_queued(store, settings):
    await edit(store, "/settings training_pre on")
    async with store.write() as connection:
        await plan(connection)
    assert await scheduler_tick(store, settings, now=DUE) == 1
    reply = await first_reply(store)
    async with store.write() as connection:
        await create_training(
            connection,
            action_key=await action(connection),
            source_chat_id=101,
            source_message_id=777,
            kind="bjj",
            local_date=DAY,
            occurred_at=DUE,
            timezone="UTC",
            duration_minutes=60,
            focus=None,
            intensity="medium",
            session_rpe_tenths=60,
        )
        assert not await guard_scheduled_reply(connection, reply, settings, now=DUE + 1)
        assert (
            await connection.scalar(
                sa.select(reminder_jobs.c.reason).where(
                    reminder_jobs.c.logical_key == "training_pre:2026-10-08:bjj"
                )
            )
            == "AlreadyCompleted"
        )


async def test_quiet_hours_are_checked_again_immediately_before_send(store, settings):
    await edit(store, "/settings weight 08:00 on")
    await edit(store, "/settings quiet 08:30 09:00")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    reply = await first_reply(store)
    async with store.write() as connection:
        assert not await guard_scheduled_reply(connection, reply, settings, now=DUE + 30 * 60)
        assert (
            await connection.scalar(
                sa.select(reminder_jobs.c.reason).where(
                    reminder_jobs.c.logical_key == "weight:2026-10-08"
                )
            )
            == "QuietHours"
        )


async def test_queue_capacity_preserves_scheduled_job_until_space_is_available(store, settings):
    await edit(store, "/settings weight 08:00 on")
    async with store.write() as connection:
        key = await action(connection)
        # Existing backlog is synthetic, with distinct action keys matching each message.
        for index in range(1000):
            update_id = 10000 + index
            backlog_key = f"backlog:{index}"
            await connection.execute(
                sa.insert(inbox).values(
                    update_id=update_id, status="done", received_at=BEFORE, payload=None
                )
            )
            await connection.execute(
                sa.insert(actions).values(
                    key=backlog_key, update_id=update_id, kind="synthetic", created_at=BEFORE
                )
            )
            await connection.execute(
                sa.insert(outbox).values(
                    action_key=backlog_key,
                    kind="message",
                    chat_id=101,
                    owner_user_id=101,
                    payload={"text": "Synthetic backlog"},
                    status="queued",
                    attempts=0,
                    created_at=BEFORE,
                    next_attempt_at=BEFORE,
                )
            )
        assert key
    assert await scheduler_tick(store, settings, now=DUE) == 0
    async with store.write() as connection:
        await connection.execute(sa.update(outbox).values(status="sent", sent_at=DUE))
    assert await scheduler_tick(store, settings, now=DUE + 1) == 1


async def test_sender_refreshes_summary_from_current_diary_state(store, settings):
    from nutrition_bot.adapters.database.checkins import mark_food_day

    await edit(store, "/settings evening 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    reply = await first_reply(store)
    assert "unknown" in reply["payload"]["text"]
    async with store.write() as connection:
        await mark_food_day(connection, DAY, "complete", action_key=await action(connection))
        assert await guard_scheduled_reply(connection, reply, settings, now=DUE + 1)
        refreshed = (
            (await connection.execute(sa.select(outbox).where(outbox.c.id == reply["id"])))
            .mappings()
            .one()
        )
    assert "All food logged for this date" in refreshed["payload"]["text"]


async def test_telegram_settings_flow_is_authorized_idempotent_and_history_safe(
    service, store, monkeypatch
):
    monkeypatch.setattr("nutrition_bot.application.service.time.time", lambda: BEFORE)
    first = await process(service, store, message(1, "/settings weight 08:00 on"))
    assert "Settings saved" in first["payload"]["text"]
    await service.accept([message(1, "/settings weight 08:00 on")])
    assert not await service.process_one()
    await service.accept([message(2, "/settings weight off", user=202)])
    assert not await service.process_one()
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(settings_events)) == 1
        assert (await load_preferences(connection)).rule("weight").enabled


async def test_settings_receipt_displays_next_known_training_time(store):
    async with store.write() as connection:
        await plan(connection)
    reply = await edit(store, "/settings training_pre on")
    assert "next Thu 08 Oct 08:00 UTC" in reply.text
    async with store.write() as connection:
        await plan(connection, state="cancelled")
    reply = await edit(store, "/settings")
    assert "no eligible timed plan in the next eight days" in reply.text


async def test_next_receipt_skips_previously_delivered_local_identity(store, settings):
    await edit(store, "/settings weight 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    queued = await first_reply(store)
    async with store.write() as connection:
        await connection.execute(
            sa.update(outbox).where(outbox.c.id == queued["id"]).values(status="sent", sent_at=DUE)
        )
    changed = await edit(store, "/settings timezone America/New_York", now=DUE + 1)
    assert "Weight: 08:00 · next Fri 09 Oct 08:00 EDT" in changed.text


async def test_next_receipt_skips_completed_weight_checkin(store):
    async with store.write() as connection:
        await weight(connection)
    reply = await edit(store, "/settings weight 08:00 on")
    assert "Weight: 08:00 · next Fri 09 Oct 08:00 UTC" in reply.text


async def test_downgrade_refuses_to_discard_private_settings_history(store, settings):
    await edit(store, "/settings report full")
    await store.close()
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    config.attributes["database_url"] = settings.resolved_database_url
    with pytest.raises(RuntimeError, match="Settings or reminder history exists"):
        await asyncio.to_thread(alembic_command.downgrade, config, "0021_training_allocations")


async def test_missing_scheduled_identity_cannot_send_orphaned_payload(store, settings):
    await edit(store, "/settings weight 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    queued = await first_reply(store)
    async with store.write() as connection:
        await connection.execute(sa.delete(reminder_jobs))
        assert not await guard_scheduled_reply(connection, queued, settings, now=DUE)
        assert (
            await connection.scalar(
                sa.select(outbox.c.error_type).where(outbox.c.id == queued["id"])
            )
            == "MissingReminderIdentity"
        )


async def test_recovered_outbox_cannot_erase_delivered_reminder_identity(store, settings):
    await edit(store, "/settings weight 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    queued = await first_reply(store)
    async with store.write() as connection:
        await connection.execute(
            sa.update(reminder_jobs)
            .where(reminder_jobs.c.outbox_id == queued["id"])
            .values(status="sent")
        )
        assert not await guard_scheduled_reply(connection, queued, settings, now=DUE)
        assert (
            await connection.scalar(
                sa.select(reminder_jobs.c.status).where(reminder_jobs.c.outbox_id == queued["id"])
            )
            == "sent"
        )


async def test_late_send_acknowledgement_survives_settings_retime(store, settings):
    await edit(store, "/settings weight 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    old = await first_reply(store)
    async with store.write() as connection:
        await connection.execute(
            sa.update(outbox).where(outbox.c.id == old["id"]).values(status="sending")
        )
    await edit(store, "/settings weight 09:00 on", now=DUE + 1)
    assert await scheduler_tick(store, settings, now=DUE + 1) == 0
    async with store.write() as connection:
        # Telegram had already accepted the old request; acknowledgement arrives after the edit.
        await connection.execute(
            sa.update(outbox).where(outbox.c.id == old["id"]).values(status="sent", sent_at=DUE + 2)
        )
    assert await scheduler_tick(store, settings, now=DUE + 3600) == 0
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(reminder_jobs.c.status).where(
                    reminder_jobs.c.logical_key == "weight:2026-10-08"
                )
            )
            == "sent"
        )


async def test_late_acknowledgement_suppresses_already_queued_replacement(store, settings):
    await edit(store, "/settings weight 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    old = await first_reply(store)
    await edit(store, "/settings weight 09:00 on", now=DUE + 1)
    assert await scheduler_tick(store, settings, now=DUE + 3600) == 1
    async with store.write() as connection:
        new = (
            (await connection.execute(sa.select(outbox).where(outbox.c.status == "queued")))
            .mappings()
            .one()
        )
        await connection.execute(
            sa.update(outbox)
            .where(outbox.c.id == old["id"])
            .values(status="sent", sent_at=DUE + 3601)
        )
    assert await scheduler_tick(store, settings, now=DUE + 3602) == 0
    async with store.write() as connection:
        assert not await guard_scheduled_reply(connection, new, settings, now=DUE + 3602)
        assert (
            await connection.scalar(
                sa.select(reminder_jobs.c.status).where(
                    reminder_jobs.c.logical_key == "weight:2026-10-08"
                )
            )
            == "sent"
        )


async def test_worker_rechecks_completion_between_claim_and_send(
    service, store, settings, monkeypatch
):
    await edit(store, "/settings weight 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    monkeypatch.setattr("nutrition_bot.application.service.time.time", lambda: DUE)
    original_claim = service.claim_reply

    async def completion_during_claim():
        claimed = await original_claim()
        async with store.write() as connection:
            await weight(connection)
        return claimed

    monkeypatch.setattr(service, "claim_reply", completion_during_claim)
    gateway = FakeGateway()
    assert await send_one(service, gateway)
    assert gateway.messages == []
    async with store.engine.connect() as connection:
        queued = (await connection.execute(sa.select(outbox))).mappings().one()
        assert queued["status"] == "failed"
        assert queued["error_type"] == "AlreadyCompleted"


async def test_worker_refreshes_current_summary_between_claim_and_send(
    service, store, settings, monkeypatch
):
    from nutrition_bot.adapters.database.checkins import mark_food_day

    await edit(store, "/settings evening 08:00 on")
    assert await scheduler_tick(store, settings, now=DUE) == 1
    monkeypatch.setattr("nutrition_bot.application.service.time.time", lambda: DUE)
    original_claim = service.claim_reply

    async def complete_day_during_claim():
        claimed = await original_claim()
        async with store.write() as connection:
            await mark_food_day(connection, DAY, "complete", action_key=await action(connection))
        return claimed

    monkeypatch.setattr(service, "claim_reply", complete_day_during_claim)

    class CheckingGateway(FakeGateway):
        async def send_message(self, *args, **kwargs):
            assert not store.writer_lock.locked()
            return await super().send_message(*args, **kwargs)

    gateway = CheckingGateway()
    assert await send_one(service, gateway)
    assert "All food logged for this date" in gateway.messages[0][1]
