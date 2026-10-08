"""Temporary proposals with optimistic revision checks and explicit retention.

The caller owns the write transaction and creates the action row first. Expiry
checks never authorize an old draft, including before the periodic cleanup runs.
"""

import math
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import actions, food_versions, inbox, meals, outbox
from nutrition_bot.adapters.database.schema_drafts import (
    draft_action_links,
    draft_food_refs,
    meal_drafts,
)
from nutrition_bot.adapters.database.schema_recipes import draft_recipe_refs
from nutrition_bot.domain.drafts import (
    DRAFT_TTL_SECONDS,
    DraftContent,
    DraftError,
    MealDraft,
)
from nutrition_bot.domain.food import MAX_INTEGER


def _identifier(value: int) -> None:
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise DraftError("Choose a saved draft from /drafts.")


def _timestamp(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise DraftError("The draft clock is unavailable; try again later.")


async def get_draft(connection: AsyncConnection, draft_id: int) -> MealDraft | None:
    _identifier(draft_id)
    row = (
        (await connection.execute(sa.select(meal_drafts).where(meal_drafts.c.id == draft_id)))
        .mappings()
        .first()
    )
    if row is None:
        return None
    return MealDraft(
        id=row["id"],
        revision=row["revision"],
        state=row["state"],
        content=DraftContent.model_validate(row["content"]) if row["content"] is not None else None,
        last_user_activity_at=row["last_user_activity_at"],
        saved_meal_id=row["saved_meal_id"],
    )


async def track_draft_action(connection: AsyncConnection, draft_id: int, action_key: str) -> None:
    """Associate recognized references, including expired or stale button presses."""
    _identifier(draft_id)
    exists = await connection.scalar(
        sa.select(meal_drafts.c.id).where(meal_drafts.c.id == draft_id)
    )
    if exists is None:
        raise DraftError("This draft is unavailable. Start a new meal proposal.")
    # Repeated tracking by the conversation and storage layer is intentional.
    await connection.execute(
        sa.insert(draft_action_links)
        .prefix_with("OR IGNORE")
        .values(draft_id=draft_id, action_key=action_key)
    )
    # New replies referencing a closed draft must also be eligible for cleanup.
    await connection.execute(
        sa.update(meal_drafts).where(meal_drafts.c.id == draft_id).values(retention_purged_at=None)
    )


async def _food_references(
    connection: AsyncConnection, draft_id: int, content: DraftContent
) -> None:
    from nutrition_bot.adapters.database.recipes import validate_recipe_item

    for item in content.items:
        if item.recipe_share is not None:
            await validate_recipe_item(connection, item)
    recipe_ids = {item.recipe_share.version_id for item in content.items if item.recipe_share}
    await connection.execute(
        sa.delete(draft_recipe_refs).where(draft_recipe_refs.c.draft_id == draft_id)
    )
    if recipe_ids:
        await connection.execute(
            sa.insert(draft_recipe_refs),
            [{"draft_id": draft_id, "recipe_version_id": value} for value in sorted(recipe_ids)],
        )
    identifiers = {item.food_version_id for item in content.items}
    found: set[int] = set(
        (
            await connection.scalars(
                sa.select(food_versions.c.id).where(
                    food_versions.c.id.in_(identifiers), food_versions.c.sealed.is_(True)
                )
            )
        ).all()
    )
    if found != identifiers:
        raise DraftError("One of these food versions is unavailable. Choose a reviewed food again.")
    await connection.execute(
        sa.delete(draft_food_refs).where(draft_food_refs.c.draft_id == draft_id)
    )
    await connection.execute(
        sa.insert(draft_food_refs),
        [{"draft_id": draft_id, "food_version_id": value} for value in sorted(identifiers)],
    )


async def create_draft(
    connection: AsyncConnection, content: DraftContent, *, action_key: str, now: float
) -> MealDraft:
    _timestamp(now)
    # Revalidate even if a caller supplied a model_copy/model_construct instance.
    content = DraftContent.model_validate(content.model_dump())
    existing = await connection.scalar(
        sa.select(meal_drafts.c.id).where(
            sa.or_(
                meal_drafts.c.created_action_key == action_key,
                sa.and_(
                    meal_drafts.c.source_chat_id == content.source_chat_id,
                    meal_drafts.c.source_message_id == content.source_message_id,
                ),
            )
        )
    )
    if existing is not None:
        raise DraftError("This message already has a draft. Open it from /drafts.")
    result = await connection.execute(
        sa.insert(meal_drafts).values(
            revision=1,
            state="open",
            content=content.model_dump(mode="json"),
            source_chat_id=content.source_chat_id,
            source_message_id=content.source_message_id,
            created_action_key=action_key,
            created_at=now,
            last_user_activity_at=now,
        )
    )
    assert result.inserted_primary_key is not None
    draft_id = result.inserted_primary_key[0]
    assert isinstance(draft_id, int)
    await _food_references(connection, draft_id, content)
    await track_draft_action(connection, draft_id, action_key)
    draft = await get_draft(connection, draft_id)
    assert draft is not None
    return draft


async def _open_current(
    connection: AsyncConnection, draft_id: int, expected_revision: int, *, now: float
) -> MealDraft:
    _timestamp(now)
    _identifier(expected_revision)
    draft = await get_draft(connection, draft_id)
    if draft is None:
        raise DraftError("This draft is unavailable. Start a new meal proposal.")
    if draft.state == "expired" or now - draft.last_user_activity_at >= DRAFT_TTL_SECONDS:
        raise DraftError(
            "This draft expired after seven days without activity. Start a new proposal."
        )
    if draft.state != "open":
        raise DraftError("This draft is already saved or cancelled. Start a new proposal.")
    if draft.revision != expected_revision:
        raise DraftError(
            "This draft changed. Open its latest revision before approving or editing it."
        )
    return draft


async def revise_draft(
    connection: AsyncConnection,
    draft_id: int,
    expected_revision: int,
    content: DraftContent,
    *,
    action_key: str,
    now: float,
) -> MealDraft:
    draft = await _open_current(connection, draft_id, expected_revision, now=now)
    content = DraftContent.model_validate(content.model_dump())
    assert draft.content is not None
    if (
        content.source_chat_id,
        content.source_message_id,
        content.target_meal_id,
        content.target_revision_id,
    ) != (
        draft.content.source_chat_id,
        draft.content.source_message_id,
        draft.content.target_meal_id,
        draft.content.target_revision_id,
    ):
        raise DraftError("A draft stays attached to its original message and meal revision.")
    if draft.revision >= MAX_INTEGER:
        raise DraftError("Start a new proposal; this draft cannot accept another revision.")
    await _food_references(connection, draft_id, content)
    await connection.execute(
        sa.update(meal_drafts)
        .where(meal_drafts.c.id == draft_id)
        .values(
            revision=draft.revision + 1,
            content=content.model_dump(mode="json"),
            last_user_activity_at=max(now, draft.last_user_activity_at),
        )
    )
    await track_draft_action(connection, draft_id, action_key)
    updated = await get_draft(connection, draft_id)
    assert updated is not None
    return updated


async def touch_draft(
    connection: AsyncConnection,
    draft_id: int,
    expected_revision: int,
    *,
    action_key: str,
    now: float,
) -> MealDraft:
    draft = await _open_current(connection, draft_id, expected_revision, now=now)
    await connection.execute(
        sa.update(meal_drafts)
        .where(meal_drafts.c.id == draft_id)
        .values(last_user_activity_at=max(now, draft.last_user_activity_at))
    )
    await track_draft_action(connection, draft_id, action_key)
    updated = await get_draft(connection, draft_id)
    assert updated is not None
    return updated


async def close_draft(
    connection: AsyncConnection,
    draft_id: int,
    expected_revision: int,
    *,
    state: Literal["saved", "cancelled"],
    action_key: str,
    now: float,
    saved_meal_id: int | None = None,
) -> MealDraft:
    draft = await _open_current(connection, draft_id, expected_revision, now=now)
    if state not in ("saved", "cancelled"):
        raise DraftError("Choose Save or Cancel on the current draft.")
    if state == "saved":
        if saved_meal_id is None:
            raise DraftError("Save the meal before closing this draft.")
        _identifier(saved_meal_id)
        if (
            await connection.scalar(sa.select(meals.c.id).where(meals.c.id == saved_meal_id))
            is None
        ):
            raise DraftError("Save the meal before closing this draft.")
    elif saved_meal_id is not None:
        raise DraftError("A cancelled draft cannot reference a saved meal.")
    await connection.execute(
        sa.update(meal_drafts)
        .where(meal_drafts.c.id == draft_id)
        .values(
            state=state,
            content=None,
            saved_meal_id=saved_meal_id,
            last_user_activity_at=max(now, draft.last_user_activity_at),
        )
    )
    await connection.execute(
        sa.delete(draft_food_refs).where(draft_food_refs.c.draft_id == draft_id)
    )
    await track_draft_action(connection, draft_id, action_key)
    # Invalidate pending previews immediately, but retain sent content until its
    # seven-day deadline. Saved meal receipts are excluded from both paths.
    await connection.execute(
        sa.delete(draft_recipe_refs).where(draft_recipe_refs.c.draft_id == draft_id)
    )
    await _purge_messages(connection, draft_id, draft.revision, state=state, purge_sent=False)
    updated = await get_draft(connection, draft_id)
    assert updated is not None
    return updated


def _safe_payload(draft_id: int, revision: int, state: str, token: str | None) -> dict[str, object]:
    text = "This draft expired after seven days without activity. Start a new proposal."
    if state == "saved":
        text = "This draft was saved. Its temporary preview has been removed."
    elif state == "cancelled":
        text = "This draft was cancelled. Its temporary preview has been removed."
    payload: dict[str, object] = {"text": text, "draft_id": draft_id, "draft_revision": revision}
    if token:
        payload["buttons"] = [
            {"text": "Approve estimate", "callback_data": f"draft:approve:{token}"},
            {"text": "Edit", "callback_data": f"draft:edit:{token}"},
            {"text": "Cancel", "callback_data": f"draft:cancel:{token}"},
        ]
    return payload


async def _purge_messages(
    connection: AsyncConnection, draft_id: int, revision: int, *, state: str, purge_sent: bool
) -> None:
    keys = sa.select(draft_action_links.c.action_key).where(
        draft_action_links.c.draft_id == draft_id
    )
    rows = (
        (await connection.execute(sa.select(outbox).where(outbox.c.action_key.in_(keys))))
        .mappings()
        .all()
    )
    receipt_ids: set[tuple[int, int]] = set()
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, dict) and any(
            type(payload.get(field)) is int for field in ("meal_id", "favorite_id", "recipe_id")
        ):
            # A reply can save a permanent favorite from an approved meal. The
            # quoted draft input still expires, but this new saved receipt does not.
            continue
        if (
            isinstance(payload, dict)
            and type(payload.get("draft_id")) is int
            and payload["draft_id"] != draft_id
        ):
            # Repeating an approved meal can link its old draft and a new draft
            # to one action. The new preview has its own inactivity deadline.
            continue
        if row["kind"] == "message" and isinstance(row["telegram_message_id"], int):
            receipt_ids.add((row["chat_id"], row["telegram_message_id"]))
        if row["status"] == "sent":
            if not purge_sent:
                continue
            safe_revision = revision
            if (
                isinstance(payload, dict)
                and type(payload.get("draft_revision")) is int
                and 1 <= payload["draft_revision"] <= MAX_INTEGER
            ):
                safe_revision = payload["draft_revision"]
            safe = (
                _safe_payload(draft_id, safe_revision, state, row["button_token"])
                if row["kind"] == "message"
                else None
            )
            await connection.execute(
                sa.update(outbox)
                .where(outbox.c.id == row["id"])
                .values(payload=sa.null() if safe is None else safe)
            )
        else:
            await connection.execute(
                sa.update(outbox)
                .where(outbox.c.id == row["id"])
                .values(payload=sa.null(), status="failed", error_type="DraftClosed")
            )
    if not purge_sent:
        return
    update_ids = sa.select(actions.c.update_id).where(actions.c.key.in_(keys))
    await connection.execute(
        sa.update(inbox).where(inbox.c.update_id.in_(update_ids)).values(payload=sa.null())
    )
    # An unhandled Telegram reply can contain a full quoted preview before its
    # action has been linked. Remove only the quoted copy; preserve the user's
    # still-pending message so transport processing can continue safely.
    if receipt_ids:
        candidates = (
            (
                await connection.execute(
                    sa.select(inbox.c.update_id, inbox.c.payload).where(
                        inbox.c.payload.is_not(None)
                    )
                )
            )
            .mappings()
            .all()
        )
        for candidate in candidates:
            payload = candidate["payload"]
            if not isinstance(payload, dict):
                continue
            changed = False
            for field in ("message", "edited_message", "channel_post", "edited_channel_post"):
                message = payload.get(field)
                if not isinstance(message, dict):
                    continue
                quote = message.get("reply_to_message")
                chat = message.get("chat")
                if (
                    isinstance(quote, dict)
                    and isinstance(chat, dict)
                    and type(chat.get("id")) is int
                    and type(quote.get("message_id")) is int
                    and (chat["id"], quote["message_id"]) in receipt_ids
                ):
                    message["reply_to_message"] = {
                        key: value
                        for key, value in quote.items()
                        if key in ("message_id", "chat", "from", "from_user", "date")
                    }
                    message.pop("quote", None)
                    changed = True
            callback = payload.get("callback_query")
            if isinstance(callback, dict):
                message = callback.get("message")
                if (
                    isinstance(message, dict)
                    and isinstance(message.get("chat"), dict)
                    and type(message["chat"].get("id")) is int
                    and type(message.get("message_id")) is int
                    and (message["chat"]["id"], message["message_id"]) in receipt_ids
                ):
                    callback["message"] = {
                        key: value
                        for key, value in message.items()
                        if key in ("message_id", "chat", "from", "from_user", "date")
                    }
                    changed = True
            if changed:
                await connection.execute(
                    sa.update(inbox)
                    .where(inbox.c.update_id == candidate["update_id"])
                    .values(payload=payload)
                )


async def expire_drafts(connection: AsyncConnection, *, now: float) -> int:
    """Purge inactive content and associated raw copies; keep identity tombstones.

    Returns the number of newly expired open drafts. Saved/cancelled previews
    receive the same retention cleanup without changing their terminal state.
    """
    _timestamp(now)
    rows = (
        (
            await connection.execute(
                sa.select(meal_drafts).where(
                    meal_drafts.c.last_user_activity_at <= now - DRAFT_TTL_SECONDS,
                    meal_drafts.c.retention_purged_at.is_(None),
                )
            )
        )
        .mappings()
        .all()
    )
    expired = 0
    for row in rows:
        state = row["state"]
        if state == "open":
            state = "expired"
            expired += 1
        await connection.execute(
            sa.update(meal_drafts)
            .where(meal_drafts.c.id == row["id"])
            .values(state=state, content=None, retention_purged_at=now)
        )
        await connection.execute(
            sa.delete(draft_food_refs).where(draft_food_refs.c.draft_id == row["id"])
        )
        await connection.execute(
            sa.delete(draft_recipe_refs).where(draft_recipe_refs.c.draft_id == row["id"])
        )
        await _purge_messages(connection, row["id"], row["revision"], state=state, purge_sent=True)
    return expired
