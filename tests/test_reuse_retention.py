"""Temporary draft expiry must not invalidate permanent favorite controls."""

import time

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.favorites import get_favorite
from nutrition_bot.adapters.database.schema import inbox, outbox
from nutrition_bot.adapters.database.schema_drafts import meal_drafts
from nutrition_bot.domain.drafts import DRAFT_TTL_SECONDS
from tests.helpers import message
from tests.test_telegram_drafts import age_draft, draft
from tests.test_telegram_drafts import press as approve_draft
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import process, reply
from tests.test_telegram_reuse import press as favorite_button


@pytest.mark.parametrize("sent", [True, False])
async def test_saved_favorite_controls_survive_source_draft_expiry_with_raw_input_purged(
    service, store, catalog, sent
):
    proposal = await process(service, store, message(1, "about 150g rice"))
    meal = await process(service, store, approve_draft(proposal, update_id=2))
    saved = await process(service, store, reply(3, "save as usual breakfast", meal), sent=sent)
    before = saved["payload"]
    assert before["favorite_id"] == 1
    assert before["buttons"][0]["callback_data"].startswith("favorite:")
    async with store.engine.connect() as connection:
        raw = await connection.scalar(sa.select(inbox.c.payload).where(inbox.c.update_id == 3))
        assert "reply_to_message" in raw["message"]
        assert "rice" in raw["message"]["reply_to_message"]["text"]
    await age_draft(store)
    await service.cleanup()
    async with store.engine.connect() as connection:
        retained = (
            (await connection.execute(sa.select(outbox).where(outbox.c.id == saved["id"])))
            .mappings()
            .one()
        )
        assert retained["payload"] == before
        assert retained["status"] == ("sent" if sent else "queued")
        assert retained["button_token"] == saved["button_token"]
        assert (
            await connection.scalar(sa.select(inbox.c.payload).where(inbox.c.update_id == 3))
            is None
        )
        favorite = await get_favorite(connection, 1)
        assert favorite is not None and favorite.items[0].estimate_basis is not None
    if not sent:
        saved["telegram_message_id"] = 1000 + saved["id"]
        await service.finish_reply(saved["id"], "sent", message_id=saved["telegram_message_id"])
    consumed = await process(service, store, favorite_button(saved, update_id=4))
    assert "fresh approval required" in consumed["payload"]["text"]
    assert consumed["payload"]["draft_id"] == 2
    assert (await draft(store, 2)).state == "open"


@pytest.mark.parametrize("sent", [True, False])
async def test_repeated_meal_draft_keeps_its_own_deadline_and_current_buttons(
    service, store, catalog, sent
):
    initial = await process(service, store, message(1, "about 150g rice"))
    meal = await process(service, store, approve_draft(initial, update_id=2))
    repeated = await process(service, store, reply(3, "same again", meal), sent=sent)
    assert repeated["payload"]["draft_id"] == 2
    async with store.write() as connection:
        await connection.execute(
            sa.update(meal_drafts)
            .where(meal_drafts.c.id == 1)
            .values(last_user_activity_at=time.time() - DRAFT_TTL_SECONDS - 1)
        )
    await service.cleanup()
    async with store.engine.connect() as connection:
        retained = (
            (await connection.execute(sa.select(outbox).where(outbox.c.id == repeated["id"])))
            .mappings()
            .one()
        )
        assert retained["payload"] == repeated["payload"]
        assert retained["status"] == ("sent" if sent else "queued")
        assert (
            await connection.scalar(sa.select(inbox.c.payload).where(inbox.c.update_id == 3))
            is None
        )
    assert (await draft(store, 2)).state == "open"
    if not sent:
        repeated["telegram_message_id"] = 1000 + repeated["id"]
        await service.finish_reply(
            repeated["id"], "sent", message_id=repeated["telegram_message_id"]
        )
    saved = await process(service, store, approve_draft(repeated, update_id=4))
    assert saved["payload"]["meal_id"] == 2
    assert (await draft(store, 2)).state == "saved"


async def test_repeated_meal_draft_still_expires_at_its_own_deadline(service, store, catalog):
    initial = await process(service, store, message(1, "about 150g rice"))
    meal = await process(service, store, approve_draft(initial, update_id=2))
    repeated = await process(service, store, reply(3, "same again", meal))
    await age_draft(store)
    await service.cleanup()
    assert (await draft(store, 2)).state == "expired"
    async with store.engine.connect() as connection:
        payload = await connection.scalar(
            sa.select(outbox.c.payload).where(outbox.c.id == repeated["id"])
        )
        assert payload["draft_id"] == 2
        assert "expired" in payload["text"]
        assert "rice" not in payload["text"]
