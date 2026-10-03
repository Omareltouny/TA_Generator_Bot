"""Environment parsing. All secrets come from env vars."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv(value: str | None, default: str = "") -> list[str]:
    return [p.strip() for p in (value if value is not None else default).split(",") if p.strip()]


@dataclass(frozen=True)
class Config:
    bot_token: str
    invite_token: str
    owner_telegram_id: int | None
    database_url: str
    update_mode: str = "polling"
    webhook_url: str = ""
    port: int = 8080
    provider_order: list[str] = field(default_factory=lambda: ["gemini", "groq", "openrouter"])
    health_port: int | None = None
    models: dict[str, list[str]] = field(default_factory=dict)
    api_keys: dict[str, str] = field(default_factory=dict)
    max_file_mb: int = 20
    max_total_material_mb: int = 60

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        e = os.environ if env is None else env
        owner = e.get("OWNER_TELEGRAM_ID", "").strip()
        db = e.get("DATABASE_URL", "")
        # Normalise common Postgres URL spellings to the asyncpg driver.
        if db.startswith("postgres://"):
            db = "postgresql+asyncpg://" + db[len("postgres://"):]
        elif db.startswith("postgresql://"):
            db = "postgresql+asyncpg://" + db[len("postgresql://"):]
        # asyncpg spells libpq's sslmode=require as ssl=require
        db = db.replace("sslmode=", "ssl=")
        return cls(
            bot_token=e.get("TELEGRAM_BOT_TOKEN", ""),
            invite_token=e.get("INVITE_TOKEN", ""),
            owner_telegram_id=int(owner) if owner else None,
            database_url=db,
            update_mode=e.get("UPDATE_MODE", "polling"),
            webhook_url=e.get("WEBHOOK_URL", ""),
            port=int(e.get("PORT", "8080")),
            health_port=int(e["PORT"]) if e.get("PORT") and e.get("UPDATE_MODE", "polling") == "polling" else None,
            provider_order=_csv(e.get("LLM_PROVIDER_ORDER"), "gemini,groq,openrouter"),
            models={
                "groq": _csv(e.get("GROQ_MODELS"), "llama-3.3-70b-versatile"),
                "gemini": _csv(e.get("GEMINI_MODELS"), "gemini-2.0-flash"),
                "openrouter": _csv(e.get("OPENROUTER_MODELS"), "meta-llama/llama-3.3-70b-instruct:free"),
            },
            api_keys={
                "groq": e.get("GROQ_API_KEY", ""),
                "gemini": e.get("GEMINI_API_KEY", ""),
                "openrouter": e.get("OPENROUTER_API_KEY", ""),
            },
            max_file_mb=int(e.get("MAX_FILE_MB", "20")),
            max_total_material_mb=int(e.get("MAX_TOTAL_MATERIAL_MB", "60")),
        )
