"""Tests for the completion watcher — the brief/watcher fix (2026-10-10).

The watcher exists because a promoted pane can finish with nobody told:
`poll()` only runs when someone asks. These tests pin the contract that
matters — terminal states are announced once, non-terminal states and
deliberate kills are never announced, and a notify that could not be sent
is retried rather than swallowed.
"""
import json

import pytest

from ledger import Ledger
import watch as watch_module
from watch import _digest, main, watch_once


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.init_schema()
    yield db
    db.close()


def _seed(ledger, task_id, *, pane="w1:p1", state="idle", prompt="p", cwd="/tmp"):
    ledger.insert_task(task_id, pane, "w1", f"s-{task_id}", prompt, cwd, 30,
                       pending_result=bool(prompt))
    ledger.update_last_state(task_id, state)
    return task_id


def _poller(states):
    """A stub poll_all() over {task_id: state}."""
    def poll():
        return [{"task_id": tid, "state": state,
                 "result": {"text": f"work for {tid}"}}
                for tid, state in states.items()]
    return poll


class RecordingNotifier:
    def __init__(self, status="sent"):
        self.calls = []
        self.status = status

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.status


# ---------------------------------------------------------------------------
# Which states get announced
# ---------------------------------------------------------------------------

def test_announces_terminal_states_only(ledger):
    for tid, state in (("t-done", "done"), ("t-blocked", "blocked"),
                       ("t-lost", "lost"), ("t-running", "running"),
                       ("t-idle", "idle"), ("t-unknown", "unknown")):
        _seed(ledger, tid, state=state)
    notifier = RecordingNotifier()
    summary = watch_once(ledger=ledger, poller=_poller({
        "t-done": "done", "t-blocked": "blocked", "t-lost": "lost",
        "t-running": "running", "t-idle": "idle", "t-unknown": "unknown",
    }), notifier=notifier)

    assert [a["task_id"] for a in summary["announced"]] == [
        "t-done", "t-blocked", "t-lost"]
    assert sorted(summary["skipped"]) == [
        "t-idle", "t-running", "t-unknown"]
    assert len(notifier.calls) == 3
    assert {c["verdict"] for c in notifier.calls} == {"done", "blocked", "lost"}


def test_running_is_never_a_trigger(ledger):
    """Silence while running is evidence, never a trigger (AGENTS.md)."""
    _seed(ledger, "t1", state="working")
    notifier = RecordingNotifier()
    summary = watch_once(ledger=ledger, poller=_poller({"t1": "running"}),
                         notifier=notifier)
    assert summary["announced"] == []
    assert notifier.calls == []


def test_killed_pane_is_never_announced(ledger):
    """A deliberate kill is not a silence to surface."""
    _seed(ledger, "t1", state="killed")
    notifier = RecordingNotifier()
    summary = watch_once(ledger=ledger, poller=_poller({"t1": "lost"}),
                         notifier=notifier)
    assert summary["announced"] == []
    assert notifier.calls == []


def test_pane_less_bookkeeping_row_is_never_announced(ledger):
    """register_session rows carry no pane; poll_all reports them 'lost'."""
    _seed(ledger, "t1", pane=None, state="idle")
    notifier = RecordingNotifier()
    summary = watch_once(ledger=ledger, poller=_poller({"t1": "lost"}),
                         notifier=notifier)
    assert summary["announced"] == []
    assert notifier.calls == []


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------

def test_announced_once(ledger):
    _seed(ledger, "t1", state="working")
    notifier = RecordingNotifier()
    poller = _poller({"t1": "done"})
    first = watch_once(ledger=ledger, poller=poller, notifier=notifier)
    second = watch_once(ledger=ledger, poller=poller, notifier=notifier)
    assert len(first["announced"]) == 1
    assert second["announced"] == []
    assert len(notifier.calls) == 1
    assert ledger.get_task("t1")["notified_state"] == "done"


def test_blocked_then_done_is_announced_again(ledger):
    """The state key means a later transition is not swallowed."""
    _seed(ledger, "t1", state="working")
    notifier = RecordingNotifier()
    watch_once(ledger=ledger, poller=_poller({"t1": "blocked"}),
               notifier=notifier)
    summary = watch_once(ledger=ledger, poller=_poller({"t1": "done"}),
                         notifier=notifier)
    assert [a["state"] for a in summary["announced"]] == ["done"]
    assert len(notifier.calls) == 2


# ---------------------------------------------------------------------------
# Notify failure is surfaced, not swallowed
# ---------------------------------------------------------------------------

def test_unsent_notify_is_not_marked_and_is_retried(ledger):
    _seed(ledger, "t1", state="working")
    failing = RecordingNotifier(status="UNSENT: push failed")
    first = watch_once(ledger=ledger, poller=_poller({"t1": "done"}),
                       notifier=failing)
    assert first["announced"] == []
    assert first["failed"] == [{"task_id": "t1", "state": "done",
                                "notify": "UNSENT: push failed"}]
    assert ledger.get_task("t1")["notified_at"] is None

    working = RecordingNotifier()
    second = watch_once(ledger=ledger, poller=_poller({"t1": "done"}),
                        notifier=working)
    assert [a["task_id"] for a in second["announced"]] == ["t1"]
    assert ledger.get_task("t1")["notified_at"] is not None


