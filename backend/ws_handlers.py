"""
WebSocket handlers — TUI terminal, VPS shell, Claude chat.
All handlers require auth via ?token= query param (set from cookie on connect).
"""
from __future__ import annotations
import asyncio, json, re, uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional


CIRCUIT_BREAKER_NOTE = (
    "[GUBERNATOR CIRCUIT-BREAKER] You have repeated the same approach several times "
    "without success — the system detected a loop. STOP. Do NOT retry that command again. "
    "Step back and do ONE of the following:\n"
    "1. If this is a repeatable, deterministic procedure (deploy, backup, scheduled job, "
    "anything with fixed steps + secrets): switch to the 'Build a tool when stuck' playbook — "
    "discover the specifics ONCE and [REMEMBER] them, set up auth ONCE, write a single "
    "idempotent script via [VPS_WRITE] with clean JSON output, wrap it in a thin SKILL.md, "
    "register it, and [REMEMBER] how to run it.\n"
    "2. If you're missing information, ask the user ONE specific question.\n"
    "Tell the user in one sentence that you're changing approach because the previous one "
    "kept failing."
)

def _action_signature(a: dict) -> str:
    """Normalised 'shape' of an action so repeats are detectable across loops."""
    t = a.get("type")
    d = (a.get("data") or "").strip().lower()
    if t == "vps_cmd":
        return "cmd:" + " ".join(d.split()[:3])
    if t == "tui_input":
        return "tui:" + " ".join(d.split()[:6])
    if t == "vps_write":
        return "write:" + (a.get("path") or "")[:50]
    return str(t)

from backend.llm import (LLMClient, PROVIDERS as LLM_PROVIDERS, normalize_provider,
                         model_info, LLMRateLimit, LLMTimeout, LLMCredits, LLMStatusError)
from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import User, VpsConnection, ChatSession, Credential, Task, MemoryFact, AuditLog
from backend.terminal import PTYSession
from backend.vault import get_user_vault_key, decrypt_secret, encrypt_secret
from backend.embeddings import embed
from backend.audit import redact as _redact
from backend.agents import agent_cfg

# ── Constants ─────────────────────────────────────────────────────────────────
TMUX_SESSION        = "ocmgr-tui"
CONTEXT_COMPRESS_AT = 100_000
KEEP_RECENT_MSGS    = 14

PROMPT_GUIDE_PATH = Path(__file__).parent.parent / "prompt_guide.md"
_FALLBACK_PROMPT  = "You are Gubernator, an AI assistant helping manage AI agents on a remote VPS."
_INTER_SESSION_RE = re.compile(r'\[Inter-session message\].*?(?=\n\n|\Z)', re.DOTALL)

# Claude's reply language that means "I'm waiting on the agent to finish"
_AWAITING_RE = re.compile(
    r'waiting for (a |the |it |its )?(response|reply|result|agent|it to|the agent)'
    r'|still (processing|working|running|rendering)'
    r'|let me wait|i\'?ll wait|wait for it to'
    r'|screen is (still )?rendering'
    r'|once it(\'?s| has)? (done|finished|complete)'
    r'|check(ing)? back'
    r'|when it (finishes|completes|is done)',
    re.IGNORECASE,
)

TUI_LOG_DIR = Path.home() / "gubernator_logs"

# Read-only VPS profiling probe — captures capabilities, never secret values.
# Built per agent framework (OpenClaw vs Hermes paths and CLIs differ).
def build_probe(agent: dict) -> str:
    skills = ("openclaw skills 2>/dev/null | head -120" if agent.get("skills_cli")
              else f"ls {agent['config_dir']}/skills/ 2>/dev/null")
    return f"""
echo '### OS'; (cat /etc/os-release 2>/dev/null | grep PRETTY_NAME); uname -sr
echo '### Agent'; {agent['version_cmd']} 2>/dev/null || echo '{agent['label']}: not installed'
echo '### Skills'; {skills}
echo '### Runtimes'; node --version 2>/dev/null; python3 --version 2>/dev/null
echo '### Tools'; for t in playwright chromium google-chrome docker git nginx apache2 mysql psql ftp lftp rsync; do command -v "$t" >/dev/null 2>&1 && echo "$t: yes"; done
echo '### ConfigFiles'; ls -1 {agent['config_dir']}/ 2>/dev/null
echo '### Credentials(names only)'; ls -1 {agent['secrets_dir']}/ 2>/dev/null
echo '### EnvVarNames(names only)'; grep -oE '^[A-Z_0-9]+=' {agent['config_dir']}/.env 2>/dev/null | tr -d '='
"""

# Match a heredoc operator (<< or <<-), but never a here-string (<<<).
_HEREDOC_RE = re.compile(r'(?<!<)<<(?!<)-?\s*([\'"]?)([A-Za-z_]\w*)\1')

