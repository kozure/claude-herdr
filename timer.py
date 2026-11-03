"""timer.py — the entry point launchd drives: `--tick` fires what is due, `--list` shows the backlog.

A tick re-arms stranded leases, selects due jobs, claims each one atomically,
dispatches its action, records the outcome on the row and prints one JSON
summary carrying exactly `schedule.TICK_SUMMARY_KEYS`.

Three properties are structural, not conventions:

* **Spend gate before the claim.** `send` and `resume` start paid work. Without
  the operator-only `--allow-spend` they are never claimed: the row stays
  `due`, `attempts` is untouched, and `last_error='spend_gate'` is written once,
  on the first deferral only. Every later tick only counts it.
* **A tick is not an alarm.** A failing action, an unreadable pane, a missing
  config or a locked DB is recorded on the row and in the summary, and the tick
  still exits 0. Only operator errors — bad arguments, an unopenable DB — exit
  non-zero.
* **No write transaction spans a dispatch.** Every write is one short statement
  committed before the next step; a handler never runs inside a transaction.

`notify` reaches the world only through `notify.py`'s existing three-layer path
and `send` only through `bridge.Bridge.send`; this module opens no socket and
defines no notifier. `snapshot` / `resume` are recorded as unavailable (their
owner is item b) — never faked.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

import ledger
import notify
import schedule
from schedule import ACTION_WORST_CASE_S, TICK_SUMMARY_KEYS

#: Actions that start a paid Claude session. Gated before the claim.
SPEND_ACTIONS = frozenset({"send", "resume"})

#: The `last_error` a spend-gate deferral leaves on the row, written once.
SPEND_GATE = "spend_gate"

#: Statuses `--list` shows: the backlog, not the history.
LIST_STATUSES = ("due", "firing", "failed")


class ActionUnavailable(RuntimeError):
    """The action has a declared worst case but no handler built yet (residual R-2)."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _is_locked(exc):
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc)


def _payload(job):
    """Read the payload file `payload_ref` names. A missing file is the action failing."""
    ref = job["payload_ref"]
    if not ref:
        return ""
    with open(ref, encoding="utf-8") as fh:
        return fh.read()


# --- the dispatch table ------------------------------------------------------

def _notify(job):
    status = notify.notify_completion(
        run_id=job["job_id"], verdict="due", grade=f"timer:{job['agent_id']}",
        report_path=job["payload_ref"] or "", digest=_payload(job).strip()[:500])
    if isinstance(status, str) and status.startswith("UNSENT"):
        raise RuntimeError(status)


def _send(job):
    from bridge import Bridge  # lazy: constructing it reaches herdr
    result = Bridge().send(_payload(job), agent_id=job["agent_id"])
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(f"send: {result['error']}: {result.get('message', '')}")


def _unavailable(job):
    raise ActionUnavailable(f"action_unavailable: {job['action']} is not built yet (item b)")


#: action -> (handler, declared worst case). One entry per `ACTION_WORST_CASE_S` key.
DISPATCH = {
    "notify": (_notify, ACTION_WORST_CASE_S["notify"]),
    "send": (_send, ACTION_WORST_CASE_S["send"]),
    "snapshot": (_unavailable, ACTION_WORST_CASE_S["snapshot"]),
    "resume": (_unavailable, ACTION_WORST_CASE_S["resume"]),
}


# --- the tick ----------------------------------------------------------------

def _defer(conn, job_id):
    """The one spend-gate write: guarded so a second deferral writes nothing."""
    with conn:
        conn.execute(
            "UPDATE jobs SET last_error=? WHERE job_id=? AND status='due' "
            "AND last_error IS NOT ?", (SPEND_GATE, job_id, SPEND_GATE))


