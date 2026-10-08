import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from nutrition_bot.adapters.database.connection import connect
from nutrition_bot.adapters.database.schema import SCHEMA_REVISION
from nutrition_bot.config import StorageSettings


class Store:
    def __init__(self, settings: StorageSettings):
        self.engine = create_async_engine(
            settings.resolved_database_url,
            async_creator=partial(connect, settings.database_path),
        )
        self.writer_lock = asyncio.Lock()
        event.listen(self.engine.sync_engine, "connect", self._configure)

    @staticmethod
    def _configure(connection: Any, _: Any) -> None:
        # SQLite LOWER only handles ASCII; use the same Unicode case folding as
        # catalog queries, including names copied from non-English food labels.
        connection.create_function(
            "unicode_casefold",
            1,
            lambda value: value.casefold() if isinstance(value, str) else "",
            deterministic=True,
        )
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.close()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncConnection]:
        async with self.writer_lock:
            # Acquisition includes asynchronous connection hooks and a queued
            # SQLite BEGIN. Cancellation must not abandon either stage before
            # the database handle and its write lock have been released.
            async def begin() -> AsyncConnection:
                connection = await self.engine.connect()
                try:
                    await connection.exec_driver_sql("BEGIN IMMEDIATE")
                except BaseException:
                    await connection.close()
                    raise
                return connection

            starting = asyncio.create_task(begin())
            try:
                connection = await asyncio.shield(starting)
            except asyncio.CancelledError:

                async def discard() -> None:
                    try:
                        connection = await starting
                    except Exception:
                        return
                    try:
                        await connection.rollback()
                    finally:
                        await connection.close()

                cleanup = asyncio.create_task(discard())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
                raise

            try:
                yield connection
                await connection.commit()
            finally:
                # close rolls back an uncommitted transaction. Join its cleanup
                # before releasing the writer lock, even under repeated stops.
                closing = asyncio.create_task(connection.close())
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    while not closing.done():
                        try:
                            await asyncio.shield(closing)
                        except asyncio.CancelledError:
                            continue
                    closing.result()
                    raise

    async def check_schema(self) -> None:
        async with self.engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != SCHEMA_REVISION:
                raise RuntimeError("Database schema mismatch; run the explicit migration")

    async def close(self) -> None:
        await self.engine.dispose()
