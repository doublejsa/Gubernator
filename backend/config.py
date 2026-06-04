"""Central config — loaded from .env at project root."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL     = os.getenv("DATABASE_URL", "postgresql+asyncpg://gubernator:gubernator@localhost:5432/gubernator")
DATABASE_URL_SYNC = DATABASE_URL.replace("+asyncpg", "")   # for Alembic

# ── Auth ──────────────────────────────────────────────────────────────────────
SECRET_KEY       = os.getenv("SECRET_KEY", "change-me-in-production")
ALGORITHM        = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))  # 24h

# ── Vault encryption ──────────────────────────────────────────────────────────
# Note: Anthropic API keys are stored per-user in the credential vault (_anthropic_key).
# There is no server-level ANTHROPIC_API_KEY — each user brings their own.
# Per-user vault keys are stored in the DB (encrypted with this master key).
# In production: use a proper KMS or at minimum a strong random value.
VAULT_MASTER_KEY = os.getenv("VAULT_MASTER_KEY", "")
