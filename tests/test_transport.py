import asyncio
import time

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import actions, cursor, inbox, outbox, profile
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.application.service import Service
from nutrition_bot.runtime.worker import send_one
from nutrition_bot.telegram.gateway import RejectedError, RetryableError
from tests.helpers import FakeGateway, callback, message


async def rows(store, table):
    async with store.engine.connect() as connection:
        return (await connection.execute(sa.select(table))).mappings().all()


async def test_replayed_update_survives_new_store_and_is_applied_once(service, store, settings):
    update = message()
    await service.accept([update])
    assert await service.offset() == 2
    # Simulate a new process after the update was committed but before processing.
    restarted = Store(settings)
    try:
        other = Service(restarted, settings)
        await other.accept([update])
        assert await other.process_one()
        await other.accept([update])
        assert not await other.process_one()
        assert len(await rows(restarted, profile)) == 1
        assert len(await rows(restarted, actions)) == 1
        assert len(await rows(restarted, outbox)) == 1
    finally:
        await restarted.close()


@pytest.mark.parametrize(
    "args",
    [
        {"user": 202},
        {"chat": 202},
        {"kind": "group"},
        {"kind": "supergroup"},
    ],
)
async def test_unauthorized_cursor_advances_without_content_or_action(service, store, args):
    await service.accept([message(text="private content must not persist", **args)])
    assert await service.offset() == 2
    assert not await rows(store, inbox)
    assert not await service.process_one()
    assert not await rows(store, actions)
    assert not await rows(store, outbox)


async def test_unauthorized_photo_and_callback_never_persist(service, store):
    photo = message(user=202).model_dump(mode="json")
    photo["message"].pop("text")
    photo["message"]["photo"] = [
        {
            "file_id": "private-photo",
            "file_unique_id": "private",
            "width": 10,
            "height": 10,
        }
    ]
    from aiogram.types import Update

    await service.accept([Update.model_validate(photo), callback("forged", user=202)])
    assert not await rows(store, inbox)
    assert not await rows(store, actions)


async def test_authorization_rechecked_after_configuration_change(service, store, settings):
    await service.accept([message()])
    changed = settings.model_copy(update={"allowed_telegram_user_id": 202})
    assert await Service(store, changed).process_one()
    assert (await rows(store, inbox))[0]["status"] == "rejected"
    assert (await rows(store, inbox))[0]["payload"] is None
    assert not await rows(store, actions)


async def test_transaction_rollback_leaves_no_profile_action_or_reply(service, store, monkeypatch):
    await service.accept([message()])
    original = service._reply

    async def fail_after_reply(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("synthetic crash")

    monkeypatch.setattr(service, "_reply", fail_after_reply)
    with pytest.raises(RuntimeError):
        await service.process_one()
    assert not await rows(store, profile)
    assert not await rows(store, actions)
    assert not await rows(store, outbox)
    assert (await rows(store, inbox))[0]["status"] == "pending"
    monkeypatch.setattr(service, "_reply", original)
    assert await service.process_one()
    assert len(await rows(store, actions)) == 1


async def test_intake_failure_does_not_advance_cursor(service, store):
    # Transaction interruption at the DB boundary, before higher-offset polling is possible.
    with pytest.raises(RuntimeError):
        async with store.write() as connection:
            await connection.execute(sa.insert(cursor).values(id=1, next_offset=99))
            raise RuntimeError("synthetic disk failure")
    assert await service.offset() == 0
    assert not await rows(store, inbox)


async def test_concurrent_duplicate_intake_and_processing(service, store):
    await asyncio.gather(*(service.accept([message()]) for _ in range(4)))
    await asyncio.gather(*(service.process_one() for _ in range(4)))
    assert len(await rows(store, inbox)) == len(await rows(store, actions)) == 1


async def test_callback_must_match_issued_token_and_message(service, store):
    await service.accept([message()])
    await service.process_one()
    gateway = FakeGateway()
    await send_one(service, gateway)
    token = gateway.messages[0][2]
    await service.accept([callback(token, message_id=999)])
    await service.process_one()
    assert len(await rows(store, actions)) == 1
    await service.accept([callback(token, update_id=3)])
    await service.process_one()
    # Same callback ID repeated with a different update ID must still deduplicate.
    await service.accept([callback(token, update_id=4)])
    await service.process_one()
    assert len(await rows(store, actions)) == 2
    await send_one(service, gateway)
    assert len(gateway.answers) == 1


async def test_status_reports_and_unsupported_food_is_not_logged(service, store):
    await service.accept([message(1, "/status"), message(2, "200 g cooked beans")])
    await service.process_one()
    await service.process_one()
    replies = await rows(store, outbox)
    status = replies[0]["payload"]["text"]
    assert "BJJ plans, recovery check-ins and /load workload guidance are available" in status
    assert "Reviewed training allocation uses /allocation" in status
    assert "AI is disabled" in status
    assert "nothing was logged" in replies[1]["payload"]["text"]
    assert not await rows(store, profile)


async def test_whitespace_input_does_not_poison_the_worker(service, store):
    await service.accept([message(1, "   "), message(2, "/status")])
    assert await service.process_one()
    assert await service.process_one()
    assert len(await rows(store, actions)) == 2


async def test_edit_cannot_reexecute_start(service, store):
    await service.accept([message(1, "/start", edited=True)])
    await service.process_one()
    assert not await rows(store, profile)


async def test_poison_payload_fails_without_blocking_next_item(service, store):
    await service.accept([message(1), message(2)])
    async with store.write() as connection:
        await connection.execute(sa.update(inbox).where(inbox.c.update_id == 1).values(payload={}))
    await service.process_one()
    await service.process_one()
    states = {r["update_id"]: r["status"] for r in await rows(store, inbox)}
    assert states == {1: "failed", 2: "done"}


async def test_interrupted_send_recovers_without_repeating_action(service, store):
    await service.accept([message()])
    await service.process_one()
    claimed = await service.claim_reply()
    assert claimed is not None
    # Crash may have happened before or after Telegram received the reply.
    await service.recover_outbox()
    gateway = FakeGateway()
    await send_one(service, gateway)
    assert len(gateway.messages) == 1
    assert len(await rows(store, actions)) == 1
    assert (await rows(store, outbox))[0]["status"] == "sent"


async def test_ambiguous_send_can_repeat_reply_but_never_action(service, store, monkeypatch):
    await service.accept([message()])
    await service.process_one()
    gateway = FakeGateway()
    original = service.finish_reply

    async def crash(*args, **kwargs):
        raise RuntimeError("after external send")

    monkeypatch.setattr(service, "finish_reply", crash)
    with pytest.raises(RuntimeError):
        await send_one(service, gateway)
    monkeypatch.setattr(service, "finish_reply", original)
    await service.recover_outbox()
    await send_one(service, gateway)
    assert len(gateway.messages) == 2
    assert len(await rows(store, actions)) == 1


async def test_retry_after_persists_schedule(service, store):
    await service.accept([message()])
    await service.process_one()
    gateway = FakeGateway()
    gateway.failure = RetryableError(123)
    await send_one(service, gateway)
    row = (await rows(store, outbox))[0]
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] >= time.time() + 120
    assert not await send_one(service, gateway)


