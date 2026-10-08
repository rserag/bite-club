"""Synthetic compact/detail meal interactions preserve their ledger contracts."""

import pytest

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.application.food_display import food_display_name
from nutrition_bot.application.meal_conversation import receipt, receipt_details
from nutrition_bot.domain.food_source import SourceProvenance
from tests.helpers import message
from tests.test_telegram_drafts import press as approve_draft
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import current, food_record, ledger_counts, press, process, reply


@pytest.mark.parametrize(
    ("name", "preparation", "expected"),
    [
        ("Rice, white, long-grain, cooked", "cooked", "Rice white long-grain cooked"),
        (
            "Chicken, breast, skinless, boneless, raw",
            "raw",
            "Chicken breast skinless boneless raw",
        ),
        ("Milk, 2% fat, pasteurized", "as_sold", "Milk 2% fat pasteurized (as sold)"),
        ("Potato, roasted, roasted", "cooked", "Potato roasted (cooked)"),
        ("  rice,  white, WHITE, cooked  ", "cooked", "rice white cooked"),
    ],
)
def test_food_labels_preserve_source_distinctions_without_invented_aliases(
    name, preparation, expected
):
    assert food_display_name(name, preparation) == expected


def test_long_food_label_keeps_qualifiers_at_end():
    name = "Synthetic " + "variety " * 50 + ", breast, 2% fat, raw, skin-on"
    label = food_display_name(name, "raw")
    assert "breast 2% fat raw skin-on" in label
    assert "…" not in label


async def test_saved_estimates_group_basis_but_details_keep_exact_item_provenance(
    service, store, catalog
):
    draft = await process(
        service, store, message(1, "about 150g rice; about 50g beans; 200g chicken")
    )
    saved_reply = await process(service, store, approve_draft(draft, update_id=2))
    saved = await current(store)
    text = saved_reply["payload"]["text"]
    assert text.index("Energy:") < text.index("1. 150 g")
    assert "approximate portions" in text.splitlines()[1]
    assert "1. 150 g rice (cooked) · estimated" in text
    assert "2. 50 g beans (cooked) · estimated" in text
    assert "3. 200 g chicken (cooked) · measured" in text
    assert text.count("Estimate basis") == 1
    assert "items 1, 2" in text
    assert "#" not in text
    assert "reply" not in text.lower()
    details = receipt_details(saved).text
    for item in saved.items[:2]:
        assert f"Food version: #{item.food_version_id}" in details
        assert f"Estimate basis: {item.quantity_basis}" in details
        assert "Approved draft: D1r1" in details
        assert item.approval_action_key in details
        assert item.food_content_sha256 in details
    assert "Original quantity: 200 g" in details
    assert "Quantity: 200 g · measured" in details
    assert "Source: manual_reviewed · Synthetic offline Telegram fixture" in details
    assert "License: Synthetic test data" in details
    assert "Fiber:" in details and "partial" in details
    assert "C: unknown" in text and "F: 0.0 g" in text
    assert await ledger_counts(store) == (1, 1)


async def test_only_identical_normalized_estimate_bases_collapse(service, store, catalog):
    draft = await process(service, store, message(1, "about 150g rice; about 50g beans"))
    await process(service, store, approve_draft(draft, update_id=2))
    saved = await current(store)
    first, second = saved.items
    same = saved.model_copy(
        update={
            "items": (
                first.model_copy(update={"quantity_basis": "Synthetic estimate basis"}),
                second.model_copy(update={"quantity_basis": "Synthetic  estimate\n basis"}),
            )
        }
    )
    assert receipt(same).text.count("Estimate basis") == 1
    different = same.model_copy(
        update={
            "items": (
                same.items[0],
                same.items[1].model_copy(update={"quantity_basis": "Different synthetic basis"}),
            )
        }
    )
    assert receipt(different).text.count("Estimate basis") == 2


async def test_details_use_snapshot_names_source_ids_and_historical_date(service, store):
    source_name = "Chicken, breast, skinless, boneless, 2% fat, cooked, grilled"
    async with store.write() as connection:
        food = await publish_reviewed_food(connection, food_record(source_name))
    saved_reply = await process(
        service, store, message(1, f"/meal 2023-10-15 Lunch: 0.1kg #{food.version_id}")
    )
    assert (
        "Chicken breast skinless boneless 2% fat cooked grilled" in saved_reply["payload"]["text"]
    )
    assert source_name not in saved_reply["payload"]["text"]
    async with store.write() as connection:
        await publish_reviewed_food(
            connection,
            food_record("New synthetic catalog revision", energy="999"),
            food_id=food.food_id,
        )
    details = await process(service, store, press(saved_reply, "details", update_id=2))
    text = details["payload"]["text"]
    assert source_name in text and "New synthetic catalog revision" not in text
    assert "2023-10-15" in text and "Original quantity: 0.1 kg" in text
    assert "Energy: 100 kcal" in text and "Energy: 999" not in text
    assert "Recorded date timezone: UTC" in text
    saved = await current(store)
    provenance = SourceProvenance(
        external_id="777000",
        fetched_at=1700000000,
        published_date="2023-01-01",
        adapter_version="synthetic-v1",
        data_type="synthetic",
        warnings=("Synthetic caveat",),
    )
    sourced = saved.model_copy(
        update={"items": (saved.items[0].model_copy(update={"provenance": provenance}),)}
    )
    text = receipt_details(sourced).text
    assert "External source ID: 777000 · type: synthetic" in text
    assert "Source published: 2023-01-01" in text
    assert "Source adapter: synthetic-v1" in text and "Source caveat: Synthetic caveat" in text
    assert await ledger_counts(store) == (1, 1)


