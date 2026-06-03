"""
Credential vault — Fernet encryption, per-user vault keys.

Each user gets a randomly generated Fernet key stored encrypted in the DB
(encrypted with the app's VAULT_MASTER_KEY). This means:
  - Rotating the master key = re-encrypt all vault keys (no re-encrypt of credentials)
  - Moving to a KMS later = swap key storage only
  - Web SaaS multi-tenancy = each user's vault is isolated
"""
import os, base64
from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from backend.config import VAULT_MASTER_KEY


def _master_fernet() -> Fernet:
    """Derive a stable Fernet key from the master key string."""
    key_bytes = VAULT_MASTER_KEY.encode() if VAULT_MASTER_KEY else b"dev-only-insecure-key"
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b"gubernator-v1", iterations=100_000)
    raw = base64.urlsafe_b64encode(kdf.derive(key_bytes))
    return Fernet(raw)


def generate_vault_key() -> str:
    """Generate a new random Fernet key for a user (stored encrypted in DB)."""
    return Fernet.generate_key().decode()


def encrypt_vault_key(raw_key: str) -> str:
    """Encrypt a user's vault key with the master key for DB storage."""
    return _master_fernet().encrypt(raw_key.encode()).decode()


def decrypt_vault_key(enc_key: str) -> str:
    """Decrypt a user's vault key from DB storage."""
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
