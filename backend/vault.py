"""
Credential vault — Fernet encryption, per-user vault keys.

Each user gets a randomly generated Fernet key stored encrypted in the DB
(encrypted with the app's VAULT_MASTER_KEY). This means:
  - Rotating the master key = re-encrypt all vault keys (no re-encrypt of credentials)
  - Moving to a KMS later = swap key storage only
  - Web SaaS multi-tenancy = each user's vault is isolated
"""
import base64
from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from backend.config import VAULT_MASTER_KEY, VAULT_MASTER_KEY_OLD, IS_PRODUCTION


def _derive(master: str) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b"gubernator-v1", iterations=100_000)
    return base64.urlsafe_b64encode(kdf.derive(master.encode()))


def _master_key_or_die() -> str:
    if VAULT_MASTER_KEY:
        return VAULT_MASTER_KEY
    if IS_PRODUCTION:
        raise RuntimeError("VAULT_MASTER_KEY is not set — refusing to encrypt secrets insecurely.")
    # Dev only: a clearly-labelled fallback so local dev works without a key.
    return "dev-only-insecure-key-do-not-use-in-prod"


def _master_fernet() -> MultiFernet:
    """MultiFernet so decryption accepts the current OR previous master key
    (during rotation). Encryption always uses the current key (first in list)."""
    keys = [Fernet(_derive(_master_key_or_die()))]
    if VAULT_MASTER_KEY_OLD:
        keys.append(Fernet(_derive(VAULT_MASTER_KEY_OLD)))
    return MultiFernet(keys)


def generate_vault_key() -> str:
    """Generate a new random Fernet key for a user (stored encrypted in DB)."""
    return Fernet.generate_key().decode()


def encrypt_vault_key(raw_key: str) -> str:
    """Encrypt a user's vault key with the current master key for DB storage."""
    return _master_fernet().encrypt(raw_key.encode()).decode()


def decrypt_vault_key(enc_key: str) -> str:
    """Decrypt a user's vault key (accepts current or old master during rotation)."""
    return _master_fernet().decrypt(enc_key.encode()).decode()


def encrypt_secret(vault_key: str, plaintext: str) -> str:
    """Encrypt a credential value with the user's vault key."""
    return Fernet(vault_key.encode()).encrypt(plaintext.encode()).decode()


def decrypt_secret(vault_key: str, ciphertext: str) -> str:
    """Decrypt a credential value with the user's vault key."""
    return Fernet(vault_key.encode()).decrypt(ciphertext.encode()).decode()


def get_user_vault_key(user) -> str:
    """Get the decrypted Fernet key for a user. Generates one if missing."""
    if user.vault_key_enc:
        return decrypt_vault_key(user.vault_key_enc)
    # First time — generate and return (caller must save to DB)
    return generate_vault_key()
