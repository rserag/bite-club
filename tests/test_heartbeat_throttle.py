import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import heartbeat
from nutrition_bot.application import service as service_module
from nutrition_bot.application.service import Service


@dataclass
class Clock:
    wall: float = 1000
    elapsed: float = 0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.wall += seconds
        self.elapsed += seconds


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(service_module, "time", value)
    return value


@pytest.fixture
def commits(store):
    calls = []

    def committed(connection):
        calls.append(None)

    sa.event.listen(store.engine.sync_engine, "commit", committed)
    yield calls
    sa.event.remove(store.engine.sync_engine, "commit", committed)


async def row(store, component):
    async with store.engine.connect() as connection:
        return (
            (
                await connection.execute(
                    sa.select(heartbeat).where(heartbeat.c.component == component)
                )
            )
            .mappings()
            .one()
        )


async def test_idle_heartbeats_commit_once_per_component_per_interval(
    service, store, clock, commits
):
    for component in ("processor", "sender"):
        await service.pulse(component, "healthy")
    for _ in range(100):
        await service.pulse("processor")
        await service.pulse("sender")
    clock.advance(4.9)
    await service.pulse("processor")
    assert len(commits) == 2
    assert (await row(store, "processor"))["touched_at"] == 1000

    clock.advance(0.2)
    for component in ("processor", "sender"):
        await service.pulse(component)
    assert len(commits) == 4
    assert (await row(store, "processor"))["touched_at"] == pytest.approx(1005.1)


async def test_degradation_recovery_and_stop_are_immediate(service, store, clock, commits):
    await service.pulse("receiver", "healthy")
    clock.advance(0.1)
    await service.pulse("receiver", "degraded")
    await service.pulse("receiver")
    assert len(commits) == 2
    assert (await row(store, "receiver"))["state"] == "degraded"

    await service.pulse("receiver", success=True)
    assert len(commits) == 3
    assert (await row(store, "receiver"))["state"] == "healthy"
    await service.pulse("receiver", "stopped")
    assert len(commits) == 4
    assert (await row(store, "receiver"))["state"] == "stopped"


async def test_throttled_success_retains_actual_event_time(service, store, clock, commits):
    await service.pulse("sender", "healthy")
    clock.advance(1)
    await service.pulse("sender", success=True)
    assert len(commits) == 2
    assert (await row(store, "sender"))["last_success_at"] == 1001
    clock.advance(1)
    await service.pulse("sender", success=True)
    assert len(commits) == 2
    assert (await row(store, "sender"))["last_success_at"] == 1001

    clock.advance(4)
    await service.pulse("sender")
    assert len(commits) == 3
    assert (await row(store, "sender"))["touched_at"] == 1006
    assert (await row(store, "sender"))["last_success_at"] == 1002


async def test_failed_write_does_not_suppress_transition_retry(
    service, store, clock, commits, monkeypatch
):
    await service.pulse("receiver", "healthy")
    original = store.write
    fail = True

    @asynccontextmanager
    async def unreliable_write():
        nonlocal fail
        if fail:
            fail = False
            raise RuntimeError("synthetic database failure")
        async with original() as connection:
            yield connection

    monkeypatch.setattr(store, "write", unreliable_write)
    with pytest.raises(RuntimeError, match="synthetic database failure"):
        await service.pulse("receiver", "degraded")
    await service.pulse("receiver", "degraded")
    assert len(commits) == 2
    assert (await row(store, "receiver"))["state"] == "degraded"


async def test_new_service_preserves_existing_state_until_explicit_update(
    service, store, settings, clock, commits
):
    await service.pulse("receiver", "degraded")
    restarted = Service(store, settings)
    await restarted.pulse("receiver")
    assert len(commits) == 2
    assert (await row(store, "receiver"))["state"] == "degraded"
    await restarted.pulse("receiver", "healthy")
    assert len(commits) == 3
    assert (await row(store, "receiver"))["state"] == "healthy"


async def test_concurrent_heartbeats_share_throttle(service, clock, commits):
    await asyncio.gather(*(service.pulse("processor") for _ in range(100)))
    assert len(commits) == 1


async def test_wall_clock_change_does_not_delay_next_heartbeat(service, store, clock, commits):
    await service.pulse("processor")
    clock.wall -= 100
    clock.advance(5)
    await service.pulse("processor")
    assert len(commits) == 2
    assert (await row(store, "processor"))["touched_at"] == 905
