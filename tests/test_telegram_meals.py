"""Offline Telegram meal acceptance tests using synthetic catalog data only."""

import time
from datetime import date

import pytest
import sqlalchemy as sa
from aiogram.types import Update

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.meals import MealError, get_meal
from nutrition_bot.adapters.database.schema import actions, inbox, meal_revisions, meals, outbox
from nutrition_bot.application import meal_conversation
from nutrition_bot.domain.food import NutrientInput, ReviewedFoodInput
from tests.helpers import callback, message


def food_record(name, *, preparation="cooked", energy="100", protein="10", fiber=None):
    # Deliberately synthetic values: these are not dietary reference data.
    return ReviewedFoodInput(
        name=name,
        preparation=preparation,
        source_reference="Synthetic offline Telegram fixture",
        source_license="Synthetic test data",
        nutrients=(
            NutrientInput(code="energy", amount=energy, unit="kcal"),
            NutrientInput(code="protein", amount=protein, unit="g"),
            NutrientInput(code="fat", amount="0", unit="g"),
            NutrientInput(code="fiber", amount=fiber, unit="g"),
        ),
    )


@pytest.fixture
async def catalog(store):
    async with store.write() as connection:
        return {
            name: await publish_reviewed_food(connection, food_record(name, **values))
            for name, values in (
                ("rice", {"energy": "100", "protein": "10", "fiber": "1"}),
                ("chicken", {"energy": "200", "protein": "20"}),
                ("beans", {"energy": "150", "protein": "15", "fiber": "2"}),
            )
        }


async def process(service, store, update, *, sent=True):
    await service.accept([update])
    assert await service.process_one()
    key = (
        f"callback:{update.callback_query.id}"
        if update.callback_query
        else f"update:{update.update_id}"
    )
    async with store.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    sa.select(outbox).where(outbox.c.action_key == key, outbox.c.kind == "message")
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    result = dict(row)
    if sent:
        result["telegram_message_id"] = 1000 + result["id"]
        await service.finish_reply(result["id"], "sent", message_id=result["telegram_message_id"])
    return result


def reply(update_id, text, receipt, *, bot_id=123456, is_bot=True, message_id=None):
    value = message(update_id, text).model_dump(mode="json", exclude_none=True)
    value["message"]["reply_to_message"] = {
        "message_id": message_id or receipt["telegram_message_id"],
        "date": 1700000000,
        "from": {"id": bot_id, "is_bot": is_bot, "first_name": "SyntheticBot"},
        "chat": {"id": 101, "type": "private"},
        "text": receipt["payload"]["text"],
    }
    return Update.model_validate(value)


def press(receipt, operation, *, update_id=2, callback_id=None, message_id=None, user=101):
    value = callback(
        receipt["button_token"],
        update_id=update_id,
        callback_id=callback_id or f"synthetic-meal-{update_id}",
        message_id=message_id or receipt["telegram_message_id"],
        user=user,
    ).model_dump(mode="json", exclude_none=True)
    value["callback_query"]["data"] = f"meal:{operation}:{receipt['button_token']}"
    return Update.model_validate(value)


async def current(store, meal_id=1):
    async with store.engine.connect() as connection:
        return await get_meal(connection, meal_id)


async def ledger_counts(store):
    async with store.engine.connect() as connection:
        return tuple(
            [
                await connection.scalar(sa.select(sa.func.count()).select_from(table))
                for table in (meals, meal_revisions)
            ]
        )


