from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from aiogram.types import Update

from nutrition_bot.adapters.database.goals import apply_proposal, create_proposal
from nutrition_bot.adapters.database.schema_checkins import daily_checkin_events
from nutrition_bot.domain.goals import manual_targets
from tests.helpers import callback, message
from tests.test_telegram_daily import daily_catalog as daily_catalog
from tests.test_telegram_meals import process

DAY = date(2023, 11, 14)


def daily_press(receipt, operation, *, update_id=10, callback_id=None):
    value = callback(
        receipt["button_token"],
        update_id=update_id,
        callback_id=callback_id or f"synthetic-daily-{update_id}",
        message_id=receipt["telegram_message_id"],
    ).model_dump(mode="json", exclude_none=True)
    value["callback_query"]["data"] = f"daily:{operation}:{receipt['button_token']}"
    return Update.model_validate(value)


async def seed_target(service, store):
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
            effective_from=DAY,
        )


async def test_daily_view_uses_historical_target_and_discloses_partial_macro_data(
    service, store, daily_catalog
):
    await seed_target(service, store)
    await process(service, store, message(1, "100g rice"))
    report = await process(service, store, message(2, "/today"))
    text = report["payload"]["text"]
    assert "Energy: 100 kcal / 1000 kcal target · 900 kcal remaining" in text
    assert "Protein: 10.0 g / 100 g target · 90.0 g remaining" in text
    assert "Fat: 0.0 g / 40 g target · 40.0 g remaining" in text
    assert "Carbohydrate: unknown / 60 g target · data 0/1 foods" in text
    assert "remaining unknown" in text
    assert "Progress is provisional until all food is logged" in text
    full = await process(service, store, message(4, "/today full"))
    assert "Target T1: 1000 kcal · P 100 g · F 40 g · C 60 g" in full["payload"]["text"]
    assert "Training does not add calories back" in full["payload"]["text"]
    assert {button["text"] for button in report["payload"]["buttons"]} == {
        "All food logged",
        "Not all logged",
        "Log food",
        "Details",
    }
    historic = await process(service, store, message(3, "/today yesterday full"))
    assert "No calorie or macro target was effective" in historic["payload"]["text"]


async def test_complete_and_incomplete_actions_are_audited(service, store, daily_catalog):
    await process(service, store, message(1, "100g rice"))
    report = await process(service, store, message(2, "/today"))
    complete = await process(service, store, daily_press(report, "complete", update_id=3))
    assert "All food logged for this date" in complete["payload"]["text"]
    incomplete = await process(service, store, daily_press(complete, "incomplete", update_id=4))
    assert "Not all food logged for this date" in incomplete["payload"]["text"]
    async with store.engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    sa.select(daily_checkin_events).order_by(daily_checkin_events.c.id)
                )
            )
            .mappings()
            .all()
        )
        assert [row["food_status"] for row in rows] == ["complete", "incomplete"]
        with pytest.raises(sa.exc.IntegrityError):
            await connection.execute(sa.update(daily_checkin_events).values(food_status="complete"))


async def test_complete_marker_becomes_unknown_after_food_log_changes(
    service, store, daily_catalog
):
    await process(service, store, message(1, "100g rice"))
    report = await process(service, store, message(2, "/today"))
    await process(service, store, daily_press(report, "complete", update_id=3))
    await process(service, store, message(4, "100g rice"))
    changed = await process(service, store, message(5, "/today"))
    assert (
        "Food log changed after it was marked all food logged; check it again"
        in changed["payload"]["text"]
    )


async def test_unresolved_draft_withholds_remaining_and_blocks_complete(
    service, store, daily_catalog
):
    await seed_target(service, store)
    await process(service, store, message(1, "about 100g rice"))
    report = await process(service, store, message(2, "/today"))
    assert "Remaining targets withheld until unresolved meal drafts" in report["payload"]["text"]
    rejected = await process(service, store, daily_press(report, "complete", update_id=3))
    assert "unresolved meal draft" in rejected["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(daily_checkin_events))
            == 0
        )


async def test_empty_day_never_turns_missing_intake_into_remaining_targets(service, store):
    await seed_target(service, store)
    report = await process(service, store, message(1, "/today"))
    text = report["payload"]["text"]
    assert "Intake is unknown, not zero" in text
    assert "Remaining targets withheld because recorded intake is unknown" in text
    assert "1000 kcal remaining" not in text

    complete = await process(service, store, daily_press(report, "complete", update_id=2))
    completed_text = complete["payload"]["text"]
    assert "explicitly marked complete, so recorded intake is zero" in completed_text
    assert "Energy 1000 kcal remaining" in completed_text


async def test_new_incomplete_wording_is_audited_and_replayed_once(service, store, daily_catalog):
    await process(service, store, message(1, "100g rice"))
    update = message(2, "not all logged")
    result = await process(service, store, update)
    assert "Not all food logged for this date" in result["payload"]["text"]
    assert "Have you logged everything eaten on 2023-11-14?" in result["payload"]["text"]
    await service.accept([update, update])
    assert not await service.process_one()
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(daily_checkin_events))
            == 1
        )


async def test_report_view_and_completion_actions_keep_the_selected_historical_date(
    service, store, daily_catalog
):
    await process(service, store, message(1, "/meal yesterday 100g rice"))
    report = await process(service, store, message(2, "/today yesterday short"))
    assert "Have you logged everything eaten on 2023-11-13?" in report["payload"]["text"]
    full = await process(service, store, daily_press(report, "full", update_id=3))
    assert "Daily food log · 2023-11-13" in full["payload"]["text"]
    short = await process(service, store, daily_press(full, "short", update_id=4))
    assert "Energy: 100 kcal" in short["payload"]["text"]
    completed = await process(service, store, daily_press(short, "complete", update_id=5))
    assert "Daily food log · 2023-11-13" in completed["payload"]["text"]
    assert "All food logged for this date" in completed["payload"]["text"]
    today = await process(service, store, message(6, "/today short"))
    assert "Have you logged everything eaten on 2023-11-14?" in today["payload"]["text"]
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(daily_checkin_events.c.local_date)) == date(
            2023, 11, 13
        )