def test_notify_payload_points_at_the_pane(ledger):
    _seed(ledger, "t1", pane="w1Q:p1", state="working")
    notifier = RecordingNotifier()
    watch_once(ledger=ledger, poller=_poller({"t1": "done"}), notifier=notifier)
    call = notifier.calls[0]
    assert call["run_id"] == "t1"
    assert call["report_path"] == "herdr pane w1Q:p1"
    assert call["digest"] == "work for t1"
    assert call["grade"] == "watcher"


# ---------------------------------------------------------------------------
# Dry run, digest, CLI
# ---------------------------------------------------------------------------

def test_dry_run_sends_nothing_and_marks_nothing(ledger):
    _seed(ledger, "t1", state="working")
    notifier = RecordingNotifier()
    summary = watch_once(ledger=ledger, poller=_poller({"t1": "done"}),
                         notifier=notifier, dry_run=True)
    assert [a["task_id"] for a in summary["announced"]] == ["t1"]
    assert summary["announced"][0]["notify"] == "dry-run"
    assert notifier.calls == []
    assert ledger.get_task("t1")["notified_at"] is None


def test_digest_shapes():
    assert _digest({"state": "done", "result": {"text": "a\nb"}}) == "a b"
    assert _digest({"state": "done", "result": {}}) == "state=done"
    assert _digest({"state": "lost", "elapsed_min": 3}) == "state=lost elapsed=3min"
    long = _digest({"state": "done", "result": {"text": "x" * 500}})
    assert len(long) == watch_module.DIGEST_LIMIT


def test_cli_json_summary(monkeypatch, capsys):
    monkeypatch.setattr(watch_module, "watch_once",
                        lambda **kw: {"checked": 2, "announced": [], "baselined": [],
                                      "skipped": [], "failed": []})
    assert main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["checked"] == 2


def test_cli_reports_failures(monkeypatch, capsys):
    monkeypatch.setattr(watch_module, "watch_once",
                        lambda **kw: {"checked": 1, "announced": [], "baselined": [],
                                      "skipped": [],
                                      "failed": [{"task_id": "t1",
                                                  "state": "done",
                                                  "notify": "UNSENT: nope"}]})
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "failed 1" in out and "UNSENT: nope" in out


# ---------------------------------------------------------------------------
# Closed business vs. a real silence
# ---------------------------------------------------------------------------

def _mark_collected(ledger, task_id, when="2026-10-10T09:00:00+00:00"):
    ledger.conn.execute("UPDATE tasks SET collected_at=? WHERE task_id=?",
                        (when, task_id))
    ledger.conn.commit()


def test_lost_after_collection_is_closed_business(ledger):
    """The pane disappearing after its result was collected is cleanup."""
    _seed(ledger, "t1", state="collected")
    _mark_collected(ledger, "t1")
    notifier = RecordingNotifier()
    summary = watch_once(ledger=ledger, poller=_poller({"t1": "lost"}),
                         notifier=notifier)
    assert summary["announced"] == []
    assert notifier.calls == []


def test_lost_without_a_collected_result_is_announced(ledger):
    """A session that vanished mid-flight is exactly the silence to surface."""
    _seed(ledger, "t1", state="working")
    notifier = RecordingNotifier()
    summary = watch_once(ledger=ledger, poller=_poller({"t1": "lost"}),
                         notifier=notifier)
    assert [a["task_id"] for a in summary["announced"]] == ["t1"]
    assert len(notifier.calls) == 1


# ---------------------------------------------------------------------------
# Baseline reconciliation (first run over pre-watcher history)
# ---------------------------------------------------------------------------

def test_baseline_marks_instead_of_notifying(ledger):
    _seed(ledger, "t1", state="working")
    notifier = RecordingNotifier()
    summary = watch_once(ledger=ledger, poller=_poller({"t1": "done"}),
                         notifier=notifier, baseline=True)
    assert [b["task_id"] for b in summary["baselined"]] == ["t1"]
    assert summary["announced"] == []
    assert notifier.calls == []
    assert ledger.get_task("t1")["notified_state"] == "done"
    # Regression: recording a literal "baseline" state never matched the
    # dedup key, so the reconciled rows were announced on the next pass.
    second = watch_once(ledger=ledger, poller=_poller({"t1": "done"}),
                        notifier=notifier)
    assert second["announced"] == []
    assert notifier.calls == []


def test_baseline_does_not_swallow_a_later_transition(ledger):
    _seed(ledger, "t1", state="working")
    watch_once(ledger=ledger, poller=_poller({"t1": "blocked"}),
               notifier=RecordingNotifier(), baseline=True)
    notifier = RecordingNotifier()
    summary = watch_once(ledger=ledger, poller=_poller({"t1": "done"}),
                         notifier=notifier)
    assert [a["state"] for a in summary["announced"]] == ["done"]


def test_cli_prints_baselined_rows(monkeypatch, capsys):
    monkeypatch.setattr(watch_module, "watch_once",
                        lambda **kw: {"checked": 1, "announced": [],
                                      "skipped": [], "failed": [],
                                      "baselined": [{"task_id": "t9",
                                                     "state": "lost",
                                                     "digest": "state=lost"}]})
    assert main(["--baseline"]) == 0
    out = capsys.readouterr().out
    assert "baselined 1" in out and "reconciled: state=lost" in out
