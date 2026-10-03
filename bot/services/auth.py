"""Auth core: invite-token gate, registration, revocation. No Telegram imports."""
from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User


class RateLimiter:
    """Sliding-window limiter keyed by Telegram ID (in-memory; resets on restart)."""

    def __init__(self, max_attempts: int = 5, window_s: float = 600, clock=time.monotonic):
        self.max, self.window, self.clock = max_attempts, window_s, clock
        self._hits: dict[int, deque] = defaultdict(deque)

    def _prune(self, key: int) -> deque:
        q, cutoff = self._hits[key], self.clock() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, key: int) -> bool:
        return len(self._prune(key)) >= self.max

    def record(self, key: int) -> None:
        self._prune(key).append(self.clock())


def token_matches(supplied: str, expected: str) -> bool:
    if not expected:  # an unset INVITE_TOKEN must never admit anyone
        return False
    return hmac.compare_digest(supplied.strip().encode(), expected.encode())


async def get_active_user(session: AsyncSession, telegram_id: int) -> User | None:
    u = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    return u if u and not u.revoked else None


async def register(session: AsyncSession, telegram_id: int, name: str | None) -> User:
    """Register (or un-revoke on re-registration with a valid token)."""
    u = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if u is None:
        u = User(telegram_id=telegram_id, name=name)
        session.add(u)
    else:
        u.revoked = False
        u.name = name or u.name
    await session.commit()
    return u


async def revoke(session: AsyncSession, target: str) -> User | None:
    """Revoke by telegram id (digits) or name. Returns the user or None."""
    stmt = select(User).where(User.telegram_id == int(target)) if target.lstrip("-").isdigit() \
        else select(User).where(User.name == target.lstrip("@"))
    u = (await session.execute(stmt)).scalars().first()
    if u:
        u.revoked = True
        await session.commit()
    return u
