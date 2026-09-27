from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from nutrition_bot.adapters.database.drafts import (
    close_draft,
    create_draft,
    expire_drafts,
    get_draft,
    revise_draft,
    touch_draft,
    track_draft_action,
)
from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.meals import MealItemInput, create_meal
from nutrition_bot.adapters.database.schema import actions, food_versions, inbox, outbox
from nutrition_bot.adapters.database.schema_drafts import (
    draft_action_links,
    draft_food_refs,
    meal_drafts,
)
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.domain.drafts import DRAFT_TTL_SECONDS, DraftContent, DraftError, PlannedItem
from tests.test_food_storage import reviewed_food

NOW = datetime(2024, 1, 15, 12, tzinfo=UTC).timestamp()
DAY = date(2024, 1, 15)


async def action(connection, key, payload=None):
    update_id = 1 + (await connection.scalar(sa.select(sa.func.count()).select_from(inbox)))
    await connection.execute(
        sa.insert(inbox).values(
            update_id=update_id, payload=payload, status="done", received_at=NOW, processed_at=NOW
        )
    )
    await connection.execute(
        sa.insert(actions).values(
            key=key, update_id=update_id, kind="synthetic_draft", created_at=NOW
        )
    )
    return update_id


@pytest.fixture
async def food(store):
    async with store.write() as connection:
        return await publish_reviewed_food(connection, reviewed_food())


def proposed(food, **overrides):
    values = dict(
        label="Synthetic lunch",
        local_date=DAY,
        timezone="UTC",
        consumed_at=NOW,
        source_chat_id=101,
        source_message_id=10,
        items=(
            PlannedItem(
                food_version_id=food.version_id,
                edible_milligrams=125000,
                original_quantity="125",
                original_unit="g",
                estimate_basis="Synthetic reviewed serving",
            ),
        ),
    )
    values.update(overrides)
    return DraftContent(**values)


async def make_draft(connection, food, *, key="create", now=NOW, **overrides):
    await action(connection, key, {"message": {"text": "synthetic temporary proposal"}})
    return await create_draft(connection, proposed(food, **overrides), action_key=key, now=now)


async def add_reply(connection, draft, *, key="create", status="sent", meal_id=None):
    payload = {
        "text": "Synthetic PRIVATE preview",
        "draft_id": draft.id,
        "draft_revision": draft.revision,
        "buttons": [{"text": "Arbitrary private text", "callback_data": "untrusted"}],
    }
    if meal_id is not None:
        payload["meal_id"] = meal_id
    result = await connection.execute(
        sa.insert(outbox).values(
            action_key=key,
            kind="message",
            chat_id=101,
            owner_user_id=101,
            payload=payload,
            status=status,
            next_attempt_at=NOW,
            created_at=NOW,
            sent_at=NOW if status == "sent" else None,
            telegram_message_id=77 if status == "sent" else None,
            button_token="opaque-" + key,
        )
    )
    return result.inserted_primary_key[0]


async def test_draft_survives_restart_with_exact_proposal_and_food_reference(store, settings, food):
    async with store.write() as connection:
        draft = await make_draft(connection, food)
    other = Store(settings)
    try:
        async with other.engine.connect() as connection:
            assert await get_draft(connection, draft.id) == draft
            assert (
                await connection.scalar(sa.select(sa.func.count()).select_from(draft_food_refs))
                == 1
            )
    finally:
        await other.close()
    assert draft.state == "open"
    assert draft.revision == 1
    assert draft.content.items[0].estimate_basis == "Synthetic reviewed serving"


async def test_revision_invalidates_old_approval_and_touch_does_not_increment(store, food):
    async with store.write() as connection:
        draft = await make_draft(connection, food)
        await action(connection, "edit")
        revised = await revise_draft(
            connection,
            draft.id,
            1,
            proposed(food, label="Changed"),
            action_key="edit",
            now=NOW + 10,
        )
        assert revised.revision == 2
        assert revised.content.label == "Changed"
        await action(connection, "stale")
        with pytest.raises(DraftError, match="changed"):
            await touch_draft(connection, draft.id, 1, action_key="stale", now=NOW + 20)
        await action(connection, "open")
        touched = await touch_draft(connection, draft.id, 2, action_key="open", now=NOW + 30)
        assert touched.revision == 2
        assert touched.last_user_activity_at == NOW + 30
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(draft_action_links)) == 3
        )


