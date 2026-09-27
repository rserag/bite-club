"""Offline owner-only draft acceptance with synthetic foods and Telegram updates."""

import time

import pytest
import sqlalchemy as sa
from aiogram.types import Update

from nutrition_bot.adapters.database.drafts import get_draft
from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.schema import food_versions, inbox, outbox
from nutrition_bot.adapters.database.schema_drafts import meal_drafts
from nutrition_bot.domain.drafts import DRAFT_TTL_SECONDS
from nutrition_bot.domain.food import PortionInput
from tests.helpers import callback, message
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import (
    current,
    food_record,
    ledger_counts,
    process,
    reply,
)


def press(receipt, operation="approve", *, update_id=2, callback_id=None, **changes):
    value = callback(
        receipt["button_token"],
        update_id=update_id,
        callback_id=callback_id or f"synthetic-draft-{update_id}",
        message_id=receipt["telegram_message_id"] or 77,
    ).model_dump(mode="json", exclude_none=True)
    value["callback_query"]["data"] = f"draft:{operation}:{receipt['button_token']}"
    value["callback_query"].update(changes)
    return Update.model_validate(value)


async def draft(store, draft_id=1):
    async with store.engine.connect() as connection:
        return await get_draft(connection, draft_id)


async def age_draft(store, *, seconds=DRAFT_TTL_SECONDS + 1):
    async with store.write() as connection:
        await connection.execute(
            sa.update(meal_drafts).values(last_user_activity_at=time.time() - seconds)
        )


@pytest.mark.parametrize(
    "text", ["about 150g rice", "/meal about 150g rice", "I ate about 150g rice"]
)
async def test_rough_meal_waits_outside_totals_with_itemized_approval(
    service, store, catalog, text
):
    result = await process(service, store, message(1, text))
    payload = result["payload"]
    assert payload["draft_id"] == 1 and payload["draft_revision"] == 1
    assert {button["text"] for button in payload["buttons"]} == {
        "Enter amount",
        "Approve estimate",
        "Cancel",
    }
    assert "150" in payload["text"] and "rice" in payload["text"]
    assert "cooked" in payload["text"] and "Basis:" in payload["text"]
    assert "estimate" in payload["text"] and "Not in your totals" in payload["text"]
    assert await ledger_counts(store) == (0, 0)
    daily = await process(service, store, message(2, "/today"))
    assert "No meals logged" in daily["payload"]["text"]
    saved_draft = await draft(store)
    assert saved_draft.state == "open" and saved_draft.content.items[0].edible_milligrams == 150000


async def test_approval_saves_exact_displayed_revision_and_visible_provenance(
    service, store, catalog
):
    first = await process(service, store, message(1, "about 150g rice; 200g chicken"))
    before = time.time()
    approved = await process(service, store, press(first, callback_id="owner-consent"))
    saved = await current(store)
    assert [item.quantity_method for item in saved.items] == ["approved_estimate", "measured"]
    estimate, measured = saved.items
    assert estimate.edible_milligrams == 150000
    assert estimate.quantity_basis and "approximate" in estimate.quantity_basis.lower()
    assert estimate.approval_action_key == "callback:owner-consent"
    assert estimate.approval_draft_id == 1 and estimate.approval_draft_revision == 1
    assert before <= estimate.approved_at <= time.time()
    assert measured.approval_action_key is None and measured.quantity_basis is None
    assert "estimate" in approved["payload"]["text"].lower()
    assert (await draft(store)).state == "saved" and (await draft(store)).content is None
    report = await process(service, store, message(3, "/today"))
    assert "approved estimate 1" in report["payload"]["text"]
    assert "approximate" in report["payload"]["text"]
    assert "550 kcal" in report["payload"]["text"]


@pytest.mark.parametrize("text", ["yes", "ok", "okay", "approve", "approve estimate", "save"])
async def test_literal_text_never_approves_an_estimate(service, store, catalog, text):
    first = await process(service, store, message(1, "about 150g rice"))
    response = await process(service, store, reply(2, text, first))
    assert "button" in response["payload"]["text"].lower()
    assert await ledger_counts(store) == (0, 0)
    saved_draft = await draft(store)
    assert saved_draft.state == "open" and saved_draft.revision == 1


async def test_entering_measured_amount_saves_without_estimate_metadata(service, store, catalog):
    first = await process(service, store, message(1, "about 150g rice"))
    response = await process(service, store, reply(2, "item 1: 120g", first))
    saved = await current(store)
    assert saved.items[0].quantity_method == "measured"
    assert saved.items[0].edible_milligrams == 120000
    assert saved.items[0].quantity_basis is None
    assert saved.items[0].approval_action_key is None
    assert "measured" in response["payload"]["text"].lower()
    assert (await draft(store)).state == "saved"
    stale = await process(service, store, press(first, update_id=3))
    assert "already saved" in stale["payload"]["text"].lower()
    assert await ledger_counts(store) == (1, 1)


