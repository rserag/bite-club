"""Daily reports through the offline Telegram service, using synthetic food values."""

from datetime import datetime

import pytest
import sqlalchemy as sa
from aiogram.types import Update

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.schema import actions, outbox, profile
from tests.helpers import message
from tests.test_telegram_meals import current, food_record, ledger_counts, process, reply


@pytest.fixture
async def daily_catalog(store):
    async with store.write() as connection:
        return {
            name: await publish_reviewed_food(connection, food_record(name, **values))
            for name, values in (
                ("rice", {"energy": "100", "protein": "10", "fiber": "1"}),
                ("chicken", {"energy": "200", "protein": "20"}),
            )
        }


def dated_message(update_id, text, timestamp, **kwargs):
    value = message(update_id, text, **kwargs).model_dump(mode="json", exclude_none=True)
    key = "edited_message" if kwargs.get("edited") else "message"
    value[key]["date"] = int(datetime.fromisoformat(timestamp).timestamp())
    return Update.model_validate(value)


async def set_timezone(store, timezone):
    async with store.write() as connection:
        exists = await connection.scalar(sa.select(profile.c.id))
        if exists is None:
            await connection.execute(
                sa.insert(profile).values(id=1, timezone=timezone, created_at=0.0)
            )
        else:
            await connection.execute(sa.update(profile).values(timezone=timezone))


async def action_kind(store, result):
    async with store.engine.connect() as connection:
        return await connection.scalar(
            sa.select(actions.c.kind).where(actions.c.key == result["action_key"])
        )


def report_text(result, day="2023-11-14"):
    assert result is not None
    assert {"text", "daily_date", "buttons"} <= set(result["payload"])
    assert result["button_token"] is not None
    text = result["payload"]["text"]
    assert f"Daily food log · {day}" in text
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    return text


async def test_full_report_needs_no_goal_setup_and_preserves_unknown_zero_and_partial_data(
    service, store, daily_catalog
):
    await process(service, store, message(1, "Lunch: 150g rice and 200g chicken"))
    result = await process(service, store, message(2, "/today full"))
    text = report_text(result)
    assert "Energy: 550 kcal" in text
    assert "Protein: 55.0 g" in text
    assert "Carbohydrate: unknown" in text
    assert "Fat: 0.0 g" in text
    assert "Fiber: 1.5 g known" in text
    assert "data 1/2 foods" in text
    for name in (
        "Sodium",
        "Potassium",
        "Calcium",
        "Magnesium",
        "Iron",
        "Zinc",
        "Vitamin D",
        "Vitamin B12",
        "Vitamin C",
    ):
        assert f"{name}: unknown" in text
    assert await action_kind(store, result) == "daily_report"
    assert await ledger_counts(store) == (1, 1)
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(profile.c.id)) is None


async def test_short_report_is_available_on_request_and_links_to_full_report(
    service, store, daily_catalog
):
    await process(service, store, message(1, "150g rice and 200g chicken"))
    short_result = await process(service, store, message(2, "/today short"))
    full_result = await process(service, store, message(3, "/today full"))
    short_text = report_text(short_result)
    full_text = report_text(full_result)
    assert "Energy: 550 kcal" in short_text
    assert "Protein: 55.0 g" in short_text
    assert "Fiber: 1.5 g known" in short_text
    assert "data 1/2 foods" in short_text
    assert "Calcium:" not in short_text
    assert "/today" in short_text
    assert "Calcium:" in full_text
    assert len(short_text) < len(full_text)
    assert await ledger_counts(store) == (1, 1)


@pytest.mark.parametrize(
    "command",
    ["/today@SyntheticBot", "/TODAY@SyntheticBot", "how am I doing today?", "show today’s totals"],
)
async def test_daily_command_suffix_and_fixed_natural_aliases(
    service, store, daily_catalog, command
):
    await process(service, store, message(1, "100g rice"))
    result = await process(service, store, message(2, command))
    assert "Energy: 100 kcal" in report_text(result)
    assert await action_kind(store, result) == "daily_report"
    assert await ledger_counts(store) == (1, 1)


