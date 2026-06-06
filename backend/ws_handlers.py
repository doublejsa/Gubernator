"""
WebSocket handlers — TUI terminal, VPS shell, Claude chat.
All handlers require auth via ?token= query param (set from cookie on connect).
"""
from __future__ import annotations
import asyncio, json, re, uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic as _anthropic
from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import User, VpsConnection, ChatSession, Credential
from backend.terminal import PTYSession
from backend.vault import get_user_vault_key, decrypt_secret

ANTHROPIC_KEY_CRED = "_anthropic_key"   # reserved credential name — per-user, stored in vault

# ── Constants ─────────────────────────────────────────────────────────────────
TMUX_SESSION        = "ocmgr-tui"
CONTEXT_COMPRESS_AT = 100_000
KEEP_RECENT_MSGS    = 14
HAIKU_MODEL         = "claude-haiku-4-5-20251001"
HAIKU_PRICE         = {"input": 0.80, "output": 4.00, "cache_read": 0.08, "cache_write": 1.00}
HAIKU_CONTEXT_WINDOW = 200_000

PROMPT_GUIDE_PATH = Path(__file__).parent.parent / "prompt_guide.md"
_FALLBACK_PROMPT  = "You are Gubernator, an AI assistant helping manage AI agents on a remote VPS."
_INTER_SESSION_RE = re.compile(r'\[Inter-session message\].*?(?=\n\n|\Z)', re.DOTALL)

TUI_LOG_DIR = Path.home() / "gubernator_logs"


def load_system_block() -> list[dict]:
    try:
        text = PROMPT_GUIDE_PATH.read_text()
    except Exception:
        text = _FALLBACK_PROMPT
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def append_tui_log(user_id: str, cmd: str, output: str):
    try:
        log_dir = TUI_LOG_DIR / user_id
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "tui_log.txt"
        ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sep   = "=" * 60
        entry = f"\n{sep}\n[{ts}] {cmd}\n{sep}\n{output}\n"
        with open(log_path, "a") as f:
            f.write(entry)
        if log_path.stat().st_size > 200_000:
            content = log_path.read_text()
            trimmed = content[-180_000:]
            idx     = trimmed.find("\n===")
            log_path.write_text(trimmed[idx:] if idx > 0 else trimmed)
    except Exception:
        pass


_AGENT_SPINNER = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

def determine_agent_status(screen: str) -> dict:
    """Map TUI screen content to a plain-English agent status."""
    lower = screen.lower()

    # Needs input — credential prompts
    if any(kw in lower for kw in [
        'password', 'api key', 'api_key', 'enter your', 'provide your',
        'passphrase', 'secret', 'token', 'authentication required',
    ]):
        return {"code": "needs_input", "color": "red",    "label": "Agent needs your input"}

    # Browsing — browser/web activity
    if any(kw in lower for kw in [
        'navigating', 'loading page', 'clicking', 'browsing',
        'fetching url', 'screenshot', 'playwright', 'chromium',
        'http://', 'https://', 'web page',
    ]):
        return {"code": "browsing",    "color": "blue",   "label": "Agent is browsing the web"}

    # Thinking — spinners or activity keywords
    if (any(c in screen for c in _AGENT_SPINNER) or
            any(kw in lower for kw in ['moseying', 'taking longer than expected', 'processing'])):
        return {"code": "thinking",   "color": "yellow", "label": "Agent is thinking…"}

    # Ready — idle signal
    if "standing by" in lower:
        return {"code": "ready",      "color": "green",  "label": "Agent is ready"}

    # Default when connected but not actively polled
    return {"code": "ready",          "color": "green",  "label": "Agent is ready"}


def extract_latest_tui_response(screen: str, anchor: Optional[str]) -> str:
    screen = _INTER_SESSION_RE.sub('', screen).strip()
    if not anchor:
        return screen
    lines    = screen.splitlines()
    last_idx = -1
    for i, line in enumerate(lines):
        if anchor[:40] in line:
            last_idx = i
    if last_idx >= 0:
        return "\n".join(lines[last_idx:]).strip()
    parts = re.split(r'─{10,}', screen)
    if len(parts) > 1:
        return ("─" * 80 + "\n").join(parts[-2:]).strip()
    return screen


