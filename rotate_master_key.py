#!/usr/bin/env python3
"""
Rotate the vault master key.

If VAULT_MASTER_KEY is ever exposed, rotate it WITHOUT re-encrypting any user
credentials — only the per-user vault keys are re-wrapped under the new master.

Procedure:
  1. Generate a new key:   python -c "import secrets; print(secrets.token_hex(32))"
  2. In .env set:
         VAULT_MASTER_KEY_OLD=<the current/old key>
         VAULT_MASTER_KEY=<the new key>
  3. Run:  .venv/bin/python rotate_master_key.py
  4. Once it reports success, REMOVE VAULT_MASTER_KEY_OLD from .env and restart.

This works because MultiFernet decrypts vault_key_enc with the old key, then we
re-encrypt it with the new (current) key.
"""
import asyncio, sys
sys.path.insert(0, ".")
from sqlalchemy import select
from backend.db import SessionLocal
from backend.models import User
from backend.config import VAULT_MASTER_KEY, VAULT_MASTER_KEY_OLD
from backend.vault import decrypt_vault_key, encrypt_vault_key


async def main():
    if not VAULT_MASTER_KEY_OLD:
        print("✗ VAULT_MASTER_KEY_OLD is not set. Set the OLD key there and the NEW key "
              "in VAULT_MASTER_KEY, then re-run.")
        return
    if VAULT_MASTER_KEY_OLD == VAULT_MASTER_KEY:
        print("✗ Old and new keys are identical — nothing to rotate.")
        return
    rotated = failed = 0
    async with SessionLocal() as db:
        users = (await db.execute(select(User).where(User.vault_key_enc.isnot(None)))).scalars().all()
        for u in users:
            try:
                raw = decrypt_vault_key(u.vault_key_enc)   # accepts old (MultiFernet)
                u.vault_key_enc = encrypt_vault_key(raw)   # re-wrap with new (current)
                rotated += 1
            except Exception as e:
                print(f"  ✗ {u.email}: {e}")
                failed += 1
        await db.commit()
    print(f"\n✓ Rotated {rotated} user vault key(s); {failed} failed.")
    if failed == 0:
        print("Now REMOVE VAULT_MASTER_KEY_OLD from .env and restart the app.")


if __name__ == "__main__":
    asyncio.run(main())
