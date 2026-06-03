"""
Gubernator — AI Agent Control Panel
FastAPI backend: auth, credential vault, VPS/TUI/Claude WebSocket sessions.
"""
from __future__ import annotations
import os, uuid
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Response, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db, engine, Base
from backend.models import User, VpsConnection, Credential
from backend.auth import (
    hash_password, verify_password, create_access_token, get_current_user
)
from backend.vault import (
    generate_vault_key, encrypt_vault_key, get_user_vault_key,
    encrypt_secret, decrypt_secret
)

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
    password_enc = encrypt_secret(vault_key, body.password)
    conn = VpsConnection(
        user_id=user.id, label=body.label, host=body.host,
        port=body.port, username=body.username, password_enc=password_enc,
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