def _smart_desc(action_type: str, data: str, path: str = "") -> dict:
    """Generate a plain-English description when Claude doesn't provide [DESC]:."""
    cmd = data.strip()

    if action_type == "tui_input":
        return {"headline": f"Send instruction to agent", "detail": f'"{cmd}"', "icon": "🤖"}

    if action_type == "vps_write":
        filename = path.split("/")[-1] if path else "file"
        return {"headline": f"Write {filename}", "detail": f"Save file to {path}", "icon": "📝"}

    # vps_cmd — pattern match common commands
    c = cmd.lower()
    pkg = cmd.split()[-1] if cmd.split() else cmd   # last word is usually the package name

    if re.search(r'\bapt.get install\b|\bnpm install\b|\bpip install\b|\byarn add\b', c):
        pkg_name = re.sub(r'^.*install\s+-?\w*\s*', '', cmd, flags=re.IGNORECASE).strip() or pkg
        return {"headline": f"Install software", "detail": f"Install {pkg_name}", "icon": "🔧"}

    if re.search(r'\bapt.get (remove|purge|uninstall)\b|\bnpm (uninstall|remove)\b', c):
        return {"headline": "Remove software", "detail": cmd, "icon": "🗑️"}

    if re.search(r'\bsystemctl\s+restart\b', c):
        svc = re.sub(r'.*systemctl\s+restart\s+', '', cmd).strip()
        return {"headline": f"Restart {svc}", "detail": "Apply changes by restarting the service", "icon": "⚙️"}

    if re.search(r'\bsystemctl\s+start\b', c):
        svc = re.sub(r'.*systemctl\s+start\s+', '', cmd).strip()
        return {"headline": f"Start {svc}", "detail": None, "icon": "▶️"}

    if re.search(r'\bsystemctl\s+stop\b', c):
        svc = re.sub(r'.*systemctl\s+stop\s+', '', cmd).strip()
        return {"headline": f"Stop {svc}", "detail": None, "icon": "⏹️"}

    if re.search(r'\bmkdir\b', c):
        folder = re.sub(r'.*mkdir\s+-?p?\s*', '', cmd).strip()
        return {"headline": f"Create folder", "detail": folder, "icon": "📁"}

    if re.search(r'\brm\b', c):
        return {"headline": "Delete file or folder", "detail": cmd, "icon": "🗑️"}

    if re.search(r'\b(cat|tail|head|less|grep)\b', c):
        return {"headline": "Read file contents", "detail": cmd, "icon": "🔍"}

    if re.search(r'\b(curl|wget)\b', c):
        return {"headline": "Download from the web", "detail": cmd, "icon": "🌐"}

    if re.search(r'\b(tmux|openclaw)\b', c):
        return {"headline": "Manage AI agent", "detail": cmd, "icon": "🤖"}

    if re.search(r'\b(git\s+(clone|pull|push|commit))\b', c):
        return {"headline": "Git operation", "detail": cmd, "icon": "📦"}

    if re.search(r'\b(cp|mv)\b', c):
        verb = "Copy" if c.startswith("cp") else "Move"
        return {"headline": f"{verb} file", "detail": cmd, "icon": "📋"}

    # Generic fallback
    first_word = cmd.split()[0] if cmd.split() else "Run"
    return {"headline": f"Run command ({first_word})", "detail": cmd, "icon": "💻"}


