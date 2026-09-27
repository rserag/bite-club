"""Regression checks for draft rejection, retention and bounded Telegram presentation."""

import time

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.schema import inbox, outbox
from nutrition_bot.adapters.database.schema_drafts import draft_action_links
from nutrition_bot.domain.food import PortionInput
from tests.helpers import message
from tests.test_telegram_drafts import age_draft, draft, press
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import current, food_record, ledger_counts, process, reply


@pytest.mark.parametrize("edit", [False, True])
async def test_long_quantity_is_rejected_without_stalling_inbox(service, store, catalog, edit):
    quantity = "about " + "0" * 40 + "150g rice"
    if edit:
        first = await process(service, store, message(1, "about 150g rice"))
        bad = reply(2, "replace: " + quantity, first)
    else:
        bad = message(2, quantity)
    result = await process(service, store, bad)
    assert "unsupported" in result["payload"]["text"]
    assert "0" * 40 not in result["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(inbox.c.status).where(inbox.c.update_id == 2))
            == "done"
        )
    good = await process(service, store, message(3, "100g rice"))
    assert "Saved M1" in good["payload"]["text"]


async def test_rejected_edit_and_listing_keep_retention_links(service, store, catalog):
    first = await process(service, store, message(1, "about 150g rice"))
    failed = await process(service, store, reply(2, "item 99: 150g private food", first))
    assert "item number" in failed["payload"]["text"]
    listed = await process(service, store, message(3, "/drafts"))
    async with store.engine.connect() as connection:
        keys = set(await connection.scalars(sa.select(draft_action_links.c.action_key)))
        assert {"update:1", "update:2", "update:3"} <= keys
    await age_draft(store)
    await service.cleanup()
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(inbox.c.payload).where(inbox.c.update_id == 2))
            is None
        )
        for row_id in (failed["id"], listed["id"]):
            payload = await connection.scalar(
                sa.select(outbox.c.payload).where(outbox.c.id == row_id)
            )
            assert "private food" not in str(payload)
            assert "2023-11-14" not in str(payload)
    assert await ledger_counts(store) == (0, 0)


async def test_new_quote_after_raw_receipt_retention_cannot_reintroduce_expired_content(
    service, store, catalog
):
    first = await process(service, store, message(1, "about 150g rice"))
    await age_draft(store)
    await service.cleanup()
    async with store.write() as connection:
        await connection.execute(
            sa.update(outbox)
            .where(outbox.c.id == first["id"])
            .values(
                created_at=time.time() - 40 * 86400,
                sent_at=time.time() - 40 * 86400,
            )
        )
    await service.cleanup()
    await process(service, store, reply(2, "item 1: 120g", first))
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(inbox.c.payload).where(inbox.c.update_id == 2))
            is None
        )
        assert (
            await connection.scalar(
                sa.select(draft_action_links.c.draft_id).where(
                    draft_action_links.c.action_key == "update:2"
                )
            )
            == 1
        )
    assert await ledger_counts(store) == (0, 0)


@pytest.mark.parametrize("command", ["/today", "/foods rice", "/meal", "/meals"])
async def test_read_commands_in_draft_reply_are_queries_not_edits(service, store, catalog, command):
    first = await process(service, store, message(1, "about 150g rice"))
    result = await process(service, store, reply(2, command, first))
    assert "Nothing was changed" not in result["payload"]["text"]
    assert (await draft(store)).revision == 1
    assert await ledger_counts(store) == (0, 0)


async def test_fractional_gram_reply_resolves_unmeasured_draft(service, store, catalog):
    first = await process(service, store, message(1, "rice"))
    await process(service, store, reply(2, ".5g", first))
    assert (await current(store)).items[0].edible_milligrams == 500
    assert (await current(store)).items[0].quantity_method == "measured"


async def test_date_edit_invalidates_old_approval_and_uses_displayed_day(service, store, catalog):
    first = await process(service, store, message(1, "about 150g rice"))
    changed = await process(service, store, reply(2, "date yesterday", first))
    assert "2023-11-13" in changed["payload"]["text"]
    await process(service, store, press(first, update_id=3))
    assert await ledger_counts(store) == (0, 0)
    await process(service, store, press(changed, update_id=4))
    saved = await current(store)
    assert str(saved.local_date) == "2023-11-13"
    assert saved.items[0].approval_draft_revision == 2


async def test_ten_long_unicode_foods_fit_preview_and_approved_receipt(service, store):
    record = food_record("😀" * 500).model_copy(
        update={
            "portions": (
                PortionInput(
                    label="😀" * 500,
                    grams="150",
                    original_measure="Synthetic portion",
                    source="😀" * 500,
                ),
            )
        }
    )
    async with store.write() as connection:
        food = await publish_reviewed_food(connection, record)
    first = await process(service, store, message(1, "; ".join([f"#{food.version_id}"] * 10)))
    assert first["payload"]["draft_id"] == 1
    assert len(first["payload"]["text"].encode("utf-16-le")) // 2 < 4096
    approved = await process(service, store, press(first))
    assert len(approved["payload"]["text"].encode("utf-16-le")) // 2 < 4096
    assert len((await current(store)).items) == 10
