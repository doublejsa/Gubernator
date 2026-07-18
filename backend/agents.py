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
    },
}
DEFAULT_AGENT = "openclaw"
MAX_BOTS = 3


def agent_cfg(agent_type: str | None) -> dict:
    return AGENTS.get(agent_type or DEFAULT_AGENT, AGENTS[DEFAULT_AGENT])
