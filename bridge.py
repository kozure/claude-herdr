"""bridge.py — herdr-backed bridge for Claude Code sessions.

Thin client: herdr owns session state, Claude owns the conversation.
The bridge maps herdr agent states to user-facing states, collects
results via terminal-text parsing, and manages a SQLite ledger for
task↔pane lookup and result caching.
"""
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone

from herdr_client import (
    HerdrClient, HerdrServerDown, HerdrTimeout, AgentNotReady, HerdrAPIError)
from result_parser import parse_result
from ledger import Ledger
from trust import preseed_trust
from composer_state import classify_composer, sentinel_for

_HOME = os.path.expanduser("~")

# Root the bridge may spawn sessions in. Override with CLAUDE_BRIDGE_ALLOWED_CWD
# when your checkout lives somewhere other than ~/DEV.
ALLOWED_CWD = os.environ.get(
    "CLAUDE_BRIDGE_ALLOWED_CWD", os.path.join(_HOME, "DEV"))

# Additional roots where a session may be spawned. The QwenPaw agent
# workspaces are allowed so bridge work can target workspace-local code
# (skills, specs, etc. under workspaces/workspaces/<agent>/).
ALLOWED_CWD_EXTRA_ROOTS = (
    os.environ.get("CLAUDE_BRIDGE_WORKSPACES_ROOT",
                   os.path.join(_HOME, "qwenpaw", "workspaces", "workspaces")),
)


def cwd_allowed(cwd: str) -> bool:
    """True when ``cwd`` is under an allowed root (DEV or an agent workspace)."""
    return cwd.startswith(ALLOWED_CWD) or any(
        cwd.startswith(root) for root in ALLOWED_CWD_EXTRA_ROOTS
    )


UNKNOWN_RETRY_INTERVAL = 2
UNKNOWN_MAX_RETRIES = 5
IDLE_CONFIRMATIONS = 3
HERDR_DOWN_MSG = ("herdr server is not running. Start with: "
                  "launchctl load ~/Library/LaunchAgents/com.luther.herdr.plist")

# Residual-dialog detection. Pre-seeding (trust.preseed_trust) is the primary
# defence; these are the fallback for a directory that was created *after*
# pre-seeding ran. Matching is deliberately content-based, not keystroke-based:
# a misfired Enter on the trust dialog's default focus ("No, exit Claude Code")
# kills the session outright.
TRUST_DIALOG_MARKERS = ("trust the files in this folder", "do you trust this folder")
IMPORTS_DIALOG_MARKERS = ("external claude.md file imports", "files you trust")

# --- Brief fix (2026-10-10): the reporting contract is structural ---------
#
# Root cause (TICKET-444 parent 2.0): the pane finished its work, the ledger
# saw it go idle, and nothing reached Alfred — because the hand-written
# brief simply never said "report back". The brief is the load-bearing
# part, so it is not left to the caller's memory: every *paired* prompt
# carries this block, appended by send() itself.
#
# A prompt that already contains the marker is left alone (idempotent), so
# a caller that writes its own reporting section does not get a second one.
REPORTING_MARKER = "## Report back (required)"

REPORTING_BLOCK = """{marker}

This session is paired to `{target}` over MCP. When you stop — finished,
blocked, or waiting on a decision — send **one** report with the paired
tool `qwenpaw_chat` (server `qwenpaw-bridge`; Claude Code shows it as
`mcp__qwenpaw-bridge__qwenpaw_chat`). Pass the report text; leave
`session_id` empty so it lands in this session's own channel.

The report carries:

- **task**: `{task_id}` (this session's id — always include it)
- **status**: done | blocked | partial
- **changed**: what you touched, with file paths
- **evidence**: the exact commands you ran and what they returned
- **blockers**: anything you could not do, and why
- **next**: what should happen now

Ending the turn without that report leaves the pane idle and nobody
informed — the watcher will eventually notice a finished session and ask,
but it cannot tell anyone *what* you did. If the MCP tool is unusable, put
the same report in the pane's final message so the transcript still
carries it."""


