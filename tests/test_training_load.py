from datetime import date, timedelta
from fractions import Fraction

import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import actions, inbox
from nutrition_bot.adapters.database.schema_training import training_plan_events
from nutrition_bot.adapters.database.training import create_training, revise_training
from nutrition_bot.adapters.database.training_analytics import weekly_training_load
from nutrition_bot.application.training_load_report import render_training_load_report
from nutrition_bot.domain.training_guidance import RecoverySignal, training_guidance
from nutrition_bot.domain.training_load import TrainingLoadSession, summarize_training_load
from tests.helpers import message
from tests.test_telegram_meals import process

AS_OF = date(2026, 9, 21)
LATEST = date(2026, 9, 14)


def session(
    day: date,
    *,
    kind: str = "bjj",
    duration: int = 60,
    rpe_tenths: int = 50,
    source: str = "reported",
) -> TrainingLoadSession:
    return TrainingLoadSession(day, kind, duration, rpe_tenths, source)  # type: ignore[arg-type]


def covered_dates(start: date, weeks: int = 5) -> frozenset[date]:
    return frozenset(start + timedelta(days=offset) for offset in range(weeks * 7))


def test_weekly_totals_keep_modality_and_effort_provenance_visible():
    sessions = (
        session(LATEST, kind="gym", duration=40, rpe_tenths=65),
        session(LATEST + timedelta(days=2), duration=75, rpe_tenths=80, source="intensity_map"),
    )
    trend = summarize_training_load(
        sessions,
        covered_dates(LATEST - timedelta(weeks=4)),
        as_of=AS_OF,
    )
    week = trend.latest
    assert week.total_load_tenths == 8_600
    assert week.gym_load_tenths == 2_600
    assert week.bjj_load_tenths == 6_000
    assert week.reported_session_count == 1
    assert week.inferred_session_count == 1
    assert week.reported_load_tenths == 2_600
    assert week.inferred_load_tenths == 6_000


def test_four_week_median_uses_only_complete_reference_weeks():
    starts = [LATEST - timedelta(weeks=offset) for offset in range(5)]
    sessions = tuple(
        session(start, duration=duration, rpe_tenths=50)
        for start, duration in zip(starts, (72, 40, 60, 80, 200), strict=True)
    )
    rests = set(covered_dates(LATEST - timedelta(weeks=4)))
    # The oldest reference week is incomplete and its outlier must be excluded.
    rests.remove(starts[4] + timedelta(days=6))
    trend = summarize_training_load(sessions, frozenset(rests), as_of=AS_OF)
    assert trend.valid_reference_weeks == 3
    assert trend.baseline_median_load_tenths == 3_000
    assert trend.change_percent == Fraction(20)
    assert trend.comparison_status == "available"


def test_sparse_latest_or_reference_coverage_suppresses_comparison():
    latest_only = summarize_training_load((session(LATEST),), frozenset({LATEST}), as_of=AS_OF)
    assert latest_only.comparison_status == "latest_week_incomplete"
    assert latest_only.change_percent is None

    four_starts = [LATEST - timedelta(weeks=offset) for offset in range(4)]
    rests = covered_dates(LATEST - timedelta(weeks=2), weeks=3)
    too_few = summarize_training_load(
        tuple(session(start) for start in four_starts), rests, as_of=AS_OF
    )
    assert too_few.valid_reference_weeks == 2
    assert too_few.comparison_status == "insufficient_reference_weeks"
    assert too_few.baseline_median_load_tenths is None


def test_zero_reference_baseline_is_a_starting_or_resuming_state():
    trend = summarize_training_load(
        (session(LATEST),),
        covered_dates(LATEST - timedelta(weeks=4)),
        as_of=AS_OF,
    )
    assert trend.valid_reference_weeks == 4
    assert trend.baseline_median_load_tenths == 0
    assert trend.change_percent is None
    assert trend.comparison_status == "zero_baseline"