def parse_heredoc(data: str) -> Optional[dict]:
    """If `data` is a single clean heredoc command, return how to run it without
    feeding a raw heredoc to the live shell (which the user shouldn't see, and
    which can hang a PTY). Returns None when there's no heredoc, or when it can't
    be parsed safely (caller falls back to steering Claude to write a file).

      {"mode": "write", "path": ..., "body": ...}   # cat > file / tee file
      {"mode": "run",   "cmd": ..., "tmp": ..., "body": ...}  # python3/bash/… <<EOF
    """
    m = _HEREDOC_RE.search(data)
    if not m:
        return None
    delim = m.group(2)
    dash  = data[m.start():m.start() + 3].startswith("<<-")
    lines = data.splitlines()
    # Locate the line holding the heredoc operator.
    offset, op_line = 0, None
    for i, ln in enumerate(lines):
        if offset <= m.start() <= offset + len(ln):
            op_line = i
            break
        offset += len(ln) + 1
    if op_line is None:
        return None
    # Find the closing delimiter line.
    close_idx = None
    for j in range(op_line + 1, len(lines)):
        if (lines[j].strip() if dash else lines[j]) == delim:
            close_idx = j
            break
    if close_idx is None:
        return None
    # Anything after the closing delimiter → bail (let the safe fallback handle it).
    if "\n".join(lines[close_idx + 1:]).strip():
        return None
    body = "\n".join(lines[op_line + 1:close_idx])
    if body and not body.endswith("\n"):
        body += "\n"
    prefix = data[:m.start()].strip()
    wm = re.match(r'^(?:cat\s*>>?|tee(?:\s+-a)?)\s+(.+)$', prefix)
    if wm:
        return {"mode": "write", "path": wm.group(1).strip().strip('\'"'), "body": body}
    toks = [t for t in prefix.split() if t not in ("-", "-s")]
    if not toks:
        return None
    tmp = f"/tmp/gub_{uuid.uuid4().hex[:10]}"
    return {"mode": "run", "cmd": f"{' '.join(toks)} {tmp}", "tmp": tmp, "body": body}


VPS_PROFILE_PROMPT = (
    "[VPS PROFILE SCAN] The user just connected this VPS. Below is a read-only scan of "
    "what's installed and configured on it. Distill it into durable capability facts using "
    "[REMEMBER:vps_profile] tags — one tag per meaningful capability or installed component.\n\n"
    "Capture: {label} version; what the agent can do (e.g. can_browse_web if Playwright/Chromium "
    "present, has_docker, has_mysql); language runtimes; and which config files / secrets / env "
    "vars EXIST (names only — never values).\n\n"
    "CRITICAL — the installed agent skills are the most important fact. Record the COMPLETE list "
    "of ready/available skills, every single one BY NAME, comma-separated, in a single fact keyed "
    "'skills_ready'. Do NOT abbreviate, summarise, or write 'and N others' — list them all in full. "
    "Separately record the count as 'skills_count' (e.g. '20 ready of 66 total').\n\n"
    "Do NOT record transient state (free memory, disk usage, running processes).\n"
    "Keep other facts concise. Use clear snake_case keys.\n\n"
    "After the [REMEMBER] tags, give the user a friendly 2–3 sentence plain-English summary of "
    "what their agent can do, based on what you found. If {label} is NOT installed on this VPS, "
    "say so and offer to install {label} (never a different agent framework).\n\n"
    "Scan output:\n{scan}"
)


def load_system_block(memory_text: str = "") -> list[dict]:
    """System prompt = prompt guide (always cached) + the user's durable memory
    facts as a second cached block. Memory lives here, NOT in the message history,
    so it is never lost to compression and is present on every API call."""
    try:
        guide = PROMPT_GUIDE_PATH.read_text()
    except Exception:
        guide = _FALLBACK_PROMPT
    blocks = [{"type": "text", "text": guide, "cache_control": {"type": "ephemeral"}}]
    if memory_text:
        blocks.append({"type": "text", "text": memory_text, "cache_control": {"type": "ephemeral"}})
    return blocks


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

# Words that mean OpenClaw is actively working (this TUI version says "streaming")
_BUSY_KEYWORDS = (
    'streaming', 'moseying', 'taking longer than expected',
    'processing', 'esc to interrupt', 'thinking',
)

# OpenClaw's persistent status bar, e.g.
#   "⋮ moseying… • 37463m 20s | connected — agent main | session main | tokens 42k/200k (21%)"
# The gerund + counter live there even when the agent is idle (the counter is
# session uptime, not work time) — so the footer alone must never mean "busy"
# unless its elapsed counter is plausible for a single response.
_FOOTER_LINE_RE    = re.compile(
    r'(?m)^.*(tokens\s+\d+[km]?/\d+[km]?'
    r'|agent\s+\w+\s*\|\s*session'
    r'|•\s*\d+m(?:\s+\d+s)?\s*\|\s*connected).*$')