def _with_reporting_contract(prompt, task_id, target_agent):
    """Append the reporting block to a paired prompt. Idempotent."""
    if not prompt or REPORTING_MARKER in prompt:
        return prompt
    return prompt.rstrip() + "\n\n" + REPORTING_BLOCK.format(
        marker=REPORTING_MARKER, task_id=task_id, target=target_agent)


def _residual_dialog_answer(pane_text):
    """Key sequence that accepts a launch dialog, or None if unrecognised.

    Only reached when pre-seeding did not prevent a dialog (e.g. a directory
    created between pre-seed and launch). The two dialogs need *different*
    keys, and getting it wrong is destructive:

      * Trust dialog — default focus is "No, exit Claude Code", so a bare
        Enter kills the session. Down moves focus to "Yes", then Enter.
      * Imports dialog — default focus is already "Yes, allow"; Down would
        move to "No", so Enter alone is correct.

    Anything unrecognised returns None so the caller surfaces it to the operator
    instead of firing keystrokes into a live REPL.
    """
    text = pane_text.lower()
    if any(m in text for m in TRUST_DIALOG_MARKERS):
        return ("Down", "Enter")
    if any(m in text for m in IMPORTS_DIALOG_MARKERS):
        return ("Enter",)
    return None


class Bridge:
    """herdr-backed bridge for Claude Code sessions."""
    def __init__(self, herdr=None, ledger=None, default_cwd=None):
        self.herdr = herdr or HerdrClient()
        self.ledger = ledger or Ledger()
        self.ledger.init_schema()
        self.default_cwd = default_cwd or ALLOWED_CWD

    def send(self, prompt, cwd=None, session_id=None, timeout_min=30,
             mcp_pair=None, mcp_agents=None, agent_id=None, pane_id=None):
        """Start a Claude session, optionally send a prompt.

        MCP pairing (mcp_pair): defaults to auto — when agent_id is set and
        mcp_pair is not explicitly False, the spawned Claude gets bidirectional
        MCP tools to talk back to its spawner. Pass mcp_pair=False to opt out.

        Workspace sharing (pane_id): when provided, skip workspace_create,
        preseed_trust, and cwd enforcement — the caller owns the workspace.
        Used by swarm.py for shared-workspace multi-session launches.
        """
        cwd = cwd or self.default_cwd
        if not cwd_allowed(cwd):
            return {"error": "cwd_not_allowed",
                    "message": f"cwd must be under an allowed root ({ALLOWED_CWD} or an agent workspace) unless the operator approved."}
        try:
            # --- MCP pairing: auto-pair when agent_id is known ---
            if mcp_pair is None:
                mcp_pair = bool(agent_id and agent_id != "legacy")
            mcp_config = None
            if mcp_pair:
                from mcp_pair import pairing_config, write_config, PairingError
                try:
                    mcp_config = pairing_config(
                        caller_agent_id=agent_id or "legacy",
                        allowlist=mcp_agents,
                        depth=1,
                    )
                except PairingError as e:
                    return {"error": "mcp_pair_error", "message": str(e)}
                # Set task_id now so the session prefix is generated
                # We need task_id before workspace_create — generate it here
                task_id = f"task-{uuid.uuid4().hex[:8]}"
                mcp_config = pairing_config(
                    caller_agent_id=agent_id or "legacy",
                    target_agent=None,
                    allowlist=mcp_agents,
                    task_id=task_id,
                    depth=1,
                )
                # Write the temp config file
                try:
                    mcp_config = write_config(mcp_config)
                except PairingError as e:
                    # Can't happen from write_config, but belt-and-suspenders
                    return {"error": "mcp_pair_error", "message": str(e)}

            # --- Original launch flow ---
            if pane_id:
                # Shared-workspace mode: skip workspace_create, trust preseed,
                # and cwd enforcement. Caller (swarm.py) owns all three.
                trust = {"key": os.path.realpath(cwd or self.default_cwd), "changed": []}
                workspace_id = ""
            else:
                # Claude Code has no flag/env to skip the "Do you trust this
                # folder?" dialog — trust is persisted state. Mark the launch
                # directory trusted *before* the pane exists, so the dialog (and
                # the external-CLAUDE.md-imports dialog behind it) never appears.
                trust = preseed_trust(os.path.realpath(cwd))
                ws = self.herdr.workspace_create(cwd=cwd)
                pane_id, workspace_id = ws.get("pane_id", ""), ws.get("workspace_id", "")
            if not mcp_pair:
                task_id = f"task-{uuid.uuid4().hex[:8]}"
            args = []
            if session_id:
                args = ["--resume", session_id]
            elif args is not None:
                args = list(args) if args else []
            # MCP pairing args
            if mcp_config and mcp_config.config_path:
                args = (args or []) + [
                    "--mcp-config", mcp_config.config_path,
                    "--strict-mcp-config",
                ]
            try: self.herdr.agent_start(name=task_id, kind="claude", pane_id=pane_id, cwd=cwd, args=args or None)
            except AgentNotReady: pass  # blocked on a dialog — handled below

            idle_count = 0
            while idle_count < IDLE_CONFIRMATIONS:
                try: agent = self.herdr.agent_get(task_id)
                except AgentNotReady: agent = {"agent_status": "blocked"}
                status = agent.get("agent_status", "unknown")
                if status == "blocked":
                    idle_count = 0
                    pane_text = self.herdr.pane_read(pane_id, source="visible")
                    answer = _residual_dialog_answer(pane_text)
                    if answer is None:
                        return {"error": "blocked", "message": "Agent blocked on permission prompt. Surface to the operator.",
                                "pane_text": pane_text[:200],
                                "trust_preseed": trust}
                    for key in answer:
                        self.herdr.pane_send_keys(pane_id, key)
                        time.sleep(1)
                    continue
                elif status == "idle":
                    idle_count += 1
                    if idle_count < IDLE_CONFIRMATIONS: time.sleep(0.5)
                else: idle_count = 0; time.sleep(0.5)

            f11_warning = self._check_bare_flag()
            # Brief fix (2026-10-10): a paired prompt always carries the
            # reporting contract — see REPORTING_BLOCK. Applied before the
            # ledger row is written, so the stored prompt is what the pane
            # was actually told.
            if prompt and mcp_config:
                prompt = _with_reporting_contract(
                    prompt, task_id, mcp_config.target_agent)
            # SDD 01 verify-before-send: off unless exactly "1" (default-off
            # parity). The sentinel goes on line 1 *after* the reporting block
            # is appended, so the stored prompt below is the wire text.
            verify = os.environ.get("HERDR_VERIFY_SEND") == "1"
            if prompt and verify:
                prompt = sentinel_for(task_id) + "\n" + prompt
            pending = bool(prompt)
            verdict = None
            if prompt:
                self.herdr.agent_prompt(task_id, prompt)
                transition = self._wait_working(task_id, "idle")
                if verify:
                    verdict = self._verdict(task_id, pane_id, transition)
                    # Unit 3: the retry's shape is fixed by `reason`. Only
                    # no_submission_evidence may re-submit; the other two are
                    # terminal — one agent_prompt for the whole dispatch.
                    if verdict.get("reason") == "no_submission_evidence":
                        verdict = self._retry_unsubmitted(task_id, pane_id, prompt, verdict)
                    pending = verdict["verified"]
            agent = self.herdr.agent_get(task_id)
            claude_session = agent.get("agent_session", {}).get("value", session_id or "")
            self.ledger.insert_task(
                task_id=task_id, pane_id=pane_id, workspace_id=workspace_id,
                session_id=claude_session, prompt=prompt or "", cwd=cwd,
                timeout_min=timeout_min, pending_result=pending,
                agent_id=agent_id or "legacy",
                mcp_config_path=mcp_config.config_path if mcp_config else None,
                mcp_target=mcp_config.target_agent if mcp_config else None,
                mcp_session_prefix=mcp_config.session_prefix if mcp_config else None)
            # The row is written either way; `working` only on acceptance.
            # A rejected dispatch is never collected: collected_at stays NULL
            # and the evidence lives in the returned error dict, not in
            # cached_result (cas_collect would stamp it as collected).
            if verdict and not verdict["verified"]:
                self.ledger.update_last_state(task_id, "blocked")
                verdict.update(error="blocked", agent_status=agent.get("agent_status"),
                               message=f"Dispatch not verified ({verdict['reason']}). "
                                       "Pane left as-is for inspection. Surface to the operator.")
            elif pending:
                self.ledger.update_last_state(task_id, "working")
            result = {"task_id": task_id, "pane_id": pane_id}
            if verdict: result.update(verdict)
            if f11_warning: result["f11_warning"] = True
            if trust.get("changed"): result["trust_preseeded"] = trust["changed"]
            if trust.get("error"): result["trust_error"] = trust["message"]
            if mcp_config:
                result["mcp_paired"] = True
                result["mcp_target"] = mcp_config.target_agent
                result["mcp_session_prefix"] = mcp_config.session_prefix
            return result
        except HerdrServerDown:
            return {"error": "herdr_server_down", "message": HERDR_DOWN_MSG}

    def poll(self, task_id):
        row = self.ledger.get_task(task_id)
        if not row:
            return {"error": "task_not_found", "task_id": task_id}
        try:
            agent = self._agent_get_with_retry(task_id)
            if agent is None:
                return {"task_id": task_id, "state": "unknown",
                        "message": "agent state undetectable after 10s — may need manual check"}
            herdr_status = agent.get("agent_status", "unknown")
            last_state = row["last_state"]
            pending = bool(row["pending_result"])
            collected = row["collected_at"]

            # working + killed = kill in transit → running (will settle to killed)
            if herdr_status == "working" and last_state == "killed":
                return self._resp(task_id, row, "running")
            # killed stays killed (unless working = kill in transit, handled above)
            if last_state == "killed":
                return self._resp(task_id, row, "killed")
            if herdr_status == "working":
                self.ledger.update_last_state(task_id, "working")
                return self._resp(task_id, row, "running")
            if herdr_status == "blocked":
                self.ledger.update_last_state(task_id, "blocked")
                return self._resp(task_id, row, "blocked")
            if herdr_status in ("idle", "done"):
                if last_state == "killed":
                    return self._resp(task_id, row, "killed")
                if collected is not None:
                    return self._cached(task_id, row)
                should_collect = (
                    last_state == "working" or
                    (pending and last_state in ("idle", None, "unknown")) or
                    (last_state == "unknown" and pending))
                if last_state == "blocked":
                    should_collect = False
                if should_collect:
                    return self._collect(task_id, row)
                elif last_state == "blocked":
                    return self._resp(task_id, row, "blocked")
                else:
                    return self._resp(task_id, row, "idle")
            return self._resp(task_id, row, "unknown")
        except HerdrServerDown:
            return {"task_id": task_id, "state": "daemon_down", "message": "herdr server unreachable"}
        except HerdrAPIError as e:
            if e.code == "agent_not_found":
                return {"task_id": task_id, "state": "lost" if row["last_state"] != "killed" else "killed",
                        "message": f"Agent not found: {e}"}
            raise

    def poll_all(self):
        """Poll all tasks. Returns list of poll results."""
        try:
            agents = self.herdr.agent_list()
        except HerdrServerDown:
            return [{"task_id": r["task_id"], "state": "daemon_down",
                     "message": "herdr server unreachable"} for r in self.ledger.list_all()]
        agent_map = {a["name"]: a for a in agents}
        results = []
        for row in self.ledger.list_all():
            tid = row["task_id"]
            if tid in agent_map:
                results.append(self.poll(tid))
            else:
                results.append({"task_id": tid, "state": "lost",
                                "message": "Task not found in herdr — may need manual restart"})
        return results

    def read(self, task_id):
        """Read raw transcript text for a task."""
        row = self.ledger.get_task(task_id)
        if not row:
            return {"error": "task_not_found", "task_id": task_id}
        try:
            text = self.herdr.agent_read(task_id, source="recent_unwrapped", format="text")
            return {"task_id": task_id, "text": text}
        except HerdrServerDown:
            return {"error": "herdr_server_down", "message": HERDR_DOWN_MSG}

    def prompt(self, task_id, text):
        """Send a follow-up prompt to an existing task (multi-turn)."""
        row = self.ledger.get_task(task_id)
        if not row:
            return {"error": "task_not_found", "task_id": task_id}
        try:
            self.ledger.reset_for_prompt(task_id)
            self.herdr.agent_prompt(task_id, text)
            self._wait_working(task_id, "done")
            return {"task_id": task_id, "state": "working"}
        except HerdrServerDown:
            return {"error": "herdr_server_down", "message": HERDR_DOWN_MSG}

    def kill(self, task_id):
        """Kill a task by closing its pane. Cleans up MCP temp config if present."""
        row = self.ledger.get_task(task_id)
        if not row:
            return {"error": "task_not_found", "task_id": task_id}
        # Clean up MCP temp config BEFORE closing pane (still have row data)
        if row.get("mcp_config_path"):
            try:
                os.unlink(row["mcp_config_path"])
            except FileNotFoundError:
                pass
            self.ledger.clear_mcp_path(task_id)
        try:
            self.herdr.pane_close(row["herdr_pane_id"])
            self.ledger.kill_task(task_id)
            return {"task_id": task_id, "state": "killed"}
        except HerdrServerDown:
            return {"error": "herdr_server_down", "message": HERDR_DOWN_MSG}

    def reconcile(self):
        """Sync ledger vs herdr. herdr auto-resumes on restart.
        Cleans up MCP temp configs for lost (dead) tasks."""
        try: agents = self.herdr.agent_list()
        except HerdrServerDown:
            time.sleep(2)
            try:
                agents = self.herdr.agent_list()
            except HerdrServerDown:
                return {"error": "herdr_server_down", "message": HERDR_DOWN_MSG}
        agent_map = {a["name"]: a for a in agents}
        results = []
        for row in self.ledger.list_all():
            tid = row["task_id"]
            if tid in agent_map:
                self.ledger.update_last_state(tid, "unknown")
                results.append({"task_id": tid, "status": "resumed"})
            else:
                # Lost task: clean up MCP temp config if present
                if row.get("mcp_config_path"):
                    try:
                        os.unlink(row["mcp_config_path"])
                    except FileNotFoundError:
                        pass
                    self.ledger.clear_mcp_path(tid)
                results.append({"task_id": tid, "status": "lost",
                                "claude_session_id": row["claude_session_id"]})
        ledger_ids = {r["task_id"] for r in self.ledger.list_all()}
        for agent in agents:
            if agent["name"] not in ledger_ids:
                results.append({"agent_name": agent["name"],
                                "pane_id": agent.get("pane_id"), "status": "orphaned"})
        return {"tasks": results}

    # --- internal helpers ---

    def _agent_get_with_retry(self, task_id):
        for _ in range(UNKNOWN_MAX_RETRIES):
            try:
                agent = self.herdr.agent_get(task_id)
            except AgentNotReady:
                return {"agent_status": "blocked"}
            except HerdrAPIError as e:
                if e.code == "agent_not_found": raise
                return None
            if agent.get("agent_status") != "unknown": return agent
            time.sleep(UNKNOWN_RETRY_INTERVAL)
        try: agent = self.herdr.agent_get(task_id)
        except AgentNotReady: return {"agent_status": "blocked"}
        except HerdrAPIError as e:
            if e.code == "agent_not_found": raise
            return None
        return None if agent.get("agent_status") == "unknown" else agent

    def _wait_working(self, task_id, prev_status="idle"):
        """Wait for agent to start working after a prompt (up to 10s).

        Returns the observed transition `T`: True if the loop saw it, False
        when the settle window was exhausted."""
        for _ in range(20):
            time.sleep(0.5)
            try: a = self.herdr.agent_get(task_id)
            except AgentNotReady: continue
            s = a.get("agent_status")
            if s == "working" or (s == "done" and prev_status != "done"): return True
        return False

    def _read_raw(self, task_id, pane_id):
        """Raw terminal text for the composer check, or "" if unobtainable.

        pane_read first, agent_read as fallback. Never parse_result()["text"]:
        that drops the ❯ composer line as a delimiter."""
        reads = (lambda: self.herdr.pane_read(pane_id, source="visible"),
                 lambda: self.herdr.agent_read(task_id, source="recent_unwrapped", format="text"))
        for read in reads:
            try: text = read()
            except (AgentNotReady, HerdrAPIError, HerdrTimeout): continue
            if isinstance(text, str) and text.strip(): return text
        return ""

    def _verdict(self, task_id, pane_id, transition):
        """Acceptance predicate T or (R and not P and M) — spec Unit 2 table.

        T is never overridden by the read-back; a disagreeing or unavailable
        composer check is recorded in the evidence, not acted on."""
        raw = self._read_raw(task_id, pane_id)
        reading = classify_composer(raw, sentinel_for(task_id))
        r, p, m = reading.legible, reading.sentinel_present, reading.marker_after_composer
        evidence = {
            "transition": transition,
            "composer_check": reading.composer_check if r else "unavailable",
            "sentinel_present": p if r else None,  # undecidable when not legible
            "marker_after_composer": m,
            "signals_consulted": ("transition",) + reading.signals_consulted,
        }
        if transition:
            return {"verified": True, "verified_by": "transition", "evidence": evidence}
        if r and not p and m:
            return {"verified": True, "verified_by": "marker", "evidence": evidence}
        reason = ("pane_unreadable" if not r else
                  "composer_holds_prompt" if p else "no_submission_evidence")
        return {"verified": False, "reason": reason, "evidence": evidence,
                "pane_text": raw[:200]}

    def _retry_unsubmitted(self, task_id, pane_id, prompt, first):
        """Unit 3's single re-submit, behind the confirmation sample.

        One settle interval later both inputs are re-sampled (fresh status ⇒
        fresh T, fresh read ⇒ fresh R/P/M) and the same predicate applied. Only
        a second all-clear (¬T ∧ R ∧ ¬P ∧ ¬M) re-submits. Otherwise the
        confirmation verdict stands with no re-submit: R1 — a T seen here is
        acceptance (verified_by: transition), never a recorded failure; P ⇒
        composer_holds_prompt; ¬R ⇒ pane_unreadable.

        On a live pane M never fires, so a fast accepted submission whose T was
        unsampled lands on no_submission_evidence — this sample is what stops a
        second typed prompt. Do not skip it to save a read.

        The re-submitted dispatch is verified once more and that verdict is
        final: at most two agent_prompt writes per dispatch. Residual window:
        a submission with no observable output across both samples is re-run
        once; both attempts' evidence is carried so that case is legible.
        """
        time.sleep(0.5)
        try: status = self.herdr.agent_get(task_id).get("agent_status")
        except AgentNotReady: status = "blocked"
        confirm = self._verdict(task_id, pane_id, status in ("working", "done"))
        confirm["first_attempt"] = first["evidence"]
        if confirm["verified"] or confirm["reason"] != "no_submission_evidence":
            return confirm
        self.herdr.agent_prompt(task_id, prompt)
        final = self._verdict(task_id, pane_id, self._wait_working(task_id, "idle"))
        final.update(first_attempt=first["evidence"], confirmation=confirm["evidence"],
                     resubmitted=True)
        return final

    def _collect(self, task_id, row):
        raw_text = self.herdr.agent_read(task_id, source="recent_unwrapped", format="text")
        parsed = parse_result(raw_text)
        affected = self.ledger.cas_collect(task_id, datetime.now(timezone.utc).isoformat(), json.dumps(parsed))
        if affected == 0: return self._cached(task_id, self.ledger.get_task(task_id))
        return {"task_id": task_id, "state": "done", "result": parsed}

    def _cached(self, task_id, row):
        cached = row["cached_result"]
        if isinstance(cached, str):
            try: cached = json.loads(cached)
            except (json.JSONDecodeError, TypeError): pass
        return {"task_id": task_id, "state": "done", "result": cached}

    def _resp(self, task_id, row, state):
        try:
            elapsed = int((datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])).total_seconds() / 60)
        except (ValueError, TypeError):
            elapsed = 0
        return {"task_id": task_id, "state": state,
                "overtime": elapsed > row["timeout_min"], "elapsed_min": elapsed}

    def _check_bare_flag(self):
        try: return "--bare" in subprocess.run(["ps","aux"],capture_output=True,text=True,timeout=5).stdout
        except Exception: return False