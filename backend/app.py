"""
Gubernator — AI Agent Control Panel
FastAPI backend: auth, credential vault, VPS/TUI/Claude WebSocket sessions.
"""
from __future__ import annotations
import asyncio, os, uuid, json as _json, shlex
from pathlib import Path
import paramiko
from fastapi import FastAPI, Depends, HTTPException, Response, WebSocket, WebSocketDisconnect, status, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import select, delete as _sqldelete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import SECRET_KEY, ALGORITHM
from backend.db import get_db, engine, Base, SessionLocal
from backend.models import User, VpsConnection, Credential, Task, MemoryFact
from backend.embeddings import embed
from backend.auth import (
    hash_password, verify_password, create_access_token, get_current_user
)
from backend.vault import (
    generate_vault_key, encrypt_vault_key, get_user_vault_key,
    encrypt_secret, decrypt_secret
)
from backend.terminal import PTYSession
from backend.ws_handlers import pty_ws_handler, chat_ws_handler
from backend.email_sender import send_verification, send_password_reset, read_token

BASE_DIR     = Path(__file__).parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="Gubernator", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── No-cache for static assets (dev) — stops stale app.js/css/html ────────────
@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static") or path in ("/", "/app"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"]        = "no-cache"
    return response

# ── DB init ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    from backend.config import assert_production_secrets
    assert_production_secrets()   # fail fast on weak/missing secrets in production
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ── Static files + SPA ───────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse(FRONTEND_DIR / "index.html")

@app.get("/app", response_class=HTMLResponse)
async def app_page():
    return FileResponse(FRONTEND_DIR / "app.html")

# ── Legal pages ───────────────────────────────────────────────────────────────
@app.get("/terms", response_class=HTMLResponse)
async def terms_page():
    return FileResponse(FRONTEND_DIR / "legal" / "terms.html")

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    return FileResponse(FRONTEND_DIR / "legal" / "privacy.html")

@app.get("/acceptable-use", response_class=HTMLResponse)
async def aup_page():
    return FileResponse(FRONTEND_DIR / "legal" / "acceptable-use.html")


# ── OpenClaw stats (proxied from VPS for the TUI header badges) ───────────────
@app.get("/api/openclaw-stats")
async def openclaw_stats(user: User = Depends(get_current_user)):
    """Fetched client-side after each VPS command to update context/cache badges.
    Requires an active shell session for this user — returns empty if not connected."""
    import re as _re
    sessions = _user_sessions.get(str(user.id), {})
    shell    = sessions.get("shell")
    if not (shell and shell.connected):
        return JSONResponse({"context_pct": None, "cache_pct": None})
    loop = asyncio.get_event_loop()
    output = await loop.run_in_executor(None, lambda: shell.exec("openclaw status", timeout=10))
    ctx   = _re.search(r'(\d+)k/(\d+)k \((\d+)%\)', output)
    cache = _re.search(r'(\d+)% cached', output)
    return {
        "context_pct":     int(ctx.group(3))   if ctx   else None,
        "context_used_k":  int(ctx.group(1))   if ctx   else None,
        "context_total_k": int(ctx.group(2))   if ctx   else None,
        "cache_pct":       int(cache.group(1)) if cache else None,
    }


# ── Auth ──────────────────────────────────────────────────────────────────────
import time as _time
from collections import defaultdict, deque as _deque

# Simple in-memory rate limiter: key → timestamps of recent attempts
_rate_hits: dict[str, _deque] = defaultdict(_deque)

def _rate_limit(key: str, max_attempts: int, window_secs: int):
    """Raise 429 if `key` exceeded max_attempts within window_secs."""
    now = _time.time()
    hits = _rate_hits[key]
    while hits and hits[0] < now - window_secs:
        hits.popleft()
    if len(hits) >= max_attempts:
        raise HTTPException(429, "Too many attempts — please wait a minute and try again.")
    hits.append(now)

def _pw_ok(pw: str) -> bool:
    return isinstance(pw, str) and len(pw) >= 8

class RegisterIn(BaseModel):
    email: str
    password: str

class LoginIn(BaseModel):
    email: str
    password: str

class ChangePwIn(BaseModel):
    current_password: str
    new_password: str