def test_lighter_gym_option_requires_high_bjj_and_several_elevated_recovery_days():
    starts = [LATEST - timedelta(weeks=offset) for offset in range(5)]
    sessions = tuple(
        session(start, duration=duration, rpe_tenths=50)
        for start, duration in zip(starts, (90, 60, 60, 60, 60), strict=True)
    )
    trend = summarize_training_load(
        sessions, covered_dates(LATEST - timedelta(weeks=4)), as_of=AS_OF
    )
    one_signal = training_guidance(trend, (RecoverySignal(LATEST, soreness=4, fatigue=None),))
    assert one_signal.total_jump_prompt
    assert one_signal.bjj_jump_supported
    assert not one_signal.lighter_gym_option
    supported = training_guidance(
        trend,
        (
            RecoverySignal(LATEST, soreness=4, fatigue=None),
            RecoverySignal(LATEST + timedelta(days=1), soreness=None, fatigue=5),
        ),
    )
    assert supported.lighter_gym_option
    report = render_training_load_report(
        trend,
        (
            RecoverySignal(LATEST, soreness=4, fatigue=None),
            RecoverySignal(LATEST + timedelta(days=1), soreness=None, fatigue=5),
        ),
    ).text
    assert "change +50%" in report
    assert "not an injury-risk threshold" in report
    assert "BJJ load is at least 30% above its baseline" in report
    assert "2 dates report soreness or fatigue at 4–5/5" in report
    assert "No workout or plan was changed" in report


async def test_load_command_discloses_missing_coverage_and_mutates_no_training(service, store):
    report = await process(service, store, message(1, "/load"))
    text = report["payload"]["text"]
    assert "Training load" in text
    assert "Coverage: 0/7 dates confirmed" in text
    assert "unknown training/rest dates" in text
    assert "No workout or plan was changed" in text


async def add_action(connection, number: int) -> str:
    key = f"update:{number}"
    await connection.execute(
        sa.insert(inbox).values(
            update_id=number,
            payload=None,
            status="done",
            received_at=float(number),
            processed_at=float(number),
        )
    )
    await connection.execute(
        sa.insert(actions).values(
            key=key,
            update_id=number,
            kind="synthetic_training_load",
            created_at=float(number),
        )
    )
    return key


async def test_database_uses_current_non_deleted_revision_and_latest_rest_state(store):
    async with store.write() as connection:
        created = await create_training(
            connection,
            action_key=await add_action(connection, 1),
            source_chat_id=101,
            source_message_id=1,
            kind="gym",
            local_date=LATEST,
            occurred_at=1.0,
            timezone="UTC",
            duration_minutes=60,
            focus="full body",
            intensity=None,
            session_rpe_tenths=70,
        )
        await revise_training(
            connection,
            created.id,
            created.revision_id,
            action_key=await add_action(connection, 2),
            duration_minutes=45,
            session_rpe_tenths=60,
        )
        deleted = await create_training(
            connection,
            action_key=await add_action(connection, 3),
            source_chat_id=101,
            source_message_id=3,
            kind="bjj",
            local_date=LATEST + timedelta(days=1),
            occurred_at=3.0,
            timezone="UTC",
            duration_minutes=90,
            focus=None,
            intensity="hard",
            session_rpe_tenths=80,
        )
        await revise_training(
            connection,
            deleted.id,
            deleted.revision_id,
            action_key=await add_action(connection, 4),
            delete=True,
        )
        for offset in range(35):
            day = LATEST - timedelta(weeks=4) + timedelta(days=offset)
            action_key = await add_action(connection, 10 + offset)
            await connection.execute(
                sa.insert(training_plan_events).values(
                    action_key=action_key,
                    local_date=day,
                    state="rest",
                    kind=None,
                    duration_minutes=None,
                    start_minute=None,
                    timezone="UTC",
                    created_at=float(10 + offset),
                )
            )
        # A later clear makes this date unknown, proving latest-event semantics.
        action_key = await add_action(connection, 100)
        await connection.execute(
            sa.insert(training_plan_events).values(
                action_key=action_key,
                local_date=LATEST + timedelta(days=2),
                state="clear",
                kind=None,
                duration_minutes=None,
                start_minute=None,
                timezone="UTC",
                created_at=100.0,
            )
        )
        trend = await weekly_training_load(connection, as_of=AS_OF)

    assert trend.latest.total_load_tenths == 2_700
    assert trend.latest.session_count == 1
    assert trend.latest.covered_days == 6
    assert trend.comparison_status == "latest_week_incomplete"
