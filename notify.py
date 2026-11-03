"""notify.py — 3-layer completion notify (plan Task 3).

Layer 1    one-way push via `channels send` — target discovered first (N3),
           one retry max (one-way push is cheap).
Layer 2    background inter-agent submission (flagged --background, N1);
           the foreground primitive is prohibited by test (f).
Layer 3/4  completion_notice.md marker (always written) + explicit gap flag.

When layer 2 fires, the marker records the returned TASK_ID (M1).
No timer arguments exist anywhere in this module by design — the liveness
habit rule replaces timers; silence is evidence, never a trigger.
"""
import json
import os
import re
import subprocess
from datetime import datetime, timezone

AGENT_ID = "alfred"
TARGET_AGENT = "default"
# Fallback recipient when session discovery yields no user id. Override with
# CLAUDE_BRIDGE_NOTIFY_USER if your console user id differs.
DEFAULT_TARGET_USER = os.environ.get("CLAUDE_BRIDGE_NOTIFY_USER", "default")
MARKER_NAME = "completion_notice.md"
GAP_FLAG = "notify may not have landed"


def _parse_sessions(raw):
    """Parse `chats list` output. The real CLI emits a top-level JSON array;
    wrapped-dict shapes ({"sessions": [...]}) are kept for compatibility."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        if "{" not in raw:
            return []
        data = json.loads(raw[raw.index("{"):])   # tolerate prefix noise
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("sessions") or data.get("chats") or []
    return []


def _pick_best(sessions):
    """Most recently updated console session (real field: updated_at)."""
    best = None
    best_key = None
    for s in sessions:
        if not isinstance(s, dict):
            continue
        if s.get("channel") not in (None, "console"):
            continue
        key = str(s.get("updated_at") or s.get("updatedAt")
                  or s.get("id") or "")
        if best is None or key > best_key:
            best, best_key = s, key
    return best


class SubprocessTransport:
    """Real transport adapter. Output captured for status only."""

    def discover_target(self):
        argv = ["qwenpaw", "chats", "list", "--agent-id", AGENT_ID,
                "--channel", "console"]
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"chats list failed: {(proc.stderr or '')[:120]}")
        best = _pick_best(_parse_sessions(proc.stdout))
        if best is None:
            raise RuntimeError("no console session found for target discovery")
        return {
            "channel": best.get("channel") or "console",
            "target_user": best.get("user_id") or best.get("user")
                            or best.get("userName") or DEFAULT_TARGET_USER,
            "target_session": best.get("id") or best.get("sessionId"),
        }

    def push(self, argv, **kwargs):
        return subprocess.run(argv, capture_output=True, text=True)

    def submit_background(self, argv, **kwargs):
        return subprocess.run(argv, capture_output=True, text=True)


def _build_text(run_id, verdict, grade, report_path, digest):
    return (f"[notify] run {run_id} complete: verdict={verdict} grade={grade} "
            f"report={report_path} digest={digest}")


def notify_completion(run_id, verdict, grade, report_path, digest,
                      notice_dir=None, transport=None):
    """Fire the 3-layer notify. Returns a status string:
    'sent' | 'fallback' | 'UNSENT: <reason>'.

    Never raises *past* the caller in the sense that every failure is turned
    into a status string — but the last-resort marker write at the end can
    still raise if the notice directory cannot be created or written, which is
    deliberate: a silent notify failure is the one outcome that must not pass.
    """
    transport = transport or SubprocessTransport()
    if notice_dir is None:
        # Absolute: a relative path would put the marker in whatever directory
        # the process happened to start in (2026-11-03 review).
        notice_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "cases", run_id)
    text = _build_text(run_id, verdict, grade, report_path, digest)
    task_id = None
    reason = None

    # -- Layer 1: discover target, then push (one retry max) ----------------
    target = None
    try:
        target = transport.discover_target()
    except Exception as disc_err:               # noqa: BLE001
        reason = f"target discovery failed: {disc_err}"

    if target is not None:
        push_argv = [
            "qwenpaw", "channels", "send",
            "--agent-id", AGENT_ID,
            "--channel", target.get("channel") or "console",
            "--target-user", target.get("target_user") or DEFAULT_TARGET_USER,
            "--target-session", str(target.get("target_session")),
            "--text", text,
        ]
        # D4 failure injection (plan Task 6): env var forces the primary
        # push to fail so the fallback path is exercisable in demos.
        dead_socket = os.environ.get("HERDR_NOTIFY_DEAD_SOCKET") == "1"
        last_err = None
        for _ in range(2):                       # initial + one retry
            try:
                if dead_socket:
                    raise RuntimeError("HERDR_NOTIFY_DEAD_SOCKET=1 "
                                       "(injected primary failure)")
                proc = transport.push(push_argv)
                if getattr(proc, "returncode", 1) == 0:
                    status = "sent"
                    break
                last_err = RuntimeError(
                    f"push rc={getattr(proc, 'returncode', '?')} "
                    f"{(getattr(proc, 'stderr', '') or '')[:120]}")
            except Exception as exc:             # noqa: BLE001
                last_err = exc
        else:
            reason = f"push failed after retry: {last_err}"
            status = None
    else:
        status = None

    # -- Layer 2: background submission (N1: always the flagged form) -------
    if status is None:
        bg_argv = ["qwenpaw", "agents", "chat", "--background", "--from-agent", AGENT_ID, "--to-agent", TARGET_AGENT, "--text", text]
        try:
            proc = transport.submit_background(bg_argv)
            out = getattr(proc, "stdout", "") or ""
            m = re.search(r"\[TASK_ID:\s*([^\]]+)\]", out)
            if m:
                task_id = m.group(1).strip()
                status = "fallback"
            else:
                rc = getattr(proc, "returncode", "?")
                status = "UNSENT: background submission returned no TASK_ID " \
                         f"(rc={rc})"
        except Exception as exc:                 # noqa: BLE001
            status = f"UNSENT: background submission failed ({exc})"

    # -- Layer 3/4: marker, always; gap flag when unconfirmed ---------------
    os.makedirs(notice_dir, exist_ok=True)
    lines = [
        f"# completion notice — {run_id}",
        f"status: {status}",
        f"reason: {reason or 'n/a'}",
        f"run_id: {run_id}",
        f"verdict: {verdict}",
        f"grade: {grade}",
        f"report_path: {report_path}",
        f"digest: {digest}",
    ]
    if task_id is not None:                      # M1: field only if layer 2 fired
        lines.append(f"fallback_task_id: {task_id}")
    if status != "sent":
        lines.append(f"gap_flag: {GAP_FLAG}")
    lines.append(f"timestamp: {datetime.now(timezone.utc).isoformat()}")
    with open(os.path.join(notice_dir, MARKER_NAME), "w") as fh:
        fh.write("\n".join(lines) + "\n")

    return status
