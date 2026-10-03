from bot.services import auth


def test_token_constant_time_and_empty_guard():
    assert auth.token_matches(" secret ", "secret")
    assert not auth.token_matches("nope", "secret")
    assert not auth.token_matches("", "")  # unset token admits nobody


def test_rate_limiter():
    t = [0.0]
    rl = auth.RateLimiter(max_attempts=3, window_s=10, clock=lambda: t[0])
    for _ in range(3):
        assert not rl.blocked(1)
        rl.record(1)
    assert rl.blocked(1) and not rl.blocked(2)
    t[0] = 11
    assert not rl.blocked(1)


async def test_register_revoke_reregister(session):
    assert await auth.get_active_user(session, 42) is None
    await auth.register(session, 42, "ann")
    assert await auth.get_active_user(session, 42)
    assert await auth.revoke(session, "42")
    assert await auth.get_active_user(session, 42) is None
    await auth.register(session, 42, "ann")  # valid token again un-revokes
    assert await auth.get_active_user(session, 42)
    assert await auth.revoke(session, "nobody") is None


def test_config_normalises_db_urls_and_defaults():
    from bot.config import Config
    c = Config.from_env({"DATABASE_URL": "postgres://u:p@h:5432/d?sslmode=require", "PORT": "8080"})
    assert c.database_url == "postgresql+asyncpg://u:p@h:5432/d?ssl=require"
    assert c.provider_order[0] == "gemini" and c.health_port == 8080
    assert Config.from_env({"UPDATE_MODE": "webhook", "PORT": "1"}).health_port is None
