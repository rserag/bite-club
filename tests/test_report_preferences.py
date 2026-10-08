from datetime import date

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.schema_checkins import daily_checkin_events
from nutrition_bot.application.daily_report import parse_request
from nutrition_bot.application.settings_service import load_preferences
from tests.helpers import message
from tests.test_daily_targets import daily_press
from tests.test_telegram_daily import daily_catalog as daily_catalog
from tests.test_telegram_meals import process
from tests.test_weekly_report import weekly_press


@pytest.mark.parametrize("command", ["/today", "/today yesterday", "how am I doing today?"])
async def test_daily_defaults_to_compact_and_exposes_details(
    service, store, daily_catalog, command
):
    await process(service, store, message(1, "/meal yesterday 100g rice"))
    await process(service, store, message(2, "/meal today 100g rice"))
    receipt = await process(service, store, message(3, command))
    text = receipt["payload"]["text"]
    assert "Calcium:" not in text
    assert "Details" in {button["text"] for button in receipt["payload"]["buttons"]}
    assert " full" in text


async def test_daily_setting_applies_to_date_refs_and_explicit_views_override(
    service, store, daily_catalog
):
    await process(service, store, message(1, "/meal yesterday 100g rice"))
    await process(service, store, message(2, "/settings report full"))
    full = await process(service, store, message(3, "/today yesterday"))
    assert "Calcium: unknown" in full["payload"]["text"]
    assert "2023-11-13" == full["payload"]["daily_date"]
    compact = await process(service, store, message(4, "/today short 2023-11-13"))
    assert "Calcium:" not in compact["payload"]["text"]
    assert "2023-11-13" == compact["payload"]["daily_date"]
    await process(service, store, message(5, "/settings report short"))
    explicit = await process(service, store, message(6, "/today 2023-11-13 full"))
    assert "Calcium: unknown" in explicit["payload"]["text"]


async def test_daily_view_toggle_preserves_bound_historical_day_and_does_not_mark_food_complete(
    service, store, daily_catalog
):
    await process(service, store, message(1, "/meal yesterday 100g rice"))
    compact = await process(service, store, message(2, "/today yesterday"))
    detailed = await process(service, store, daily_press(compact, "full", update_id=3))
    assert "Daily food log · 2023-11-13" in detailed["payload"]["text"]
    assert "Energy: 100 kcal" in detailed["payload"]["text"]
    assert "Calcium: unknown" in detailed["payload"]["text"]
    assert detailed["payload"]["daily_date"] == compact["payload"]["daily_date"]
    again = await process(service, store, daily_press(detailed, "short", update_id=4))
    assert "Daily food log · 2023-11-13" in again["payload"]["text"]
    assert "Calcium:" not in again["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(daily_checkin_events))
            == 0
        )
        assert (await load_preferences(connection)).report_length == "short"


async def test_unoffered_daily_toggle_is_rejected_by_bound_callback(service, store):
    compact = await process(service, store, message(1, "/today"))
    rejected = await process(service, store, daily_press(compact, "short", update_id=2))
    assert rejected is None


@pytest.mark.parametrize("command", ["/week", "/week 2023-11-13", "weekly report"])
async def test_weekly_defaults_to_compact_and_respects_saved_report_setting(
    service, store, command
):
    compact = await process(service, store, message(1, command))
    assert "Micronutrient data" not in compact["payload"]["text"]
    assert compact["payload"]["buttons"][0]["text"] == "Full report"
    assert " full" in compact["payload"]["text"]
    await process(service, store, message(2, "/settings report full"))
    detailed = await process(service, store, message(3, command))
    assert "Micronutrient data" in detailed["payload"]["text"]
    assert detailed["payload"]["weekly_end"] == compact["payload"]["weekly_end"]
    compact_override = await process(service, store, message(4, "/week short"))
    assert "Micronutrient data" not in compact_override["payload"]["text"]


async def test_weekly_toggle_preserves_date_and_does_not_change_preferences(service, store):
    compact = await process(service, store, message(1, "/week 2023-11-13"))
    detailed = await process(service, store, weekly_press(compact, "full", update_id=2))
    assert "2023-11-13" == detailed["payload"]["weekly_end"]
    assert "Micronutrient data" in detailed["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (await load_preferences(connection)).report_length == "short"


@pytest.mark.parametrize(
    "text,default_short,short",
    [
        ("/today yesterday", True, True),
        ("/today yesterday", False, False),
        ("/today full yesterday", True, False),
        ("/today short yesterday", False, True),
    ],
)
def test_daily_parser_default_never_overrides_explicit_view(text, default_short, short):
    request = parse_request(text, today=date(2023, 11, 14), default_short=default_short)
    assert request.day == date(2023, 11, 13)
    assert request.short is short
