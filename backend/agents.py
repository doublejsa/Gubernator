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
        "memory_hint": "OpenClaw has a memory system — check `openclaw memory --help`. Configure "
                       "memory to be GLOBAL/shared across all sessions and channels (not per-session). "
                       "If it can't be made global, keep a workspace/MEMORY.md that the persona reads "
                       "and appends to every turn — that file is shared by all channels.",
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
        "memory_hint": "Hermes has built-in persistent cross-session memory (it searches its own past "
                       "conversations). Ensure that memory is enabled and shared across channels — "
                       "check `hermes memory --help` / `hermes --help`. As a fallback keep a shared "
                       "MEMORY.md the persona reads and updates.",
        "model_hint": "use `hermes model` (interactive picker) or `hermes config set` — check "
                      "`hermes model --help` first. Never hand-edit session files.",
    },
}
DEFAULT_AGENT = "openclaw"
MAX_BOTS = 3


def agent_cfg(agent_type: str | None) -> dict:
    return AGENTS.get(agent_type or DEFAULT_AGENT, AGENTS[DEFAULT_AGENT])
