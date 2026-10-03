"""Async engine/session factory."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def make_engine(url: str) -> AsyncEngine:
    kw = {"connect_args": {"statement_cache_size": 0}} if url.startswith("postgresql") else {}  # Supabase/pgbouncer pooler
    return create_async_engine(url, pool_pre_ping=True, **kw)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
