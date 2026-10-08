"""Synthetic long receipt delivery, source retention and revision-bound actions."""

import time

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.schema import actions, inbox, outbox
from nutrition_bot.adapters.database.schema_drafts import draft_action_links, meal_drafts
from nutrition_bot.application.meal_conversation import receipt_details
from nutrition_bot.runtime.worker import send_one
from nutrition_bot.telegram.gateway import RejectedError, RetryableError
from tests.helpers import FakeGateway, message
from tests.test_navigation import tap
from tests.test_telegram_drafts import press as approve
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import current, food_record, ledger_counts, press, process, reply


@pytest.fixture
async def long_meal(service, store):
    async with store.write() as connection:
        food = await publish_reviewed_food(
            connection, food_record("Synthetic cooked rice " + "𝓐" * 470)
        )
    saved = await process(
        service,
        store,
        message(1, "/meal " + "; ".join([f"100g #{food.version_id}"] * 10)),
        sent=False,
    )
    gateway = DistinctMessages()
    while await send_one(service, gateway):
        pass
    async with store.engine.connect() as connection:
        return dict(
            (await connection.execute(sa.select(outbox).where(outbox.c.id == saved["id"])))
            .mappings()
            .one()
        )


async def group(store, key):
    async with store.engine.connect() as connection:
        return [
            dict(row)
            for row in (
                await connection.execute(
                    sa.select(outbox)
                    .where(outbox.c.payload["message_group"].as_string() == key)
                    .order_by(outbox.c.id)
                )
            ).mappings()
        ]


class DistinctMessages(FakeGateway):
    async def send_message(self, *args, **kwargs):
        await super().send_message(*args, **kwargs)
        return 5000 + len(self.messages)


async def test_long_details_keep_full_sources_and_bind_every_delivered_part(
    service, store, long_meal
):
    expected = receipt_details(await current(store)).text
    await process(service, store, tap(long_meal, "Details", 2), sent=False)
    parts = await group(store, "callback:nav-2")
    assert len(parts) >= 3
    assert "".join(row["payload"]["text"].split("\n\n", 1)[1] for row in parts) == expected
    assert [row["payload"]["message_part"] for row in parts] == list(range(1, len(parts) + 1))
    for row in parts:
        assert row["payload"]["meal_id"] == long_meal["payload"]["meal_id"]
        assert row["payload"]["meal_revision_id"] == long_meal["payload"]["meal_revision_id"]
        assert len(row["payload"]["text"].encode("utf-16-le")) // 2 <= 4096
    assert all(
        row["button_token"] is None and "buttons" not in row["payload"] for row in parts[:-1]
    )
    assert parts[-1]["button_token"] is not None
    assert parts[-1]["action_key"] == "callback:nav-2"
    assert len({row["created_at"] for row in parts}) == 1
    gateway = DistinctMessages()
    while await send_one(service, gateway):
        pass
    delivered = await group(store, "callback:nav-2")
    assert all(row["status"] == "sent" for row in delivered)
    assert len({row["telegram_message_id"] for row in delivered}) == len(parts)
    edited = await process(service, store, reply(3, "item 1: 120g", delivered[0]))
    assert edited is not None
    assert (await current(store)).items[0].edible_milligrams == 120_000
    stale = await process(service, store, tap(delivered[-1], "Back to meal", 4))
    assert stale is not None
    assert "has changed" in (await group(store, "callback:nav-4"))[0]["payload"]["text"]
    assert await ledger_counts(store) == (1, 2)


@pytest.mark.parametrize("failure", [RetryableError(120), RejectedError()])
async def test_failed_earlier_part_blocks_its_final_actions_without_blocking_other_messages(
    service, store, long_meal, failure
):
    await process(service, store, tap(long_meal, "Details", 2), sent=False)
    gateway = DistinctMessages()
    assert await send_one(service, gateway)  # callback answer
    gateway.failure = failure
    assert await send_one(service, gateway)
    gateway.failure = None
    assert not await send_one(service, gateway)
    parts = await group(store, "callback:nav-2")
    assert parts[0]["status"] == ("failed" if isinstance(failure, RejectedError) else "queued")
    terminal = isinstance(failure, RejectedError)
    assert all(row["status"] == ("failed" if terminal else "queued") for row in parts[1:])
    await process(service, store, message(3, "/home"), sent=False)
    assert await send_one(service, gateway)
    assert "What would you like" in gateway.messages[-1][1]
    assert not await send_one(service, gateway)
    if isinstance(failure, RejectedError):
        assert await service.retry_failed_replies() == len(parts)
    else:
        async with store.write() as connection:
            await connection.execute(
                sa.update(outbox).where(outbox.c.id == parts[0]["id"]).values(next_attempt_at=0)
            )
    while await send_one(service, gateway):
        pass
    assert all(row["status"] == "sent" for row in await group(store, "callback:nav-2"))


async def test_terminal_group_failure_and_retention_cannot_deliver_orphaned_source_parts(
    service, store, long_meal
):
    await process(service, store, tap(long_meal, "Details", 2), sent=False)
    gateway = DistinctMessages()
    assert await send_one(service, gateway)
    gateway.failure = RejectedError()
    assert await send_one(service, gateway)
    parts = await group(store, "callback:nav-2")
    assert all(row["status"] == "failed" for row in parts)
    cutoff = time.time() - (service.settings.raw_input_retention_days + 1) * 86400
    async with store.write() as connection:
        await connection.execute(
            sa.update(outbox)
            .where(outbox.c.id.in_([row["id"] for row in parts]))
            .values(created_at=cutoff)
        )
    await service.cleanup()
    async with store.engine.connect() as connection:
        expired = (
            (
                await connection.execute(
                    sa.select(outbox).where(outbox.c.id.in_([row["id"] for row in parts]))
                )
            )
            .mappings()
            .all()
        )
    assert all(row["payload"] is None and row["button_token"] is None for row in expired)
    gateway.failure = None
    assert not await send_one(service, gateway)
    assert await service.retry_failed_replies() == 0
    assert await ledger_counts(store) == (1, 1)


