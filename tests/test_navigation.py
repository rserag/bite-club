"""Synthetic guided daily-use acceptance, including stale flows and replay."""

import pytest
import sqlalchemy as sa
from aiogram.types import Update

from nutrition_bot.adapters.database.schema_ui import ui_flows
from nutrition_bot.application.navigation import begin
from nutrition_bot.application.service import Service
from nutrition_bot.runtime.worker import send_one
from tests.helpers import FakeGateway, callback, message
from tests.test_telegram_drafts import press as approve_draft
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import ledger_counts, process


def tap(receipt, label, uid):
    buttons = receipt["payload"]["buttons"]
    button = next(b for b in buttons if b["text"] == label)
    update = callback(
        receipt["button_token"],
        update_id=uid,
        callback_id=f"nav-{uid}",
        message_id=receipt["telegram_message_id"],
    ).model_dump(mode="json", exclude_none=True)
    update["callback_query"]["data"] = button["callback_data"]
    return Update.model_validate(update)


async def test_start_help_topics_are_short_and_home_is_clickable(service, store):
    start = await process(service, store, message(1, "/start"))
    assert len(start["payload"]["text"]) < 200
    assert len(start["payload"]["buttons"]) == 9
    help_ = await process(service, store, message(2, "/help"))
    assert len(help_["payload"]["text"]) < 250
    assert "Targets & setup" in [b["text"] for b in help_["payload"]["buttons"]]
    gateway = FakeGateway()
    await process(service, store, message(3, "/home"), sent=False)
    assert await send_one(service, gateway)
    assert len(gateway.keyboards[0]) == 9


async def guided(service, store, catalog, *, rough=False):
    home = await process(service, store, message(1, "/home"))
    await process(service, store, tap(home, "Log food", 2))
    choices = await process(service, store, message(3, "rice"))
    select = choices["payload"]["buttons"][0]["text"]
    await process(service, store, tap(choices, select, 4))
    return await process(service, store, message(5, "about 150 g" if rough else "150 g"))


@pytest.mark.parametrize("rough", [False, True])
async def test_guided_food_portion_review_uses_existing_ledger(service, store, catalog, rough):
    review = await guided(service, store, catalog, rough=rough)
    assert await ledger_counts(store) == (0, 0)
    update = tap(review, "Save / review draft", 6)
    result = await process(service, store, update)
    assert result is not None
    if rough:
        assert "Not in your totals" in result["payload"]["text"]
        assert result["payload"]["draft_revision"] == 1
        assert await ledger_counts(store) == (0, 0)
    else:
        assert "Saved" in result["payload"]["text"]
        assert await ledger_counts(store) == (1, 1)
    await service.accept([update])
    assert not await service.process_one()


