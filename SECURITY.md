# Gubernator — Security Notes

## Threat model (the one that matters)
Gubernator stores users' **SSH credentials** and **API keys** and runs commands
on their servers. The highest-impact compromise is the **vault master key**
(`VAULT_MASTER_KEY`): with DB access + that key, an attacker could decrypt every
user's credentials. Protecting and being able to rotate that key is priority #1.

## How secrets are protected
- Each user has a random **per-user Fernet key**. It encrypts that user's
  credentials and VPS passwords (`credentials.password_enc`, `vps_connections.password_enc`).
- The per-user key is itself encrypted (`users.vault_key_enc`) with a key derived
  (PBKDF2-SHA256) from `VAULT_MASTER_KEY`.
- So a DB dump alone is useless without the master key (envelope encryption).
- JWTs are signed with `SECRET_KEY`. SMTP password in `SMTP_PASS`.
- Anthropic API keys are **per user**, stored in the vault — no server-wide key.

## Production requirements
Set `GUBERNATOR_ENV=production`. The app then **refuses to start** unless
`VAULT_MASTER_KEY` and `SECRET_KEY` are present and ≥32 chars (no insecure
dev fallback). Session cookies become `Secure` (HTTPS-only) automatically
(`COOKIE_SECURE`).

### Where the master key should live (NOT in the repo)
- Never commit `.env` (it's gitignored).
- On the server, inject secrets via a **systemd `EnvironmentFile`** with
  `chmod 600` owned by root, or Docker/host secrets — not a world-readable file,
  and ideally not on the same disk as DB backups.
- Generate strong values:
  `python -c "import secrets; print(secrets.token_hex(32))"`

## Rotating the master key (incident response)
If `VAULT_MASTER_KEY` is exposed:
1. Generate a new key.
2. In `.env`: `VAULT_MASTER_KEY_OLD=<old>` and `VAULT_MASTER_KEY=<new>`.
3. Run `python rotate_master_key.py` (re-wraps every user's vault key; credentials
   are untouched).
4. Remove `VAULT_MASTER_KEY_OLD` and restart.

Rotate `SECRET_KEY` similarly by just changing it — this invalidates all existing
JWT sessions (everyone re-logs in), which is the desired effect after a breach.

## Other controls in place
- Login/register/reset are rate-limited (in-memory, per email).
- Passwords hashed with bcrypt; min 8 chars.
- Email verification required before login; password reset via signed 1h token.
- Credential values are never returned in list endpoints or logs; reveal requires
  an explicit authenticated request.
- WebSocket connections require a valid JWT (`?token=`).

## Still to do before public launch
- KMS-backed master key (key never on the app host) — currently env-injected.
- Per-command audit log of what's run on each user's server.
- SPF/DKIM/DMARC DNS for gubernator.co so transactional mail isn't spam-filtered.
- Independent security review / pen test.
- Terms of Service + Acceptable Use + Privacy Policy.