async def test_permanent_reply_failure_does_not_lose_action(service, store):
    await service.accept([message()])
    await service.process_one()
    gateway = FakeGateway()
    gateway.failure = RejectedError()
    await send_one(service, gateway)
    assert (await rows(store, outbox))[0]["status"] == "failed"
    assert len(await rows(store, actions)) == 1


async def test_exhausted_replies_can_be_explicitly_requeued(service, store):
    await service.accept([message()])
    await service.process_one()
    gateway = FakeGateway()
    gateway.failure = RetryableError()
    async with store.write() as connection:
        await connection.execute(sa.update(outbox).values(attempts=7))
    await send_one(service, gateway)
    assert (await rows(store, outbox))[0]["status"] == "failed"
    assert await service.retry_failed_replies() == 1
    gateway.failure = None
    await send_one(service, gateway)
    assert (await rows(store, outbox))[0]["status"] == "sent"
    assert len(await rows(store, actions)) == 1


async def test_expired_failed_reply_cannot_be_requeued(service, store):
    await service.accept([message()])
    await service.process_one()
    async with store.write() as connection:
        await connection.execute(
            sa.update(outbox).values(status="failed", created_at=time.time() - 31 * 86400)
        )
    assert await service.retry_failed_replies() == 0


async def test_outbox_rechecks_both_owner_and_chat(service, store, settings):
    await service.accept([message()])
    await service.process_one()
    changed = settings.model_copy(update={"allowed_telegram_user_id": 202})
    other = Service(store, changed)
    gateway = FakeGateway()
    await send_one(other, gateway)
    assert not gateway.messages
    assert (await rows(store, outbox))[0]["error_type"] == "AuthorizationChanged"
    assert await other.retry_failed_replies() == 0


async def test_retention_scrubs_payloads_and_invalidates_buttons(service, store):
    await service.accept([message()])
    await service.process_one()
    gateway = FakeGateway()
    await send_one(service, gateway)
    old = time.time() - 31 * 86400
    async with store.write() as connection:
        await connection.execute(sa.update(inbox).values(processed_at=old))
        await connection.execute(sa.update(outbox).values(created_at=old, sent_at=old))
    await service.cleanup()
    assert (await rows(store, inbox))[0]["payload"] is None
    assert (await rows(store, outbox))[0]["payload"] is None
    assert (await rows(store, outbox))[0]["button_token"] is None
    await service.accept([callback(gateway.messages[0][2])])
    await service.process_one()
    assert len(await rows(store, actions)) == 1
