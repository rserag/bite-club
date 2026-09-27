from datetime import date, datetime, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from aiogram.types import Update

from nutrition_bot.adapters.database.schema_weights import body_weight_revisions, body_weights
from nutrition_bot.adapters.database.weights import daily_weights
from nutrition_bot.domain.weight_trends import DailyWeight, summarize_weight
from tests.helpers import callback, message
from tests.test_telegram_meals import process


def weight_press(receipt, operation, *, update_id=2, callback_id=None):
    value = callback(
        receipt["button_token"],
        update_id=update_id,
        callback_id=callback_id or f"synthetic-weight-{update_id}",
        message_id=receipt["telegram_message_id"],
    ).model_dump(mode="json", exclude_none=True)
    value["callback_query"]["data"] = f"weight:{operation}:{receipt['button_token']}"
    return Update.model_validate(value)


def test_sparse_measurements_never_produce_trends():
    today = date(2026, 9, 21)
    one = summarize_weight([DailyWeight(today, Fraction(79400), 1, True)], as_of=today)
    assert one.latest is not None
    assert one.rolling_7d_grams is None
    assert one.weekly_rate_grams is None


def test_rolling_average_requires_four_calendar_dates_and_theil_sen_is_robust():
    today = date(2026, 9, 21)
    points = [
        DailyWeight(today - timedelta(days=day), Fraction(80000 + day * 100), 1, True)
        for day in range(0, 22, 3)
    ]
    result = summarize_weight(points, as_of=today)
    assert result.rolling_dates == 3
    assert result.rolling_7d_grams is None
    assert result.trend_dates == 8
    assert result.trend_span_days == 21
    assert result.weekly_rate_grams == -700


async def test_natural_weight_log_reports_sparse_evidence(service, store):
    saved = await process(service, store, message(1, "weight 79.4"))
    assert saved["payload"]["weight_id"] == 1
    assert "79.4 kg" in saved["payload"]["text"]
    assert "7-day average: unavailable (1/7" in saved["payload"]["text"]
    assert "Multiweek rate: unavailable" in saved["payload"]["text"]
    assert {button["text"] for button in saved["payload"]["buttons"]} == {
        "Edit",
        "Delete",
        "Undo",
    }


async def test_corrections_and_delete_undo_retain_all_revisions(service, store):
    await process(service, store, message(1, "/weight 79.4 morning"))
    corrected = await process(service, store, message(2, "/weight edit W1r1 79.2"))
    assert "W1r2" in corrected["payload"]["text"]
    deleted = await process(service, store, weight_press(corrected, "delete", update_id=3))
    assert "Deleted from trend" in deleted["payload"]["text"]
    restored = await process(service, store, weight_press(deleted, "undo", update_id=4))
    assert "W1r4" in restored["payload"]["text"]
    assert "79.2 kg" in restored["payload"]["text"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(body_weights)) == 1
        revisions = (
            (
                await connection.execute(
                    sa.select(body_weight_revisions).order_by(body_weight_revisions.c.id)
                )
            )
            .mappings()
            .all()
        )
        assert [row["operation"] for row in revisions] == ["create", "edit", "delete", "undo"]
        assert [row["weight_grams"] for row in revisions] == [79400, 79200, 79200, 79200]
        assert all(row["timing"] == "morning" for row in revisions)
        with pytest.raises(sa.exc.IntegrityError):
            await connection.execute(sa.update(body_weight_revisions).values(weight_grams=70000))


async def test_old_weight_button_cannot_change_new_revision(service, store):
    saved = await process(service, store, message(1, "/weight 79.4"))
    await process(service, store, message(2, "/weight edit W1r1 79.2"))
    stale = await process(service, store, weight_press(saved, "delete", update_id=3))
    assert "older receipt" in stale["payload"]["text"]
    async with store.engine.connect() as connection:
        rows = await daily_weights(connection, start=date(2000, 1, 1), end=date(2100, 1, 1))
    assert rows[-1].grams == 79200


async def test_daily_representative_prefers_morning_then_uses_median(service, store, settings):
    await process(service, store, message(1, "weight 79.8"))
    async with store.engine.connect() as connection:
        initial = await daily_weights(connection, start=date(2000, 1, 1), end=date(2100, 1, 1))
    today = initial[0].day
    yesterday = today - timedelta(days=1)
    await process(service, store, message(2, "weight 79.2 morning"))
    await process(service, store, message(3, f"/weight {yesterday} 80.0"))
    await process(service, store, message(4, f"/weight {yesterday} 79.0"))
    async with store.engine.connect() as connection:
        rows = await daily_weights(connection, start=yesterday, end=today)
    assert rows[0].grams == 79500
    assert rows[0].measurement_count == 2 and not rows[0].used_morning
    assert rows[1].grams == 79200
    assert rows[1].measurement_count == 2 and rows[1].used_morning


async def test_weight_history_and_invalid_future_date_are_safe(service, store, settings):
    await process(service, store, message(1, "/weight 79.4"))
    history = await process(service, store, message(2, "/weight history"))
    assert "W1r1" in history["payload"]["text"]
    future = datetime.now(ZoneInfo(settings.app_timezone)).date() + timedelta(days=1)
    rejected = await process(service, store, message(3, f"/weight {future} 79.0"))
    assert "Future" in rejected["payload"]["text"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(body_weights)) == 1