@pytest.mark.parametrize("offset", [DRAFT_TTL_SECONDS, DRAFT_TTL_SECONDS + 1])
async def test_expired_draft_rejects_use_before_cleanup(store, food, offset):
    async with store.write() as connection:
        draft = await make_draft(connection, food)
        await action(connection, "late")
        for operation in ("touch", "edit", "cancel"):
            with pytest.raises(DraftError, match="expired"):
                if operation == "touch":
                    await touch_draft(connection, draft.id, 1, action_key="late", now=NOW + offset)
                elif operation == "edit":
                    await revise_draft(
                        connection, draft.id, 1, proposed(food), action_key="late", now=NOW + offset
                    )
                else:
                    await close_draft(
                        connection,
                        draft.id,
                        1,
                        state="cancelled",
                        action_key="late",
                        now=NOW + offset,
                    )
        assert (await get_draft(connection, draft.id)).last_user_activity_at == NOW


async def test_activity_extends_expiry_and_reading_does_not(store, food):
    async with store.write() as connection:
        draft = await make_draft(connection, food)
        assert await expire_drafts(connection, now=NOW + DRAFT_TTL_SECONDS - 1) == 0
        await action(connection, "touch")
        touched = await touch_draft(connection, draft.id, 1, action_key="touch", now=NOW + 100)
        assert await expire_drafts(connection, now=NOW + DRAFT_TTL_SECONDS) == 0
        assert await get_draft(connection, draft.id) == touched
        assert await expire_drafts(connection, now=NOW + 100 + DRAFT_TTL_SECONDS) == 1
        expired = await get_draft(connection, draft.id)
        assert expired.state == "expired"
        assert expired.content is None
        assert expired.last_user_activity_at == NOW + 100
        assert await expire_drafts(connection, now=NOW + 100 + DRAFT_TTL_SECONDS) == 0


async def test_duplicate_origin_and_duplicate_action_are_rejected(store, food):
    async with store.write() as connection:
        await make_draft(connection, food)
        await action(connection, "again")
        with pytest.raises(DraftError, match="already has a draft"):
            await create_draft(connection, proposed(food), action_key="again", now=NOW)
        with pytest.raises(DraftError, match="already has a draft"):
            await create_draft(
                connection, proposed(food, source_message_id=11), action_key="create", now=NOW
            )
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meal_drafts)) == 1


async def test_food_reference_blocks_deletion_and_expiry_releases_it(store, food):
    async with store.write() as connection:
        await make_draft(connection, food)
        async with connection.begin_nested():
            with pytest.raises(IntegrityError):
                await connection.execute(
                    sa.delete(food_versions).where(food_versions.c.id == food.version_id)
                )
        assert await expire_drafts(connection, now=NOW + DRAFT_TTL_SECONDS) == 1
        assert await connection.scalar(sa.select(sa.func.count()).select_from(draft_food_refs)) == 0
        await connection.execute(
            sa.delete(food_versions).where(food_versions.c.id == food.version_id)
        )


async def test_cancel_purges_content_references_and_invalidates_queued_preview(store, food):
    async with store.write() as connection:
        draft = await make_draft(connection, food)
        reply_id = await add_reply(connection, draft, status="queued")
        await action(connection, "cancel")
        closed = await close_draft(
            connection, draft.id, 1, state="cancelled", action_key="cancel", now=NOW + 10
        )
        assert closed.content is None and closed.state == "cancelled"
        assert await connection.scalar(sa.select(sa.func.count()).select_from(draft_food_refs)) == 0
        reply = (
            (await connection.execute(sa.select(outbox).where(outbox.c.id == reply_id)))
            .mappings()
            .one()
        )
        assert reply["status"] == "failed"
        assert reply["payload"] is None
        with pytest.raises(DraftError, match="already saved or cancelled"):
            await touch_draft(connection, draft.id, 1, action_key="cancel", now=NOW + 11)


@pytest.mark.parametrize("status", ["queued", "sending", "failed", "sent"])
async def test_expiry_removes_raw_content_and_keeps_only_safe_sent_references(store, food, status):
    async with store.write() as connection:
        draft = await make_draft(connection, food)
        reply_id = await add_reply(connection, draft, status=status)
        assert await expire_drafts(connection, now=NOW + DRAFT_TTL_SECONDS) == 1
        assert await connection.scalar(sa.select(inbox.c.payload)) is None
        reply = (
            (await connection.execute(sa.select(outbox).where(outbox.c.id == reply_id)))
            .mappings()
            .one()
        )
        if status == "sent":
            assert reply["status"] == "sent"
            assert set(reply["payload"]) == {"text", "draft_id", "draft_revision", "buttons"}
            assert "expired" in reply["payload"]["text"]
            assert "PRIVATE" not in str(reply["payload"])
            assert reply["payload"]["buttons"][0]["callback_data"] == "draft:approve:opaque-create"
            assert reply["button_token"] == "opaque-create"
        else:
            assert reply["status"] == "failed"
            assert reply["payload"] is None