@pytest.mark.parametrize("command", ["/today", "/today short"])
async def test_empty_day_is_unlogged_not_zero_intake(service, store, command):
    result = await process(service, store, message(1, command))
    text = report_text(result)
    assert "No meals logged" in text
    assert "unknown" in text.casefold()
    assert "Energy: 0" not in text
    assert "Protein: 0" not in text
    assert await ledger_counts(store) == (0, 0)


@pytest.mark.parametrize(
    "command",
    [
        "/today yesterday",
        "/today yesterday short",
        "/today short yesterday",
        "/today yesterday full",
        "/today 2023-11-13",
        "/today 2023-11-13 full",
        "/today short 2023-11-13",
    ],
)
async def test_explicit_past_day_reports_only_that_calendar_date(
    service, store, daily_catalog, command
):
    await process(service, store, message(1, "100g rice"))
    await process(service, store, message(2, "/meal yesterday 100g chicken"))
    result = await process(service, store, message(3, command))
    text = report_text(result, "2023-11-13")
    assert "Energy: 200 kcal" in text
    assert "Protein: 20.0 g" in text
    assert await ledger_counts(store) == (2, 2)


@pytest.mark.parametrize(
    "command",
    [
        "/today tomorrow",
        "/today 2023-11-15",
        "/today 2023-02-29",
        "/today 2023-11-13 2023-11-14",
        "/today yesterday yesterday",
        "/today short full",
        "/today 2023-11-13 nonsense",
        "/today calories 150g rice",
    ],
)
async def test_malformed_or_future_report_request_provides_help_without_mutation(
    service, store, daily_catalog, command
):
    await process(service, store, message(1, "100g rice"))
    result = await process(service, store, message(2, command))
    assert "/today" in result["payload"]["text"]
    assert not result["payload"]["text"].startswith("Daily food log ·")
    assert await ledger_counts(store) == (1, 1)


async def test_corrections_deletion_and_undo_are_immediately_reflected_in_totals(
    service, store, daily_catalog
):
    receipt = await process(service, store, message(1, "100g rice"))
    first = await process(service, store, message(2, "/today"))
    assert "Energy: 100 kcal" in report_text(first)
    edited = await process(service, store, reply(3, "rice was 120g", receipt))
    second = await process(service, store, message(4, "/today"))
    assert "Energy: 120 kcal" in report_text(second)
    deleted = await process(service, store, reply(5, "delete", edited))
    third = await process(service, store, message(6, "/today"))
    assert "No meals logged" in report_text(third)
    await process(service, store, reply(7, "undo", deleted))
    fourth = await process(service, store, message(8, "/today"))
    assert "Energy: 120 kcal" in report_text(fourth)
    assert await ledger_counts(store) == (1, 4)


async def test_meal_date_correction_moves_totals_between_days(service, store, daily_catalog):
    receipt = await process(service, store, message(1, "100g rice"))
    await process(service, store, reply(2, "date yesterday", receipt))
    today = await process(service, store, message(3, "/today"))
    yesterday = await process(service, store, message(4, "/today yesterday"))
    assert "No meals logged" in report_text(today)
    assert "Energy: 100 kcal" in report_text(yesterday, "2023-11-13")
    assert await ledger_counts(store) == (1, 2)


async def test_report_request_in_receipt_reply_is_read_only(service, store, daily_catalog):
    receipt = await process(service, store, message(1, "100g rice"))
    result = await process(service, store, reply(2, "/today", receipt))
    assert "Energy: 100 kcal" in report_text(result)
    assert await action_kind(store, result) == "daily_report"
    assert await ledger_counts(store) == (1, 1)


async def test_edited_telegram_message_does_not_turn_into_daily_report(
    service, store, daily_catalog
):
    await process(service, store, message(1, "100g rice"))
    update = message(2, "/today", edited=True).model_dump(mode="json", exclude_none=True)
    update["edited_message"]["message_id"] = 1
    result = await process(service, store, Update.model_validate(update))
    assert "diary unchanged" in result["payload"]["text"]
    assert await action_kind(store, result) != "daily_report"
    assert await ledger_counts(store) == (1, 1)