async def test_details_and_more_are_read_only_and_stale_views_open_current_receipt(
    service, store, catalog
):
    first = await process(service, store, message(1, "100g rice"))
    details = await process(service, store, press(first, "details", update_id=2))
    more = await process(service, store, press(details, "more", update_id=3))
    labels = {button["text"] for button in more["payload"]["buttons"]}
    assert {"Save favorite", "Delete", "Back to meal", "Details"} <= labels
    assert "Delete" not in {button["text"] for button in first["payload"]["buttons"]}
    compact = await process(service, store, press(more, "compact", update_id=4))
    assert "1. 100 g rice" in compact["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)
    await process(service, store, reply(5, "120g", compact))
    stale = await process(service, store, press(first, "details", update_id=6))
    assert "older receipt" in stale["payload"]["text"]
    assert "M1r2" in stale["payload"]["text"] and "120 g rice" in stale["payload"]["text"]
    assert await ledger_counts(store) == (1, 2)


async def test_undo_save_keeps_edit_available_to_restore_with_a_correction(service, store, catalog):
    saved = await process(service, store, message(1, "100g rice"))
    undone = await process(service, store, press(saved, "undo_save", update_id=2))
    assert (await current(store)).deleted
    assert "Edit" in {button["text"] for button in undone["payload"]["buttons"]}
    assert not any("Undo" in button["text"] for button in undone["payload"]["buttons"])
    guidance = await process(service, store, press(undone, "edit", update_id=3))
    assert "What would you like to change?" in guidance["payload"]["text"]
    restored = await process(service, store, message(4, "item 1: 120g"))
    assert "Updated" in restored["payload"]["text"]
    assert not (await current(store)).deleted
    assert (await current(store)).items[0].edible_milligrams == 120_000
    assert "Undo edit" in {button["text"] for button in restored["payload"]["buttons"]}
    assert await ledger_counts(store) == (1, 3)


@pytest.mark.parametrize(
    ("operation", "token", "label", "lead", "deleted", "grams"),
    [
        ("save", "undo_save", "Undo save", "Save undone", True, 100_000),
        ("edit", "undo_edit", "Undo edit", "Edit undone", False, 100_000),
        ("delete", "restore", "Restore meal", "Restored", False, 100_000),
    ],
)
async def test_contextual_undo_applies_once_to_actual_operation(
    service, store, catalog, operation, token, label, lead, deleted, grams
):
    selected = await process(service, store, message(1, "100g rice"))
    if operation == "edit":
        selected = await process(service, store, reply(2, "120g", selected))
    elif operation == "delete":
        more = await process(service, store, press(selected, "more", update_id=2))
        selected = await process(service, store, press(more, "delete", update_id=3))
        assert "Removed from your recorded totals" in selected["payload"]["text"]
        assert "Energy:" not in selected["payload"]["text"]
    assert label in {button["text"] for button in selected["payload"]["buttons"]}
    undone = await process(service, store, press(selected, token, update_id=4))
    assert undone["payload"]["text"].startswith(lead)
    meal = await current(store)
    assert meal.deleted == deleted and meal.items[0].edible_milligrams == grams
    count = await ledger_counts(store)
    assert await process(service, store, press(undone, token, update_id=5)) is None
    assert await ledger_counts(store) == count


async def test_editing_deleted_meal_uses_undo_edit_and_retains_deleted_status(
    service, store, catalog
):
    await process(service, store, message(1, "100g rice"))
    await process(service, store, message(2, "/delete M1r1"))
    edited = await process(service, store, message(3, "/edit M1r2 date yesterday"))
    labels = {button["text"] for button in edited["payload"]["buttons"]}
    assert "Undo edit" in labels and "Restore meal" not in labels
    undone = await process(service, store, press(edited, "undo_edit", update_id=4))
    assert undone["payload"]["text"].startswith("Edit undone")
    assert (await current(store)).deleted
    assert "2023-11-14" in undone["payload"]["text"]