async def test_old_save_button_cannot_save_reopened_guided_flow(service, store, catalog):
    review = await guided(service, store, catalog)
    await process(service, store, message(6, "/cancel"))
    home = await process(service, store, message(7, "/home"))
    await process(service, store, tap(home, "Log food", 8))
    choices = await process(service, store, message(9, "chicken"))
    await process(service, store, tap(choices, choices["payload"]["buttons"][0]["text"], 10))
    await process(service, store, message(11, "200 g"))
    stale = await process(service, store, tap(review, "Save / review draft", 12))
    assert "changed or expired" in stale["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)


async def test_guided_flow_survives_new_service_and_expires_without_saving(
    service, store, settings, catalog
):
    home = await process(service, store, message(1, "/home"))
    await process(service, store, tap(home, "Log food", 2))
    restarted = Service(store, settings)
    choices = await process(restarted, store, message(3, "rice"))
    assert "Choose the exact" in choices["payload"]["text"]
    async with store.write() as connection:
        await connection.execute(sa.update(ui_flows).values(updated_at=1))
    result = await process(
        restarted, store, tap(choices, choices["payload"]["buttons"][0]["text"], 4)
    )
    assert "expired" in result["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)


async def test_receipt_repeat_save_and_edit_keep_current_revision(service, store, catalog):
    saved = await process(service, store, message(1, "150g rice"))
    more = await process(service, store, tap(saved, "More", 2))
    favorite_prompt = await process(service, store, tap(more, "Save favorite", 3))
    assert "name" in favorite_prompt["payload"]["text"]
    named = await process(service, store, message(4, "usual breakfast"))
    assert "Saved favorite" in named["payload"]["text"]
    repeated = await process(service, store, tap(saved, "Log again", 5))
    assert "Saved" in repeated["payload"]["text"]
    assert await ledger_counts(store) == (2, 2)
    await process(service, store, tap(saved, "Edit", 6))
    edit = await process(service, store, message(7, "item 1: 120g"))
    assert "120" in edit["payload"]["text"]
    stale = await process(service, store, tap(saved, "Log again", 8))
    assert "has changed" in stale["payload"]["text"]
    assert await ledger_counts(store) == (2, 3)


async def test_forged_navigation_action_is_rejected_without_flow(service, store):
    home = await process(service, store, message(1, "/home"))
    update = tap(home, "Log food", 2).model_dump(mode="json", exclude_none=True)
    update["callback_query"]["data"] = f"ui:99:{home['button_token']}"
    assert await process(service, store, Update.model_validate(update)) is None
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(ui_flows)) == 0


async def test_internal_events_never_change_polling_cursor(service, store):
    internal = message(1, "/home").model_copy(update={"update_id": -21})
    await service.accept_internal([internal])
    assert await service.offset() == 0
    assert await service.process_one()
    with pytest.raises(ValueError):
        await service.accept_internal([message(2, "/home")])


@pytest.mark.parametrize("quantity", ["0 g", "50001 g", "51 kg", "0.001 mg", "9" * 1000])
async def test_guided_amount_rejects_unsupported_mass_before_review(
    service, store, catalog, quantity
):
    async with store.write() as connection:
        await begin(
            connection,
            101,
            "amount",
            {"food_id": catalog["rice"].version_id, "name": "rice", "items": []},
        )
    response = await process(service, store, message(1, quantity))
    assert "positive measured amount" in response["payload"]["text"]
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(ui_flows))).mappings().one()
        assert row["stage"] == "amount"
        assert row["payload"]["items"] == []
    assert await ledger_counts(store) == (0, 0)


async def test_guided_ten_food_review_is_bounded_and_keeps_save(service, store, catalog):
    long_name = "𝓐" * 500
    item = {"food_id": catalog["rice"].version_id, "name": long_name, "quantity": "150g"}
    async with store.write() as connection:
        await begin(
            connection,
            101,
            "amount",
            {
                "food_id": catalog["rice"].version_id,
                "name": long_name,
                "items": [item.copy() for _ in range(9)],
            },
        )
    review = await process(service, store, message(1, "000150.000 g"))
    assert len(review["payload"]["text"].encode("utf-16-le")) // 2 < 3800
    assert "150.000g" in review["payload"]["text"]
    assert "Add another food" not in [button["text"] for button in review["payload"]["buttons"]]
    assert "Save / review draft" in [button["text"] for button in review["payload"]["buttons"]]
    result = await process(service, store, tap(review, "Save / review draft", 2))
    assert "Saved" in result["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)


async def test_same_repeat_receipt_does_not_duplicate_distinct_taps(service, store, catalog):
    saved = await process(service, store, message(1, "150g rice"))
    repeated = await process(service, store, tap(saved, "Log again", 2))
    assert "Saved" in repeated["payload"]["text"]
    second = await process(service, store, tap(saved, "Log again", 3))
    assert "Nothing was changed" in second["payload"]["text"]
    assert await ledger_counts(store) == (2, 2)
    fresh_intent = await process(service, store, tap(repeated, "Log again", 4))
    assert "Saved" in fresh_intent["payload"]["text"]
    assert await ledger_counts(store) == (3, 3)


async def test_new_repeat_receipt_needs_fresh_estimate_approval(service, store, catalog):
    draft = await process(service, store, message(1, "about 150g rice"))
    saved = await process(service, store, approve_draft(draft, update_id=2))
    repeated = await process(service, store, tap(saved, "Log again", 3))
    assert repeated["payload"]["draft_id"] != draft["payload"]["draft_id"]
    assert await ledger_counts(store) == (1, 1)
    duplicate = await process(service, store, tap(saved, "Log again", 4))
    assert "Nothing was changed" in duplicate["payload"]["text"]
    approved_repeat = await process(service, store, approve_draft(repeated, update_id=5))
    assert await ledger_counts(store) == (2, 2)
    next_repeat = await process(service, store, tap(approved_repeat, "Log again", 6))
    assert next_repeat["payload"]["draft_id"] not in {
        draft["payload"]["draft_id"],
        repeated["payload"]["draft_id"],
    }
    assert "fresh approval required" in next_repeat["payload"]["text"]
    assert await ledger_counts(store) == (2, 2)