async def test_replayed_report_update_has_one_receipt_and_no_ledger_mutation(
    service, store, daily_catalog
):
    await process(service, store, message(1, "100g rice"))
    update = message(2, "/today")
    first = await process(service, store, update)
    await service.accept([update, update])
    assert not await service.process_one()
    assert "Energy: 100 kcal" in report_text(first)
    assert await ledger_counts(store) == (1, 1)
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(sa.func.count())
                .select_from(outbox)
                .where(outbox.c.action_key == "update:2")
            )
            == 1
        )


@pytest.mark.parametrize("identity", [{"user": 202}, {"chat": 202}, {"kind": "group"}])
async def test_unauthorized_daily_report_request_produces_no_reply(
    service, store, daily_catalog, identity
):
    await process(service, store, message(1, "100g rice"))
    await service.accept([message(2, "/today", **identity)])
    assert not await service.process_one()
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(outbox)) == 1
        assert (
            await connection.scalar(sa.select(actions.c.kind).where(actions.c.key == "update:2"))
            is None
        )
    assert await ledger_counts(store) == (1, 1)


@pytest.mark.parametrize(
    ("timezone", "before_midnight", "after_midnight", "day"),
    [
        ("UTC", "2024-03-10T23:59:00+00:00", "2024-03-11T00:01:00+00:00", "2024-03-11"),
        (
            "America/New_York",
            "2024-03-10T04:59:00+00:00",
            "2024-03-10T05:01:00+00:00",
            "2024-03-10",
        ),
        (
            "America/New_York",
            "2024-11-04T04:59:00+00:00",
            "2024-11-04T05:01:00+00:00",
            "2024-11-04",
        ),
    ],
)
async def test_request_day_uses_profile_timezone_and_local_midnight(
    service, store, daily_catalog, timezone, before_midnight, after_midnight, day
):
    await set_timezone(store, timezone)
    await process(service, store, dated_message(1, "100g rice", before_midnight))
    await process(service, store, dated_message(2, "100g chicken", after_midnight))
    result = await process(service, store, dated_message(3, "/today", after_midnight))
    assert "Energy: 200 kcal" in report_text(result, day)
    assert await ledger_counts(store) == (2, 2)


@pytest.mark.parametrize(
    ("earlier", "later", "day"),
    [
        ("2024-03-10T06:30:00+00:00", "2024-03-10T07:30:00+00:00", "2024-03-10"),
        ("2024-11-03T05:30:00+00:00", "2024-11-03T06:30:00+00:00", "2024-11-03"),
    ],
)
async def test_dst_clock_jump_and_repeated_hour_stay_in_same_local_day(
    service, store, daily_catalog, earlier, later, day
):
    await set_timezone(store, "America/New_York")
    await process(service, store, dated_message(1, "100g rice", earlier))
    await process(service, store, dated_message(2, "100g chicken", later))
    result = await process(service, store, dated_message(3, "/today", later))
    assert "Energy: 300 kcal" in report_text(result, day)


async def test_timezone_change_does_not_reassign_historical_meal_date(
    service, store, daily_catalog
):
    timestamp = "2024-03-11T01:00:00+00:00"
    await set_timezone(store, "UTC")
    await process(service, store, dated_message(1, "100g rice", timestamp))
    await set_timezone(store, "America/New_York")
    result = await process(service, store, dated_message(2, "/today", timestamp))
    assert "No meals logged" in report_text(result, "2024-03-10")
    historic = await process(
        service, store, dated_message(3, "/today 2024-03-11", "2024-03-12T12:00:00+00:00")
    )
    assert "Energy: 100 kcal" in report_text(historic, "2024-03-11")
    assert (await current(store)).timezone == "UTC"
    assert await ledger_counts(store) == (1, 1)


async def test_catalog_refresh_never_rewrites_daily_snapshot_totals(service, store, daily_catalog):
    await process(service, store, message(1, "100g rice"))
    async with store.write() as connection:
        await publish_reviewed_food(
            connection,
            food_record("rice", energy="999", protein="99", fiber="9"),
            food_id=daily_catalog["rice"].food_id,
        )
    original = await process(service, store, message(2, "/today"))
    assert "Energy: 100 kcal" in report_text(original)
    await process(service, store, message(3, "100g rice"))
    revised = await process(service, store, message(4, "/today"))
    assert "Energy: 1099 kcal" in report_text(revised)
    assert "Protein: 109.0 g" in report_text(revised)
    assert await ledger_counts(store) == (2, 2)