class DeleteAccountIn(BaseModel):
    password: str

@app.post("/api/auth/register")
async def register(body: RegisterIn, db: AsyncSession = Depends(get_db)):
    _rate_limit(f"register:{body.email.lower()}", max_attempts=5, window_secs=300)
    if not _pw_ok(body.password):
        raise HTTPException(400, "Password must be at least 8 characters.")
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Email already registered")
    vault_key     = generate_vault_key()
    vault_key_enc = encrypt_vault_key(vault_key)
    user = User(
        email         = body.email,
        password_hash = hash_password(body.password),
        vault_key_enc = vault_key_enc,
        email_verified = False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    await send_verification(user.email, str(user.id))
    return {"id": str(user.id), "email": user.email, "verify_required": True}

@app.post("/api/auth/login")
async def login(body: LoginIn, response: Response, db: AsyncSession = Depends(get_db)):
    _rate_limit(f"login:{body.email.lower()}", max_attempts=8, window_secs=300)
    result = await db.execute(select(User).where(User.email == body.email))
    user   = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    if not user.email_verified:
        raise HTTPException(403, "Please verify your email first. Check your inbox for the confirmation link.")
    token = create_access_token(str(user.id))
    # Set httpOnly cookie (web) + return token (API clients)
    from backend.config import COOKIE_SECURE
    response.set_cookie(
        key="gubernator_session", value=token,
        httponly=True, samesite="lax", secure=COOKIE_SECURE,
        max_age=86400
    )
    return {"access_token": token, "token_type": "bearer"}

@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie("gubernator_session")
    return {"ok": True}

@app.get("/api/auth/me")
async def me(user: User = Depends(get_current_user)):
    return {"id": str(user.id), "email": user.email}

class EmailIn(BaseModel):
    email: str

class ResetPwIn(BaseModel):
    token: str
    new_password: str

@app.get("/api/auth/verify")
async def verify_email(token: str, db: AsyncSession = Depends(get_db)):
    uid = read_token(token, "verify")
    if not uid:
        return HTMLResponse("<h3>This confirmation link is invalid or has expired.</h3>"
                            "<p><a href='/'>Back to sign in</a></p>", status_code=400)
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if not user:
        return HTMLResponse("<h3>Account not found.</h3>", status_code=404)
    if not user.email_verified:
        user.email_verified = True
        await db.commit()
    return RedirectResponse(url="/?verified=1", status_code=303)

@app.post("/api/auth/resend-verification")
async def resend_verification(body: EmailIn, db: AsyncSession = Depends(get_db)):
    _rate_limit(f"resend:{body.email.lower()}", max_attempts=3, window_secs=300)
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if user and not user.email_verified:
        await send_verification(user.email, str(user.id))
    return {"ok": True}   # don't leak whether the email exists

@app.post("/api/auth/forgot-password")
async def forgot_password(body: EmailIn, db: AsyncSession = Depends(get_db)):
    _rate_limit(f"forgot:{body.email.lower()}", max_attempts=3, window_secs=300)
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if user:
        await send_password_reset(user.email, str(user.id))
    return {"ok": True}   # always ok — don't reveal which emails exist

@app.post("/api/auth/reset-password")
async def reset_password(body: ResetPwIn, db: AsyncSession = Depends(get_db)):
    uid = read_token(body.token, "reset")
    if not uid:
        raise HTTPException(400, "This reset link is invalid or has expired.")
    if not _pw_ok(body.new_password):
        raise HTTPException(400, "Password must be at least 8 characters.")
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "Account not found.")
    user.password_hash  = hash_password(body.new_password)
    user.email_verified = True   # resetting via emailed link also proves email ownership
    await db.commit()
    return {"ok": True}

