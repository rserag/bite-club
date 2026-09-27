"""Retain ownership of SQLite handles when cancellation interrupts opening."""

import asyncio
from pathlib import Path

import aiosqlite


async def connect(database: Path) -> aiosqlite.Connection:
    # aiosqlite opens SQLite on another thread. Cancelling its opening future can
    # discard the returned raw handle before the proxy takes ownership of it.
    opening = asyncio.ensure_future(aiosqlite.connect(database, check_same_thread=False))
    try:
        return await asyncio.shield(opening)
    except asyncio.CancelledError:

        async def release() -> None:
            try:
                connection = await opening
            except Exception:
                # Opening failed, so there is no handle to close. The caller's
                # cancellation still takes precedence over that opening error.
                return
            await connection.close()

        cleanup = asyncio.create_task(release())
        # A second shutdown/cancellation must not abandon the cleanup either.
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()
        raise