async def test_measured_multi_food_log_has_exact_snapshot_and_truthful_receipt(
    service, store, catalog
):
    result = await process(service, store, message(1, "Lunch: 150g rice and 200g chicken"))
    saved = await current(store)
    assert saved.label == "Lunch"
    assert saved.local_date == date(2023, 11, 14)
    assert [item.edible_milligrams for item in saved.items] == [150_000, 200_000]
    assert [item.food_version_id for item in saved.items] == [
        catalog["rice"].version_id,
        catalog["chicken"].version_id,
    ]
    assert all(item.quantity_method == "measured" for item in saved.items)
    assert "Saved M1r1" in result["payload"]["text"]
    assert "Energy: 550 kcal" in result["payload"]["text"]
    assert "P: 55.0 g" in result["payload"]["text"]
    assert "C: unknown" in result["payload"]["text"]
    assert "F: 0.0 g" in result["payload"]["text"]
    assert "Fiber:" not in result["payload"]["text"]
    assert result["payload"]["text"].index("Energy: 550 kcal") < result["payload"]["text"].index(
        "1. 150 g rice"
    )
    assert {button["text"] for button in result["payload"]["buttons"]} == {
        "Edit",
        "Log again",
        "Undo save",
        "Details",
        "More",
    }
    details = await process(service, store, press(result, "details", update_id=2))
    assert "Fiber: 1.5 g known (partial; 1 item(s) unknown)" in details["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)


async def test_duplicate_update_and_same_source_message_never_double_log(service, store, catalog):
    update = message(1, "150g rice")
    await process(service, store, update)
    await service.accept([update, update])
    assert not await service.process_one()
    repeated = update.model_dump(mode="json", exclude_none=True)
    repeated["update_id"] = 2
    response = await process(service, store, Update.model_validate(repeated))
    assert "already has a meal" in response["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)


async def test_reply_corrects_quantity_food_and_date_with_separate_receipt_revisions(
    service, store, catalog
):
    first = await process(service, store, message(1, "150g rice; 200g chicken"))
    second = await process(service, store, reply(2, "rice was 120g", first))
    changed = await current(store)
    assert changed.revision_number == 2
    assert [item.edible_milligrams for item in changed.items] == [120_000, 200_000]
    third = await process(service, store, reply(3, "item 2: 80g beans", second))
    changed = await current(store)
    assert changed.items[1].food_version_id == catalog["beans"].version_id
    assert changed.items[1].edible_milligrams == 80_000
    fourth = await process(service, store, reply(4, "date yesterday", third))
    changed = await current(store)
    assert changed.local_date == date(2023, 11, 13)
    assert changed.revision_number == 4
    assert "Updated M1r4" in fourth["payload"]["text"]
    assert await ledger_counts(store) == (1, 4)


async def test_single_item_quantity_only_and_whole_meal_replacement(service, store, catalog):
    first = await process(service, store, message(1, "100g rice"))
    second = await process(service, store, reply(2, "125g", first))
    assert (await current(store)).items[0].edible_milligrams == 125_000
    await process(service, store, reply(3, "replace: 50g beans; 25g chicken", second))
    assert [(item.food_name, item.edible_milligrams) for item in (await current(store)).items] == [
        ("beans", 50_000),
        ("chicken", 25_000),
    ]


async def test_item_delete_does_not_allow_empty_meal(service, store, catalog):
    first = await process(service, store, message(1, "100g rice; 50g chicken"))
    second = await process(service, store, reply(2, "item 2: delete", first))
    assert len((await current(store)).items) == 1
    rejected = await process(service, store, reply(3, "item 1: delete", second))
    assert "one-item meal" in rejected["payload"]["text"]
    assert await ledger_counts(store) == (1, 2)


async def test_explicit_mutations_require_current_revision_and_can_delete_and_undo(
    service, store, catalog
):
    await process(service, store, message(1, "100g rice"))
    rejected = await process(service, store, message(2, "/edit M1 item 1: 120g"))
    assert "current receipt" in rejected["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)
    await process(service, store, message(3, "/edit M1r1 item 1: 120g"))
    rejected = await process(service, store, message(4, "/delete M1r1"))
    assert "current receipt" in rejected["payload"]["text"]
    assert not (await current(store)).deleted
    await process(service, store, message(5, "/delete M1r2"))
    assert (await current(store)).deleted
    await process(service, store, message(6, "/undo M1r3"))
    restored = await current(store)
    assert restored.revision_number == 4
    assert not restored.deleted
    assert restored.items[0].edible_milligrams == 120_000
    view = await process(service, store, message(7, "/meal M1"))
    assert "M1r4" in view["payload"]["text"]
    assert await ledger_counts(store) == (1, 4)


async def test_delete_undo_buttons_are_durable_and_replayed_callbacks_do_not_repeat_mutation(
    service, store, catalog
):
    first = await process(service, store, message(1, "100g rice"))
    more = await process(service, store, press(first, "more", update_id=2))
    delete_update = press(more, "delete", update_id=3, callback_id="synthetic-delete")
    deleted = await process(service, store, delete_update)
    assert (await current(store)).deleted
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(sa.func.count())
                .select_from(outbox)
                .where(
                    outbox.c.action_key == "callback:synthetic-delete",
                    outbox.c.kind == "callback_answer",
                )
            )
            == 1
        )
    replay = delete_update.model_dump(mode="json", exclude_none=True)
    replay["update_id"] = 4
    await process(service, store, Update.model_validate(replay))
    assert await ledger_counts(store) == (1, 2)
    await process(service, store, press(deleted, "restore", update_id=5))
    assert not (await current(store)).deleted
    stale = await process(service, store, press(more, "delete", update_id=6))
    assert "older receipt" in stale["payload"]["text"]
    assert not (await current(store)).deleted
    assert await ledger_counts(store) == (1, 3)


async def test_edit_button_only_opens_current_correction_help(service, store, catalog):
    first = await process(service, store, message(1, "100g rice"))
    edit = await process(service, store, press(first, "edit"))
    assert "item 1: 120g" in edit["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)
    await process(service, store, reply(3, "120g", edit))
    assert (await current(store)).items[0].edible_milligrams == 120_000


@pytest.mark.parametrize("forgery", ["message_id", "token", "action", "unsent", "expired"])
async def test_forged_or_expired_meal_buttons_cannot_mutate(service, store, catalog, forgery):
    first = await process(service, store, message(1, "100g rice"))
    more = await process(service, store, press(first, "more", update_id=2))
    if forgery in {"unsent", "expired"}:
        values = {"status": "queued"} if forgery == "unsent" else {"sent_at": 1.0}
        async with store.write() as connection:
            await connection.execute(
                sa.update(outbox).where(outbox.c.id == more["id"]).values(**values)
            )
    update = press(
        more, "delete", update_id=3, message_id=99999 if forgery == "message_id" else None
    )
    if forgery in {"token", "action"}:
        payload = update.model_dump(mode="json", exclude_none=True)
        payload["callback_query"]["data"] = (
            "meal:delete:not-issued" if forgery == "token" else f"meal:erase:{more['button_token']}"
        )
        update = Update.model_validate(payload)
    assert await process(service, store, update) is None
    assert await ledger_counts(store) == (1, 1)
    async with store.engine.connect() as connection:
        rejected = await connection.execute(sa.select(inbox).where(inbox.c.update_id == 3))
        row = rejected.mappings().one()
        assert row["status"] == "rejected"
        assert row["payload"] is None


async def test_unissued_operation_with_otherwise_valid_receipt_token_is_rejected(
    service, store, catalog
):
    first = await process(service, store, message(1, "100g rice"))
    undone = await process(service, store, press(first, "undo_save", update_id=2))
    assert "Undo save" not in [button["text"] for button in undone["payload"]["buttons"]]
    assert await process(service, store, press(undone, "undo_save", update_id=3)) is None
    assert await ledger_counts(store) == (1, 2)


async def test_unauthorized_button_never_enters_inbox(service, store, catalog):
    first = await process(service, store, message(1, "100g rice"))
    await service.accept([press(first, "delete", user=202)])
    assert not await service.process_one()
    assert await ledger_counts(store) == (1, 1)
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(inbox)) == 1


async def test_stale_reply_does_not_create_another_meal_or_revision(service, store, catalog):
    first = await process(service, store, message(1, "100g rice"))
    await process(service, store, reply(2, "120g rice", first))
    rejected = await process(service, store, reply(3, "140g rice", first))
    assert "older" in rejected["payload"]["text"]
    assert (await current(store)).items[0].edible_milligrams == 120_000
    assert await ledger_counts(store) == (1, 2)


@pytest.mark.parametrize(
    "forgery", ["message_id", "wrong_author", "human_author", "unsent", "expired", "owner"]
)
async def test_unrecognized_reply_cannot_fall_through_to_a_new_meal(
    service, store, catalog, forgery
):
    first = await process(service, store, message(1, "100g rice"))
    if forgery in {"unsent", "expired", "owner"}:
        values = {
            "unsent": {"status": "queued"},
            "expired": {"sent_at": 1.0},
            "owner": {"owner_user_id": 202},
        }[forgery]
        async with store.write() as connection:
            await connection.execute(
                sa.update(outbox).where(outbox.c.id == first["id"]).values(**values)
            )
    update = reply(
        2,
        "120g rice",
        first,
        message_id=99999 if forgery == "message_id" else None,
        bot_id=987654 if forgery == "wrong_author" else 123456,
        is_bot=forgery != "human_author",
    )
    await process(service, store, update)
    assert await ledger_counts(store) == (1, 1)
    assert (await current(store)).items[0].edible_milligrams == 100_000


@pytest.mark.parametrize(
    "text",
    ["100g rice; 50g missing", "about 100g rice", "100g rice and a banana", "2 eggs", "1 cup rice"],
)
async def test_unresolved_or_rough_meals_never_partially_save(service, store, catalog, text):
    response = await process(service, store, message(1, text))
    if text == "about 100g rice":
        assert "Not in your totals" in response["payload"]["text"]
        assert response["payload"]["draft_revision"] == 1
    elif text == "100g rice; 50g missing":
        assert "No matching food yet" in response["payload"]["text"]
    else:
        assert "Nothing was changed" in response["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    async with store.engine.connect() as connection:
        expected_kind = (
            "draft_receipt"
            if text == "about 100g rice"
            else "navigation"
            if text == "100g rice; 50g missing"
            else "meal_rejected"
        )
        assert await connection.scalar(sa.select(actions.c.kind)) == expected_kind


async def test_ambiguous_preparation_requires_food_version_selection(service, store, catalog):
    async with store.write() as connection:
        raw = await publish_reviewed_food(connection, food_record("rice", preparation="raw"))
    response = await process(service, store, message(1, "100g rice; 50g chicken"))
    assert "Choose the food and preparation" in response["payload"]["text"]
    assert "raw" in response["payload"]["text"] and "cooked" in response["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    choices = await process(service, store, message(2, "/foods rice"))
    assert f"#{raw.version_id}" in choices["payload"]["text"]
    assert f"#{catalog['rice'].version_id}" in choices["payload"]["text"]
    await process(service, store, message(3, f"100g #{raw.version_id}"))
    assert (await current(store)).items[0].preparation == "raw"


async def test_catalog_revision_never_recalculates_existing_meal_or_quantity_correction(
    service, store, catalog
):
    original = await process(service, store, message(1, "100g rice"))
    async with store.write() as connection:
        changed = await publish_reviewed_food(
            connection,
            food_record("rice", energy="999", protein="99", fiber="9"),
            food_id=catalog["rice"].food_id,
        )
    await process(service, store, reply(2, "rice was 200g", original))
    old_meal = await current(store)
    assert old_meal.items[0].food_version_id == catalog["rice"].version_id
    assert next(n.amount_scaled for n in old_meal.items[0].nutrients if n.code == "energy") == (
        200_000_000
    )
    await process(service, store, message(3, "100g rice"))
    new_meal = await current(store, 2)
    assert new_meal.items[0].food_version_id == changed.version_id
    assert next(n.amount_scaled for n in new_meal.items[0].nutrients if n.code == "energy") == (
        999_000_000
    )


async def test_original_telegram_message_edit_provides_guidance_without_relogging(
    service, store, catalog
):
    await process(service, store, message(1, "100g rice"))
    changed = message(2, "200g rice", edited=True).model_dump(mode="json", exclude_none=True)
    changed["edited_message"]["message_id"] = 1
    response = await process(service, store, Update.model_validate(changed))
    assert "diary unchanged" in response["payload"]["text"]
    assert "100 g rice" in response["payload"]["text"]
    assert (await current(store)).items[0].edible_milligrams == 100_000
    unknown = await process(service, store, message(3, "300g rice", edited=True))
    assert "did not create a meal" in unknown["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)


@pytest.mark.parametrize(
    "correction",
    [
        "item 1: yesterday 120g rice",
        "item 1: Lunch: 120g rice",
        "replace: yesterday 120g rice",
        "replace: Dinner: 120g rice",
        "yesterday 120g rice",
        "Dinner: 120g rice",
    ],
)
async def test_correction_never_silently_discards_embedded_date_or_label(
    service, store, catalog, correction
):
    first = await process(service, store, message(1, "100g rice"))
    await process(service, store, reply(2, correction, first))
    assert await ledger_counts(store) == (1, 1)
    assert (await current(store)).items[0].edible_milligrams == 100_000


@pytest.mark.parametrize("mass", ["100G", "0.1KG", "100000MG"])
async def test_uppercase_mass_units_log_exactly_without_losing_original_unit(
    service, store, catalog, mass
):
    await process(service, store, message(1, f"{mass} rice"))
    saved = await current(store)
    assert saved.items[0].edible_milligrams == 100_000
    assert saved.items[0].original_unit.isupper()


async def test_meal_receipts_remain_viewable_after_raw_outbox_cleanup(service, store, catalog):
    first = await process(service, store, message(1, "100g rice"))
    async with store.write() as connection:
        await connection.execute(
            sa.update(outbox)
            .where(outbox.c.id == first["id"])
            .values(created_at=time.time() - 40 * 86400)
        )
    await service.cleanup()
    view = await process(service, store, message(2, "/meal M1"))
    assert "M1r1" in view["payload"]["text"]
    assert "100 g rice" in view["payload"]["text"]
    await process(service, store, reply(3, "120g", view))
    assert (await current(store)).items[0].edible_milligrams == 120_000


async def test_rejected_action_rolls_back_completed_ledger_write_but_keeps_error_receipt(
    service, store, catalog, monkeypatch
):
    create = meal_conversation.create_meal

    async def fail_after_write(*args, **kwargs):
        await create(*args, **kwargs)
        raise MealError("Synthetic post-write rejection")

    monkeypatch.setattr(meal_conversation, "create_meal", fail_after_write)
    rejected = await process(service, store, message(1, "100g rice"))
    assert "Synthetic post-write rejection" in rejected["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(actions.c.kind)) == "meal_rejected"
        assert await connection.scalar(sa.select(inbox.c.status)) == "done"
        assert await connection.scalar(sa.select(sa.func.count()).select_from(outbox)) == 1
