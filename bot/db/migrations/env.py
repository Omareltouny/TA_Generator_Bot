import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from bot.config import Config
from bot.db.models import Base

target_metadata = Base.metadata


def _url() -> str:
    return Config.from_env().database_url or os.environ["DATABASE_URL"]


def _run(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _online():
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_online())