async def test_expiry_scrubs_quoted_copies_without_destroying_pending_message(store, food):
    async with store.write() as connection:
        draft = await make_draft(connection, food)
        await add_reply(connection, draft)
        message = {
            "message_id": 88,
            "chat": {"id": 101},
            "text": "show this",
            "quote": {"text": "PRIVATE"},
            "reply_to_message": {
                "message_id": 77,
                "chat": {"id": 101},
                "date": int(NOW),
                "text": "PRIVATE",
                "from": {"id": 123456, "is_bot": True},
            },
        }
        await connection.execute(
            sa.insert(inbox).values(
                update_id=30,
                payload={"message": message},
                status="pending",
                received_at=NOW + DRAFT_TTL_SECONDS,
            )
        )
        await connection.execute(
            sa.insert(inbox).values(
                update_id=31,
                payload={
                    "callback_query": {
                        "id": "cq",
                        "data": "draft:approve:opaque-create",
                        "message": {
                            "message_id": 77,
                            "chat": {"id": 101},
                            "date": int(NOW),
                            "text": "PRIVATE",
                        },
                    }
                },
                status="pending",
                received_at=NOW + DRAFT_TTL_SECONDS,
            )
        )
        await expire_drafts(connection, now=NOW + DRAFT_TTL_SECONDS)
        pending = await connection.scalar(sa.select(inbox.c.payload).where(inbox.c.update_id == 30))
        assert pending["message"]["text"] == "show this"
        assert pending["message"]["reply_to_message"]["message_id"] == 77
        assert "PRIVATE" not in str(pending)
        pending = await connection.scalar(sa.select(inbox.c.payload).where(inbox.c.update_id == 31))
        assert pending["callback_query"]["data"] == "draft:approve:opaque-create"
        assert "PRIVATE" not in str(pending)


async def test_saved_receipt_is_never_purged_with_temporary_draft(store, food):
    async with store.write() as connection:
        draft = await make_draft(connection, food)
        preview_id = await add_reply(connection, draft)
        await action(connection, "save")
        meal = await create_meal(
            connection,
            items=(MealItemInput(food.version_id, 125000, "125", "g"),),
            label="Synthetic lunch",
            local_date=DAY,
            timezone="UTC",
            consumed_at=NOW,
            action_key="save",
            source_chat_id=101,
            source_message_id=10,
        )
        closed = await close_draft(
            connection,
            draft.id,
            1,
            state="saved",
            action_key="save",
            now=NOW + 10,
            saved_meal_id=meal.id,
        )
        assert closed.content is None and closed.saved_meal_id == meal.id
        receipt_id = await add_reply(
            connection, draft, key="save", status="queued", meal_id=meal.id
        )
        before = await connection.scalar(
            sa.select(outbox.c.payload).where(outbox.c.id == receipt_id)
        )
        assert await expire_drafts(connection, now=NOW + 10 + DRAFT_TTL_SECONDS) == 0
        assert (await get_draft(connection, draft.id)).state == "saved"
        assert (
            await connection.scalar(sa.select(outbox.c.payload).where(outbox.c.id == receipt_id))
            == before
        )
        assert (
            await connection.scalar(sa.select(outbox.c.status).where(outbox.c.id == receipt_id))
            == "queued"
        )
        assert "PRIVATE" not in str(
            await connection.scalar(sa.select(outbox.c.payload).where(outbox.c.id == preview_id))
        )


async def test_late_expired_reference_is_repurged_without_extending_activity(store, food):
    async with store.write() as connection:
        draft = await make_draft(connection, food)
        await expire_drafts(connection, now=NOW + DRAFT_TTL_SECONDS)
        update_id = await action(connection, "late", {"text": "PRIVATE old quote"})
        await track_draft_action(connection, draft.id, "late")
        assert (await get_draft(connection, draft.id)).last_user_activity_at == NOW
        assert await expire_drafts(connection, now=NOW + DRAFT_TTL_SECONDS + 5) == 0
        assert (
            await connection.scalar(
                sa.select(inbox.c.payload).where(inbox.c.update_id == update_id)
            )
            is None
        )


async def test_draft_binding_cannot_be_changed(store, food):
    async with store.write() as connection:
        draft = await make_draft(connection, food)
        await action(connection, "change")
        with pytest.raises(DraftError, match="original message"):
            await revise_draft(
                connection,
                draft.id,
                1,
                proposed(food, source_message_id=11),
                action_key="change",
                now=NOW + 1,
            )
        with pytest.raises(DraftError, match="original message"):
            await revise_draft(
                connection,
                draft.id,
                1,
                proposed(food, target_meal_id=1, target_revision_id=1),
                action_key="change",
                now=NOW + 1,
            )
        assert await get_draft(connection, draft.id) == draft


async def test_unresolved_amount_can_be_persisted_without_an_estimate(store, food):
    async with store.write() as connection:
        draft = await make_draft(
            connection, food, items=(PlannedItem(food_version_id=food.version_id),)
        )
        reloaded = await get_draft(connection, draft.id)
        assert reloaded.content.items[0].edible_milligrams is None
        assert reloaded.content.items[0].estimate_basis is None