_FOOTER_ELAPSED_RE = re.compile(r'(\d+)m(?:\s+\d+s)?\s*\|')
_STALE_BUSY_MINUTES = 30   # no single response runs this long — counter is uptime

def is_agent_busy(screen: str) -> bool:
    """True if the agent appears to be actively working."""
    body  = _FOOTER_LINE_RE.sub('', screen)
    lower = body.lower()
    if any(c in body for c in _AGENT_SPINNER) or any(kw in lower for kw in _BUSY_KEYWORDS):
        return True
    # Busy words on the footer line count only while its counter is plausible.
    for line in screen.splitlines():
        ll = line.lower()
        if any(kw in ll for kw in _BUSY_KEYWORDS):
            m = _FOOTER_ELAPSED_RE.search(line)
            if m and int(m.group(1)) >= _STALE_BUSY_MINUTES:
                continue
            return True
    return False

_QUESTION_HINTS = ('which would you', 'would you like', 'do you want', 'let me know',
                   'you provide', 'can you', 'your call', '(y/n)', 'yes/no')

def agent_awaits_reply(screen: str) -> bool:
    """True if the (idle) agent's last words look like a question to the user."""
    body = _FOOTER_LINE_RE.sub('', screen)
    tail = [l.strip() for l in body.splitlines()
            if l.strip() and not re.fullmatch(r'[\s│┃|>─━┌┐└┘╭╮╰╯·⋮_-]+', l.strip())]
    # A short typed-but-unsent word (e.g. "stop") can sit below the question,
    # so scan the last few visible lines rather than only the very last one.
    for line in tail[-3:]:
        l = line.lower().rstrip(' )"\'.]…')
        if l.endswith('?') or any(h in l for h in _QUESTION_HINTS):
            return True
    return False

def determine_agent_status(screen: str) -> dict:
    """Map TUI screen content to a plain-English agent status."""
    lower = screen.lower()
    busy  = is_agent_busy(screen)

    # Needs input — credential prompts (only when NOT actively streaming;
    # avoids false reds when the agent merely mentions 'password' mid-thought)
    if not busy and any(kw in lower for kw in [
        'enter your', 'provide your', 'paste your', 'type your',
        'authentication required', 'enter the password', 'enter password',
    ]):
        return {"code": "needs_input", "color": "red",    "label": "Agent needs your input"}

    # Needs input — the agent finished by asking the user a question
    if not busy and agent_awaits_reply(screen):
        return {"code": "needs_input", "color": "red",    "label": "Agent asked you a question"}

    # Browsing — browser/web activity (more specific than generic 'thinking')
    if any(kw in lower for kw in [
        'navigating', 'loading page', 'clicking', 'browsing the',
        'fetching url', 'taking screenshot', 'playwright', 'chromium',
    ]):
        return {"code": "browsing",    "color": "blue",   "label": "Agent is browsing the web"}

    # Thinking — busy signals (spinner, 'streaming', etc.)
    if busy:
        return {"code": "thinking",   "color": "yellow", "label": "Agent is thinking…"}

    # Ready — explicit idle signals
    if "idle" in lower or "standing by" in lower:
        return {"code": "ready",      "color": "green",  "label": "Agent is ready"}

    # Default when connected but quiet
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


