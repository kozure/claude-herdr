"""swarm.py — orchestrated multi-Claude swarm with bidirectional MCP pairing.

Manages the full swarm lifecycle: launch N Claude sessions in a shared herdr
workspace with MCP pairing, provide peer discovery and task coordination,
collect results with blocking poll, and teardown with tempfile cleanup.

Depends on:
  - Bridge.send(pane_id=..., mcp_pair=True, agent_id=...) for per-session launch
  - Bridge.poll(task_id) for result collection (non-blocking, ~100ms)
  - Bridge.prompt(task_id, text) for peer message routing (fire-and-forget)
  - Bridge.kill(task_id) for teardown (triggers per-task MCP cleanup)
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from bridge import Bridge, ALLOWED_CWD, cwd_allowed
from trust import preseed_trust  # noqa: F401 — re-exported for callers/tests


# Timing constants (from spec)
POLL_INTERVAL = 5       # seconds between Bridge.poll() calls
MAX_PEER_POLL_SECS = 300  # max wait for a peer response (within 900s read_timeout)


def _resolve_cwd(cwd: str | None) -> str:
    cwd = cwd or ALLOWED_CWD
    if not cwd_allowed(cwd):
        raise ValueError(
            f"cwd must be under an allowed root ({ALLOWED_CWD} or an agent workspace)"
        )
    return cwd


@dataclass
class SwarmHandle:
    """Live handle for an active swarm."""

    swarm_id: str
    roles: dict[str, str]        # {role: task_id}
    task_ids: list[str]
    mcp_config_paths: dict[str, str]  # {task_id: config_path}
    workspace_id: str
    cwd: str
    task: str                    # the swarm's mission prompt
    launched_at: str = ""
    state: str = "launching"
    orchestrator_agent_id: str = ""   # who coordinates; used in the peer prompt
    # Internal
    _pending_peer_requests: set[tuple[str, str]] = field(default_factory=set)
    _bridge: Any = field(default=None, repr=False)

    def role_for(self, task_id: str) -> str | None:
        for role, tid in self.roles.items():
            if tid == task_id:
                return role
        return None

    def task_id_for(self, role: str) -> str | None:
        return self.roles.get(role)


class SwarmError(RuntimeError):
    """Raised for swarm-level failures (launch, routing, collection)."""


def launch(
    orchestrator_agent_id: str,
    task: str,
    swarm_size: int = 3,
    cwd: str | None = None,
    bridge: Bridge | None = None,
) -> SwarmHandle:
    """Launch a swarm of N Claude sessions in a shared herdr workspace.

    Returns a SwarmHandle. On partial failure, kills launched sessions and
    raises SwarmError with details.
    """
    cwd = _resolve_cwd(cwd)
    bridge = bridge or Bridge()
    swarm_id = uuid.uuid4().hex[:12]

    # No shared workspace is created here: `Bridge.send()` (called once per
    # session below with no pane_id) creates each session's own workspace and
    # pre-seeds trust itself. The shared-workspace path needs herdr
    # `pane_split`, which is deferred — until then a workspace created here
    # would be created, never used, and never closed: a leak per launch
    # (2026-11-03 review).
    real_cwd = os.path.realpath(cwd)

    handle = SwarmHandle(
        swarm_id=swarm_id,
        roles={},
        task_ids=[],
        mcp_config_paths={},
        workspace_id="",
        cwd=real_cwd,
        task=task,
        launched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        state="launching",
        orchestrator_agent_id=orchestrator_agent_id,
        _bridge=bridge,
    )

    launched = []
    errors = []

    for i in range(1, swarm_size + 1):
        role = f"claude-{i}"

        try:
            # When pane_id is None, Bridge.send() creates its own workspace.
            # Shared-workspace mode (pane_id set) requires herdr pane_split
            # support, deferred to v2.
            result = bridge.send(
                prompt="",  # prompt sent later in coordinate()
                cwd=real_cwd,
                mcp_pair=True,
                agent_id=orchestrator_agent_id,
            )
        except Exception as exc:
            errors.append({"role": role, "reason": str(exc)})
            break

        if "error" in result:
            errors.append({"role": role, "reason": result.get("message", result["error"])})
            break

        task_id = result["task_id"]
        handle.roles[role] = task_id
        handle.task_ids.append(task_id)
        launched.append(role)

        # Small delay between launches to let herdr settle
        if i < swarm_size:
            time.sleep(3)

        # Track MCP config path for teardown
        row = bridge.ledger.get_task(task_id)
        if row and row.get("mcp_config_path"):
            handle.mcp_config_paths[task_id] = row["mcp_config_path"]

    if errors:
        # Partial launch: kill what we started
        for tid in handle.task_ids:
            try:
                bridge.kill(tid)
            except Exception:
                pass
        handle.state = "error"
        raise SwarmError(
            f"Partial launch: {len(launched)}/{swarm_size} sessions started. "
            f"Failed: {errors}"
        )

    handle.state = "launched"
    return handle


def coordinate(handle: SwarmHandle) -> None:
    """Send each Claude session its peer discovery prompt.

    Sets handle.state to "coordinating" then "working" after all prompts
    are sent. Call after launch().
    """
    if handle.state not in ("launched",):
        raise SwarmError(f"Cannot coordinate from state '{handle.state}'")

    handle.state = "coordinating"
    bridge = handle._bridge

    for role, task_id in handle.roles.items():
        # Build peer list
        peer_lines = []
        for other_role in handle.roles:
            if other_role == role:
                continue
            session_id = f"swarm-{handle.swarm_id}-{role}-to-{other_role}"
            peer_lines.append(
                f"  {other_role} — reach via qwenpaw_chat with "
                f"session_id=\"{session_id}\""
            )

        peer_section = "\n".join(peer_lines) if peer_lines else "  (no peers)"

        prompt = (
            f"You are {role} in swarm {handle.swarm_id}. Your peers:\n\n"
            f"{peer_section}\n\n"
            f"The orchestrator ({handle.orchestrator_agent_id or 'the coordinator'}) "
            f"is at session_id=\"swarm-{handle.swarm_id}-{role}\".\n\n"
            f"Peer messages take 60-300s round-trip. Be patient. "
            f"Coordinate as needed.\n\n"
            f"Your task: {handle.task}"
        )

        bridge.prompt(task_id, prompt)

    handle.state = "working"


def collect(
    handle: SwarmHandle,
    timeout_min: int = 15,
) -> dict[str, dict[str, Any]]:
    """Block until all swarm sessions complete or timeout.

    Returns {role: {"status": "completed"|"failed"|"timeout", "result": ...}}.
    """
    if handle.state not in ("working", "collecting"):
        raise SwarmError(f"Cannot collect from state '{handle.state}'")

    handle.state = "collecting"
    bridge = handle._bridge
    deadline = time.time() + (timeout_min * 60)
    results: dict[str, dict[str, Any]] = {}

    pending = set(handle.task_ids)

    while pending and time.time() < deadline:
        for task_id in list(pending):
            r = bridge.poll(task_id)
            state = r.get("state")

            if state == "done":
                role = handle.role_for(task_id)
                results[role or task_id] = {
                    "status": "completed",
                    "result": r.get("result"),
                }
                pending.discard(task_id)
            elif state in ("killed", "lost"):
                role = handle.role_for(task_id)
                results[role or task_id] = {
                    "status": "failed",
                    "error": f"session {state}",
                }
                pending.discard(task_id)

        if pending:
            time.sleep(POLL_INTERVAL)

    # Any remaining sessions timed out
    for task_id in pending:
        role = handle.role_for(task_id)
        results[role or task_id] = {
            "status": "timeout",
            "error": f"did not complete within {timeout_min}min",
        }

    handle.state = "done"
    return results


def teardown(handle: SwarmHandle) -> dict[str, Any]:
    """Kill all swarm sessions. Temp config cleanup runs per-task via kill().

    Returns {"closed": [...], "errors": [...]}.
    """
    bridge = handle._bridge
    closed = []
    errors = []

    for task_id in handle.task_ids:
        try:
            result = bridge.kill(task_id)
            if result.get("state") == "killed":
                closed.append(task_id)
            else:
                errors.append({"task_id": task_id, "error": result})
        except Exception as exc:
            errors.append({"task_id": task_id, "error": str(exc)})

    handle.state = "done"
    handle._pending_peer_requests.clear()
    return {"closed": closed, "errors": errors}


def route_peer_message(
    handle: SwarmHandle,
    session_id: str,
    text: str,
) -> str:
    """Route a peer message through the orchestrator.

    Inspects the session_id for a peer-target pattern
    (swarm-<id>-<from>-to-<to>), validates against the active swarm,
    checks for deadlock, calls Bridge.prompt() + poll loop on the target,
    and returns the target's response.

    Returns an error string if routing fails (timeout, deadlock, invalid target).
    """
    bridge = handle._bridge

    # Validate session_id format: must be swarm-<id>-<from>-to-<to>
    parts = session_id.split("-to-")
    if len(parts) != 2:
        return f"Error: invalid peer session_id format: {session_id}"

    from_part = parts[0]
    to_role = parts[1]

    # Extract from_role from the path
    # Extract from_role: everything after "swarm-<swarm_id>-" and before "-to-"
    prefix = f"swarm-{handle.swarm_id}-"
    if not from_part.startswith(prefix):
        return f"Error: session_id does not match active swarm {handle.swarm_id}"
    from_role = from_part[len(prefix):]  # e.g. "claude-1"

    # Look up target task_id
    target_task_id = handle.task_id_for(to_role)
    if not target_task_id:
        return f"Error: peer '{to_role}' not found in swarm"

    # Deadlock check
    if (to_role, from_role) in handle._pending_peer_requests:
        return (
            f"Error: cannot route to {to_role} — that peer is currently "
            f"waiting on you (potential deadlock)."
        )

    handle._pending_peer_requests.add((from_role, to_role))

    try:
        # Fire the prompt
        prompt_result = bridge.prompt(
            target_task_id,
            f"[From {from_role}]: {text}",
        )

        if "error" in prompt_result:
            return f"Error: {prompt_result['error']}: {prompt_result.get('message', '')}"

        # Poll for response
        deadline = time.time() + MAX_PEER_POLL_SECS
        while time.time() < deadline:
            r = bridge.poll(target_task_id)
            state = r.get("state")

            if state == "done":
                result = r.get("result", {})
                text_out = result.get("text", "") if isinstance(result, dict) else str(result)
                return text_out
            elif state in ("killed", "lost"):
                return f"Error: peer {to_role} session is {state}"

            time.sleep(POLL_INTERVAL)

        return (
            f"Error: peer {to_role} did not respond within "
            f"{MAX_PEER_POLL_SECS}s"
        )
    finally:
        handle._pending_peer_requests.discard((from_role, to_role))
