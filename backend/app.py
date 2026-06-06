"""
Gubernator — AI Agent Control Panel
FastAPI backend: auth, credential vault, VPS/TUI/Claude WebSocket sessions.
"""
from __future__ import annotations
import asyncio, os, uuid
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Response, WebSocket, WebSocketDisconnect, status, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import select
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

# ── DB init ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
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
class RegisterIn(BaseModel):
    email: str
    password: str

class LoginIn(BaseModel):
    email: str
    password: str

@app.post("/api/auth/register")
async def register(body: RegisterIn, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Email already registered")
    vault_key     = generate_vault_key()
    vault_key_enc = encrypt_vault_key(vault_key)
    user = User(
        email         = body.email,
        password_hash = hash_password(body.password),
        vault_key_enc = vault_key_enc,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {"id": str(user.id), "email": user.email}

@app.post("/api/auth/login")
async def login(body: LoginIn, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user   = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    token = create_access_token(str(user.id))
    # Set httpOnly cookie (web) + return token (API clients)
    response.set_cookie(
        key="gubernator_session", value=token,
        httponly=True, samesite="lax", secure=False,  # secure=True in production
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