async def test_group_retry_reserves_every_remaining_part_and_preserves_sent_prefix(
    service, store, long_meal, monkeypatch
):
    await process(service, store, tap(long_meal, "Details", 2), sent=False)
    gateway = DistinctMessages()
    assert await send_one(service, gateway)  # callback answer
    assert await send_one(service, gateway)  # successful first part
    gateway.failure = RejectedError()
    assert await send_one(service, gateway)
    parts = await group(store, "callback:nav-2")
    assert parts[0]["status"] == "sent"
    assert all(row["status"] == "failed" for row in parts[1:])
    monkeypatch.setattr("nutrition_bot.application.service.MAX_PENDING_REPLIES", len(parts) - 2)
    assert await service.retry_failed_replies() == 0
    monkeypatch.setattr("nutrition_bot.application.service.MAX_PENDING_REPLIES", len(parts) - 1)
    assert await service.retry_failed_replies() == len(parts) - 1
    gateway.failure = None
    while await send_one(service, gateway):
        pass
    delivered = await group(store, "callback:nav-2")
    assert all(row["status"] == "sent" for row in delivered)
    assert delivered[0]["telegram_message_id"] == parts[0]["telegram_message_id"]
    assert len(gateway.messages) == len(parts)


async def multipart_draft(service, store, long_meal):
    food_id = (await current(store)).items[0].food_version_id
    return await process(
        service,
        store,
        message(2, "/meal " + "; ".join([f"about 100g #{food_id}"] * 10)),
        sent=False,
    )


async def test_long_draft_approval_is_bound_to_the_last_delivered_part(service, store, long_meal):
    draft = await multipart_draft(service, store, long_meal)
    parts = await group(store, "update:2")
    assert len(parts) >= 3
    assert all(
        row["button_token"] is None and "buttons" not in row["payload"] for row in parts[:-1]
    )
    assert await process(service, store, approve(draft, update_id=3)) is None
    assert await ledger_counts(store) == (1, 1)
    gateway = DistinctMessages()
    while await send_one(service, gateway):
        pass
    delivered = await group(store, "update:2")
    approved = await process(service, store, approve(delivered[-1], update_id=4))
    assert approved is not None
    assert await ledger_counts(store) == (2, 2)
    async with store.engine.connect() as connection:
        links = set(
            (
                await connection.scalars(
                    sa.select(draft_action_links.c.action_key).where(
                        draft_action_links.c.draft_id == draft["payload"]["draft_id"]
                    )
                )
            ).all()
        )
    assert {row["action_key"] for row in parts} <= links


async def test_long_draft_expiry_purges_all_queued_part_provenance(service, store, long_meal):
    draft = await multipart_draft(service, store, long_meal)
    parts = await group(store, "update:2")
    async with store.write() as connection:
        await connection.execute(
            sa.update(meal_drafts)
            .where(meal_drafts.c.id == draft["payload"]["draft_id"])
            .values(last_user_activity_at=1)
        )
    assert await service.claim_reply() is None
    async with store.engine.connect() as connection:
        expired = (
            (
                await connection.execute(
                    sa.select(outbox).where(outbox.c.id.in_([row["id"] for row in parts]))
                )
            )
            .mappings()
            .all()
        )
    assert all(row["status"] == "failed" and row["payload"] is None for row in expired)
    assert await ledger_counts(store) == (1, 1)


async def test_long_details_reserve_capacity_atomically_without_applying_an_action(
    service, store, long_meal, monkeypatch
):
    update = tap(long_meal, "Details", 2)
    async with store.engine.connect() as connection:
        previous_actions = await connection.scalar(sa.select(sa.func.count()).select_from(actions))
    monkeypatch.setattr("nutrition_bot.application.service.MAX_PENDING_REPLIES", 2)
    await service.accept([update])
    assert not await service.process_one()
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(actions))
            == previous_actions
        )
        assert (
            await connection.scalar(sa.select(inbox.c.status).where(inbox.c.update_id == 2))
            == "pending"
        )
    assert not await group(store, "callback:nav-2")
    assert await ledger_counts(store) == (1, 1)
    monkeypatch.setattr("nutrition_bot.application.service.MAX_PENDING_REPLIES", 100)
    assert await service.process_one()
    assert len(await group(store, "callback:nav-2")) >= 3
    await service.accept([update])
    assert not await service.process_one()


async def test_compact_receipt_does_not_authorize_hidden_delete_or_wrong_details_message(
    service, store, catalog
):
    saved = await process(service, store, message(1, "100g rice"))
    assert "Delete" not in [button["text"] for button in saved["payload"]["buttons"]]
    assert await process(service, store, press(saved, "delete", update_id=2)) is None
    assert (
        await process(service, store, press(saved, "details", update_id=3, message_id=999)) is None
    )
    await service.accept([press(saved, "details", update_id=4, user=202)])
    assert not await service.process_one()
    more = await process(service, store, tap(saved, "More", 5))
    deleted = await process(service, store, tap(more, "Delete", 6))
    assert "Restore meal" in [button["text"] for button in deleted["payload"]["buttons"]]
    restored = await process(service, store, tap(deleted, "Restore meal", 7))
    assert "Restored" in restored["payload"]["text"]
    assert await ledger_counts(store) == (1, 3)