async def test_changed_rough_amount_requires_new_revision_button(service, store, catalog):
    first = await process(service, store, message(1, "about 150g rice"))
    second = await process(service, store, reply(2, "item 1: about 120g", first))
    assert second["payload"]["draft_revision"] == 2
    old = await process(service, store, press(first, update_id=3))
    assert "Old button" in old["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    await process(service, store, press(second, update_id=4))
    saved = await current(store)
    assert saved.items[0].edible_milligrams == 120000
    assert saved.items[0].approval_draft_revision == 2


async def test_measured_item_edit_does_not_approve_other_estimated_item(service, store, catalog):
    first = await process(service, store, message(1, "about 150g rice; 200g chicken"))
    updated = await process(service, store, reply(2, "item 2: 180g", first))
    assert updated["payload"]["draft_revision"] == 2
    assert await ledger_counts(store) == (0, 0)
    await process(service, store, press(updated, update_id=3))
    assert [
        (item.edible_milligrams, item.quantity_method) for item in (await current(store)).items
    ] == [
        (150000, "approved_estimate"),
        (180000, "measured"),
    ]


async def test_unknown_amount_stays_unresolved_without_an_approval_button(service, store, catalog):
    first = await process(service, store, message(1, "rice"))
    payload = first["payload"]
    assert payload["draft_id"] == 1
    assert "amount needed" in payload["text"]
    assert {button["text"] for button in payload["buttons"]} == {"Enter amount", "Cancel"}
    forged = await process(service, store, press(first, update_id=2))
    assert forged is None
    assert await ledger_counts(store) == (0, 0)
    assert (await draft(store)).content.items[0].edible_milligrams is None
    await process(service, store, reply(3, "120g", first))
    assert (await current(store)).items[0].quantity_method == "measured"


async def test_draft_list_open_and_explicit_revision_edit(service, store, catalog):
    await process(service, store, message(1, "about 150g rice"))
    await age_draft(store, seconds=3600)
    old_activity = (await draft(store)).last_user_activity_at
    listing = await process(service, store, message(2, "/drafts"))
    assert "D1r1" in listing["payload"]["text"]
    assert (await draft(store)).last_user_activity_at == old_activity
    opened = await process(service, store, message(3, "/draft D1"))
    assert opened["payload"]["draft_revision"] == 1
    assert (await draft(store)).last_user_activity_at > old_activity
    changed = await process(service, store, message(4, "/draft D1r1 item 1: about 125g"))
    assert changed["payload"]["draft_revision"] == 2
    rejected = await process(service, store, message(5, "/draft D1r1 item 1: 50g"))
    assert "Old receipt" in rejected["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    await process(service, store, message(6, "/draft D1r2 item 1: 125g"))
    assert (await current(store)).items[0].edible_milligrams == 125000


async def test_edit_button_only_displays_current_draft(service, store, catalog):
    first = await process(service, store, message(1, "about 150g rice"))
    await age_draft(store, seconds=3600)
    before = (await draft(store)).last_user_activity_at
    result = await process(service, store, press(first, "edit", update_id=2))
    assert "reply" in result["payload"]["text"].lower()
    assert (await draft(store)).revision == 1
    assert (await draft(store)).last_user_activity_at > before
    assert await ledger_counts(store) == (0, 0)


@pytest.mark.parametrize("via_button", [True, False])
async def test_cancelled_draft_cannot_be_approved(service, store, catalog, via_button):
    first = await process(service, store, message(1, "about 150g rice"))
    cancellation = press(first, "cancel", update_id=2) if via_button else reply(2, "cancel", first)
    await process(service, store, cancellation)
    assert (await draft(store)).state == "cancelled" and (await draft(store)).content is None
    result = await process(service, store, press(first, update_id=3))
    assert "cancelled" in result["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    listing = await process(service, store, message(4, "/drafts"))
    assert "No open drafts" in listing["payload"]["text"]


async def test_duplicate_update_and_duplicate_callback_cannot_duplicate_meals(
    service, store, catalog
):
    update = message(1, "about 150g rice")
    first = await process(service, store, update)
    await service.accept([update, update])
    assert not await service.process_one()
    consent = press(first, callback_id="stable-consent", update_id=2)
    await process(service, store, consent)
    repeated = consent.model_dump(mode="json", exclude_none=True)
    repeated["update_id"] = 3
    await service.accept([Update.model_validate(repeated)])
    assert await service.process_one()
    assert await ledger_counts(store) == (1, 1)
    again = await process(service, store, press(first, update_id=4))
    assert "already saved" in again["payload"]["text"].lower()
    assert await ledger_counts(store) == (1, 1)


@pytest.mark.parametrize(
    "forgery",
    ["wrong_message", "wrong_token", "unsent", "wrong_owner", "meal_namespace", "status_namespace"],
)
async def test_callback_requires_bound_sent_receipt_and_namespace(service, store, catalog, forgery):
    first = await process(service, store, message(1, "about 150g rice"), sent=forgery != "unsent")
    request = press(first).model_dump(mode="json", exclude_none=True)
    if forgery == "wrong_message":
        request["callback_query"]["message"]["message_id"] += 1
    elif forgery == "wrong_token":
        request["callback_query"]["data"] = "draft:approve:unrecognized"
    elif forgery == "wrong_owner":
        async with store.write() as connection:
            await connection.execute(
                sa.update(outbox).where(outbox.c.id == first["id"]).values(owner_user_id=202)
            )
    elif forgery == "meal_namespace":
        request["callback_query"]["data"] = f"meal:delete:{first['button_token']}"
    elif forgery == "status_namespace":
        request["callback_query"]["data"] = f"status:{first['button_token']}"
    result = await process(service, store, Update.model_validate(request))
    assert result is None
    assert await ledger_counts(store) == (0, 0)
    assert (await draft(store)).state == "open"


@pytest.mark.parametrize("change", ["owner", "chat", "group"])
async def test_unauthorized_draft_callback_is_not_accepted(service, store, catalog, change):
    first = await process(service, store, message(1, "about 150g rice"))
    request = press(first).model_dump(mode="json", exclude_none=True)
    if change == "owner":
        request["callback_query"]["from_user"]["id"] = 202
    elif change == "chat":
        request["callback_query"]["message"]["chat"]["id"] = 202
    else:
        request["callback_query"]["message"]["chat"]["type"] = "group"
    await service.accept([Update.model_validate(request)])
    assert not await service.process_one()
    assert await ledger_counts(store) == (0, 0)
    assert (await draft(store)).state == "open"


async def test_expiry_is_checked_before_callback_even_without_cleanup(service, store, catalog):
    first = await process(service, store, message(1, "about 150g rice"))
    await age_draft(store)
    response = await process(service, store, press(first))
    assert "expired" in response["payload"]["text"].lower()
    assert await ledger_counts(store) == (0, 0)
    expired = await draft(store)
    assert expired.state == "expired" and expired.content is None
    async with store.engine.connect() as connection:
        original = await connection.scalar(sa.select(inbox.c.payload).where(inbox.c.update_id == 1))
        preview = await connection.scalar(
            sa.select(outbox.c.payload).where(outbox.c.id == first["id"])
        )
    assert original is None
    assert "rice" not in preview["text"] and "150" not in preview["text"]


async def test_opening_expired_draft_cannot_revive_it(service, store, catalog):
    await process(service, store, message(1, "about 150g rice"))
    await age_draft(store)
    result = await process(service, store, message(2, "/draft D1"))
    assert "expired" in result["payload"]["text"].lower()
    assert (await draft(store)).state == "expired"
    assert await ledger_counts(store) == (0, 0)


async def test_rough_correction_leaves_original_until_explicit_approval(service, store, catalog):
    first = await process(service, store, message(1, "150g rice; 200g chicken"))
    correction = await process(service, store, reply(2, "item 1: about 120g", first))
    assert correction["payload"]["draft_id"] == 1
    assert "saved meal is unchanged" in correction["payload"]["text"]
    assert (await current(store)).items[0].edible_milligrams == 150000
    assert await ledger_counts(store) == (1, 1)
    await process(service, store, press(correction, update_id=3))
    changed = await current(store)
    assert changed.items[0].edible_milligrams == 120000
    assert changed.items[0].quantity_method == "approved_estimate"
    assert changed.items[1].quantity_method == "measured"
    assert await ledger_counts(store) == (1, 2)


async def test_correction_draft_cannot_overwrite_a_newer_meal_revision(service, store, catalog):
    first = await process(service, store, message(1, "150g rice"))
    correction = await process(service, store, reply(2, "item 1: about 120g", first))
    await process(service, store, message(3, "/edit M1r1 item 1: 170g"))
    before = await current(store)
    response = await process(service, store, press(correction, update_id=4))
    assert "changed" in response["payload"]["text"].lower()
    assert await current(store) == before
    assert await ledger_counts(store) == (1, 2)
    assert (await draft(store)).state == "open"


async def test_measured_correction_preserves_other_estimate_and_undo(service, store, catalog):
    proposal = await process(service, store, message(1, "about 150g rice; 200g chicken"))
    first = await process(service, store, press(proposal, update_id=2))
    original = await current(store)
    changed = await process(service, store, reply(3, "item 2: 180g", first))
    assert (await current(store)).items[0] == original.items[0]
    assert (await current(store)).items[1].edible_milligrams == 180000
    await process(service, store, reply(4, "undo", changed))
    assert (await current(store)).items == original.items


async def test_food_refresh_does_not_change_displayed_draft_selection(service, store, catalog):
    first = await process(service, store, message(1, "about 150g rice"))
    async with store.write() as connection:
        changed = await publish_reviewed_food(
            connection, food_record("rice", energy="999"), food_id=catalog["rice"].food_id
        )
        before = await connection.scalar(sa.select(sa.func.count()).select_from(food_versions))
    await process(service, store, press(first))
    saved = await current(store)
    assert saved.items[0].food_version_id == catalog["rice"].version_id != changed.version_id
    assert (
        next(n for n in saved.items[0].nutrients if n.code == "energy").amount_scaled == 150000000
    )
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(food_versions)) == before
        )


async def test_editing_original_telegram_text_does_not_change_or_approve_draft(
    service, store, catalog
):
    first = await process(service, store, message(1, "about 150g rice"))
    edit = message(2, "120g rice", edited=True).model_dump(mode="json", exclude_none=True)
    edit["edited_message"]["message_id"] = 1
    result = await process(service, store, Update.model_validate(edit))
    assert result["payload"]["draft_id"] == first["payload"]["draft_id"]
    assert "draft unchanged" in result["payload"]["text"]
    assert (await draft(store)).revision == 1
    assert (await draft(store)).content.items[0].edible_milligrams == 150000
    assert await ledger_counts(store) == (0, 0)


async def test_expired_reply_keeps_identity_for_expiry_explanation(service, store, catalog):
    first = await process(service, store, message(1, "about 150g rice"))
    await age_draft(store)
    response = await process(service, store, reply(2, "item 1: 120g", first))
    assert "expired" in response["payload"]["text"].lower()
    assert (await draft(store)).state == "expired"
    assert await ledger_counts(store) == (0, 0)


async def test_history_amount_is_a_proposal_and_never_automatic_consent(service, store, catalog):
    for update_id, grams in enumerate((100, 200, 150), 1):
        await process(service, store, message(update_id, f"{grams}g rice"))
    result = await process(service, store, message(4, "rice"))
    assert "History estimate" in result["payload"]["text"]
    assert "150 g" in result["payload"]["text"]
    assert await ledger_counts(store) == (3, 3)
    assert (await draft(store)).content.items[0].estimate_basis
    await process(service, store, press(result, update_id=5))
    saved = await current(store, 4)
    assert saved.items[0].edible_milligrams == 150000
    assert saved.items[0].quantity_method == "approved_estimate"


async def test_unique_catalog_portion_still_requires_button_approval(service, store):
    record = food_record("synthetic pasta").model_copy(
        update={
            "portions": (
                PortionInput(
                    label="Synthetic usual portion",
                    grams="80",
                    original_measure="Synthetic serving",
                    source="Offline test fixture only",
                    is_estimate=False,
                ),
            )
        }
    )
    async with store.write() as connection:
        await publish_reviewed_food(connection, record)
    result = await process(service, store, message(1, "synthetic pasta"))
    assert "Catalog estimate" in result["payload"]["text"]
    assert "Offline test fixture only" in result["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    await process(service, store, press(result))
    assert (await current(store)).items[0].quantity_method == "approved_estimate"


@pytest.mark.parametrize(
    "text", ["about 150g rice; unknown food", "about 150g rice with unrecorded oil"]
)
async def test_unresolved_food_identity_never_partially_creates_a_draft(
    service, store, catalog, text
):
    await process(service, store, message(1, text))
    assert await ledger_counts(store) == (0, 0)
    assert await draft(store) is None


async def test_explicit_draft_edit_requires_revision(service, store, catalog):
    await process(service, store, message(1, "about 150g rice"))
    result = await process(service, store, message(2, "/draft D1 item 1: 120g"))
    assert "revision" in result["payload"]["text"]
    assert (await draft(store)).revision == 1
    assert await ledger_counts(store) == (0, 0)


async def test_same_original_message_cannot_create_another_draft(service, store, catalog):
    first = message(1, "about 150g rice")
    await process(service, store, first)
    repeated = first.model_dump(mode="json", exclude_none=True)
    repeated["update_id"] = 2
    result = await process(service, store, Update.model_validate(repeated))
    assert "already has a draft" in result["payload"]["text"]
    assert await draft(store, 2) is None
    assert await ledger_counts(store) == (0, 0)