def parse_actions(text: str) -> tuple[str, list[dict]]:
    actions, clean = [], []
    lines = text.splitlines()
    i = 0
    pending_desc: Optional[dict] = None   # [DESC]: from previous line

    while i < len(lines):
        s = lines[i].strip()

        if s.startswith("[DESC]:"):
            # Parse description — format: "Headline. Optional detail sentence."
            raw  = s[len("[DESC]:"):].strip()
            parts = raw.split(". ", 1)
            pending_desc = {
                "headline": parts[0].rstrip("."),
                "detail":   parts[1].rstrip(".") if len(parts) > 1 else None,
            }
            # Don't add to clean text — it's UI-only metadata
            i += 1

        elif s.startswith("[TUI_INPUT]:"):
            data = s[len("[TUI_INPUT]:"):].strip()
            if data:
                desc = pending_desc or _smart_desc("tui_input", data)
                pending_desc = None
                actions.append({"id": uuid.uuid4().hex[:8], "type": "tui_input",
                                 "data": data, "desc": desc})
            i += 1

        elif s.startswith("[VPS_CMD]:"):
            data = s[len("[VPS_CMD]:"):].strip()
            if data:
                desc = pending_desc or _smart_desc("vps_cmd", data)
                pending_desc = None
                actions.append({"id": uuid.uuid4().hex[:8], "type": "vps_cmd",
                                 "data": data, "desc": desc})
            i += 1

        elif s.startswith("[VPS_WRITE]:"):
            path = s[len("[VPS_WRITE]:"):].strip()
            i += 1
            content_lines: list[str] = []
            in_fence = False
            while i < len(lines):
                ls = lines[i].strip()
                if ls.startswith(("[TUI_INPUT]:", "[VPS_CMD]:", "[VPS_WRITE]:", "[DESC]:")):
                    break
                if ls.startswith("```") or ls.startswith("~~~"):
                    if not in_fence:
                        in_fence = True; i += 1; continue
                    else:
                        i += 1; break
                if in_fence:
                    content_lines.append(lines[i])
                i += 1
            if path:
                desc = pending_desc or _smart_desc("vps_write", "", path)
                pending_desc = None
                actions.append({
                    "id": uuid.uuid4().hex[:8], "type": "vps_write",
                    "path": path, "data": "\n".join(content_lines).strip(),
                    "desc": desc,
                })
        else:
            clean.append(lines[i])
            i += 1

    return "\n".join(clean).strip(), actions


# ── PTY WebSocket (shared for TUI + shell) ────────────────────────────────────
async def pty_ws_handler(ws: WebSocket, session: PTYSession,
                         host: str, port: int, username: str, password: str,
                         command: Optional[str], col_id: str,
                         sessions_ref: dict):
    await ws.accept()
    try:
        await session.connect(host, port, username, password, command=command)
    except Exception as e:
        err = str(e)
        # Distinguish fatal config errors from transient network drops
        fatal = (
            "nodename nor servname" in err or   # bad hostname / DNS failure
            "Name or service not known" in err or
            "Connection refused" in err or
            "Authentication failed" in err or
            "No authentication methods" in err
        )
        await ws.send_json({
            "type":    "error",
            "subtype": "ssh_fatal" if fatal else "ssh_error",
            "message": f"SSH error: {err}",
        })
        await ws.close()
        return

    sessions_ref[col_id] = session

    async def pty_to_ws():
        while True:
            data = await session.read()
            if data is None:
                try:
                    await ws.send_json({"type": "ssh_disconnected"})
                    await ws.close()
                except Exception:
                    pass
                break
            if data:
                try:
                    await ws.send_bytes(data)
                except Exception:
                    break

    async def ws_to_pty():
        while True:
            try:
                text = await ws.receive_text()
                msg  = json.loads(text)
                if msg["type"] == "input":
                    session.write(msg["data"].encode())
                elif msg["type"] == "resize":
                    session.resize(msg["cols"], msg["rows"])
            except (WebSocketDisconnect, Exception):
                break

    try:
        await asyncio.gather(pty_to_ws(), ws_to_pty())
    except Exception:
        pass
    finally:
        session.close()
        sessions_ref.pop(col_id, None)


