"""Offline recipe acceptance: batch definitions, exact shares, and per-use consent."""

from datetime import date
from fractions import Fraction

import pytest
import sqlalchemy as sa
from aiogram.types import Update

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.schema import inbox, outbox
from tests.helpers import callback, message
from tests.test_telegram_drafts import age_draft, draft
from tests.test_telegram_drafts import press as approve_draft
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import current, food_record, ledger_counts, process, reply


def press(receipt, operation="portion", *, update_id=2, callback_id=None):
    update = callback(
        receipt["button_token"],
        update_id=update_id,
        callback_id=callback_id or f"synthetic-recipe-{update_id}",
        message_id=receipt["telegram_message_id"] or 77,
    ).model_dump(mode="json", exclude_none=True)
    update["callback_query"]["data"] = f"recipe:{operation}:{receipt['button_token']}"
    return Update.model_validate(update)


async def definition(service, store, catalog, *, basis="yield 1200g", estimated=False):
    rice = catalog["rice"].version_id
    chicken = catalog["chicken"].version_id
    second = "about 20g" if estimated else "20g"
    return await process(
        service,
        store,
        message(1, f"/recipe create chili = 500g #{rice}; {second} #{chicken} | {basis}"),
    )


def nutrient(item, code):
    return next(value.amount_scaled for value in item.nutrients if value.code == code)


def share(item):
    return Fraction(item.recipe_share.portion_units, item.recipe_share.total_units)


