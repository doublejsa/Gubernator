"""Central config — loaded from .env at project root."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL     = os.getenv("DATABASE_URL", "postgresql+asyncpg://gubernator:gubernator@localhost:5432/gubernator")
DATABASE_URL_SYNC = DATABASE_URL.replace("+asyncpg", "")   # for Alembic

# ── Environment ───────────────────────────────────────────────────────────────
GUBERNATOR_ENV = os.getenv("GUBERNATOR_ENV", "dev").lower()   # "dev" | "production"
IS_PRODUCTION  = GUBERNATOR_ENV == "production"

# ── Auth ──────────────────────────────────────────────────────────────────────
SECRET_KEY       = os.getenv("SECRET_KEY", "change-me-in-production")
ALGORITHM        = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "43200"))  # 30 days (sliding — refreshed while the app is open)
# Session cookie is Secure (HTTPS-only) in production. Override with COOKIE_SECURE=1/0.
COOKIE_SECURE    = os.getenv("COOKIE_SECURE", "1" if IS_PRODUCTION else "0") == "1"

# ── Vault encryption ──────────────────────────────────────────────────────────
# Note: Anthropic API keys are stored per-user in the credential vault (_anthropic_key).
# There is no server-level ANTHROPIC_API_KEY — each user brings their own.
# Per-user vault keys are stored in the DB (encrypted with this master key).
# In production: use a proper KMS or at minimum a strong random value.
VAULT_MASTER_KEY     = os.getenv("VAULT_MASTER_KEY", "")
VAULT_MASTER_KEY_OLD = os.getenv("VAULT_MASTER_KEY_OLD", "")   # set during key rotation only


def assert_production_secrets():
    """Fail fast if critical secrets are missing/weak in production. Called at startup."""
    if not IS_PRODUCTION:
        return
    problems = []
    if not VAULT_MASTER_KEY or len(VAULT_MASTER_KEY) < 32:
        problems.append("VAULT_MASTER_KEY missing or too short (need ≥32 chars)")
    if not SECRET_KEY or SECRET_KEY == "change-me-in-production" or len(SECRET_KEY) < 32:
        problems.append("SECRET_KEY missing, default, or too short (need ≥32 chars)")
    if problems:
        raise RuntimeError(
            "Refusing to start in production with insecure secrets:\n  - "
            + "\n  - ".join(problems)
            + "\nGenerate with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )

# ── Email (SMTP) ──────────────────────────────────────────────────────────────
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))        # 465 SSL, or 587 STARTTLS
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "noreply@gubernator.co")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")  # for links in emails

# Resend (transactional email). If set, used instead of raw SMTP for delivery.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM     = os.getenv("EMAIL_FROM", SMTP_FROM)   # must be a verified Resend sender

# ── PayPal ────────────────────────────────────────────────────────────────────
PAYPAL_ENV        = os.getenv("PAYPAL_ENV", "sandbox").lower()   # "sandbox" | "live"
PAYPAL_CLIENT_ID  = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET     = os.getenv("PAYPAL_SECRET", "")
PAYPAL_PLAN_ID    = os.getenv("PAYPAL_PLAN_ID", "")
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "")
PAYPAL_API_BASE   = ("https://api-m.paypal.com" if PAYPAL_ENV == "live"
                     else "https://api-m.sandbox.paypal.com")
PLAN_PRICE_USD    = os.getenv("PLAN_PRICE_USD", "29.00")
TRIAL_DAYS        = int(os.getenv("TRIAL_DAYS", "14"))