def tick(conn, now=None, allow_spend=False, watchdog=None, watcher=None):
    """Run one tick and return the summary dict (exactly `TICK_SUMMARY_KEYS`).

    `watchdog(conn, now)` is the probe pass (2.0); it may return counts for
    `probe_miss` / `stuck_candidates`. `watcher()` is the best-effort
    `watch.py` pass (assumption Q2). Neither can change the tick's verdict.
    """
    now = now or _now()
    summary = dict.fromkeys(TICK_SUMMARY_KEYS, 0)

    try:
        rearmed = schedule.recover_leases(conn, now)
        summary["rearmed_lease"] = rearmed
        summary["lease_rearm_count"] += rearmed
    except sqlite3.OperationalError as exc:
        if not _is_locked(exc):
            raise
        summary["locked"] += 1

    try:
        due = schedule.due_jobs(conn, now)
    except sqlite3.OperationalError as exc:
        if not _is_locked(exc):
            raise
        summary["locked"] += 1
        due = []

    for job in due:
        job_id, action = job["job_id"], job["action"]
        if action in SPEND_ACTIONS and not allow_spend:
            summary["spend_gate"] += 1
            if job["last_error"] != SPEND_GATE:
                try:
                    _defer(conn, job_id)
                except sqlite3.OperationalError as exc:
                    if not _is_locked(exc):
                        raise
                    summary["locked"] += 1
            continue

        if not schedule.try_claim(conn, job_id, summary):  # locked => counted, row untouched
            continue
        summary["claimed"] += 1

        handler, _worst = DISPATCH[action]
        try:
            handler(job)
        except Exception as exc:  # noqa: BLE001 — a failing action is recorded, never raised
            try:
                schedule.mark_failed(job_id, f"{type(exc).__name__}: {exc}")
            except sqlite3.OperationalError as db_exc:
                if not _is_locked(db_exc):
                    raise
                summary["locked"] += 1  # row stays `firing`; its lease recovers it
            continue

        try:
            landed = schedule.mark_fired(conn, job_id, now)
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc):
                raise
            summary["locked"] += 1
            landed = False
        if landed:
            summary["fired"] += 1
        else:
            # A skipped `mark_fired` is itself a re-fire source (Unit 1, r3).
            summary["lease_rearm_count"] += 1

    if watchdog is not None:
        try:
            counts = watchdog(conn, now) or {}
            summary["probe_miss"] += int(counts.get("probe_miss", 0))
            summary["stuck_candidates"] += int(counts.get("stuck_candidates", 0))
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc):
                raise
            summary["locked"] += 1
        except Exception:  # noqa: BLE001 — an unreadable pane is not an alarm
            summary["probe_miss"] += 1

    if watcher is not None:
        try:
            watcher()
        except Exception:  # noqa: BLE001 — best-effort by contract (Q2)
            pass

    return summary


# --- the list ----------------------------------------------------------------

LIST_COLUMNS = ("job_id", "status", "action", "agent_id", "due_at", "attempts",
                "lease_s", "last_error")


def render_list(conn):
    rows = [r for r in schedule.list_jobs(conn) if r["status"] in LIST_STATUSES]
    table = [LIST_COLUMNS] + [
        tuple(str(v) for v in (r["job_id"], r["status"], r["action"], r["agent_id"],
                               r["due_at"], r["attempts"], schedule.lease_s(r["action"]),
                               r["last_error"] or "-"))
        for r in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(LIST_COLUMNS))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in table]
    counts = schedule.status_counts(conn)
    lines.append("counts: " + " ".join(f"{k}={counts.get(k, 0)}"
                                       for k in schedule.STATUS_VOCABULARY))
    return "\n".join(lines)


# --- the CLI -----------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="claude_bridge timer: fire due jobs")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tick", action="store_true", help="fire what is due, print a JSON summary")
    mode.add_argument("--list", action="store_true", help="show due / firing / failed rows")
    parser.add_argument("--allow-spend", action="store_true",
                        help="operator-only: allow send/resume. Never in the plist.")
    parser.add_argument("--no-watch", action="store_true",
                        help="skip the best-effort watch.py pass")
    return parser


def _default_watcher():
    import watch
    watch.watch_once()


def main(argv=None, now=None, watchdog=None, watcher=None):
    try:
        ns = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse already printed the usage error
        return exc.code or 2

    try:
        # One path, resolved at call time: `mark_failed(job_id, error)` takes no
        # connection, so a second path here would split one tick across two DBs.
        conn = schedule._connect(ledger._default_db_path())
        schedule.ensure_schema(conn)
    except sqlite3.OperationalError as exc:
        if ns.tick and _is_locked(exc):
            summary = dict.fromkeys(TICK_SUMMARY_KEYS, 0)
            summary["locked"] = 1
            print(json.dumps(summary))
            return 0
        print(f"timer: cannot open DB: {exc}", file=sys.stderr)
        return 2

    try:
        if ns.list:
            print(render_list(conn))
            return 0
        # Every real `--tick` runs the probe pass (2.0). The `watchdog`
        # parameter stays the test seam; `--no-watch` scopes to the watch.py
        # pass alone and never disables the probe (spec N-2).
        if watchdog is None:
            watchdog = schedule.watchdog
        if watcher is None and not ns.no_watch:
            watcher = _default_watcher
        summary = tick(conn, now=now, allow_spend=ns.allow_spend,
                       watchdog=watchdog, watcher=None if ns.no_watch else watcher)
        print(json.dumps(summary))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
