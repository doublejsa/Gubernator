"""
Agent registry — per-agent-type commands and paths.

Each user VPS ("bot") runs one agent framework. Everything framework-specific
lives here so supporting a new agent is a registry entry, not a code hunt.
The agent TUI always runs inside our own tmux session (ocmgr-tui) on the
user's VPS, so view reset / capture-pane stay agent-agnostic.

Hermes entries are provisional until verified against a live install
(hermes-agent.nousresearch.com).
"""

AGENTS: dict[str, dict] = {
    "openclaw": {
        "label":       "OpenClaw",
        "icon":        "🦞",
        "tui_cmd":     "openclaw tui",
        "config_dir":  "/root/.openclaw",
        "secrets_dir": "/root/.openclaw/credentials",
        "restart_cmd": "openclaw gateway restart 2>/dev/null || systemctl restart openclaw-gateway",
        "version_cmd": "openclaw --version",
        "skills_cli":  True,    # ClawHub marketplace via `openclaw skills`
        "install_hint": "npm install -g openclaw@latest, then `openclaw onboard` (check docs.openclaw.ai if that fails)",
        "awareness_path": "/root/.openclaw/workspace/GUBERNATOR.md",
        "instructions_path": "/root/.openclaw/workspace/AGENTS.md",
        "shared_memory_path": "/root/.openclaw/workspace/MEMORY.md",
        "memory_hint": "VERIFIED: OpenClaw's long-term memory is workspace/MEMORY.md, already active "
                       "and shared across ALL channels (TUI/Telegram/WhatsApp). No per-channel setup "
                       "needed. `openclaw memory status` shows the index; `openclaw memory promote` "
                       "ranks recent recalls and appends the best to MEMORY.md. To make sure something "
                       "is remembered everywhere, ensure it lands in MEMORY.md (durable facts) or "
                       "AGENTS.md (standing rules) — both are loaded in every channel.",
        "model_hint": "use the built-in CLI: `openclaw models status --plain` to see the current "
                      "model, and `openclaw models --help` for the set/switch subcommand. Then "
                      "restart the gateway. NEVER hand-edit session JSON — a half-applied switch "
                      "leaves liveModelSwitchPending=true and breaks the agent.",
    },
    "hermes": {
        "label":       "Hermes",
        "icon":        "🪽",
        "tui_cmd":     "hermes --tui",
        "config_dir":  "/root/.hermes",
        "secrets_dir": "/root/.hermes/credentials",
        "restart_cmd": "hermes gateway restart",
        "version_cmd": "hermes --version",
        "skills_cli":  False,   # different skills system — marketplace hidden
        "install_hint": "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash, "
                        "then `hermes setup` (interactive — user runs it in the Console), "
                        "then `hermes gateway install` to run it as a service",
        "awareness_path": "/root/.hermes/GUBERNATOR.md",
        "instructions_path": "/root/.hermes/AGENTS.md",
        "shared_memory_path": "/root/.hermes/MEMORY.md",
        "memory_hint": "VERIFIED: Hermes built-in memory (MEMORY.md/USER.md) is ALWAYS active and "
                       "shared across channels — no setup needed for basic cross-channel memory. "
                       "Run `hermes memory status` to see the config and the exact MEMORY.md path "
                       "([REMEMBER] it). Durable facts belong in MEMORY.md, standing rules in AGENTS.md. "
                       "Optional stronger recall via external providers: `hermes memory setup` "
                       "(mem0/honcho/…), but don't bother unless the user asks.",
        "model_hint": "use `hermes model` (interactive picker) or `hermes config set` — check "
                      "`hermes model --help` first. Never hand-edit session files.",
    },
}
DEFAULT_AGENT = "openclaw"
MAX_BOTS = 3


def agent_cfg(agent_type: str | None) -> dict:
    return AGENTS.get(agent_type or DEFAULT_AGENT, AGENTS[DEFAULT_AGENT])