@app.post("/api/auth/change-password")
async def change_password(body: ChangePwIn, response: Response,
                          user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect.")
    if not _pw_ok(body.new_password):
        raise HTTPException(400, "New password must be at least 8 characters.")
    user.password_hash = hash_password(body.new_password)
    await db.commit()
    # Rotate the session token after a password change
    response.delete_cookie("gubernator_session")
    return {"ok": True}

@app.delete("/api/auth/account")
async def delete_account(body: DeleteAccountIn, response: Response,
                         user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(400, "Password is incorrect.")
    # Tear down any live sessions, then explicitly delete all the user's rows
    # (explicit deletes are reliable under async; ORM lazy cascade is not).
    _user_sessions.pop(str(user.id), None)
    uid = user.id
    for model in (VpsConnection, Credential, ChatSession, Task, MemoryFact):
        await db.execute(_sqldelete(model).where(model.user_id == uid))
    await db.execute(_sqldelete(User).where(User.id == uid))
    await db.commit()
    response.delete_cookie("gubernator_session")
    return {"ok": True}


# ── VPS Connections ───────────────────────────────────────────────────────────
class VpsIn(BaseModel):
    label:    str = "My VPS"
    host:     str
    port:     int = 22
    username: str
    password: str

@app.get("/api/vps")
async def list_vps(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(VpsConnection).where(VpsConnection.user_id == user.id))
    conns  = result.scalars().all()
    return [{"id": str(c.id), "label": c.label, "host": c.host, "port": c.port,
             "username": c.username, "is_default": c.is_default} for c in conns]

@app.post("/api/vps")
async def add_vps(body: VpsIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    vault_key    = get_user_vault_key(user)
    password_enc = encrypt_secret(vault_key, body.password.strip())   # strip accidental whitespace
    # Upsert: update existing default if one exists, otherwise create new
    existing = (await db.execute(
        select(VpsConnection).where(VpsConnection.user_id == user.id)
        .order_by(VpsConnection.created_at).limit(1)
    )).scalar_one_or_none()
    if existing:
        existing.label        = body.label
        existing.host         = body.host.strip()
        existing.port         = body.port
        existing.username     = body.username.strip()
        existing.password_enc = password_enc
        existing.is_default   = True
        conn = existing
    else:
        conn = VpsConnection(
            user_id=user.id, label=body.label, host=body.host.strip(),
            port=body.port, username=body.username.strip(), password_enc=password_enc,
        )
        db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return {"id": str(conn.id), "label": conn.label, "host": conn.host}

@app.delete("/api/vps/{conn_id}")
async def delete_vps(conn_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(VpsConnection).where(VpsConnection.id == conn_id, VpsConnection.user_id == user.id)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(404)
    await db.delete(conn)
    await db.commit()
    return {"ok": True}


# ── Credential Vault ──────────────────────────────────────────────────────────
class CredentialIn(BaseModel):
    name:       str
    username:   str = ""
    password:   str
    notes:      str = ""
    vps_synced: bool = False

@app.get("/api/credentials")
async def list_credentials(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Credential).where(Credential.user_id == user.id))
    creds  = result.scalars().all()
    # Never return password values in list — names and metadata only
    return [{"id": str(c.id), "name": c.name, "username": c.username,
             "notes": c.notes, "vps_synced": c.vps_synced,
             "updated_at": c.updated_at.isoformat()} for c in creds]

@app.post("/api/credentials")
async def save_credential(body: CredentialIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    vault_key    = get_user_vault_key(user)
    password_enc = encrypt_secret(vault_key, body.password)
    # Upsert by name
    result = await db.execute(
        select(Credential).where(Credential.user_id == user.id, Credential.name == body.name)
    )
    cred = result.scalar_one_or_none()
    if cred:
        cred.username     = body.username
        cred.password_enc = password_enc
        cred.notes        = body.notes
        cred.vps_synced   = body.vps_synced
    else:
        cred = Credential(
            user_id=user.id, name=body.name, username=body.username,
            password_enc=password_enc, notes=body.notes, vps_synced=body.vps_synced,
        )
        db.add(cred)
    await db.commit()
    await db.refresh(cred)
    return {"id": str(cred.id), "name": cred.name}

@app.get("/api/credentials/{cred_id}/reveal")
async def reveal_credential(cred_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Credential).where(Credential.id == cred_id, Credential.user_id == user.id)
    )
    cred = result.scalar_one_or_none()
    if not cred:
        raise HTTPException(404)
    vault_key = get_user_vault_key(user)
    return {"password": decrypt_secret(vault_key, cred.password_enc)}

@app.delete("/api/credentials/{cred_id}")
async def delete_credential(cred_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Credential).where(Credential.id == cred_id, Credential.user_id == user.id)
    )
    cred = result.scalar_one_or_none()
    if not cred:
        raise HTTPException(404)
    await db.delete(cred)
    await db.commit()
    return {"ok": True}


# ── Tasks (Activity panel) ────────────────────────────────────────────────────
@app.get("/api/tasks")
async def list_tasks(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Task).where(Task.user_id == user.id).order_by(Task.created_at.desc()).limit(100)
    )).scalars().all()
    return [{"id": str(t.id), "title": t.title, "status": t.status, "outcome": t.outcome,
             "created_at": t.created_at.isoformat(),
             "completed_at": t.completed_at.isoformat() if t.completed_at else None}
            for t in rows]