@pytest.mark.parametrize(
    "changes",
    [
        {"food_version_id": True},
        {"food_version_id": 0},
        {"food_version_id": 2**63},
        {"edible_milligrams": 1.5},
        {"edible_milligrams": 0},
        {"edible_milligrams": 50_000_001},
        {"original_quantity": "124"},
        {"original_quantity": "NaN"},
        {"original_unit": "ml"},
        {"estimate_basis": ""},
        {"estimate_basis": "x" * 301},
        {"edible_milligrams": None},
        {"original_unit": None},
        {"original_quantity": None},
    ],
)
def test_planned_item_rejects_invalid_or_incomplete_quantity(changes):
    data = dict(
        food_version_id=1, edible_milligrams=125000, original_quantity="125", original_unit="g"
    )
    data.update(changes)
    with pytest.raises(ValidationError):
        PlannedItem(**data)


def test_unresolved_item_cannot_claim_estimate_basis():
    with pytest.raises(ValidationError):
        PlannedItem(food_version_id=1, estimate_basis="Unspecified serving")


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "Unknown/Synthetic"},
        {"local_date": date(2024, 1, 16)},
        {"consumed_at": float("inf")},
        {"consumed_at": True},
        {"label": ""},
        {"source_chat_id": True},
        {"source_message_id": 0},
        {"target_meal_id": 1},
        {"target_revision_id": 1},
        {"items": ()},
    ],
)
def test_draft_content_validates_calendar_identity_and_target_pair(changes):
    data = dict(
        label="Synthetic lunch",
        local_date=DAY,
        timezone="UTC",
        consumed_at=NOW,
        source_chat_id=101,
        source_message_id=10,
        items=(PlannedItem(food_version_id=1),),
    )
    data.update(changes)
    with pytest.raises(ValidationError):
        DraftContent(**data)


async def test_malformed_unrelated_inbox_cannot_block_expiry(store, food):
    async with store.write() as connection:
        draft = await make_draft(connection, food)
        await add_reply(connection, draft)
        malformed = {
            "message": {"chat": {"id": {}}, "reply_to_message": {"message_id": []}},
            "callback_query": {"message": {"chat": {"id": []}, "message_id": {}}},
        }
        await connection.execute(
            sa.insert(inbox).values(
                update_id=30, payload=malformed, status="pending", received_at=NOW
            )
        )
        assert await expire_drafts(connection, now=NOW + DRAFT_TTL_SECONDS) == 1
        assert (
            await connection.scalar(sa.select(inbox.c.payload).where(inbox.c.update_id == 30))
            == malformed
        )


async def test_backwards_clock_does_not_shorten_activity(store, food):
    async with store.write() as connection:
        draft = await make_draft(connection, food)
        await action(connection, "touch")
        touched = await touch_draft(connection, draft.id, 1, action_key="touch", now=NOW - 1)
        assert touched.last_user_activity_at == NOW


async def test_draft_rejects_missing_food_before_it_can_be_used(store, food):
    async with store.write() as connection:
        await action(connection, "create")
        with pytest.raises(DraftError, match="food versions"):
            async with connection.begin_nested():
                await create_draft(
                    connection,
                    proposed(food, items=(PlannedItem(food_version_id=999),)),
                    action_key="create",
                    now=NOW,
                )
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meal_drafts)) == 0


async def test_terminal_draft_rejects_content_in_database(store, food):
    async with store.write() as connection:
        draft = await make_draft(connection, food)
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    sa.update(meal_drafts)
                    .where(meal_drafts.c.id == draft.id)
                    .values(state="expired")
                )
        assert (await get_draft(connection, draft.id)).state == "open"


async def test_expiry_preserves_aiogram_serialized_bot_author_on_pending_quote(store, food):
    from aiogram.types import Update

    from tests.helpers import callback

    async with store.write() as connection:
        draft = await make_draft(connection, food)
        await add_reply(connection, draft)
        payload = callback("opaque-create", update_id=30, message_id=77).model_dump(
            mode="json", exclude_none=True
        )
        payload["callback_query"]["message"]["text"] = "PRIVATE preview"
        assert "from_user" in payload["callback_query"]["message"]
        await connection.execute(
            sa.insert(inbox).values(
                update_id=30, payload=payload, status="pending", received_at=NOW
            )
        )
        await expire_drafts(connection, now=NOW + DRAFT_TTL_SECONDS)
        cleaned = await connection.scalar(sa.select(inbox.c.payload).where(inbox.c.update_id == 30))
        assert "PRIVATE" not in str(cleaned)
        parsed = Update.model_validate(cleaned)
        assert parsed.callback_query.message.from_user.id == 123456
        assert parsed.callback_query.message.from_user.is_bot