def extract_knowledge(text: str) -> tuple[str, list[dict], list[dict]]:
    """Pull [REMEMBER], [TASK_START], [TASK_DONE], [TASK_FAIL] tags out of
    Claude's reply. These are auto-persisted (no user confirmation) and
    stripped from the visible chat text. Returns (clean_text, facts, tasks)."""
    facts, tasks, clean = [], [], []
    for line in text.splitlines():
        s = line.strip()

        if s.startswith("[REMEMBER"):
            # [REMEMBER]: key = value   OR   [REMEMBER:category]: key = value
            m = re.match(r'\[REMEMBER(?::([\w\-]+))?\]:\s*(.+?)\s*=\s*(.+)', s)
            if m:
                facts.append({"category": (m.group(1) or "general").strip(),
                              "key": m.group(2).strip(), "value": m.group(3).strip()})
            continue

        if s.startswith("[TASK_START]:"):
            title = s[len("[TASK_START]:"):].strip()
            if title:
                tasks.append({"action": "start", "title": title, "outcome": ""})
            continue

        if s.startswith("[TASK_DONE]:"):
            body = s[len("[TASK_DONE]:"):].strip()
            title, _, outcome = body.partition("|")
            tasks.append({"action": "done", "title": title.strip(), "outcome": outcome.strip()})
            continue

        if s.startswith("[TASK_FAIL]:"):
            body = s[len("[TASK_FAIL]:"):].strip()
            title, _, outcome = body.partition("|")
            tasks.append({"action": "fail", "title": title.strip(), "outcome": outcome.strip()})
            continue

        clean.append(line)

    return "\n".join(clean).strip(), facts, tasks


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
async def chat_ws_handler(ws: WebSocket, user: User, db: AsyncSession, sessions_ref: dict,
                          vps_id: Optional[str] = None):
    await ws.accept()

    # Each user must supply their own API key for their chosen provider —
    # no server-level fallback. Claude is the tuned default; others experimental.
    vault_key    = get_user_vault_key(user)
    llm_provider = normalize_provider(getattr(user, "llm_provider", None))
    llm_cfg      = LLM_PROVIDERS[llm_provider]
    key_cred  = (await db.execute(
        select(Credential).where(Credential.user_id == user.id, Credential.name == llm_cfg["cred"])
    )).scalar_one_or_none()

    if not key_cred:
        await ws.send_json({"type": "error", "subtype": "no_api_key",
                            "provider": llm_cfg["label"]})
        return

    effective_api_key = decrypt_secret(vault_key, key_cred.password_enc)

    try:
        llm = LLMClient(llm_provider, effective_api_key, getattr(user, "llm_model", None))
    except RuntimeError as e:
        await ws.send_json({"type": "error", "message": str(e)})
        return

    # Resolve the active bot (VPS) — by id when given, else the user's first
    vps_q = select(VpsConnection).where(VpsConnection.user_id == user.id)
    if vps_id:
        try:
            vps_q = vps_q.where(VpsConnection.id == uuid.UUID(vps_id))
        except ValueError:
            vps_q = vps_q.order_by(VpsConnection.created_at)
    else:
        vps_q = vps_q.order_by(VpsConnection.created_at)
    vps_conn = (await db.execute(vps_q)).scalars().first()
    if not vps_conn:
        await ws.send_json({"type": "error", "message": "No VPS configured. Add one in Settings."})
    active_vps_id = vps_conn.id if vps_conn else None   # scopes chat/memory/tasks/audit to this bot
    agent_info = agent_cfg(getattr(vps_conn, "agent_type", None) if vps_conn else None)
    agent_block = (
        f"## About this bot\n"
        f"The agent on this VPS is **{agent_info['label']}** {agent_info['icon']} — NOT any other framework.\n"
        f"- Config dir: {agent_info['config_dir']}\n"
        f"- Credentials folder (Vault 'Sync to VPS' writes here): {agent_info['secrets_dir']}\n"
        f"- Restart the agent: `{agent_info['restart_cmd']}`\n"
        f"- If {agent_info['label']} is not installed yet, offer to install it: {agent_info['install_hint']}\n"
        f"Wherever the general guide says OpenClaw paths, use this bot's paths above instead."
    )

    def get_vps_creds():
        if not vps_conn:
            return None, None, None, None
        vault_key = get_user_vault_key(user)
        password  = decrypt_secret(vault_key, vps_conn.password_enc) if vps_conn.password_enc else ""
        return vps_conn.host, vps_conn.port, vps_conn.username, password

    vps_host, vps_port, vps_user, vps_pass = get_vps_creds()

    # Load or create ChatSession in DB — one conversation thread per bot
    sess_result = await db.execute(
        select(ChatSession).where(ChatSession.user_id == user.id,
                                  ChatSession.vps_id == active_vps_id)
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
    # Loop / thrash detection state
    recent_sigs    = deque(maxlen=10)   # normalised signatures of recent actions
    error_streak   = 0                  # consecutive failed actions
    thrash_pending = False              # inject circuit-breaker on next Claude turn
    # Coordination flag — true while poll_tui_until_done() is running so the
    # always-on background poller doesn't double-capture/fight it.
    status_poll_lock = {"active": False}
    # Durable memory facts rendered for the system prompt (refreshed on change)
    memory_block_text = ""

    async def refresh_memory_block():
        """Re-render the user's memory facts into the system-prompt block.
        Called at connect and whenever facts change."""
        nonlocal memory_block_text
        facts = (await db.execute(
            select(MemoryFact).where(MemoryFact.user_id == user.id,
                                     MemoryFact.vps_id == active_vps_id)
            .order_by(MemoryFact.category, MemoryFact.key)
        )).scalars().all()
        if not facts:
            memory_block_text = agent_block
            return
        lines = [f"- {f.key}: {f.value}" + (f"  [{f.category}]" if f.category != "general" else "")
                 for f in facts]
        memory_block_text = (
            agent_block + "\n\n"
            "## What you remember about this user's setup\n"
            "Durable facts carried across all sessions. Treat as authoritative unless "
            "the user corrects you. If something here is now wrong, update it with [REMEMBER].\n\n"
            + "\n".join(lines)
        )

    async def has_vps_profile() -> bool:
        row = (await db.execute(
            select(MemoryFact).where(MemoryFact.user_id == user.id,
                                     MemoryFact.vps_id == active_vps_id,
                                     MemoryFact.category == "vps_profile").limit(1)
        )).scalar_one_or_none()
        return row is not None

    async def clear_vps_profile():
        await db.execute(delete(MemoryFact).where(
            MemoryFact.user_id == user.id, MemoryFact.vps_id == active_vps_id,
            MemoryFact.category == "vps_profile"))
        await db.commit()
        await refresh_memory_block()

    async def profile_vps():
        """Read-only scan of the VPS → Claude distils into vps_profile memory facts."""
        _, shell = get_sessions()
        if not (shell and shell.connected):
            return
        await ws.send_json({"type": "status",
                            "message": "🔍 Profiling your VPS — checking what's installed…"})
        scan = await run_vps_cmd(build_probe(agent_info))
        if not scan or "(VPS shell not connected)" in scan:
            await ws.send_json({"type": "status", "message": "⚠ Couldn't scan the VPS — skipping profile"})
            return
        full = await stream_claude(VPS_PROFILE_PROMPT.format(scan=scan, label=agent_info['label']))
        if full:
            await finish_response(full)   # persists [REMEMBER:vps_profile] facts

    async def save_history():
        nonlocal db_session
        if not claude_history:
            return
        if db_session:
            db_session.history    = list(claude_history)
            db_session.updated_at = datetime.utcnow()
        else:
            db_session = ChatSession(user_id=user.id, vps_id=active_vps_id, history=list(claude_history))
            db.add(db_session)
        await db.commit()

    async def persist_facts(facts: list[dict]):
        """Upsert memory facts by key, with embeddings. Notify frontend."""
        if not facts:
            return
        for f in facts:
            existing = (await db.execute(
                select(MemoryFact).where(MemoryFact.user_id == user.id,
                                         MemoryFact.vps_id == active_vps_id, MemoryFact.key == f["key"])
            )).scalar_one_or_none()
            vec = await embed(f"{f['key']}: {f['value']}")
            if existing:
                existing.value    = f["value"]
                existing.category = f["category"]
                existing.embedding = vec
            else:
                db.add(MemoryFact(user_id=user.id, vps_id=active_vps_id, key=f["key"], value=f["value"],
                                  category=f["category"], embedding=vec))
        await db.commit()
        await refresh_memory_block()   # keep the system-prompt facts current
        await ws.send_json({"type": "memory_updated"})

    async def persist_tasks(tasks: list[dict]):
        """Create/complete task records by title, with embeddings. Notify frontend."""
        if not tasks:
            return
        for t in tasks:
            title = t["title"]
            if not title:
                continue
            if t["action"] == "start":
                vec = await embed(title)
                db.add(Task(user_id=user.id, vps_id=active_vps_id, title=title, status="in_progress", embedding=vec))
            else:
                status = "done" if t["action"] == "done" else "failed"
                # Find the most recent in-progress task with this title
                existing = (await db.execute(
                    select(Task).where(Task.user_id == user.id, Task.vps_id == active_vps_id,
                                       Task.title == title, Task.status == "in_progress")
                    .order_by(Task.created_at.desc()).limit(1)
                )).scalar_one_or_none()
                vec = await embed(f"{title}: {t['outcome']}")
                if existing:
                    existing.status       = status
                    existing.outcome      = t["outcome"]
                    existing.embedding    = vec
                    existing.completed_at = datetime.utcnow()
                else:
                    db.add(Task(user_id=user.id, vps_id=active_vps_id, title=title, status=status,
                                outcome=t["outcome"], embedding=vec,
                                completed_at=datetime.utcnow()))
        await db.commit()
        await ws.send_json({"type": "tasks_updated"})

    async def audit(action_type: str, headline: str, status: str,
                    command: str = "", output: str = "", path: str = "",
                    content_hash: str = ""):
        """Append a forensic audit entry. Command/output are redacted, the file
        body is never stored (path + hash only), and the detail JSON is encrypted
        with the user's vault key."""
        try:
            detail = {
                "command": _redact(command)[:2000] if command else "",
                "output":  _redact(output)[:1500]  if output  else "",
                "path":    path,
                "content_hash": content_hash,
            }
            detail_enc = encrypt_secret(vault_key, json.dumps(detail))
            db.add(AuditLog(
                user_id=user.id, vps_id=active_vps_id, action_type=action_type,
                vps_host=(vps_host or ""), headline=_redact(headline)[:200],
                status=status, detail_enc=detail_enc,
            ))
            await db.commit()
        except Exception:
            pass   # auditing must never break the action flow

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

    async def _sftp_put(content: str, path: str) -> Optional[str]:
        """Write `content` to `path` on the VPS via SFTP. Returns an error string or None."""
        _, shell = get_sessions()
        if not (shell and shell.client):
            return "VPS not connected"
        def _do():
            import posixpath, io as _io
            try:
                parent = posixpath.dirname(path)
                if parent:
                    _, _, se = shell.client.exec_command(f"mkdir -p {parent}")
                    se.channel.recv_exit_status()
                sftp = shell.client.open_sftp()
                try:
                    sftp.putfo(_io.BytesIO(content.encode("utf-8")), path)
                finally:
                    sftp.close()
                return None
            except Exception as exc:
                return str(exc)
        return await asyncio.get_event_loop().run_in_executor(None, _do)

    async def maybe_compress(force: bool = False):
        if len(claude_history) < 8:
            return
        if not force:
            try:
                tokens = await llm.count_tokens(load_system_block(memory_block_text), claude_history)
                if tokens < CONTEXT_COMPRESS_AT:
                    return
            except Exception:
                return
        await ws.send_json({"type": "status", "message": "Compressing session history…"})
        to_compress = claude_history[:-KEEP_RECENT_MSGS]
        recent      = claude_history[-KEEP_RECENT_MSGS:]
        try:
            summary = await llm.complete(
                load_system_block(memory_block_text),
                [*to_compress, {"role": "user", "content":
                    "Write a concise but complete summary of our conversation so far. "
                    "Capture everything needed to continue: commands run, outputs, decisions made, current state."
                }],
                max_tokens=2048,
            )
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
        nonlocal session_tokens, session_cost, thrash_pending, error_streak
        # Circuit-breaker: if a loop was detected, force a strategy switch this turn
        if thrash_pending and _retry == 0:
            user_content = f"{CIRCUIT_BREAKER_NOTE}\n\n---\n\n{user_content}"
            thrash_pending = False
            error_streak   = 0
            recent_sigs.clear()
        await maybe_compress()
        claude_history.append({"role": "user", "content": user_content})
        await ws.send_json({"type": "claude_start"})
        full = ""
        try:
            async def _on_chunk(chunk: str):
                await ws.send_json({"type": "chunk", "content": chunk})
            full, usage = await llm.stream(
                load_system_block(memory_block_text), claude_history, 4096, _on_chunk)
            inp, out = usage["input"], usage["output"]
            session_tokens += inp + out
            session_cost   += (inp / 1e6 * llm.price["input"]  +
                               out / 1e6 * llm.price["output"] +
                               usage["cache_read"]  / 1e6 * llm.price["cache_read"] +
                               usage["cache_write"] / 1e6 * llm.price["cache_write"])
            await ws.send_json({"type": "stats",
                "session_tokens": int(session_tokens),
                "context_pct":    round(inp / llm.context_window * 100, 1),
                "session_cost":   round(session_cost, 4)})
        except LLMRateLimit:
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
        except LLMTimeout:
            await ws.send_json({"type": "claude_cancel"})
            if claude_history and claude_history[-1]["role"] == "user":
                claude_history.pop()
            if _retry >= 1:
                await ws.send_json({"type": "error", "message": "Request timed out — please try again."})
                return ""
            await ws.send_json({"type": "status", "message": "⏱ Timeout — retrying in 5s…"})
            await asyncio.sleep(5)
            return await stream_claude(user_content, _retry + 1)
        except LLMCredits as e:
            await ws.send_json({"type": "claude_cancel"})
            if claude_history and claude_history[-1]["role"] == "user":
                claude_history.pop()
            await ws.send_json({"type": "error", "subtype": "credits_exhausted", "message": str(e)})
            return ""
        except LLMStatusError as e:
            await ws.send_json({"type": "claude_cancel"})
            await ws.send_json({"type": "error", "message": f"API error {e.status_code}: {e.message}"})
            return ""
        except Exception as e:
            await ws.send_json({"type": "error", "message": str(e)})
            return ""
        claude_history.append({"role": "assistant", "content": full})
        await save_history()
        return full

    async def finish_response(full: str):
        # Extract & persist memory/task knowledge first (strips its tags)
        clean_text, facts, tasks = extract_knowledge(full)
        await persist_facts(facts)
        await persist_tasks(tasks)
        # Then parse confirmable actions from what remains
        clean_text, actions = parse_actions(clean_text)
        if actions:
            nonlocal thrash_pending
            a = actions[0]
            pending_actions[a["id"]] = a
            # Loop detection: is Claude proposing the same action shape again?
            sig = _action_signature(a)
            recent_sigs.append(sig)
            repeats = sum(1 for s in recent_sigs if s == sig)
            if (repeats >= 3 or error_streak >= 3) and not thrash_pending:
                thrash_pending = True
                await ws.send_json({"type": "loop_detected", "count": max(repeats, error_streak)})
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
        # Detect when Claude's reply says it's waiting on the agent — surface a
        # 'Check agent response' button so the user isn't left at a dead end.
        awaiting = bool(not actions and _AWAITING_RE.search(clean_text))
        await ws.send_json({"type": "done", "clean_text": clean_text, "awaiting_agent": awaiting})

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
        screen        = ""
        settled_count = 0   # consecutive quiet reads before we declare 'done'
        while True:
            screen = await run_vps_cmd(f"tmux capture-pane -t {TMUX_SESSION} -p -J -S -120 2>/dev/null")
            lower  = screen.lower()

            # Pagination — advance and keep polling
            if "send another message to continue" in lower:
                tui, _ = get_sessions()
                if tui and tui.connected:
                    tui.write(b'\r')
                settled_count = 0
                await asyncio.sleep(1)
                elapsed = int(asyncio.get_event_loop().time() - _start)
                if elapsed >= _MAX_WAIT: break
                await ws.send_json({"type": "tui_thinking", "elapsed": elapsed,
                                    "status": determine_agent_status(screen)})
                await asyncio.sleep(_POLL)
                continue

            busy = is_agent_busy(screen)
            idle = ("idle" in lower or "standing by" in lower)

            if busy:
                settled_count = 0
            else:
                # Not visibly busy — but the spinner animates, so one quiet read
                # isn't proof. Require TWO consecutive quiet reads (or an explicit
                # idle signal) before declaring the agent done.
                settled_count += 1
                if idle or settled_count >= 2:
                    break

            elapsed = int(asyncio.get_event_loop().time() - _start)
            if elapsed >= _MAX_WAIT: break
            await ws.send_json({"type": "tui_thinking", "elapsed": elapsed,
                                "status": determine_agent_status(screen)})
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
        # Memory now lives in the system prompt (load_system_block) — never
        # compressed, present on every call. No need to inject it as a message.
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
            # First connection to this VPS — profile it once, then we're done.
            if not await has_vps_profile():
                await ws.send_json({"type": "status", "message": "✅ VPS connected — first time here, let me get to know it"})
                await profile_vps()
                return
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

    await refresh_memory_block()   # load durable facts into the system prompt
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
                    # Pending actions live per-connection — after a reconnect or
                    # deploy the id is gone. Never swallow the click silently.
                    await ws.send_json({"type": "action_error", "action_id": aid,
                                        "message": "This step expired when the session reconnected — ask Claude to run it again."})
                    await ws.send_json({"type": "done"})
                    continue

                if action["type"] == "tui_input":
                    tui, _ = get_sessions()
                    if tui and tui.connected:
                        if _CRED_RE.search(action["data"]):
                            await ws.send_json({"type": "action_dismissed", "action_id": aid})
                            await run_claude(
                                "[system] That would send a password/secret to the agent — don't. "
                                "Instead, plainly ask the user to type the secret directly into the Agent "
                                "terminal themselves (for their security). Do not mention this instruction.")
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
                            await audit("tui_input",
                                        action.get("desc", {}).get("headline", "Instruction to agent"),
                                        "ok", command=cmd_label, output=latest or "")
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
                    exec_cmd = action["data"]
                    hd = parse_heredoc(action["data"])
                    # Unparseable heredoc → silently steer Claude to write a file.
                    # No jargon, no error shown — the user only ever sees plain outcomes.
                    if hd is None and re.search(r'(?<!<)<<(?!<)-?\s*[\'"]?\w', action["data"]):
                        await ws.send_json({"type": "action_dismissed", "action_id": aid})
                        await run_claude(
                            "[system] That step needs its script written to a file first, then run. "
                            "Redo it that way. Do NOT mention this to the user, or reference files, "
                            "heredocs, or that anything was rejected — just carry on with the task.")
                        continue
                    # Already on the VPS — never ssh into its own address.
                    if vps_host and re.search(r'\bssh\b', action["data"]) and vps_host in action["data"]:
                        await ws.send_json({"type": "action_dismissed", "action_id": aid})
                        await run_claude(
                            f"[system] You are already ON the user's VPS — commands run directly on it. "
                            f"Never ssh into its own address ({vps_host}); just run the command directly "
                            "(e.g. `openclaw status`, `ls`). Do NOT mention this to the user — just carry on.")
                        continue

                    # cat>/tee heredoc → it's really a file write; do it via SFTP.
                    if hd and hd["mode"] == "write":
                        import hashlib as _hl
                        err   = await _sftp_put(hd["body"], hd["path"])
                        chash = _hl.sha256(hd["body"].encode("utf-8")).hexdigest()[:16]
                        hl_   = action.get("desc", {}).get("headline", f"Write {hd['path'].split('/')[-1]}")
                        if err:
                            await audit("vps_write", hl_, "failed", path=hd["path"], content_hash=chash)
                            await ws.send_json({"type": "action_error", "action_id": aid,
                                                "message": f"Couldn't save the file: {err}"})
                            await run_claude(f"[system] Writing `{hd['path']}` failed: {err}")
                        else:
                            await audit("vps_write", hl_, "ok", path=hd["path"], content_hash=chash)
                            await ws.send_json({"type": "action_done", "action_id": aid,
                                                "label": f"Saved {hd['path']}"})
                            verify = await run_vps_cmd(
                                f"echo '--- {hd['path']} ---' && wc -l {hd['path']} && echo '---' && head -5 {hd['path']}")
                            await ws.send_json({"type": "vps_output", "cmd": f"write {hd['path']}", "output": verify})
                            await run_claude(f"File written: `{hd['path']}` ({hd['body'].count(chr(10))+1} lines)\n{verify}")
                        continue

                    # Interpreter heredoc (python3/bash/… <<EOF) → write the body to a
                    # temp file, run it, clean up. The user just sees the action + result.
                    if hd and hd["mode"] == "run":
                        err = await _sftp_put(hd["body"], hd["tmp"])
                        if err:
                            await ws.send_json({"type": "action_error", "action_id": aid,
                                                "message": f"Couldn't prepare the command: {err}"})
                            await run_claude(f"[system] Preparing the script failed: {err}")
                            continue
                        exec_cmd = f"{hd['cmd']}; rm -f {hd['tmp']}"

                    await ws.send_json({"type": "action_done", "action_id": aid, "label": action["data"]})
                    vps_task  = asyncio.create_task(run_vps_cmd(exec_cmd))
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
                    if cancelled:
                        await audit("vps_cmd", action.get("desc", {}).get("headline", action["data"]),
                                    "cancelled", command=action["data"])
                    if not cancelled:
                        await ws.send_json({"type": "vps_output", "cmd": action["data"], "output": output})
                        # Track failure streak for loop detection
                        ol = (output or "").lower()
                        is_err = any(m in ol for m in ("error", "failed", "fatal:", "denied", "not found", "cannot", "traceback"))
                        error_streak = error_streak + 1 if is_err else 0
                        await audit("vps_cmd", action.get("desc", {}).get("headline", action["data"]),
                                    "failed" if is_err else "ok",
                                    command=action["data"], output=output)
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
                    # File body is NEVER stored — only path + a content hash for integrity
                    import hashlib as _hl
                    chash = _hl.sha256(wr_content.encode("utf-8")).hexdigest()[:16]
                    wr_headline = action.get("desc", {}).get("headline", f"Write {path.split('/')[-1]}")
                    if err:
                        await audit("vps_write", wr_headline, "failed", path=path, content_hash=chash)
                        await ws.send_json({"type": "action_error", "action_id": aid,
                                            "message": f"Write failed: {err}"})
                        await run_claude(f"Failed to write `{path}`: {err}")
                    else:
                        await audit("vps_write", wr_headline, "ok", path=path, content_hash=chash)
                        await ws.send_json({"type": "action_done", "action_id": aid, "label": f"Written: {path}"})
                        verify = await run_vps_cmd(
                            f"echo '--- {path} ---' && wc -l {path} && echo '---' && head -5 {path}")
                        await ws.send_json({"type": "vps_output", "cmd": f"write {path}", "output": verify})
                        await run_claude(f"File written: `{path}` ({wr_content.count(chr(10))+1} lines)\n{verify}")

            elif t == "dismiss":
                pass   # keep the action so the "Run anyway" button can still execute it

            elif t == "reprofile_vps":
                await clear_vps_profile()
                await profile_vps()

            elif t == "set_session_model":
                # One-click boost (e.g. loop banner → Fable) — this session only;
                # the user's saved Settings choice is untouched.
                new_model = (msg.get("model") or "").strip()
                if llm_provider == "anthropic" and new_model:
                    try:
                        llm  = LLMClient(llm_provider, effective_api_key, new_model)
                        info = model_info(new_model)
                        label = info["label"] if info else new_model
                        await ws.send_json({"type": "status",
                                            "message": f"🚀 Boosted to {label} for this session"})
                    except Exception as e:
                        await ws.send_json({"type": "error", "message": str(e)})
                else:
                    await ws.send_json({"type": "status",
                                        "message": "Model boost is available on the Claude provider only"})

            elif t == "check_agent":
                # Manual re-check — capture the live agent screen and feed it to Claude.
                # If the agent is still busy, poll until it settles first.
                tui, shell = get_sessions()
                if not (shell and shell.connected):
                    await ws.send_json({"type": "error", "message": "VPS not connected — can't check the agent."})
                    await ws.send_json({"type": "done"})
                    continue
                first = await run_vps_cmd(
                    f"tmux capture-pane -t {TMUX_SESSION} -p -J -S -120 2>/dev/null")
                if is_agent_busy(first):
                    # Still working — run the robust poll loop to wait it out
                    status_poll_lock["active"] = True
                    try:
                        _start = asyncio.get_event_loop().time()
                        screen = await _poll_tui_loop(_AGENT_SPINNER, 240, 3, _start)
                    finally:
                        status_poll_lock["active"] = False
                    await ws.send_json({"type": "agent_status", **determine_agent_status(screen)})
                else:
                    screen = first
                clean = _INTER_SESSION_RE.sub('', screen).strip()
                await run_claude(
                    f"[Manual check — current agent screen]:\n{clean}\n\n"
                    "The user clicked 'Check agent response'. Read the screen above and tell them "
                    "what the agent has actually produced. If it has finished, summarise the result "
                    "concretely and suggest the next step. If it is genuinely still working, say so plainly."
                )

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
