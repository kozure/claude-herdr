"""watch.py — the completion watcher (brief/watcher fix, 2026-10-10).

The gap this closes (TICKET-444 parent 2.0): a promoted pane finished its work,
the ledger learned it went idle, and nobody was told. `poll()` only runs when
someone asks, so a silent finish waits for a human. The watcher is the backstop
the pane's own report cannot be: it turns "a session stopped and nobody has
been told" into a notify.

Liveness discipline (AGENTS.md): *running* is never evidence, and silence is
never a trigger. Only terminal states are announced — never twice for the same
state:

    done        the pane finished and its result is collected
    blocked     waiting on a human (permission dialog, decision)
    lost        the session is gone (died, or the pane was never re-registered)

    idle        prompted but still settling — NOT terminal
    unknown     undetectable — silence is evidence, never a trigger
    killed      deliberate (kill() was called) — nothing to announce
    daemon_down herdr itself is unreachable — the whole pass is skipped

De-duplication is per (task, state) via ledger.notified_at + notified_state, so
a task announced as `blocked` that later reaches `done` is announced again.
Rows with no pane (`register_session` bookkeeping) and rows already `killed`
are never announced, and a `lost` row whose result was already collected is
closed business, not a silence.

History: the watcher post-dates the ledger, so its first run would otherwise
announce every stale row. `--baseline` reconciles that pre-watcher history
explicitly — it marks those rows instead of notifying, and prints what it
reconciled so the pass is never silent.

Usage (heartbeat or cron):
    python3 watch.py                  # poll every ledger task, announce new ones
    python3 watch.py --dry-run        # show what would be announced, send nothing
    python3 watch.py --baseline       # first run: reconcile history, notify nothing
    python3 watch.py --json           # machine-readable summary
"""
import argparse
import json

from ledger import Ledger
from notify import notify_completion

ANNOUNCEABLE = ("done", "blocked", "lost")
DIGEST_LIMIT = 240


def _digest(poll_result):
    """One-line digest of a poll result, for the notify text."""
    result = poll_result.get("result")
    if isinstance(result, dict):
        text = (result.get("text") or "").strip()
        if text:
            return " ".join(text.split())[:DIGEST_LIMIT]
        if result:
            return " ".join(json.dumps(result, default=str).split())[:DIGEST_LIMIT]
    state = poll_result.get("state", "?")
    elapsed = poll_result.get("elapsed_min")
    if elapsed is None:
        return f"state={state}"
    return f"state={state} elapsed={elapsed}min"


def watch_once(bridge=None, ledger=None, poller=None, notifier=None,
               dry_run=False, baseline=False):
    """Poll every ledger task; announce those whose terminal state is new.

    Returns {"checked", "announced", "baselined", "skipped", "failed"};
    `announced` holds {"task_id", "state", "digest", "notify"} entries. A
    notify that could not be sent is reported in `failed` and left unmarked,
    so the next pass retries instead of swallowing it. With `baseline=True`
    qualifying rows are marked (state `baseline`) instead of notified.
    """
    ledger = ledger or Ledger()
    ledger.init_schema()
    if poller is None:
        if bridge is None:
            from bridge import Bridge
            bridge = Bridge(ledger=ledger)
        poller = bridge.poll_all
    notifier = notifier or notify_completion

    polls = {p["task_id"]: p for p in (poller() or []) if p.get("task_id")}
    announced, skipped, failed, baselined = [], [], [], []

    # Iterate the same settled-filtered view the poller used. Walking every row
    # ever written just to skip it is the other half of the "slower every
    # minute" problem (2026-11-03 review, finding 9).
    rows = getattr(ledger, "list_unsettled", ledger.list_all)()
    for row in rows:
        task_id = row["task_id"]
        poll = polls.get(task_id)
        if poll is None:
            skipped.append(task_id)
            continue
        state = poll.get("state")
        # Non-terminal, deliberate, or bookkeeping-only: never announced.
        if (state not in ANNOUNCEABLE
                or not row.get("herdr_pane_id")
                or row.get("last_state") == "killed"):
            skipped.append(task_id)
            continue
        # A result that was collected before the pane disappeared is closed
        # business: that is cleanup, not a silence.
        if state == "lost" and row.get("collected_at"):
            skipped.append(task_id)
            continue
        if row.get("notified_at") is not None and row.get("notified_state") == state:
            skipped.append(task_id)
            continue

        digest = _digest(poll)
        if baseline:
            # Record the *real* state: the dedup key is (task, state), so a
            # literal "baseline" marker would never match and the row would be
            # announced again on the next pass. The reconciliation is
            # documented by what this pass prints, not in the column.
            ledger.mark_notified(task_id, state)
            baselined.append({"task_id": task_id, "state": state,
                              "digest": digest})
            continue
        if dry_run:
            announced.append({"task_id": task_id, "state": state,
                              "digest": digest, "notify": "dry-run"})
            continue

        pane = row.get("herdr_pane_id")
        status = notifier(
            run_id=task_id, verdict=state, grade="watcher",
            report_path=f"herdr pane {pane}" if pane else "",
            digest=digest)
        if isinstance(status, str) and status.startswith("UNSENT"):
            failed.append({"task_id": task_id, "state": state, "notify": status})
            continue
        ledger.mark_notified(task_id, state)
        announced.append({"task_id": task_id, "state": state,
                          "digest": digest, "notify": status})
    return {"checked": len(polls), "announced": announced,
            "baselined": baselined, "skipped": skipped, "failed": failed}


def main(argv=None):
    parser = argparse.ArgumentParser(description="herdr completion watcher")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be announced; send nothing")
    parser.add_argument("--baseline", action="store_true",
                        help="reconcile pre-watcher history: mark, never notify")
    parser.add_argument("--json", action="store_true",
                        help="print the summary as JSON")
    ns = parser.parse_args(argv)
    summary = watch_once(dry_run=ns.dry_run, baseline=ns.baseline)
    if ns.json:
        print(json.dumps(summary, indent=2))
        return 0
    print(f"watcher: checked {summary['checked']}, "
          f"announced {len(summary['announced'])}, "
          f"baselined {len(summary['baselined'])}, "
          f"skipped {len(summary['skipped'])}, "
          f"failed {len(summary['failed'])}")
    for b in summary["baselined"]:
        print(f"  ~ {b['task_id']}  [{b['state']}]  reconciled: "
              f"{b['digest'][:100]}")
    for a in summary["announced"]:
        print(f"  {a['task_id']}  [{a['state']}]  {a['digest'][:100]}  "
              f"notify={a['notify']}")
    for f in summary["failed"]:
        print(f"  ! {f['task_id']}  [{f['state']}]  notify={f['notify']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
