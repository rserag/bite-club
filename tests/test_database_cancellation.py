import asyncio
import sqlite3
import threading

import aiosqlite
import pytest
from sqlalchemy.ext.asyncio import AsyncConnection


@pytest.mark.parametrize("cancel_count", [1, 3])
@pytest.mark.parametrize("opening_error", [False, True])
async def test_cancel_during_connection_open_closes_sqlite_handle(
    store, monkeypatch, cancel_count, opening_error
):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    opened = []
    original = sqlite3.connect

    def delayed_connect(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release connection creation"
        if opening_error:
            finished.set()
            raise sqlite3.OperationalError("synthetic open failure")
        connection = original(*args, **kwargs)
        opened.append(connection)
        finished.set()
        return connection

    monkeypatch.setattr(sqlite3, "connect", delayed_connect)

    async def query():
        async with store.engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")

    task = asyncio.create_task(query())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(cancel_count):
            task.cancel()
            # Deliver cancellation while the underlying thread is still opening.
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(finished.wait, 5)
        if not opening_error:
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                opened[0].execute("SELECT 1")
        monkeypatch.setattr(sqlite3, "connect", original)
        # Cancelled acquisition must not poison the pool or prevent future work.
        await query()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(finished.wait, 5)
        for connection in opened:
            connection.close()


async def test_repeated_cancellation_waits_for_handle_cleanup(store, monkeypatch):
    entered = threading.Event()
    release_open = threading.Event()
    closing = asyncio.Event()
    release_close = asyncio.Event()
    opened = []
    original_connect = sqlite3.connect
    original_close = aiosqlite.Connection.close

    def delayed_connect(*args, **kwargs):
        entered.set()
        assert release_open.wait(5)
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    async def delayed_close(connection):
        closing.set()
        await release_close.wait()
        await original_close(connection)

    monkeypatch.setattr(sqlite3, "connect", delayed_connect)
    monkeypatch.setattr(aiosqlite.Connection, "close", delayed_close)

    async def query():
        async with store.engine.connect():
            pytest.fail("cancelled acquisition should not reach the caller")

    task = asyncio.create_task(query())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release_open.set()
        await asyncio.wait_for(closing.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            opened[0].execute("SELECT 1")
    finally:
        release_open.set()
        release_close.set()
        await asyncio.gather(task, return_exceptions=True)
        for connection in opened:
            connection.close()


async def test_cancelled_write_is_rolled_back_and_next_write_succeeds(store):
    async with store.write() as connection:
        await connection.exec_driver_sql("CREATE TABLE cancellation_probe (value INTEGER)")
    inserted = asyncio.Event()

    async def write():
        async with store.write() as connection:
            await connection.exec_driver_sql("INSERT INTO cancellation_probe VALUES (1)")
            inserted.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(write())
    await asyncio.wait_for(inserted.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with store.write() as connection:
        result = await connection.exec_driver_sql("SELECT count(*) FROM cancellation_probe")
        assert result.scalar_one() == 0
        await connection.exec_driver_sql("INSERT INTO cancellation_probe VALUES (2)")
    async with store.engine.connect() as connection:
        result = await connection.exec_driver_sql("SELECT value FROM cancellation_probe")
        assert result.scalars().all() == [2]


@pytest.mark.parametrize("stage", ["connection", "begin"])
@pytest.mark.parametrize("cancel_count", [1, 3])
async def test_cancelled_write_startup_retains_lock_until_cleanup(
    store, monkeypatch, stage, cancel_count
):
    entered = asyncio.Event()
    release = asyncio.Event()
    blocked = False
    original_start = AsyncConnection.start
    original_execute = AsyncConnection.exec_driver_sql

    async def block_once():
        nonlocal blocked
        if not blocked:
            blocked = True
            entered.set()
            await release.wait()

    async def delayed_start(connection, *args, **kwargs):
        result = await original_start(connection, *args, **kwargs)
        if stage == "connection":
            await block_once()
        return result

    async def delayed_execute(connection, statement, *args, **kwargs):
        result = await original_execute(connection, statement, *args, **kwargs)
        if stage == "begin" and statement == "BEGIN IMMEDIATE":
            await block_once()
        return result

    monkeypatch.setattr(AsyncConnection, "start", delayed_start)
    monkeypatch.setattr(AsyncConnection, "exec_driver_sql", delayed_execute)

    async def write():
        async with store.write():
            pytest.fail("Cancelled startup must not reach the caller")

    task = asyncio.create_task(write())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        # Allow cancellation cleanup to run; startup is deliberately still held.
        await asyncio.sleep(0.05)
        assert not task.done()
        assert store.writer_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not store.writer_lock.locked()
        async with store.write() as connection:
            assert (await connection.exec_driver_sql("SELECT 1")).scalar_one() == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
