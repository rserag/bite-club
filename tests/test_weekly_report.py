from datetime import date, timedelta
from decimal import Decimal

import pytest
from aiogram.types import Update

from nutrition_bot.adapters.database.goals import apply_proposal, create_proposal
from nutrition_bot.application.weekly_report import WeeklyRequestError, parse_week_request
from nutrition_bot.domain.goals import manual_targets
from tests.helpers import callback, message
from tests.test_daily_targets import daily_press
from tests.test_telegram_daily import daily_catalog as daily_catalog
from tests.test_telegram_meals import process

END = date(2023, 11, 14)
START = END - timedelta(days=6)


def weekly_press(receipt, operation, *, update_id=80):
    value = callback(
        receipt["button_token"],
        update_id=update_id,
        callback_id=f"synthetic-weekly-{update_id}",
        message_id=receipt["telegram_message_id"],
    ).model_dump(mode="json", exclude_none=True)
    value["callback_query"]["data"] = f"weekly:{operation}:{receipt['button_token']}"
    return Update.model_validate(value)


async def seed_week_target(service, store):
    await process(service, store, message(900, "/status"))
    await process(service, store, message(901, "/status"))
    proposal = manual_targets(
        mode="maintenance",
        weight_kg=Decimal("80"),
        rate_kg_per_week=Decimal("0"),
        energy_kcal=1000,
        protein_grams=100,
        fat_grams=40,
    )
    async with store.write() as connection:
        saved = await create_proposal(connection, proposal, action_key="update:900")
        await apply_proposal(
            connection,
            saved.id,
            action_key="update:901",
            effective_from=START,
        )


@pytest.mark.parametrize(
    ("text", "short", "end"),
    [
        ("/week", False, END),
        ("/week short", True, END),
        ("/week full 2023-11-10", False, date(2023, 11, 10)),
        ("show this week's calories", False, END),
        ("short weekly report", True, END),
    ],
)
def test_week_request_parser(text, short, end):
    request = parse_week_request(text, today=END)
    assert request is not None and request.short is short and request.end == end


@pytest.mark.parametrize("text", ["/week extra", "/week short full", "/week 2023-11-15"])
def test_invalid_week_request(text):
    with pytest.raises(WeeklyRequestError):
        parse_week_request(text, today=END)


async def _populate_week(service, store):
    for offset in range(7):
        day = START + timedelta(days=offset)
        grams = 100 if offset < 5 else 200 if offset == 5 else 300
        await process(service, store, message(1 + offset, f"/meal {day} {grams}g rice"))
        report = await process(service, store, message(20 + offset, f"/today {day}"))
        if offset < 5:
            await process(
                service,
                store,
                daily_press(report, "complete", update_id=40 + offset),
            )
        elif offset == 5:
            await process(
                service,
                store,
                daily_press(report, "incomplete", update_id=45),
            )


async def test_full_week_uses_only_complete_dates_and_historical_targets(
    service, store, daily_catalog
):
    await seed_week_target(service, store)
    await _populate_week(service, store)
    report = await process(service, store, message(70, "/week full"))
    text = report["payload"]["text"]
    assert f"Weekly report · {START} to {END}" in text
    assert "Coverage: complete 5 · incomplete 1 · unknown 1" in text
    assert text.count(" · complete · ") == 5
    assert "2023-11-13 · incomplete · energy excluded / target 1000" in text
    assert "2023-11-14 · unknown · energy excluded / target 1000" in text
    assert "Energy: 100 kcal · data 5/5 · avg difference vs target -900 · 5 dates" in text
    assert "Weight trend: unavailable" in text
    assert "no reviewed micronutrient reference set" in text
    assert "Incomplete and unknown dates are excluded rather than counted as zero" in text
    assert {button["text"] for button in report["payload"]["buttons"]} == {"Short version"}
    assert len(text.encode("utf-16-le")) // 2 <= 4096


async def test_short_week_uses_same_facts_and_can_switch_back(service, store, daily_catalog):
    await _populate_week(service, store)
    full = await process(service, store, message(70, "/week full"))
    short = await process(service, store, weekly_press(full, "short", update_id=71))
    text = short["payload"]["text"]
    assert "Coverage: complete 5 · incomplete 1 · unknown 1" in text
    assert "Complete-day averages use 5/7 dates" in text
    assert "Micronutrient data" not in text
    assert "Full report: /week 2023-11-14" in text
    assert short["payload"]["buttons"][0]["text"] == "Full report"
    restored = await process(service, store, weekly_press(short, "full", update_id=72))
    assert "Micronutrient data" in restored["payload"]["text"]


async def test_sparse_week_withholds_micronutrient_screening(service, store, daily_catalog):
    await process(service, store, message(1, "/meal 2023-11-14 100g rice"))
    today = await process(service, store, message(2, "/today"))
    await process(service, store, daily_press(today, "complete", update_id=3))
    report = await process(service, store, message(4, "/week full"))
    assert (
        "Possible-gap screening withheld: fewer than 5 complete dates" in report["payload"]["text"]
    )
