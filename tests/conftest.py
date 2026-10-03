import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from bot.db.models import Base
from bot.db.session import make_session_factory


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as s:
        yield s
    await engine.dispose()