@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    t = (await db.execute(select(Task).where(Task.id == task_id, Task.user_id == user.id))).scalar_one_or_none()
    if not t:
        raise HTTPException(404)
    await db.delete(t)
    await db.commit()
    return {"ok": True}


# ── Memory facts ──────────────────────────────────────────────────────────────
class MemoryIn(BaseModel):
    key:      str
    value:    str
    category: str = "general"

@app.get("/api/memory")
async def list_memory(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(MemoryFact).where(MemoryFact.user_id == user.id)
        .order_by(MemoryFact.category, MemoryFact.key)
    )).scalars().all()
    return [{"id": str(m.id), "key": m.key, "value": m.value, "category": m.category,
             "updated_at": m.updated_at.isoformat()} for m in rows]

@app.post("/api/memory")
async def save_memory(body: MemoryIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    vec = await embed(f"{body.key}: {body.value}")
    existing = (await db.execute(
        select(MemoryFact).where(MemoryFact.user_id == user.id, MemoryFact.key == body.key)
    )).scalar_one_or_none()
    if existing:
        existing.value = body.value; existing.category = body.category; existing.embedding = vec
    else:
        db.add(MemoryFact(user_id=user.id, key=body.key, value=body.value,
                          category=body.category, embedding=vec))
    await db.commit()
    return {"ok": True}

@app.delete("/api/memory/{fact_id}")
async def delete_memory(fact_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    m = (await db.execute(select(MemoryFact).where(MemoryFact.id == fact_id, MemoryFact.user_id == user.id))).scalar_one_or_none()
    if not m:
        raise HTTPException(404)
    await db.delete(m)
    await db.commit()
    return {"ok": True}


# ── Semantic search (pgvector) over tasks + memory ────────────────────────────
@app.get("/api/search")
async def semantic_search(q: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    vec = await embed(q)
    if vec is None:
        return {"tasks": [], "memory": []}
    task_rows = (await db.execute(
        select(Task).where(Task.user_id == user.id, Task.embedding.isnot(None))
        .order_by(Task.embedding.cosine_distance(vec)).limit(5)
    )).scalars().all()
    mem_rows = (await db.execute(
        select(MemoryFact).where(MemoryFact.user_id == user.id, MemoryFact.embedding.isnot(None))
        .order_by(MemoryFact.embedding.cosine_distance(vec)).limit(5)
    )).scalars().all()
    return {
        "tasks":  [{"title": t.title, "status": t.status, "outcome": t.outcome} for t in task_rows],
        "memory": [{"key": m.key, "value": m.value, "category": m.category} for m in mem_rows],
    }


# ── Skills marketplace (ClawHub via openclaw CLI) ─────────────────────────────
async def _vps_exec(user: User, db: AsyncSession, cmd: str, timeout: int = 60) -> str:
    """Run a command on the user's VPS over a short-lived SSH exec connection.
    Independent of the chat WebSocket, so REST endpoints can use it directly."""
    vps = (await db.execute(
        select(VpsConnection).where(VpsConnection.user_id == user.id)
        .order_by(VpsConnection.created_at)
    )).scalars().first()
    if not vps:
        raise HTTPException(400, "No VPS configured")
    vault_key = get_user_vault_key(user)
    password  = decrypt_secret(vault_key, vps.password_enc) if vps.password_enc else ""

    def _run():
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(vps.host, port=vps.port, username=vps.username, password=password, timeout=15)
        try:
            _, o, e = c.exec_command(cmd, timeout=timeout)
            o.channel.recv_exit_status()
            return o.read().decode() + e.read().decode()
        finally:
            c.close()
    return await asyncio.get_event_loop().run_in_executor(None, _run)


@app.get("/api/skills/search")
async def skills_search(q: str = "", user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Search the ClawHub catalog and mark which results are already installed."""
    query = shlex.quote(q) if q else ""
    cmd = (f"openclaw skills search {query} --json --limit 30 2>/dev/null; "
           f"echo '@@@SPLIT@@@'; "
           f"openclaw skills list --json 2>/dev/null")
    out = await _vps_exec(user, db, cmd)
    search_part, _, list_part = out.partition("@@@SPLIT@@@")
    try:    results = _json.loads(search_part).get("results", [])
    except Exception: results = []
    try:    installed = {s.get("name") for s in _json.loads(list_part).get("skills", [])}
    except Exception: installed = set()
    return [{
        "slug":      r.get("slug", ""),
        "name":      r.get("displayName") or r.get("slug", ""),
        "summary":   r.get("summary", ""),
        "owner":     (r.get("owner") or {}).get("displayName", "") or r.get("ownerHandle", ""),
        "installed": r.get("slug", "") in installed,
    } for r in results if r.get("slug")]


class SkillInstallIn(BaseModel):
    slug: str

@app.post("/api/skills/install")
async def skills_install(body: SkillInstallIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Install a skill from ClawHub into the active workspace."""
    out = await _vps_exec(user, db, f"openclaw skills install {shlex.quote(body.slug)} 2>&1", timeout=180)
    low = out.lower()
    ok  = ("installed" in low or "success" in low or "added" in low) and "error" not in low
    return {"ok": ok, "output": out[-2500:]}


@app.get("/api/skills/installed")
async def skills_installed(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """List all skills on the VPS, bucketed into ready / installable / unavailable."""
    out = await _vps_exec(user, db, "openclaw skills list --json 2>/dev/null")
    try:    skills = _json.loads(out).get("skills", [])
    except Exception: skills = []
    result = []
    for s in skills:
        missing = s.get("missing") or {}
        os_req  = missing.get("os") or []
        bins    = (missing.get("bins") or []) + (missing.get("anyBins") or [])
        env     = missing.get("env") or []
        cfg     = missing.get("config") or []
        if s.get("eligible"):
            state, needs = "ready", []
        elif os_req:
            state, needs = "unavailable", [f"requires {', '.join(os_req)}"]
        elif bins or env or cfg:
            state, needs = "installable", bins + [f"env:{e}" for e in env] + [f"config:{c}" for c in cfg]
        else:
            state, needs = "unavailable", []
        result.append({
            "name": s.get("name", ""), "emoji": s.get("emoji", "🧩"),
            "description": s.get("description", ""), "source": s.get("source", ""),
            "state": state, "needs": needs,
        })
    order = {"ready": 0, "installable": 1, "unavailable": 2}
    result.sort(key=lambda x: (order.get(x["state"], 3), x["name"]))
    return result


@app.get("/api/suggestions")
async def suggestions(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Capability-aware quick-start ideas, filtered by the user's known skills/facts."""
    rows  = (await db.execute(select(MemoryFact).where(MemoryFact.user_id == user.id))).scalars().all()
    blob  = " ".join((m.key + " " + m.value).lower() for m in rows)
    def has(*kw): return any(k in blob for k in kw)

    cards = [{
        "icon": "🌐", "title": "Set up a website",
        "prompt": "Help me set up a new website on my server. Walk me through it step by step, "
                  "asking one question at a time (domain, pages, etc.).",
    }]
    if has("browser", "playwright", "can_browse", "chromium"):
        cards.append({
            "icon": "🔍", "title": "Research something online",
            "prompt": "Use the agent to browse the web and research a topic for me. Ask me what to research."})
    if has("email", "imap", "gmail", "outlook", "inbox"):
        cards.append({
            "icon": "📬", "title": "Check my emails",
            "prompt": "Check my inbox and summarise what needs my attention."})
    if has("deploy", "deployment", "ftp", "hostgator", "cpanel"):
        cards.append({
            "icon": "🚀", "title": "Deploy my website",
            "prompt": "Help me deploy my website using my usual deployment flow. Confirm each step with me."})
    cards.append({
        "icon": "⏰", "title": "Schedule a recurring task",
        "prompt": "I want to schedule a task to run automatically on a schedule (a cron job). "
                  "Help me set it up step by step."})
    cards.append({
        "icon": "📊", "title": "Set up a daily report",
        "prompt": "Help me set up an automated daily report. Ask me what it should contain and where to send it."})
    return cards


@app.post("/api/skills/restart-agent")
async def restart_agent(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Restart the OpenClaw gateway so newly-installed skills are activated."""
    out = await _vps_exec(
        user, db,
        "openclaw gateway stop 2>&1; sleep 2; openclaw gateway start 2>&1",
        timeout=120,
    )
    low = out.lower()
    ok  = "error" not in low and "failed" not in low
    return {"ok": ok, "output": out[-1500:]}


# ── WebSocket auth helper ─────────────────────────────────────────────────────
async def ws_get_user(token: str, db: AsyncSession) -> User:
    """Authenticate a WebSocket connection via ?token= query param."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
    except JWTError:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


# Per-user terminal sessions — keyed by user_id string
_user_sessions: dict[str, dict] = {}   # {user_id: {"tui": PTYSession, "shell": PTYSession}}

def get_user_sessions(user_id: str) -> dict:
    if user_id not in _user_sessions:
        _user_sessions[user_id] = {}
    return _user_sessions[user_id]


# ── WebSocket routes ──────────────────────────────────────────────────────────
@app.websocket("/ws/tui")
async def ws_tui(ws: WebSocket, token: str = Query(...)):
    async with SessionLocal() as db:
        user = await ws_get_user(token, db)
        if not user:
            await ws.accept()
            await ws.send_json({"type": "error", "subtype": "auth_expired"})
            await ws.close()
            return
        vps = (await db.execute(
            select(VpsConnection).where(VpsConnection.user_id == user.id)
            .order_by(VpsConnection.created_at)
        )).scalars().first()
        if not vps:
            await ws.accept()
            await ws.send_json({"type": "error", "message": "No VPS configured"})
            await ws.close()
            return
        vault_key = get_user_vault_key(user)
        password  = decrypt_secret(vault_key, vps.password_enc) if vps.password_enc else ""
        TMUX = "ocmgr-tui"
        cmd  = (f"tmux new-session -d -x 220 -y 50 -s {TMUX} 'openclaw tui' 2>/dev/null; "
                f"tmux set-option -t {TMUX} mouse on 2>/dev/null; "
                f"tmux attach-session -t {TMUX}")
        sessions = get_user_sessions(str(user.id))
        await pty_ws_handler(ws, PTYSession(), vps.host, vps.port, vps.username, password, cmd, "tui", sessions)


@app.websocket("/ws/shell")
async def ws_shell(ws: WebSocket, token: str = Query(...)):
    async with SessionLocal() as db:
        user = await ws_get_user(token, db)
        if not user:
            await ws.accept()
            await ws.send_json({"type": "error", "subtype": "auth_expired"})
            await ws.close()
            return
        vps = (await db.execute(
            select(VpsConnection).where(VpsConnection.user_id == user.id)
            .order_by(VpsConnection.created_at)
        )).scalars().first()
        if not vps:
            await ws.accept()
            await ws.send_json({"type": "error", "message": "No VPS configured"})
            await ws.close()
            return
        vault_key = get_user_vault_key(user)
        password  = decrypt_secret(vault_key, vps.password_enc) if vps.password_enc else ""
        sessions  = get_user_sessions(str(user.id))
        await pty_ws_handler(ws, PTYSession(), vps.host, vps.port, vps.username, password, None, "shell", sessions)


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket, token: str = Query(...)):
    async with SessionLocal() as db:
        user = await ws_get_user(token, db)
        if not user:
            await ws.accept()
            await ws.send_json({"type": "error", "subtype": "auth_expired"})
            await ws.close()
            return
        sessions = get_user_sessions(str(user.id))
        await chat_ws_handler(ws, user, db, sessions)
