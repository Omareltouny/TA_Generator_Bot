"""Drop EVERY table (including alembic_version) of the configured DATABASE_URL.

The rules/feedback rewrite changed the schema without migrations, so existing databases must be reset:

    python scripts/reset_db.py && alembic upgrade head

Usage:  python scripts/reset_db.py [--yes]
Without --yes you must type RESET to confirm. All courses, materials, rules and jobs are deleted for good.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from bot.config import Config  # noqa: E402
from bot.db.models import Base  # noqa: E402
from bot.db.session import make_engine  # noqa: E402


async def reset(url: str) -> None:
    """Drop all model tables and the alembic bookkeeping table."""
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    finally:
        await engine.dispose()


def main(argv: list[str]) -> int:
    cfg = Config.from_env()
    if not cfg.database_url:
        print("DATABASE_URL is not set.")
        return 1
    target = cfg.database_url.split("@")[-1] if "@" in cfg.database_url else cfg.database_url
    if "--yes" not in argv:
        print(f"This DELETES ALL DATA in: {target}")
        if input("Type RESET to continue: ").strip() != "RESET":
            print("Aborted.")
            return 1
    asyncio.run(reset(cfg.database_url))
    print("All tables dropped. Now run: alembic upgrade head")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
