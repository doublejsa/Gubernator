"""
Transactional email via SMTP (cPanel mail server) + signed token helpers
for email verification and password reset.
"""
from __future__ import annotations
import asyncio, smtplib, ssl
from datetime import datetime, timedelta
from email.message import EmailMessage

from jose import jwt, JWTError
from backend.config import (
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM, APP_BASE_URL,
    SECRET_KEY, ALGORITHM, RESEND_API_KEY, EMAIL_FROM,
)

# ── Signed tokens (no DB storage needed) ──────────────────────────────────────
def make_token(user_id: str, purpose: str, hours: int) -> str:
    exp = datetime.utcnow() + timedelta(hours=hours)
    return jwt.encode({"sub": user_id, "purpose": purpose, "exp": exp},
                      SECRET_KEY, algorithm=ALGORITHM)

def read_token(token: str, purpose: str) -> str | None:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if payload.get("purpose") != purpose:
        return None
    return payload.get("sub")


# ── SMTP send ─────────────────────────────────────────────────────────────────
def _send_sync(to: str, subject: str, html: str, text: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        # No SMTP configured — log to console so dev still works
        print(f"\n[EMAIL — SMTP not configured, would send]\nTo: {to}\nSubject: {subject}\n{text}\n")
        return
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=20) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

async def _send_resend(to: str, subject: str, html: str, text: str) -> bool:
    """Send via Resend's REST API (reliable inbox delivery)."""
    import httpx
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": EMAIL_FROM, "to": [to], "subject": subject, "html": html, "text": text},
        )
        if r.status_code >= 400:
            print(f"[RESEND ERROR] {r.status_code}: {r.text}")
            return False
        return True

async def send_email(to: str, subject: str, html: str, text: str) -> bool:
    # Prefer Resend when configured; fall back to raw SMTP otherwise.
    if RESEND_API_KEY:
        try:
            return await _send_resend(to, subject, html, text)
        except Exception as e:
            print(f"[RESEND ERROR] {e}")
            return False
    try:
        await asyncio.get_event_loop().run_in_executor(None, _send_sync, to, subject, html, text)
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False


# ── Templated emails ──────────────────────────────────────────────────────────
def _wrap(title: str, body_html: str, btn_text: str, btn_url: str) -> str:
    return f"""\
<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:480px;margin:0 auto;color:#1a1a1a">
  <h2 style="font-weight:700">⚙️ Gubernator</h2>
  <h3>{title}</h3>
  <p style="line-height:1.6;color:#444">{body_html}</p>
  <p><a href="{btn_url}" style="display:inline-block;background:#238636;color:#fff;
     text-decoration:none;padding:11px 22px;border-radius:8px;font-weight:600">{btn_text}</a></p>
  <p style="font-size:12px;color:#888">If the button doesn't work, paste this link into your browser:<br>{btn_url}</p>
</div>"""

async def send_verification(to: str, user_id: str) -> bool:
    token = make_token(user_id, "verify", hours=24)
    url   = f"{APP_BASE_URL}/api/auth/verify?token={token}"
    html  = _wrap("Confirm your email",
                  "Welcome! Please confirm your email address to activate your Gubernator account.",
                  "Confirm email", url)
    text  = f"Confirm your Gubernator email: {url}"
    return await send_email(to, "Confirm your Gubernator email", html, text)

async def send_trial_reminder(to: str, ends_str: str) -> bool:
    url  = f"{APP_BASE_URL}/app"
    html = _wrap("Your free trial ends soon",
                 f"Heads up — your Gubernator free trial ends on <strong>{ends_str}</strong>, after which "
                 f"your subscription is $29/month. No action needed if you'd like to continue. "
                 f"If you'd rather not, you can cancel anytime from Settings before then and you won't be charged.",
                 "Open Gubernator", url)
    text = f"Your Gubernator free trial ends on {ends_str}. To continue, do nothing — $29/mo begins then. To cancel, open Settings: {url}"
    return await send_email(to, "Your Gubernator trial ends in 2 days", html, text)

async def send_password_reset(to: str, user_id: str) -> bool:
    token = make_token(user_id, "reset", hours=1)
    url   = f"{APP_BASE_URL}/?reset={token}"
    html  = _wrap("Reset your password",
                  "We received a request to reset your Gubernator password. This link expires in 1 hour. "
                  "If you didn't request it, you can ignore this email.",
                  "Reset password", url)
    text  = f"Reset your Gubernator password (expires in 1 hour): {url}"
    return await send_email(to, "Reset your Gubernator password", html, text)