async def test_definition_and_preview_do_not_log_consumption(service, store, catalog):
    created = await definition(service, store, catalog)
    assert "R1v1" in created["payload"]["text"] and "chili" in created["payload"]["text"]
    assert created["payload"]["recipe_id"] == 1
    assert type(created["payload"]["recipe_version_id"]) is int
    assert {button["text"] for button in created["payload"]["buttons"]} == {
        "Enter portion",
        "Archive",
    }
    listing = await process(service, store, message(2, "/recipes"))
    assert "chili" in {button["text"] for button in listing["payload"]["buttons"]}
    opened = await process(service, store, message(3, "/recipe R1"))
    assert opened["payload"]["recipe_version_id"] == created["payload"]["recipe_version_id"]
    requested = await process(service, store, press(opened, update_id=4))
    assert "How much chili did you eat?" in requested["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)


async def test_cooked_portion_keeps_batch_masses_and_scales_nutrients_only(service, store, catalog):
    await definition(service, store, catalog)
    result = await process(service, store, message(2, "/recipe log R1v1 300g"))
    saved = await current(store)
    assert [item.edible_milligrams for item in saved.items] == [500000, 20000]
    assert [share(item) for item in saved.items] == [Fraction(1, 4), Fraction(1, 4)]
    assert sum(nutrient(item, "energy") for item in saved.items) == 135000000
    assert sum(nutrient(item, "protein") for item in saved.items) == 13500000
    assert nutrient(saved.items[0], "fiber") == 1250000
    assert nutrient(saved.items[1], "fiber") is None
    assert all(nutrient(item, "fat") == 0 for item in saved.items)
    assert all(item.quantity_method == "measured" for item in saved.items)
    assert all(item.recipe_share.recipe_id == 1 for item in saved.items)
    assert [item.recipe_share.ingredient_index for item in saved.items] == [0, 1]
    text = result["payload"]["text"].lower()
    assert "recipe r1v1: 300 g cooked · 1/4 of batch" in text
    assert "ingredient equivalents" in text
    assert "ingredient" in text and "equivalent" in text
    report = await process(service, store, message(3, "/today full"))
    assert "135 kcal" in report["payload"]["text"]
    assert "Calcium: unknown" in report["payload"]["text"]
    assert "calculated recipe" in report["payload"]["text"].lower()
    assert "measured 2" not in report["payload"]["text"].lower()
    assert await ledger_counts(store) == (1, 1)


@pytest.mark.parametrize(
    ("amount", "basis", "portion", "batch_mass", "expected_energy"),
    [
        ("1g", "servings 3", "1 serving", 1000, 333333),
        ("0.001g", "yield 0.003g", "0.001g", 1, 333),
    ],
)
async def test_thirds_and_submilligram_equivalents_are_not_rounded_to_fake_mass(
    service, store, catalog, amount, basis, portion, batch_mass, expected_energy
):
    await process(
        service,
        store,
        message(
            1, f"/recipe create small batch = {amount} #{catalog['rice'].version_id} | {basis}"
        ),
    )
    await process(service, store, message(2, f"/recipe log R1v1 {portion}"))
    item = (await current(store)).items[0]
    assert item.edible_milligrams == batch_mass
    assert share(item) == Fraction(1, 3)
    assert nutrient(item, "energy") == expected_energy
    assert item.quantity_method == "measured"


async def test_fractional_servings_use_exact_fraction(service, store, catalog):
    await definition(service, store, catalog, basis="servings 4")
    await process(service, store, message(2, "/recipe log R1v1 1.5 servings"))
    saved = await current(store)
    assert all(share(item) == Fraction(3, 8) for item in saved.items)
    assert all(item.recipe_share.unit == "serving" for item in saved.items)
    assert sum(nutrient(item, "energy") for item in saved.items) == 202500000


async def test_recipe_versions_and_food_versions_stay_pinned(service, store, catalog):
    created = await definition(service, store, catalog)
    old_version = created["payload"]["recipe_version_id"]
    await process(service, store, message(2, "/recipe log R1v1 300g"))
    source = await current(store)
    async with store.write() as connection:
        refreshed = await publish_reviewed_food(
            connection, food_record("rice", energy="999"), food_id=catalog["rice"].food_id
        )
    update = await process(
        service,
        store,
        message(3, f"/recipe update R1v1 = 600g #{refreshed.version_id} | yield 900g"),
    )
    assert "R1v2" in update["payload"]["text"]
    historical = await process(service, store, message(4, "/recipe R1v1"))
    assert historical["payload"]["recipe_version_id"] == old_version
    await process(service, store, message(5, "/recipe log R1v1 300g"))
    copied = await current(store, 2)
    assert copied.items == source.items
    assert all(item.recipe_share.version_id == old_version for item in copied.items)
    await process(service, store, message(6, "/recipe log R1v2 300g"))
    latest = await current(store, 3)
    assert latest.items[0].food_version_id == refreshed.version_id
    assert share(latest.items[0]) == Fraction(1, 3)
    assert await current(store) == source


@pytest.mark.parametrize("uncertainty", ["ingredient", "yield", "portion"])
async def test_uncertainty_stays_outside_totals_until_explicit_per_use_consent(
    service, store, catalog, uncertainty
):
    definition_response = await definition(
        service,
        store,
        catalog,
        basis="yield about 1200g" if uncertainty == "yield" else "yield 1200g",
        estimated=uncertainty == "ingredient",
    )
    assert "Approve estimate" not in {
        button["text"] for button in definition_response["payload"]["buttons"]
    }
    portion = "about 300g" if uncertainty == "portion" else "300g"
    first = await process(service, store, message(2, f"/recipe log R1v1 {portion}"))
    assert "draft_id" in first["payload"]
    assert await ledger_counts(store) == (0, 0)
    await process(
        service, store, approve_draft(first, update_id=3, callback_id="recipe-first-consent")
    )
    saved = await current(store)
    methods = [item.quantity_method for item in saved.items]
    expected = (
        ["measured", "approved_estimate"]
        if uncertainty == "ingredient"
        else ["approved_estimate"] * 2
    )
    assert methods == expected
    for item in saved.items:
        if item.quantity_method == "approved_estimate":
            assert item.approval_action_key == "callback:recipe-first-consent"
    again = await process(service, store, message(4, f"/recipe log R1v1 {portion}"))
    assert "draft_id" in again["payload"]
    assert again["payload"]["draft_id"] != first["payload"]["draft_id"]
    assert await ledger_counts(store) == (1, 1)


async def test_measured_portion_cannot_erase_uncertain_yield(service, store, catalog):
    await definition(service, store, catalog, basis="yield about 1200g")
    first = await process(service, store, message(2, "/recipe log R1v1 about 300g"))
    updated = await process(service, store, reply(3, "portion 250g", first))
    assert updated["payload"]["draft_revision"] == 2
    assert await ledger_counts(store) == (0, 0)
    await process(service, store, approve_draft(updated, update_id=4))
    saved = await current(store)
    assert all(item.quantity_method == "approved_estimate" for item in saved.items)
    assert all(item.recipe_share.yield_estimate_basis for item in saved.items)
    assert all(item.recipe_share.portion_estimate_basis is None for item in saved.items)
    assert all(share(item) == Fraction(5, 24) for item in saved.items)


async def test_measured_portion_can_resolve_only_portion_uncertainty(service, store, catalog):
    await definition(service, store, catalog)
    first = await process(service, store, message(2, "/recipe log R1v1 about 300g"))
    await process(service, store, reply(3, "portion 250g", first))
    saved = await current(store)
    assert all(item.quantity_method == "measured" for item in saved.items)
    assert all(item.approval_action_key is None for item in saved.items)
    assert all(share(item) == Fraction(5, 24) for item in saved.items)
    assert (await draft(store)).state == "saved"


async def test_changed_rough_recipe_draft_invalidates_original_approval(service, store, catalog):
    await definition(service, store, catalog)
    first = await process(service, store, message(2, "/recipe log R1v1 about 300g"))
    updated = await process(service, store, reply(3, "portion about 250g", first))
    assert updated["payload"]["draft_revision"] == 2
    await process(service, store, approve_draft(first, update_id=4))
    assert await ledger_counts(store) == (0, 0)
    await process(service, store, approve_draft(updated, update_id=5))
    saved = await current(store)
    assert all(share(item) == Fraction(5, 24) for item in saved.items)
    assert all(item.approval_draft_revision == 2 for item in saved.items)


async def test_measured_favorite_and_repeat_preserve_recipe_fraction(service, store, catalog):
    await definition(service, store, catalog)
    await process(service, store, message(2, "/recipe log R1v1 300g"))
    await process(service, store, message(3, "/favorite save chili lunch = M1r1"))
    await process(service, store, message(4, "/eat F1v1 x0.5"))
    reused = await current(store, 2)
    assert all(share(item) == Fraction(1, 8) for item in reused.items)
    assert [item.edible_milligrams for item in reused.items] == [500000, 20000]
    assert sum(nutrient(item, "energy") for item in reused.items) == 67500000
    await process(service, store, message(5, "/repeat M1r1 x2"))
    repeated = await current(store, 3)
    assert all(share(item) == Fraction(1, 2) for item in repeated.items)
    assert [item.edible_milligrams for item in repeated.items] == [500000, 20000]
    await process(service, store, message(6, "/repeat M1r1 x5"))
    assert await ledger_counts(store) == (3, 3)


async def test_estimated_recipe_favorite_requires_fresh_approval(service, store, catalog):
    await definition(service, store, catalog, basis="yield about 1200g")
    first = await process(service, store, message(2, "/recipe log R1v1 300g"))
    await process(service, store, approve_draft(first, update_id=3, callback_id="recipe-original"))
    await process(service, store, message(4, "/favorite save chili lunch = M1r1"))
    reused = await process(service, store, message(5, "/eat F1v1 x0.5"))
    assert "draft_id" in reused["payload"]
    assert await ledger_counts(store) == (1, 1)
    await process(service, store, approve_draft(reused, update_id=6, callback_id="recipe-reuse"))
    saved = await current(store, 2)
    assert all(share(item) == Fraction(1, 8) for item in saved.items)
    assert all(item.approval_action_key == "callback:recipe-reuse" for item in saved.items)
    assert all(item.recipe_share.yield_estimate_basis for item in saved.items)


async def test_portion_correction_pins_original_recipe_and_undo_preserves_history(
    service, store, catalog
):
    await definition(service, store, catalog)
    first = await process(service, store, message(2, "/recipe log R1v1 300g"))
    original = await current(store)
    await process(
        service,
        store,
        message(3, f"/recipe update R1v1 = 600g #{catalog['beans'].version_id} | yield 900g"),
    )
    corrected = await process(service, store, reply(4, "portion 250g", first))
    saved = await current(store)
    assert saved.revision_number == 2
    assert all(share(item) == Fraction(5, 24) for item in saved.items)
    assert all(
        item.recipe_share.version_id == original.items[0].recipe_share.version_id
        for item in saved.items
    )
    assert sum(nutrient(item, "energy") for item in saved.items) == 112500000
    await process(service, store, reply(5, "undo", corrected))
    assert (await current(store)).items == original.items


async def test_rough_portion_correction_waits_and_rejects_stale_target(service, store, catalog):
    await definition(service, store, catalog)
    first = await process(service, store, message(2, "/recipe log R1v1 300g"))
    original = await current(store)
    correction = await process(service, store, reply(3, "portion about 250g", first))
    assert "draft_id" in correction["payload"]
    assert await current(store) == original
    await process(service, store, message(4, "/edit M1r1 portion 280g"))
    measured = await current(store)
    await process(service, store, approve_draft(correction, update_id=5))
    assert await current(store) == measured
    assert all(share(item) == Fraction(7, 30) for item in measured.items)


async def test_recipe_item_edits_reject_but_explicit_whole_replacement_works(
    service, store, catalog
):
    await definition(service, store, catalog)
    first = await process(service, store, message(2, "/recipe log R1v1 300g"))
    original = await current(store)
    rejected = await process(service, store, reply(3, "item 1: 200g", first))
    assert "portion" in rejected["payload"]["text"].lower()
    assert await current(store) == original
    await process(service, store, reply(4, "replace: 100g beans", first))
    saved = await current(store)
    assert len(saved.items) == 1 and saved.items[0].recipe_share is None
    assert saved.items[0].food_name == "beans" and saved.items[0].edible_milligrams == 100000


async def test_date_delete_and_undo_preserve_recipe_share(service, store, catalog):
    await definition(service, store, catalog)
    first = await process(service, store, message(2, "/recipe log R1v1 300g"))
    original = await current(store)
    dated = await process(service, store, reply(3, "date yesterday", first))
    assert (await current(store)).local_date == date(2023, 11, 13)
    assert (await current(store)).items == original.items
    deleted = await process(service, store, reply(4, "delete", dated))
    assert (await current(store)).deleted
    await process(service, store, reply(5, "undo", deleted))
    restored = await current(store)
    assert not restored.deleted and restored.local_date == date(2023, 11, 13)
    assert restored.items == original.items


@pytest.mark.parametrize(
    "portion",
    ["", "some", "1 serving", "0g", "-1g", "1200.001g", "0.0001g", "300g plus oil", "about 1300g"],
)
async def test_invalid_or_unknown_mass_portion_never_logs(service, store, catalog, portion):
    await definition(service, store, catalog)
    await process(service, store, message(2, f"/recipe log R1v1 {portion}"))
    assert await ledger_counts(store) == (0, 0)


async def test_serving_recipe_does_not_guess_grams_or_excess_servings(service, store, catalog):
    await definition(service, store, catalog, basis="servings 4")
    await process(service, store, message(2, "/recipe log R1v1 300g"))
    await process(service, store, message(3, "/recipe log R1v1 4.001 servings"))
    assert await ledger_counts(store) == (0, 0)


async def test_explicit_backdate_allowed_but_future_date_rejected(service, store, catalog):
    await definition(service, store, catalog)
    await process(service, store, message(2, "/recipe log yesterday R1v1 300g"))
    assert (await current(store)).local_date == date(2023, 11, 13)
    await process(service, store, message(3, "/recipe log 2023-11-15 R1v1 300g"))
    assert await ledger_counts(store) == (1, 1)


async def test_archive_blocks_historical_use_and_stale_mutation(service, store, catalog):
    original = await definition(service, store, catalog)
    updated = await process(
        service,
        store,
        message(2, f"/recipe update R1v1 = 600g #{catalog['rice'].version_id} | yield 900g"),
    )
    await process(service, store, press(original, "archive", update_id=3))
    await process(service, store, message(4, "/recipe log R1v1 300g"))
    assert await ledger_counts(store) == (1, 1)
    archived = await process(service, store, press(updated, "archive", update_id=5))
    assert "R1v3" in archived["payload"]["text"]
    assert {b["text"] for b in archived["payload"]["buttons"]} == {"Restore"}
    await process(service, store, message(6, "/recipe log R1v1 300g"))
    assert await ledger_counts(store) == (1, 1)
    active = await process(service, store, message(7, "/recipes"))
    assert "R1v3" not in active["payload"]["text"]
    listing = await process(service, store, message(8, "/recipes all"))
    assert "R1v3" in listing["payload"]["text"]
    await process(service, store, press(archived, "restore", update_id=9))
    await process(service, store, message(10, "/recipe log R1v1 300g"))
    assert await ledger_counts(store) == (2, 2)


async def test_explicit_archive_restore_and_stale_update(service, store, catalog):
    await definition(service, store, catalog)
    await process(service, store, message(2, "/recipe archive R1v1"))
    await process(service, store, message(3, "/recipe restore R1v1"))
    await process(service, store, message(4, "/recipe log R1v1 300g"))
    assert await ledger_counts(store) == (0, 0)
    await process(service, store, message(5, "/recipe restore R1v2"))
    await process(
        service,
        store,
        message(6, f"/recipe update R1v1 = 100g #{catalog['beans'].version_id} | servings 2"),
    )
    await process(service, store, message(7, "/recipe log R1v3 300g"))
    assert (await current(store)).items[0].food_version_id == catalog["rice"].version_id


@pytest.mark.parametrize(
    "forgery",
    [
        "wrong_message",
        "wrong_token",
        "unsent",
        "wrong_owner",
        "favorite_namespace",
        "draft_namespace",
        "meal_namespace",
        "status_namespace",
    ],
)
async def test_recipe_callback_requires_bound_sent_preview(service, store, catalog, forgery):
    preview = await definition(service, store, catalog)
    if forgery == "unsent":
        async with store.write() as connection:
            await connection.execute(
                sa.update(outbox).where(outbox.c.id == preview["id"]).values(status="queued")
            )
    request = press(preview, "archive").model_dump(mode="json", exclude_none=True)
    if forgery == "wrong_message":
        request["callback_query"]["message"]["message_id"] += 1
    elif forgery == "wrong_token":
        request["callback_query"]["data"] = "recipe:archive:unrecognized"
    elif forgery == "wrong_owner":
        async with store.write() as connection:
            await connection.execute(
                sa.update(outbox).where(outbox.c.id == preview["id"]).values(owner_user_id=202)
            )
    elif forgery.endswith("_namespace"):
        namespace = forgery.split("_")[0]
        action = {"favorite": "archive", "draft": "approve", "meal": "delete"}.get(namespace)
        request["callback_query"]["data"] = (
            f"{namespace}:{action}:{preview['button_token']}"
            if action
            else f"status:{preview['button_token']}"
        )
    assert await process(service, store, Update.model_validate(request)) is None
    await process(service, store, message(3, "/recipe log R1v1 300g"))
    assert await ledger_counts(store) == (1, 1)


@pytest.mark.parametrize("forgery", ["owner", "chat", "group"])
async def test_unauthorized_recipe_callback_never_enters_inbox(service, store, catalog, forgery):
    preview = await definition(service, store, catalog)
    request = press(preview, "archive").model_dump(mode="json", exclude_none=True)
    if forgery == "owner":
        request["callback_query"]["from_user"]["id"] = 202
    elif forgery == "chat":
        request["callback_query"]["message"]["chat"]["id"] = 202
    else:
        request["callback_query"]["message"]["chat"]["type"] = "group"
    await service.accept([Update.model_validate(request)])
    assert not await service.process_one()
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(inbox)) == 1
    assert await ledger_counts(store) == (0, 0)


