import asyncio
import logging
import random
import time
from collections.abc import Coroutine
from typing import Any

from nutrition_bot.application.service import QueueFullError, Service
from nutrition_bot.runtime.health import COMPONENTS
from nutrition_bot.telegram.gateway import Gateway, RejectedError, RetryableError

logger = logging.getLogger("nutrition_bot.worker")


def backoff(attempt: int) -> float:
    return min(60.0, 2.0 ** min(attempt, 6)) + random.uniform(0, 1)


async def pause(
    service: Service, name: str, stop: asyncio.Event, delay: float, state: str = "healthy"
) -> None:
    deadline = time.monotonic() + delay
    while not stop.is_set() and (remaining := deadline - time.monotonic()) > 0:
        await service.pulse(name, state)
        try:
            await asyncio.wait_for(stop.wait(), timeout=min(remaining, 10))
        except TimeoutError:
            pass


async def receiver(service: Service, gateway: Gateway, stop: asyncio.Event) -> None:
    attempts, checked = 0, False
    while not stop.is_set():
        await service.pulse("receiver", "degraded" if attempts else "healthy")
        try:
            if not checked:
                await gateway.preflight()
                await gateway.configure_menu(service.settings.allowed_telegram_chat_id)
                if service.settings.miniapp_enabled:
                    assert service.settings.miniapp_url is not None
                    await gateway.configure_miniapp(
                        service.settings.allowed_telegram_chat_id, service.settings.miniapp_url
                    )
                checked = True
            updates = await gateway.poll(await service.offset())
        except RetryableError as exc:
            attempts += 1
            logger.warning("telegram_poll_retry", extra={"error_type": type(exc).__name__})
            await pause(service, "receiver", stop, max(exc.delay, backoff(attempts)), "degraded")
            continue
        # Persistence errors escape and stop the process; never acknowledge uncommitted intake.
        try:
            await service.accept(updates)
        except QueueFullError:
            await pause(service, "receiver", stop, 5, "degraded")
            continue
        await service.pulse("receiver", success=True)
        attempts = 0
        if not updates:
            await pause(service, "receiver", stop, 0.2)


async def processor(service: Service, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await service.pulse("processor")
        if not await service.process_one():
            await pause(service, "processor", stop, 0.25)


async def food_lookup(service: Service, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await service.pulse("food_lookup")
        if not await service.process_food_lookup():
            await pause(service, "food_lookup", stop, 0.25)


async def send_one(service: Service, gateway: Gateway) -> bool:
    row = await service.claim_reply()
    if row is None:
        return False
    row = await service.prepare_reply(row)
    if row is None:
        return True
    if (
        row["chat_id"] != service.settings.allowed_telegram_chat_id
        or row["owner_user_id"] != service.settings.allowed_telegram_user_id
    ):
        await service.finish_reply(row["id"], "failed", error_type="AuthorizationChanged")
        return True
    try:
        payload = row["payload"]
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise RejectedError()
        message_id = None
        if row["kind"] == "message":
            buttons = payload.get("buttons")
            if buttons is None:
                message_id = await gateway.send_message(
                    row["chat_id"], payload["text"], row["button_token"]
                )
            else:
                if (
                    not isinstance(buttons, list)
                    or not 1 <= len(buttons) <= 12
                    or any(
                        not isinstance(item, dict)
                        or not isinstance(item.get("text"), str)
                        or not isinstance(item.get("callback_data"), str)
                        or not 1 <= len(item["callback_data"].encode()) <= 64
                        for item in buttons
                    )
                ):
                    raise RejectedError()
                message_id = await gateway.send_message(
                    row["chat_id"], payload["text"], row["button_token"], buttons
                )
        else:
            if not isinstance(payload.get("callback_id"), str):
                raise RejectedError()
            await gateway.answer_callback(payload["callback_id"], payload["text"])
    except RetryableError as exc:
        attempts = row["attempts"]
        await service.finish_reply(
            row["id"],
            "failed" if attempts >= 8 else "queued",
            delay=max(exc.delay, backoff(attempts)),
            error_type=type(exc).__name__,
        )
        await service.pulse("sender", "degraded")
        logger.warning("telegram_send_retry", extra={"error_type": type(exc).__name__})
    except RejectedError as exc:
        await service.finish_reply(row["id"], "failed", error_type=type(exc).__name__)
        logger.warning("telegram_send_rejected", extra={"error_type": type(exc).__name__})
    else:
        # Sending precedes this commit: process death here can repeat the reply,
        # but cannot repeat the application action that generated it.
        await service.finish_reply(row["id"], "sent", message_id=message_id)
        await service.pulse("sender", success=True)
    return True


async def sender(service: Service, gateway: Gateway, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await service.pulse("sender")
        if not await send_one(service, gateway):
            await pause(service, "sender", stop, 0.5)


async def scheduler(service: Service, stop: asyncio.Event) -> None:
    from nutrition_bot.runtime.scheduler import scheduler_tick

    while not stop.is_set():
        await service.pulse("scheduler")
        await scheduler_tick(service.store, service.settings, now=time.time())
        await pause(service, "scheduler", stop, 15)


async def cleanup(service: Service, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await service.pulse("cleanup")
        await service.cleanup()
        await pause(service, "cleanup", stop, 60)


async def drain_loops(service: Service, tasks: list[asyncio.Task[None]]) -> None:
    async def drain() -> None:
        # Let local SQL finish before cancellation. A cancelled DBAPI cursor can
        # retain SQLite's writer even after its connection has been invalidated.
        # External calls run outside this lock and remain immediately cancellable.
        async with service.store.writer_lock:
            for task in tasks:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    draining = asyncio.create_task(drain())
    cancelled = False
    while not draining.done():
        try:
            await asyncio.shield(draining)
        except asyncio.CancelledError:
            # Repeated stop signals must not abandon the barrier or child cleanup.
            cancelled = True
    draining.result()
    if cancelled:
        raise asyncio.CancelledError


async def run_worker(service: Service, gateway: Gateway, stop: asyncio.Event) -> None:
    service.gateway = gateway
    await service.store.check_schema()
    await service.recover_outbox()
    await service.recover_food_lookups()
    for component in COMPONENTS:
        await service.pulse(component)
    logger.info("worker_started")
    failures: list[Exception] = []

    async def supervise(work: Coroutine[Any, Any, None]) -> None:
        try:
            await work
        except Exception as exc:
            # TaskGroup's immediate cancellation could interrupt another writer.
            # Keep the fatal failure, then use the same safe stop barrier.
            failures.append(exc)
            stop.set()

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(supervise(receiver(service, gateway, stop))),
                group.create_task(supervise(processor(service, stop))),
                group.create_task(supervise(food_lookup(service, stop))),
                group.create_task(supervise(sender(service, gateway, stop))),
                group.create_task(supervise(cleanup(service, stop))),
                group.create_task(supervise(scheduler(service, stop))),
            ]
            try:
                await stop.wait()
            finally:
                await drain_loops(service, tasks)
        if failures:
            raise ExceptionGroup("Worker loop failed", failures)
    finally:
        for component in COMPONENTS:
            await service.pulse(component, "stopped")
        logger.info("worker_stopped")
