"""mcp_pair.py — MCP config generation + validation for the bidirectional herdr pairing.

Self-contained helper imported by Bridge.send(). Generates per-session temp
MCP configs pointing at the qwenpaw-bridge server with depth-marked session-id
prefixes, validates target-agent binding against the allowlist, and exposes a
pure pairing_config() function for testing without spawning anything.

The config file is a temp file (0600, delete=False) that outlives the Python
process — Claude Code may re-read on MCP reconnect. Cleanup is the caller's
responsibility (Bridge.kill / Bridge.reconcile), keyed off the config_path.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass


# Absolute path to the qwenpaw-bridge MCP server.
SERVER_SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "default", "skills",
                 "mcp_builder_local_server", "server.py")
)

# QwenPaw venv python interpreter that has the 'mcp' package.
QWEN_PYTHON = os.environ.get(
    "CLAUDE_BRIDGE_QWEN_PYTHON",
    os.path.expanduser("~/qwenpaw/qwen_env/bin/python"),
)


@dataclass
class PairingConfig:
    """Resolved pairing configuration returned by pairing_config()."""

    target_agent: str
    allowlist: str       # comma-separated
    session_prefix: str
    config_path: str     # path to the generated temp MCP config JSON
    config_json: dict    # the JSON payload (for test inspection, not written)


class PairingError(ValueError):
    """Raised when pairing config generation fails (allowlist, resolution)."""


def pairing_config(
    caller_agent_id: str,
    target_agent: str | None = None,
    allowlist: str | None = None,
    task_id: str | None = None,
    depth: int = 1,
) -> PairingConfig:
    """Resolve and generate a pairing MCP config without writing a temp file.

    Resolution order: explicit target_agent > caller_agent_id > "default".
    Every candidate is validated against the allowlist.

    Args:
        caller_agent_id: The agent_id of the caller (from the ledger).
        target_agent: Explicit override (None = resolve from caller).
        allowlist: Comma-separated agent allowlist (None = caller_agent_id only).
        task_id: The herdr task_id for session-id naming (None = no depth prefix).
        depth: Spawn chain depth (default 1).

    Returns:
        PairingConfig with resolved target, allowlist, session_prefix, and
        the config JSON payload ready for writing.
    """
    # Resolve target agent
    resolved = target_agent or caller_agent_id or "default"

    # Resolve allowlist
    allowlist_parts = [a.strip() for a in (allowlist or "").split(",") if a.strip()]
    if not allowlist_parts:
        # Default: caller + resolved (deduplicated)
        allowlist_parts = list(dict.fromkeys(
            a for a in (caller_agent_id, resolved) if a
        ))
    allowlist_str = ",".join(allowlist_parts)

    # Validate: resolved must be in the allowlist
    if resolved not in allowlist_parts:
        raise PairingError(
            f"agent_id '{resolved}' is not in the allowlist "
            f"({allowlist_str}). Reconfigure the pairing to add it."
        )

    # Build session-id prefix
    if task_id:
        session_prefix = f"mcp-bridge-herdr-{task_id}-d{depth}"
    else:
        # Fallback: use the default prefix (e.g. for tests without a task_id)
        session_prefix = f"mcp-bridge-{resolved}"

    # Build the MCP config JSON
    config_json = {
        "mcpServers": {
            "qwenpaw-bridge": {
                "command": QWEN_PYTHON,
                "args": [
                    SERVER_SCRIPT,
                    "--agent", resolved,
                    "--agents", allowlist_str,
                    "--session-prefix", session_prefix,
                ],
            }
        }
    }

    return PairingConfig(
        target_agent=resolved,
        allowlist=allowlist_str,
        session_prefix=session_prefix,
        config_path="",  # not written yet
        config_json=config_json,
    )


def write_config(config: PairingConfig) -> PairingConfig:
    """Write the config JSON to a temp file (0600, delete=False).

    Returns a new PairingConfig with config_path set. The caller is
    responsible for cleanup (os.unlink) via kill/reconcile.
    """
    fd, path = tempfile.mkstemp(
        prefix="herdr-mcp-",
        suffix=".json",
        text=True,
    )
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config.config_json, f, indent=2)

    return PairingConfig(
        target_agent=config.target_agent,
        allowlist=config.allowlist,
        session_prefix=config.session_prefix,
        config_path=path,
        config_json=config.config_json,
    )


def cleanup_config(config_path: str) -> None:
    """Best-effort unlink of a temp MCP config file. Idempotent."""
    try:
        os.unlink(config_path)
    except FileNotFoundError:
        pass