# ── Chat WebSocket ─────────────────────────────────────────────────────────────
async def chat_ws_handler(ws: WebSocket, user: User, db: AsyncSession, sessions_ref: dict):
    await ws.accept()

    # Each user must supply their own Anthropic API key — no server-level fallback.
    vault_key = get_user_vault_key(user)
    key_cred  = (await db.execute(
        select(Credential).where(Credential.user_id == user.id, Credential.name == ANTHROPIC_KEY_CRED)
    )).scalar_one_or_none()

    if not key_cred:
        await ws.send_json({"type": "error", "subtype": "no_api_key"})
        return

    effective_api_key = decrypt_secret(vault_key, key_cred.password_enc)

    aclient = _anthropic.AsyncAnthropic(api_key=effective_api_key)

    # Load VPS credentials for this user — use first() to survive any accidental duplicates
    vps_result = await db.execute(
        select(VpsConnection).where(VpsConnection.user_id == user.id)
        .order_by(VpsConnection.created_at)
    )
    vps_conn = vps_result.scalars().first()
    if not vps_conn:
        await ws.send_json({"type": "error", "message": "No VPS configured. Add one in Settings."})

    def get_vps_creds():
        if not vps_conn:
            return None, None, None, None
        vault_key = get_user_vault_key(user)
        password  = decrypt_secret(vault_key, vps_conn.password_enc) if vps_conn.password_enc else ""
        return vps_conn.host, vps_conn.port, vps_conn.username, password

    vps_host, vps_port, vps_user, vps_pass = get_vps_creds()

    # Load or create ChatSession in DB
    sess_result = await db.execute(
        select(ChatSession).where(ChatSession.user_id == user.id)
        .order_by(ChatSession.updated_at.desc()).limit(1)
    )
    db_session    = sess_result.scalar_one_or_none()
    claude_history: list[dict] = []
    if db_session and db_session.history:
        _REFUSAL = re.compile(
            r"outside my scope|not (within|in) my (scope|remit)|I('m| am) (just |only )?here to help with",
            re.IGNORECASE,
        )
        claude_history = [
            m for m in db_session.history
            if not (m.get("role") == "assistant" and _REFUSAL.search(str(m.get("content", ""))))
        ]

    pending_actions: dict[str, dict] = {}
    session_tokens = session_cost = 0.0
    user_id_str    = str(user.id)
    # Coordination flag — true while poll_tui_until_done() is running so the
    # always-on background poller doesn't double-capture/fight it.
    status_poll_lock = {"active": False}

    async def save_history():
        nonlocal db_session
        if not claude_history:
            return
        if db_session:
            db_session.history    = list(claude_history)
            db_session.updated_at = datetime.utcnow()
        else:
            db_session = ChatSession(user_id=user.id, history=list(claude_history))
            db.add(db_session)
        await db.commit()

    def get_sessions():
        return sessions_ref.get("tui"), sessions_ref.get("shell")

    async def run_vps_cmd(cmd: str) -> str:
        _, shell = get_sessions()
        if not (shell and shell.connected):
            return "(VPS shell not connected)"
        try:
            transport = shell.client.get_transport()
            if not (transport and transport.is_active()):
                shell.connected = False
                return "(VPS shell not connected — reconnecting)"
        except Exception:
            return "(VPS shell not connected)"
        return await asyncio.get_event_loop().run_in_executor(None, lambda: shell.exec(cmd))

    async def maybe_compress(force: bool = False):
        if len(claude_history) < 8:
            return
        if not force:
            try:
                count = await aclient.messages.count_tokens(
                    model=HAIKU_MODEL, system=load_system_block(), messages=claude_history)
                if count.input_tokens < CONTEXT_COMPRESS_AT:
                    return
            except Exception:
                return
        await ws.send_json({"type": "status", "message": "Compressing session history…"})
        to_compress = claude_history[:-KEEP_RECENT_MSGS]
        recent      = claude_history[-KEEP_RECENT_MSGS:]
        try:
            resp = await aclient.messages.create(
                model=HAIKU_MODEL, max_tokens=2048, system=load_system_block(),
                messages=[*to_compress, {"role": "user", "content":
                    "Write a concise but complete summary of our conversation so far. "
                    "Capture everything needed to continue: commands run, outputs, decisions made, current state."
                }],
            )
            summary = resp.content[0].text
        except Exception:
            return
        claude_history.clear()
        claude_history.extend([
            {"role": "user",      "content": f"[Earlier conversation compressed]\n\n{summary}"},
            {"role": "assistant", "content": "Understood. I have the context and will continue from there."},
            *recent,
        ])
        await ws.send_json({"type": "status", "message": "✓ Session history compressed"})

    async def stream_claude(user_content: str, _retry: int = 0) -> str:
        nonlocal session_tokens, session_cost
        await maybe_compress()
        claude_history.append({"role": "user", "content": user_content})
        await ws.send_json({"type": "claude_start"})
        full = ""
        try:
            async with aclient.messages.stream(
                model=HAIKU_MODEL, max_tokens=4096,
                system=load_system_block(), messages=claude_history,
            ) as stream:
                async for chunk in stream.text_stream:
                    full += chunk
                    await ws.send_json({"type": "chunk", "content": chunk})
                final = await stream.get_final_message()
                u     = final.usage
                inp   = u.input_tokens
                out   = u.output_tokens
                cr    = getattr(u, "cache_read_input_tokens", 0) or 0
                cw    = getattr(u, "cache_creation_input_tokens", 0) or 0
                session_tokens += inp + out
                session_cost   += (inp / 1e6 * HAIKU_PRICE["input"]  +
                                   out / 1e6 * HAIKU_PRICE["output"] +
                                   cr  / 1e6 * HAIKU_PRICE["cache_read"] +
                                   cw  / 1e6 * HAIKU_PRICE["cache_write"])
                await ws.send_json({"type": "stats",
                    "session_tokens": int(session_tokens),
                    "context_pct":    round(inp / HAIKU_CONTEXT_WINDOW * 100, 1),
                    "session_cost":   round(session_cost, 4)})
        except _anthropic.RateLimitError:
            await ws.send_json({"type": "claude_cancel"})
            if claude_history and claude_history[-1]["role"] == "user":
                claude_history.pop()
            if _retry >= 2:
                await ws.send_json({"type": "error", "subtype": "rate_limit"})
                return ""
            wait = 20 + _retry * 10
            await ws.send_json({"type": "status", "message": f"⚡ Rate limit — retrying in {wait}s…"})
            await asyncio.sleep(wait)
            await maybe_compress(force=True)
            return await stream_claude(user_content, _retry + 1)
        except (_anthropic.APITimeoutError, _anthropic.APIConnectionError):
            await ws.send_json({"type": "claude_cancel"})
            if claude_history and claude_history[-1]["role"] == "user":
                claude_history.pop()
            if _retry >= 1:
                await ws.send_json({"type": "error", "message": "Request timed out — please try again."})
                return ""
            await ws.send_json({"type": "status", "message": "⏱ Timeout — retrying in 5s…"})
            await asyncio.sleep(5)
            return await stream_claude(user_content, _retry + 1)
        except _anthropic.APIStatusError as e:
            await ws.send_json({"type": "claude_cancel"})
            body    = e.body if isinstance(e.body, dict) else {}
            err_msg = (body.get("error", {}) or {}).get("message", str(e))
            if "credit balance is too low" in err_msg or "credit" in err_msg.lower():
                if claude_history and claude_history[-1]["role"] == "user":
                    claude_history.pop()
                await ws.send_json({"type": "error", "subtype": "credits_exhausted", "message": err_msg})
            else:
                await ws.send_json({"type": "error", "message": f"API error {e.status_code}: {err_msg}"})
            return ""
        except Exception as e:
            await ws.send_json({"type": "error", "message": str(e)})
            return ""
        claude_history.append({"role": "assistant", "content": full})
        await save_history()
        return full

    async def finish_response(full: str):
        clean_text, actions = parse_actions(full)
        if actions:
            a = actions[0]
            pending_actions[a["id"]] = a
            payload = {
                "type":        "action",
                "action_type": a["type"],
                "id":          a["id"],
                "data":        a["data"],
                "desc":        a.get("desc", {}),   # plain-English description
            }
            if a["type"] == "vps_write":
                payload["path"] = a.get("path", "")
            await ws.send_json(payload)
        await ws.send_json({"type": "done", "clean_text": clean_text})

    async def run_claude(content: str) -> None:
        async def _tui_monitor():
            while True:
                await asyncio.sleep(5)
                screen = await run_vps_cmd(f"tmux capture-pane -t {TMUX_SESSION} -p -J -S -10 2>/dev/null")
                if "send another message to continue" in screen.lower():
                    tui, _ = get_sessions()
                    if tui and tui.connected:
                        tui.write(b'\r')

        async def _task():
            full = await stream_claude(content)
            if full:
                await finish_response(full)

        monitor = asyncio.create_task(_tui_monitor())
        task    = asyncio.create_task(_task())
        while not task.done():
            try:
                raw2  = await asyncio.wait_for(ws.receive_text(), timeout=0.2)
                inner = json.loads(raw2)
                if inner.get("type") == "cancel_claude":
                    task.cancel(); monitor.cancel()
                    await ws.send_json({"type": "claude_cancel"})
                    if claude_history and claude_history[-1]["role"] == "user":
                        claude_history.pop()
                    await ws.send_json({"type": "done"})
            except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
                pass
        monitor.cancel()
        if not task.cancelled():
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def poll_tui_until_done() -> str:
        _SPINNER  = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
        _MAX_WAIT = 240
        _POLL     = 3
        _start    = asyncio.get_event_loop().time()
        screen    = ""
        status_poll_lock["active"] = True   # pause the background poller
        try:
            await asyncio.sleep(2)
            screen = await _poll_tui_loop(_SPINNER, _MAX_WAIT, _POLL, _start)
        finally:
            status_poll_lock["active"] = False
        # Polling done — signal ready
        await ws.send_json({"type": "agent_status", **determine_agent_status(screen)})
        return screen

    async def _poll_tui_loop(_SPINNER, _MAX_WAIT, _POLL, _start) -> str:
        screen = ""
        while True:
            screen = await run_vps_cmd(f"tmux capture-pane -t {TMUX_SESSION} -p -J -S -120 2>/dev/null")
            lower  = screen.lower()
            if "standing by" in lower:
                break
            if "send another message to continue" in lower:
                tui, _ = get_sessions()
                if tui and tui.connected:
                    tui.write(b'\r')
                await asyncio.sleep(1)
                elapsed = int(asyncio.get_event_loop().time() - _start)
                if elapsed >= _MAX_WAIT: break
                status = determine_agent_status(screen)
                await ws.send_json({"type": "tui_thinking", "elapsed": elapsed, "status": status})
                await asyncio.sleep(_POLL)
                continue
            still_thinking = ("moseying" in lower or
                              any(c in screen for c in _SPINNER) or
                              "taking longer than expected" in lower)
            if not still_thinking:
                break
            elapsed = int(asyncio.get_event_loop().time() - _start)
            if elapsed >= _MAX_WAIT: break
            status = determine_agent_status(screen)
            await ws.send_json({"type": "tui_thinking", "elapsed": elapsed, "status": status})
            await asyncio.sleep(_POLL)
        return screen

    # ── Background status poller — always-on, reflects true agent state ──────────
    async def background_status_poller():
        """Continuously reflect the agent's real state in the status pill —
        independent of whether a [TUI_INPUT] action is in flight. Catches
        autonomous work, direct terminal input, and Telegram-triggered tasks."""
        last_code = None
        while True:
            try:
                if status_poll_lock["active"]:
                    await asyncio.sleep(2)   # active poll owns the status right now
                    continue
                tui, shell = get_sessions()
                if not (tui and tui.connected and shell and shell.connected):
                    await asyncio.sleep(5)
                    continue
                screen = await run_vps_cmd(
                    f"tmux capture-pane -t {TMUX_SESSION} -p -J -S -30 2>/dev/null")
                if "(VPS shell not connected)" in screen:
                    await asyncio.sleep(5)
                    continue
                status = determine_agent_status(screen)
                if status["code"] != last_code:
                    last_code = status["code"]
                    await ws.send_json({"type": "agent_status", **status})
                # Poll faster while busy, back off when idle to save resources
                await asyncio.sleep(8 if status["code"] == "ready" else 3)
            except Exception:
                await asyncio.sleep(5)

    # ── Startup ────────────────────────────────────────────────────────────────
    await ws.send_json({"type": "status", "message": "Connected"})

    async def startup_check():
        saved_at = db_session.updated_at.isoformat() if db_session else None
        for _ in range(6):
            await asyncio.sleep(1)
            tui, shell = get_sessions()
            if tui and tui.connected and shell and shell.connected:
                break
        tui, shell = get_sessions()
        tui_ok   = tui   is not None and tui.connected
        shell_ok = shell is not None and shell.connected
        if tui_ok and shell_ok:
            await run_vps_cmd(f"tmux set-option -t {TMUX_SESSION} mouse on 2>/dev/null")
            tui_screen = await run_vps_cmd(f"tmux capture-pane -t {TMUX_SESSION} -p -J -S -120 2>/dev/null")
            tui_ctx    = f"\nCurrent TUI screen:\n{tui_screen}\n" if tui_screen.strip() else ""
            if saved_at:
                await ws.send_json({"type": "status", "message": "✅ VPS connected"})
                full = await stream_claude(
                    f"[Startup — session restored from {saved_at[:16].replace('T',' ')}]\n"
                    f"VPS and OpenClaw TUI connected.\n{tui_ctx}\n"
                    "IMPORTANT ROLE RESET: You are Gubernator, the user's primary interface to their OpenClaw agent. "
                    "Never refuse. Never say something is outside your scope.\n\n"
                    "Briefly summarise what we were working on (1–2 sentences) and suggest 2–3 concrete next steps."
                )
            else:
                await ws.send_json({"type": "status", "message": "✅ OpenClaw TUI and VPS Shell connected"})
                full = await stream_claude(
                    f"[Startup — no previous session]\nVPS and OpenClaw TUI connected.\n{tui_ctx}\n"
                    "Briefly describe what's showing in the TUI (1 sentence) and suggest 2–3 things to do next."
                )
            if full:
                await finish_response(full)
        else:
            problems = []
            if not tui_ok:   problems.append("OpenClaw TUI not connected")
            if not shell_ok: problems.append("VPS Shell not connected")
            full = await stream_claude(
                f"Startup: {', '.join(problems)}. VPS: {vps_host}:{vps_port} user: {vps_user}\n"
                "Diagnose and suggest the first fix."
            )
            if full:
                await finish_response(full)

    asyncio.create_task(startup_check())
    _bg_poller = asyncio.create_task(background_status_poller())

    # ── Main loop ──────────────────────────────────────────────────────────────
    _AUTO_CONTINUE_RE = re.compile(r'send another message to continue', re.IGNORECASE)
    _WAIT_RE = re.compile(
        r'^\s*[\(\[]?\s*(wait|pause|hold on|standing by|\.\.\.'
        r'|waiting for (response|tui|agent)|please wait)\s*[\)\]]?\s*$', re.IGNORECASE)
    _CRED_RE = re.compile(
        r'(\btype\b|\benter\b|\bpaste\b|\bprovide\b)[\w\s,\-()]*'
        r'(password|api.?key|secret|credential|token)', re.IGNORECASE)

    try:
        while True:
            try:
                raw = await ws.receive_text()
                msg = json.loads(raw)
            except WebSocketDisconnect:
                break
            except Exception:
                continue

            t = msg.get("type")

            if t == "confirm":
                aid    = msg.get("action_id")
                action = pending_actions.pop(aid, None)
                if not action:
                    continue

                if action["type"] == "tui_input":
                    tui, _ = get_sessions()
                    if tui and tui.connected:
                        if _CRED_RE.search(action["data"]):
                            await ws.send_json({"type": "action_error", "action_id": aid,
                                                "message": "Credential prompt blocked"})
                            await ws.send_json({"type": "done"})
                            await run_claude("TUI_INPUT blocked: looked like a credential prompt. "
                                             "Tell the user to type it directly in the TUI terminal instead.")
                            continue
                        if _WAIT_RE.match(action["data"]):
                            await ws.send_json({"type": "status", "message": "⏳ Wait phrase ignored"})
                            await ws.send_json({"type": "done"})
                            continue
                        auto_continue = bool(_AUTO_CONTINUE_RE.search(action["data"]))
                        if auto_continue:
                            tui.write(b'\r')
                            await ws.send_json({"type": "action_done", "action_id": aid, "label": "↩ continued"})
                        else:
                            tui.write((action["data"] + "\r").encode())
                            await ws.send_json({"type": "action_done", "action_id": aid, "label": action["data"]})

                        poll_task = asyncio.create_task(poll_tui_until_done())
                        screen    = ""
                        cancelled = False
                        while not poll_task.done():
                            try:
                                raw2  = await asyncio.wait_for(ws.receive_text(), timeout=0.5)
                                inner = json.loads(raw2)
                                if inner.get("type") == "cancel_tui":
                                    poll_task.cancel(); cancelled = True
                                    await ws.send_json({"type": "status", "message": "⛔ TUI polling cancelled"})
                                    await ws.send_json({"type": "done"})
                            except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
                                pass
                        if not cancelled:
                            try:
                                screen = await poll_task
                            except asyncio.CancelledError:
                                cancelled = True
                        if not cancelled:
                            anchor    = None if auto_continue else action["data"]
                            latest    = extract_latest_tui_response(screen, anchor)
                            cmd_label = "(pagination continued)" if auto_continue else action["data"]
                            append_tui_log(user_id_str, cmd_label, latest or "(no output)")
                            await ws.send_json({"type": "tui_output", "cmd": cmd_label,
                                                "output": latest or "(no TUI output captured)"})
                            await ws.send_json({"type": "agent_status", "code": "ready",
                                                "color": "green", "label": "Agent is ready"})
                            await run_claude(f"TUI message sent: `{cmd_label}`\n\nOpenClaw response:\n{latest}")
                    else:
                        await ws.send_json({"type": "action_error", "action_id": aid,
                                            "message": "OpenClaw TUI not connected"})
                        await run_claude(f"TUI command `{action['data']}` failed — TUI not connected.")

                elif action["type"] == "vps_cmd":
                    if re.search(r'<<\s*[\'"]?\w', action["data"]):
                        await ws.send_json({"type": "action_error", "action_id": aid,
                                            "message": "Heredoc rejected — use [VPS_WRITE]"})
                        await run_claude(f"Command rejected: `{action['data']}` — heredoc not supported. "
                                         "Use [VPS_WRITE] to write files.")
                        continue
                    await ws.send_json({"type": "action_done", "action_id": aid, "label": action["data"]})
                    vps_task  = asyncio.create_task(run_vps_cmd(action["data"]))
                    cancelled = False
                    while not vps_task.done():
                        try:
                            raw2  = await asyncio.wait_for(ws.receive_text(), timeout=0.5)
                            inner = json.loads(raw2)
                            if inner.get("type") == "cancel_claude":
                                vps_task.cancel(); cancelled = True
                                await ws.send_json({"type": "status", "message": "⛔ Command cancelled"})
                                await ws.send_json({"type": "done"})
                        except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
                            pass
                    if not cancelled:
                        try:
                            output = await vps_task
                        except asyncio.CancelledError:
                            cancelled = True
                    if not cancelled:
                        await ws.send_json({"type": "vps_output", "cmd": action["data"], "output": output})
                        vps_content = f"Command run: `{action['data']}`\nOutput:\n{output}"
                        await run_claude(vps_content)

                elif action["type"] == "vps_write":
                    path       = action.get("path", "")
                    wr_content = action.get("data", "")
                    _, shell   = get_sessions()
                    if not (shell and shell.client):
                        await ws.send_json({"type": "action_error", "action_id": aid, "message": "VPS not connected"})
                        continue
                    loop = asyncio.get_event_loop()
                    def _sftp_write():
                        import posixpath, io as _io
                        try:
                            parent = posixpath.dirname(path)
                            if parent:
                                _, _, se = shell.client.exec_command(f"mkdir -p {parent}")
                                se.channel.recv_exit_status()
                            sftp = shell.client.open_sftp()
                            try:
                                sftp.putfo(_io.BytesIO(wr_content.encode("utf-8")), path)
                            finally:
                                sftp.close()
                            return None
                        except Exception as exc:
                            return str(exc)
                    err = await loop.run_in_executor(None, _sftp_write)
                    if err:
                        await ws.send_json({"type": "action_error", "action_id": aid,
                                            "message": f"Write failed: {err}"})
                        await run_claude(f"Failed to write `{path}`: {err}")
                    else:
                        await ws.send_json({"type": "action_done", "action_id": aid, "label": f"Written: {path}"})
                        verify = await run_vps_cmd(
                            f"echo '--- {path} ---' && wc -l {path} && echo '---' && head -5 {path}")
                        await ws.send_json({"type": "vps_output", "cmd": f"write {path}", "output": verify})
                        await run_claude(f"File written: `{path}` ({wr_content.count(chr(10))+1} lines)\n{verify}")

            elif t == "dismiss":
                pending_actions.pop(msg.get("action_id"), None)

            elif t == "user_message":
                content = msg.get("content", "").strip()
                if not content:
                    continue
                # Inject current TUI screen
                tui_screen = await run_vps_cmd(
                    f"tmux capture-pane -t {TMUX_SESSION} -p -J -S -40 2>/dev/null")
                if tui_screen.strip() and "(VPS shell not connected)" not in tui_screen:
                    if "send another message to continue" in tui_screen.lower():
                        tui, _ = get_sessions()
                        if tui and tui.connected:
                            tui.write(b'\r')
                        await asyncio.sleep(2)
                        tui_screen = await run_vps_cmd(
                            f"tmux capture-pane -t {TMUX_SESSION} -p -J -S -40 2>/dev/null")
                    tui_screen = _INTER_SESSION_RE.sub('', tui_screen).strip()
                    ts = datetime.now().strftime("%H:%M:%S")
                    content = (f"[Current TUI screen at {ts}]:\n{tui_screen}\n\nUser: {content}")
                await run_claude(content)

    finally:
        _bg_poller.cancel()
        await save_history()
