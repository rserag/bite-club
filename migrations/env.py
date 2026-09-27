import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from nutrition_bot.adapters.database.schema import metadata
from nutrition_bot.config import StorageSettings

config = context.config
url = config.attributes.get("database_url") or StorageSettings().resolved_database_url


def execute(connection):
    context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            await connection.exec_driver_sql("PRAGMA synchronous=FULL")
            await connection.commit()
            await connection.run_sync(execute)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=url, target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
