"""Offline Telegram aliases, versioned favorites and explicit meal reuse acceptance."""

from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from aiogram.types import Update

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.schema import inbox, outbox, profile
from tests.helpers import callback, message
from tests.test_telegram_drafts import press as approve_draft
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import (
    current,
    food_record,
    ledger_counts,
    process,
    reply,
)


def press(receipt, operation="log", *, update_id=3, callback_id=None):
    update = callback(
        receipt["button_token"],
        update_id=update_id,
        callback_id=callback_id or f"synthetic-favorite-{update_id}",
        message_id=receipt["telegram_message_id"] or 77,
    ).model_dump(mode="json", exclude_none=True)
    update["callback_query"]["data"] = f"favorite:{operation}:{receipt['button_token']}"
    return Update.model_validate(update)


async def favorite(service, store, *, source="150g rice; 200g chicken", name="usual breakfast"):
    meal = await process(service, store, message(1, source))
    preview = await process(service, store, message(2, f"/favorite save {name} = M1r1"))
    return meal, preview


async def test_alias_creation_and_lookup_pin_exact_food_version(service, store, catalog):
    result = await process(
        service, store, message(1, f"/alias my rice = #{catalog['rice'].version_id}")
    )
    assert "A1r1" in result["payload"]["text"] and "my rice" in result["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    listing = await process(service, store, message(2, "/aliases"))
    assert "my rice" in listing["payload"]["text"] and "A1r1" in listing["payload"]["text"]
    opened = await process(service, store, message(3, "/alias A1"))
    assert "my rice" in opened["payload"]["text"]
    await process(service, store, message(4, "150g my rice"))
    saved = await current(store)
    assert saved.items[0].food_version_id == catalog["rice"].version_id
    assert saved.items[0].edible_milligrams == 150000
    assert saved.items[0].quantity_method == "measured"


async def test_unicode_casefold_alias_matches_without_rewriting_food_identity(
    service, store, catalog
):
    await process(service, store, message(1, f"/alias Straße Reis = #{catalog['rice'].version_id}"))
    await process(service, store, message(2, "150g STRASSE REIS"))
    assert (await current(store)).items[0].food_name == "rice"
    assert (await current(store)).items[0].food_version_id == catalog["rice"].version_id


async def test_alias_remap_requires_current_revision_and_keeps_old_meals(service, store, catalog):
    await process(service, store, message(1, f"/alias my rice = #{catalog['rice'].version_id}"))
    await process(service, store, message(2, "150g my rice"))
    original = await current(store)
    updated = await process(
        service,
        store,
        message(3, f"/alias update A1r1 = #{catalog['beans'].version_id}"),
    )
    assert "A1r2" in updated["payload"]["text"]
    rejected = await process(
        service,
        store,
        message(4, f"/alias update A1r1 = #{catalog['chicken'].version_id}"),
    )
    assert (
        "current" in rejected["payload"]["text"].lower()
        or "changed" in rejected["payload"]["text"].lower()
    )
    await process(service, store, message(5, "150g my rice"))
    assert (await current(store, 2)).items[0].food_version_id == catalog["beans"].version_id
    assert await current(store) == original


async def test_alias_off_and_on_are_revision_bound(service, store, catalog):
    await process(service, store, message(1, f"/alias my rice = #{catalog['rice'].version_id}"))
    off = await process(service, store, message(2, "/alias off A1r1"))
    assert "A1r2" in off["payload"]["text"]
    await process(service, store, message(3, "150g my rice"))
    assert await ledger_counts(store) == (0, 0)
    await process(service, store, message(4, "/alias on A1r1"))
    await process(service, store, message(5, "150g my rice"))
    assert await ledger_counts(store) == (0, 0)
    on = await process(service, store, message(6, "/alias on A1r2"))
    assert "A1r3" in on["payload"]["text"]
    await process(service, store, message(7, "150g my rice"))
    assert await ledger_counts(store) == (1, 1)


async def test_alias_does_not_follow_new_food_catalog_versions(service, store, catalog):
    await process(service, store, message(1, f"/alias my rice = #{catalog['rice'].version_id}"))
    async with store.write() as connection:
        refreshed = await publish_reviewed_food(
            connection, food_record("rice", energy="999"), food_id=catalog["rice"].food_id
        )
    await process(service, store, message(2, "100g my rice"))
    saved = await current(store)
    assert saved.items[0].food_version_id == catalog["rice"].version_id != refreshed.version_id
    assert (
        next(n.amount_scaled for n in saved.items[0].nutrients if n.code == "energy") == 100000000
    )


@pytest.mark.parametrize("use_reply", [False, True])
async def test_saving_favorite_never_logs_another_consumption(service, store, catalog, use_reply):
    meal = await process(service, store, message(1, "150g rice; 200g chicken"))
    request = (
        reply(2, "save as usual breakfast", meal)
        if use_reply
        else message(2, "/favorite save usual breakfast = M1r1")
    )
    result = await process(service, store, request)
    assert "F1v1" in result["payload"]["text"]
    assert "usual breakfast" in result["payload"]["text"]
    assert result["payload"]["favorite_id"] == 1
    assert type(result["payload"]["favorite_version_id"]) is int
    assert {button["text"] for button in result["payload"]["buttons"]} == {"Log this", "Archive"}
    assert await ledger_counts(store) == (1, 1)
    listing = await process(service, store, message(3, "/favorites"))
    assert "usual breakfast" in listing["payload"]["text"] and "F1v1" in listing["payload"]["text"]
    opened = await process(service, store, message(4, "/favorite F1"))
    assert opened["payload"]["favorite_version_id"] == result["payload"]["favorite_version_id"]
    assert await ledger_counts(store) == (1, 1)


@pytest.mark.parametrize(
    "text", ["/eat F1v1", "/eat usual breakfast", "usual breakfast", "I ate usual breakfast"]
)
async def test_exact_favorite_use_logs_measured_portions(service, store, catalog, text):
    await favorite(service, store)
    response = await process(service, store, message(3, text))
    saved = await current(store, 2)
    assert "Saved" in response["payload"]["text"]
    assert [item.edible_milligrams for item in saved.items] == [150000, 200000]
    assert all(item.quantity_method == "measured" for item in saved.items)
    assert await ledger_counts(store) == (2, 2)


async def test_favorite_button_logs_only_the_displayed_version(service, store, catalog):
    _, preview = await favorite(service, store)
    response = await process(service, store, press(preview))
    assert "Saved" in response["payload"]["text"]
    saved = await current(store, 2)
    assert [item.edible_milligrams for item in saved.items] == [150000, 200000]
    assert saved.items[0].food_version_id == catalog["rice"].version_id


async def test_favorite_callback_uses_received_local_date_not_original_preview_date(
    service, store, catalog
):
    _, preview = await favorite(service, store)
    source = await current(store)
    received = datetime(2026, 3, 28, 21, 30, tzinfo=UTC).timestamp()
    # The callback message still has its original 2023 Telegram receipt timestamp.
    # The received update is a new consumption, interpreted in the current profile zone.
    request = press(preview, update_id=3)
    await service.accept([request])
    async with store.write() as connection:
        await connection.execute(
            sa.insert(profile).values(id=1, timezone="Pacific/Kiritimati", created_at=received)
        )
        await connection.execute(
            sa.update(inbox).where(inbox.c.update_id == 3).values(received_at=received)
        )
    assert await service.process_one()
    reused = await current(store, 2)
    assert source.local_date == date(2023, 11, 14)
    assert reused.local_date == date(2026, 3, 29)
    assert reused.timezone == "Pacific/Kiritimati"
    assert reused.items == source.items
    assert await current(store) == source


async def test_favorite_scale_and_backdate_use_exact_milligrams(service, store, catalog):
    await favorite(service, store, source="150.125g rice; 200g chicken")
    response = await process(service, store, message(3, "/eat yesterday F1v1 x0.5"))
    # Half a milligram cannot be silently rounded to an exact-looking portion.
    assert "Saved" not in response["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)
    saved_source = await process(service, store, message(4, "150g rice; 200g chicken"))
    assert "M2r1" in saved_source["payload"]["text"]
    await process(service, store, message(5, "/favorite update F1v1 = M2r1"))
    await process(service, store, message(6, "/eat yesterday F1v2 x0.5"))
    saved = await current(store, 3)
    assert saved.local_date == date(2023, 11, 13)
    assert [item.edible_milligrams for item in saved.items] == [75000, 100000]


@pytest.mark.parametrize(
    "scale", ["x0", "x-1", "x100.001", "x0.0001", "x1.0001", "xNaN", "x1-2", "x1.2.3"]
)
async def test_invalid_favorite_scale_never_writes_consumption(service, store, catalog, scale):
    await favorite(service, store)
    await process(service, store, message(3, f"/eat F1v1 {scale}"))
    assert await ledger_counts(store) == (1, 1)


async def test_favorite_update_keeps_history_and_invalidates_old_preview(service, store, catalog):
    _, first = await favorite(service, store)
    original = await current(store)
    await process(service, store, message(3, "70g beans"))
    updated = await process(service, store, message(4, "/favorite update F1v1 = M2r1"))
    assert "F1v2" in updated["payload"]["text"]
    assert updated["payload"]["favorite_version_id"] != first["payload"]["favorite_version_id"]
    stale = await process(service, store, press(first, update_id=5))
    assert "Saved" not in stale["payload"]["text"]
    assert await ledger_counts(store) == (2, 2)
    await process(service, store, message(6, "/eat F1v1"))
    assert await ledger_counts(store) == (2, 2)
    await process(service, store, message(7, "/eat F1v2"))
    assert (await current(store, 3)).items[0].food_name == "beans"
    assert (await current(store, 3)).items[0].edible_milligrams == 70000
    assert await current(store) == original


async def test_source_meal_correction_does_not_rewrite_saved_favorite(service, store, catalog):
    await favorite(service, store)
    await process(service, store, message(3, "/edit M1r1 item 1: 120g"))
    await process(service, store, message(4, "/eat F1v1"))
    assert (await current(store)).items[0].edible_milligrams == 120000
    assert (await current(store, 2)).items[0].edible_milligrams == 150000


async def test_archive_restore_advances_versions_and_does_not_revive_stale_buttons(
    service, store, catalog
):
    _, original = await favorite(service, store)
    archived = await process(service, store, press(original, "archive", update_id=3))
    assert "F1v2" in archived["payload"]["text"]
    assert {button["text"] for button in archived["payload"]["buttons"]} == {"Restore"}
    active = await process(service, store, message(4, "/favorites"))
    assert "No saved favorites" in active["payload"]["text"]
    assert "F1v2" not in active["payload"]["text"]
    all_items = await process(service, store, message(5, "/favorites all"))
    assert "usual breakfast" in all_items["payload"]["text"]
    await process(service, store, message(6, "/eat F1v2"))
    assert await ledger_counts(store) == (1, 1)
    restored = await process(service, store, press(archived, "restore", update_id=7))
    assert "F1v3" in restored["payload"]["text"]
    await process(service, store, press(original, update_id=8))
    assert await ledger_counts(store) == (1, 1)
    await process(service, store, message(9, "/eat F1v3"))
    assert await ledger_counts(store) == (2, 2)


async def test_explicit_archive_and_restore_require_current_version(service, store, catalog):
    await favorite(service, store)
    archived = await process(service, store, message(3, "/favorite archive F1v1"))
    assert "F1v2" in archived["payload"]["text"]
    await process(service, store, message(4, "/favorite restore F1v1"))
    await process(service, store, message(5, "/eat usual breakfast"))
    assert await ledger_counts(store) == (1, 1)
    await process(service, store, message(6, "/favorite restore F1v2"))
    await process(service, store, message(7, "/eat usual breakfast"))
    assert await ledger_counts(store) == (2, 2)


async def test_favorite_collision_with_food_requires_explicit_eat(service, store, catalog):
    await favorite(service, store, source="70g beans", name="rice")
    response = await process(service, store, message(3, "rice"))
    assert "/eat" in response["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)
    await process(service, store, message(4, "/eat rice"))
    assert (await current(store, 2)).items[0].food_name == "beans"


async def test_favorite_collision_with_alias_requires_explicit_eat(service, store, catalog):
    await favorite(service, store, source="70g beans")
    await process(
        service, store, message(3, f"/alias usual breakfast = #{catalog['rice'].version_id}")
    )
    response = await process(service, store, message(4, "I ate usual breakfast"))
    assert "/eat" in response["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)
    await process(service, store, message(5, "/eat usual breakfast"))
    assert (await current(store, 2)).items[0].food_name == "beans"


@pytest.mark.parametrize("text", ["/repeat M1r1", "/repeat M1r1 x2", "/repeat yesterday M1r1 x2"])
async def test_measured_repeat_pins_source_and_scales_exactly(service, store, catalog, text):
    await process(service, store, message(1, "Lunch: 150g rice"))
    result = await process(service, store, message(2, text))
    assert "Saved" in result["payload"]["text"]
    saved = await current(store, 2)
    assert saved.items[0].edible_milligrams == (300000 if "x2" in text else 150000)
    assert saved.items[0].quantity_method == "measured"
    assert saved.local_date == date(2023, 11, 13 if "yesterday" in text else 14)


async def test_reply_same_again_logs_a_separate_meal(service, store, catalog):
    first = await process(service, store, message(1, "Lunch: 150g rice"))
    await process(service, store, reply(2, "same again", first))
    assert await ledger_counts(store) == (2, 2)
    assert (await current(store, 2)).items == (await current(store)).items


async def test_same_lunch_as_yesterday_requires_unique_matching_meal(service, store, catalog):
    await process(service, store, message(1, "/meal yesterday Lunch: 150g rice"))
    await process(service, store, message(2, "same Lunch as yesterday"))
    copied = await current(store, 2)
    assert copied.local_date == date(2023, 11, 14)
    assert copied.items[0].edible_milligrams == 150000
    await process(service, store, message(3, "/meal yesterday Lunch: 90g beans"))
    response = await process(service, store, message(4, "same Lunch as yesterday"))
    assert await ledger_counts(store) == (3, 3)
    assert "/repeat" in response["payload"]["text"] or "M1" in response["payload"]["text"]


async def test_repeat_stale_or_deleted_source_never_logs_a_guess(service, store, catalog):
    await process(service, store, message(1, "150g rice"))
    await process(service, store, message(2, "/edit M1r1 item 1: 120g"))
    await process(service, store, message(3, "/repeat M1r1"))
    assert await ledger_counts(store) == (1, 2)
    await process(service, store, message(4, "/delete M1r2"))
    await process(service, store, message(5, "/repeat M1r3"))
    assert await ledger_counts(store) == (1, 3)


async def test_estimated_favorite_requires_new_draft_approval_each_time(service, store, catalog):
    proposed = await process(service, store, message(1, "about 150g rice; 200g chicken"))
    await process(
        service, store, approve_draft(proposed, update_id=2, callback_id="original-estimate")
    )
    original = await current(store)
    await process(service, store, message(3, "/favorite save usual breakfast = M1r1"))
    proposed_again = await process(service, store, message(4, "/eat F1v1"))
    assert "draft_id" in proposed_again["payload"]
    assert await ledger_counts(store) == (1, 1)
    await process(
        service, store, approve_draft(proposed_again, update_id=5, callback_id="new-estimate")
    )
    reused = await current(store, 2)
    assert reused.items[0].quantity_method == "approved_estimate"
    assert reused.items[1].quantity_method == "measured"
    assert reused.items[0].approval_action_key == "callback:new-estimate"
    assert reused.items[0].approval_draft_id != original.items[0].approval_draft_id
    third = await process(service, store, message(6, "/eat usual breakfast"))
    assert "draft_id" in third["payload"]
    assert await ledger_counts(store) == (2, 2)


async def test_estimated_repeat_cannot_reuse_source_approval(service, store, catalog):
    first = await process(service, store, message(1, "about 150g rice"))
    await process(service, store, approve_draft(first, update_id=2))
    repeated = await process(service, store, message(3, "/repeat M1r1 x2"))
    assert "draft_id" in repeated["payload"] and "300" in repeated["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)
    await process(service, store, approve_draft(repeated, update_id=4))
    saved = await current(store, 2)
    assert saved.items[0].edible_milligrams == 300000
    assert saved.items[0].quantity_method == "approved_estimate"
    assert saved.items[0].approval_action_key != (await current(store)).items[0].approval_action_key


async def test_replayed_favorite_callback_never_duplicates_consumption(service, store, catalog):
    _, preview = await favorite(service, store)
    request = press(preview, callback_id="one-consumption")
    await process(service, store, request)
    await service.accept([request, request])
    assert not await service.process_one()
    replayed = request.model_dump(mode="json", exclude_none=True)
    replayed["update_id"] = 4
    await service.accept([Update.model_validate(replayed)])
    assert await service.process_one()
    assert await ledger_counts(store) == (2, 2)


@pytest.mark.parametrize(
    "forgery",
    [
        "wrong_message",
        "wrong_token",
        "unsent",
        "wrong_owner",
        "draft_namespace",
        "meal_namespace",
        "status_namespace",
    ],
)
async def test_favorite_callback_requires_bound_sent_preview(service, store, catalog, forgery):
    await process(service, store, message(1, "150g rice"))
    preview = await process(
        service,
        store,
        message(2, "/favorite save usual breakfast = M1r1"),
        sent=forgery != "unsent",
    )
    request = press(preview).model_dump(mode="json", exclude_none=True)
    if forgery == "wrong_message":
        request["callback_query"]["message"]["message_id"] += 1
    elif forgery == "wrong_token":
        request["callback_query"]["data"] = "favorite:log:unrecognized"
    elif forgery == "wrong_owner":
        async with store.write() as connection:
            await connection.execute(
                sa.update(outbox).where(outbox.c.id == preview["id"]).values(owner_user_id=202)
            )
    elif forgery == "draft_namespace":
        request["callback_query"]["data"] = f"draft:approve:{preview['button_token']}"
    elif forgery == "meal_namespace":
        request["callback_query"]["data"] = f"meal:delete:{preview['button_token']}"
    elif forgery == "status_namespace":
        request["callback_query"]["data"] = f"status:{preview['button_token']}"
    result = await process(service, store, Update.model_validate(request))
    assert result is None
    assert await ledger_counts(store) == (1, 1)


@pytest.mark.parametrize("forgery", ["owner", "chat", "group"])
async def test_unauthorized_favorite_callback_never_enters_inbox(service, store, catalog, forgery):
    _, preview = await favorite(service, store)
    request = press(preview).model_dump(mode="json", exclude_none=True)
    if forgery == "owner":
        request["callback_query"]["from_user"]["id"] = 202
    elif forgery == "chat":
        request["callback_query"]["message"]["chat"]["id"] = 202
    else:
        request["callback_query"]["message"]["chat"]["type"] = "group"
    await service.accept([Update.model_validate(request)])
    assert not await service.process_one()
    assert await ledger_counts(store) == (1, 1)
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(inbox)) == 2