async def test_duplicate_archive_callback_does_not_advance_recipe_twice(service, store, catalog):
    preview = await definition(service, store, catalog)
    action = press(preview, "archive", callback_id="one-archive")
    archived = await process(service, store, action)
    assert "R1v2" in archived["payload"]["text"]
    replay = action.model_dump(mode="json", exclude_none=True)
    replay["update_id"] = 3
    await service.accept([Update.model_validate(replay)])
    assert await service.process_one()
    opened = await process(service, store, message(4, "/recipe R1"))
    assert "R1v2" in opened["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)


async def test_editing_original_recipe_message_does_not_rewrite_definition(service, store, catalog):
    original = await definition(service, store, catalog)
    update = message(
        2,
        f"/recipe create replacement = 100g #{catalog['beans'].version_id} | yield 100g",
        edited=True,
    ).model_dump(mode="json", exclude_none=True)
    update["edited_message"]["message_id"] = 1
    await process(service, store, Update.model_validate(update))
    opened = await process(service, store, message(3, "/recipe R1"))
    assert opened["payload"]["recipe_version_id"] == original["payload"]["recipe_version_id"]
    await process(service, store, message(4, "/recipe log R1v1 300g"))
    assert len((await current(store)).items) == 2
    assert sum(nutrient(item, "energy") for item in (await current(store)).items) == 135000000


async def test_expiry_removes_quoted_draft_without_purging_recipe_definition_view(
    service, store, catalog
):
    await definition(service, store, catalog)
    proposed = await process(service, store, message(2, "/recipe log R1v1 about 300g"))
    opened = await process(service, store, reply(3, "/recipe R1", proposed))
    assert opened["payload"]["recipe_id"] == 1
    await age_draft(store)
    await service.cleanup()
    async with store.engine.connect() as connection:
        raw = await connection.scalar(sa.select(inbox.c.payload).where(inbox.c.update_id == 3))
        kept = await connection.scalar(
            sa.select(outbox.c.payload).where(outbox.c.id == opened["id"])
        )
        assert (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all() == []
    assert raw is None or "text" not in raw["message"].get("reply_to_message", {})
    assert kept["recipe_id"] == 1
    await process(service, store, message(4, "/recipe log R1v1 300g"))
    assert await ledger_counts(store) == (1, 1)


async def test_recipe_batch_masses_do_not_become_single_food_portion_history(
    service, store, catalog
):
    await definition(service, store, catalog)
    for update_id in (2, 3, 4):
        await process(service, store, message(update_id, "/recipe log R1v1 300g"))
    proposed = await process(service, store, message(5, "rice"))
    assert "draft_id" in proposed["payload"]
    pending = await draft(store, proposed["payload"]["draft_id"])
    assert pending.content.items[0].edible_milligrams is None
    assert await ledger_counts(store) == (3, 3)
