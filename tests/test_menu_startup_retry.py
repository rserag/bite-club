"""Offline startup menu failures use the receiver's durable retry path."""

import asyncio

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import heartbeat, inbox
from nutrition_bot.runtime.worker import receiver
from nutrition_bot.telegram.gateway import RetryableError
from tests.helpers import FakeGateway, message


@pytest.mark.parametrize("failing_menu", ["commands", "miniapp"])
async def test_transient_menu_registration_preserves_receiver_and_eventually_polls(
    service, store, monkeypatch, failing_menu
):
    stop = asyncio.Event()
    calls = []
    delays = []
    service.settings = service.settings.model_copy(
        update={"miniapp_enabled": True, "miniapp_url": "https://example.invalid"}
    )

    class Gateway(FakeGateway):
        failures = 1

        async def preflight(self):
            assert not store.writer_lock.locked()
            calls.append("preflight")

        async def register(self, name):
            assert not store.writer_lock.locked()
            calls.append(name)
            if name == failing_menu and self.failures:
                self.failures -= 1
                raise RetryableError(7)

        async def configure_menu(self, chat_id):
            assert chat_id == 101
            await self.register("commands")

        async def configure_miniapp(self, chat_id, url):
            assert chat_id == 101 and url == "https://example.invalid"
            await self.register("miniapp")

        async def poll(self, offset):
            assert not store.writer_lock.locked()
            calls.append("poll")
            stop.set()
            return [message(1, "/status")]

    async def immediate_retry(service, component, stop, delay, state="healthy"):
        assert component == "receiver" and state == "degraded" and delay >= 7
        await service.pulse(component, state)
        async with store.engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.select(heartbeat.c.state).where(heartbeat.c.component == "receiver")
                )
                == "degraded"
            )
            assert await connection.scalar(sa.select(inbox.c.update_id)) is None
        delays.append(delay)

    monkeypatch.setattr("nutrition_bot.runtime.worker.pause", immediate_retry)
    await receiver(service, Gateway(), stop)
    assert len(delays) == 1
    assert calls.count("preflight") == 2 and calls.count(failing_menu) == 2
    assert calls[-1] == "poll" and calls.count("poll") == 1
    assert await service.offset() == 2
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(inbox.c.status)) == "pending"
        assert (
            await connection.scalar(
                sa.select(heartbeat.c.state).where(heartbeat.c.component == "receiver")
            )
            == "healthy"
        )
