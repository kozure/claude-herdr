"""Tests for timer.py — the `--tick` / `--list` entry point (parent 4.0).

Doubles over live servers: `notify.notify_completion` and the handlers are
monkeypatched, the DB is a `tmp_path` file reached through the one path
helper (`ledger._default_db_path`), and `now` is injected so every timing
case is a table, not a sleep.
"""
import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
from functools import partial

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ledger
import notify
import schedule
import timer
from schedule import LEASE_MIN_S, TICK_SUMMARY_KEYS

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = "2026-10-14T12:00:00+00:00"
PAST = "2026-10-14T11:00:00+00:00"
FUTURE = "2026-10-14T13:00:00+00:00"
STALE = "2026-10-14T10:00:00+00:00"   # 2 h before NOW: past every lease


# --- fixtures ----------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "ledger.db")
    monkeypatch.setattr(ledger, "_default_db_path", lambda: path)
    return path


@pytest.fixture
def opened(monkeypatch):
    """Every connection schedule hands out, so a test can inspect all of them."""
    conns = []
    real = schedule._connect

    def spy(*a, **kw):
        c = real(*a, **kw)
        conns.append(c)
        return c

    monkeypatch.setattr(schedule, "_connect", spy)
    return conns


@pytest.fixture
def conn(db_path):
    c = schedule._connect(db_path)
    schedule.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def notified(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        return "sent"

    monkeypatch.setattr(notify, "notify_completion", fake)
    return calls


def _add(conn, job_id, action, due_at=PAST, **kw):
    schedule.add_job(conn, job_id, action=action, caller_agent_id="alfred", due_at=due_at, **kw)


def _strand(conn, job_id):
    """Put a row in `firing` with a `fired_at` older than any lease."""
    assert schedule.claim_due(conn, job_id)
    with conn:
        conn.execute("UPDATE jobs SET fired_at=? WHERE job_id=?", (STALE, job_id))


def _row(conn, job_id):
    return dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())


def _run(capsys, argv, **kw):
    rc = timer.main(argv, now=NOW, **kw)
    out = capsys.readouterr().out
    return rc, out


# --- 4.1 the argument surface and --list --------------------------------------

def test_mode_set_is_exactly_tick_and_list():
    parser = timer.build_parser()
    group = parser._mutually_exclusive_groups[0]
    assert {a.option_strings[0] for a in group._group_actions} == {"--tick", "--list"}
    assert group.required
    options = {s for a in parser._actions for s in a.option_strings}
    assert options == {"-h", "--help", "--tick", "--list", "--allow-spend", "--no-watch"}


@pytest.mark.parametrize("argv", [[], ["--fire"], ["--tick", "--list"], ["--allow-spend"]])
def test_bad_arguments_exit_non_zero(argv, capsys):
    assert timer.main(argv) != 0


def test_list_renders_backlog_columns(db_path, conn, capsys):
    _add(conn, "job-due", "notify")
    _add(conn, "job-send", "send")
    _add(conn, "job-firing", "snapshot")
    assert schedule.claim_due(conn, "job-firing")
    rc, out = _run(capsys, ["--list"])
    assert rc == 0
    header = out.splitlines()[0].split()
    assert header == list(timer.LIST_COLUMNS)
    assert {"due_at", "attempts", "action", "caller_agent_id", "lease_s"} <= set(header)
    lines = {l.split()[0]: l.split() for l in out.splitlines()[1:-1]}
    assert lines["job-due"][1:4] == ["due", "notify", "alfred"]
    assert lines["job-due"][6] == str(max(LEASE_MIN_S, 60))
    assert lines["job-send"][6] == str(LEASE_MIN_S)   # UNBOUNDED => LEASE_MIN_S
    assert lines["job-firing"][1] == "firing"
    assert out.splitlines()[-1].startswith("counts: due=2 firing=1")


def test_lease_for_unbounded_is_stated_not_computed():
    assert schedule.lease_s("send") == LEASE_MIN_S
    assert schedule.lease_s("notify") == max(LEASE_MIN_S, 60)


# --- 4.2 the summary ----------------------------------------------------------

def test_summary_keys_are_exactly_the_frozen_tuple(db_path, conn, capsys):
    rc, out = _run(capsys, ["--tick", "--no-watch"])
    assert rc == 0
    assert set(json.loads(out)) == set(TICK_SUMMARY_KEYS)
    assert len(json.loads(out)) == len(TICK_SUMMARY_KEYS)


# --- 4.3 the dispatch table ---------------------------------------------------

def test_dispatch_covers_exactly_the_declared_actions():
    assert set(timer.DISPATCH) == set(schedule.ACTION_WORST_CASE_S)
    for action, (_handler, worst) in timer.DISPATCH.items():
        assert worst is schedule.ACTION_WORST_CASE_S[action]


@pytest.mark.parametrize("action", ["snapshot", "resume"])
def test_unavailable_actions_are_recorded_never_faked(action, db_path, conn, capsys):
    _add(conn, f"job-{action}", action)
    rc, out = _run(capsys, ["--tick", "--allow-spend", "--no-watch"])
    summary = json.loads(out)
    row = _row(conn, f"job-{action}")
    assert rc == 0
    assert summary["fired"] == 0
    assert row["status"] != "fired"
    assert "unavailable" in row["last_error"] and action in row["last_error"]
    assert row["attempts"] == 1


def test_send_reaches_bridge_send(db_path, conn, capsys, tmp_path, monkeypatch):
    import bridge
    sent = []

    class FakeBridge:
        def send(self, prompt, **kw):
            sent.append((prompt, kw))
            return {"task_id": "t-1"}

    monkeypatch.setattr(bridge, "Bridge", FakeBridge)
    payload = tmp_path / "prompt.txt"
    payload.write_text("hello")
    _add(conn, "job-send", "send", payload_ref=str(payload))
    rc, out = _run(capsys, ["--tick", "--allow-spend", "--no-watch"])
    assert rc == 0
    assert sent == [("hello", {"agent_id": "alfred"})]
    assert json.loads(out)["fired"] == 1


# --- 4.4 / 4.5 the spend gate --------------------------------------------------

def test_spend_gate_is_checked_before_the_claim(db_path, conn, monkeypatch, notified):
    _add(conn, "job-send", "send")
    _add(conn, "job-notify", "notify")
    claimed = []
    real = schedule.claim_due
    monkeypatch.setattr(schedule, "claim_due",
                        lambda c, j: claimed.append(j) or real(c, j))
    summary = timer.tick(conn, now=NOW)
    assert claimed == ["job-notify"]           # the gated row was never claimed
    row = _row(conn, "job-send")
    assert (row["status"], row["attempts"], row["last_error"]) == ("due", 0, "spend_gate")
    assert summary["spend_gate"] == 1


def test_one_hundred_gated_ticks_write_once(db_path, conn):
    _add(conn, "job-send", "send")
    before = conn.total_changes
    summaries = [timer.tick(conn, now=NOW) for _ in range(100)]
    assert conn.total_changes - before == 1
    assert all(s["spend_gate"] == 1 for s in summaries)
    row = _row(conn, "job-send")
    assert (row["status"], row["attempts"], row["last_error"]) == ("due", 0, "spend_gate")


# --- 4.6 the three-row tick ---------------------------------------------------

@pytest.mark.parametrize("stranded_action", ["notify", "send"])
def test_three_row_tick(stranded_action, db_path, conn, capsys, notified):
    _add(conn, "a-notify", "notify")
    _add(conn, "b-send", "send")
    _add(conn, "c-stranded", stranded_action)
    _strand(conn, "c-stranded")
    rc, out = _run(capsys, ["--tick", "--no-watch"])
    summary = json.loads(out)
    assert rc == 0

    a, b, c = (_row(conn, j) for j in ("a-notify", "b-send", "c-stranded"))
    assert a["status"] == "fired"
    assert (b["status"], b["attempts"], b["last_error"]) == ("due", 0, "spend_gate")
    assert summary["rearmed_lease"] == 1
    assert summary["lease_rearm_count"] == 1
    if stranded_action == "notify":           # bounded: recovered and fired once
        assert c["status"] == "fired"
        assert [n["run_id"] for n in notified] == ["a-notify", "c-stranded"]
        assert (summary["claimed"], summary["fired"], summary["spend_gate"]) == (2, 2, 1)
    else:                                     # UNBOUNDED: recovered, then spend-deferred
        assert (c["status"], c["attempts"], c["last_error"]) == ("due", 0, "spend_gate")
        assert [n["run_id"] for n in notified] == ["a-notify"]
        assert (summary["claimed"], summary["fired"], summary["spend_gate"]) == (1, 1, 2)
    fired = sum(r["status"] == "fired" for r in (a, b, c))
    assert summary["fired"] == fired


def test_stranded_bounded_row_fires_once_across_ticks(db_path, conn, notified):
    _add(conn, "job-n", "notify")
    _strand(conn, "job-n")
    first = timer.tick(conn, now=NOW)
    second = timer.tick(conn, now=NOW)
    assert len(notified) == 1
    assert first["lease_rearm_count"] == 1 and second["lease_rearm_count"] == 0
    assert _row(conn, "job-n")["status"] == "fired"


def test_skipped_mark_fired_counts_as_a_rearm(db_path, conn, monkeypatch, notified):
    _add(conn, "job-n", "notify")
    monkeypatch.setattr(schedule, "mark_fired", lambda c, j, n: False)
    summary = timer.tick(conn, now=NOW)
    assert summary["fired"] == 0 and summary["lease_rearm_count"] == 1


# --- 4.7 a tick is not an alarm ---------------------------------------------------

def test_failing_action_is_recorded_and_exits_zero(db_path, conn, capsys, monkeypatch):
    def boom(**kw):
        raise RuntimeError("push exploded")

    monkeypatch.setattr(notify, "notify_completion", boom)
    _add(conn, "job-n", "notify")
    rc, out = _run(capsys, ["--tick", "--no-watch"])
    row = _row(conn, "job-n")
    assert rc == 0
    assert json.loads(out)["claimed"] == 1 and json.loads(out)["fired"] == 0
    assert row["attempts"] == 1 and "push exploded" in row["last_error"]


def test_missing_config_is_recorded_and_exits_zero(db_path, conn, capsys, tmp_path,
                                                   monkeypatch, notified):
    monkeypatch.setenv(schedule.LIVENESS_TOML_ENV, str(tmp_path / "absent.toml"))
    _add(conn, "job-n", "notify", payload_ref=str(tmp_path / "absent-payload.md"))
    rc, out = _run(capsys, ["--tick", "--no-watch"])
    row = _row(conn, "job-n")
    assert rc == 0
    assert notified == []
    assert row["attempts"] == 1 and "absent-payload.md" in row["last_error"]


def test_unreadable_pane_is_counted_and_exits_zero(db_path, conn, capsys):
    def watchdog(c, now):
        raise TimeoutError("pane unreadable")

    rc, out = _run(capsys, ["--tick", "--no-watch"], watchdog=watchdog)
    assert rc == 0
    assert json.loads(out)["probe_miss"] == 1


def test_locked_db_leaves_row_as_found_and_exits_zero(db_path, conn, capsys, monkeypatch):
    _add(conn, "job-n", "notify")
    before = _row(conn, "job-n")

    def locked(c, j):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(schedule, "claim_due", locked)
    rc, out = _run(capsys, ["--tick", "--no-watch"])
    assert rc == 0
    assert json.loads(out)["locked"] == 1
    assert _row(conn, "job-n") == before


def test_unopenable_db_exits_non_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ledger, "_default_db_path", lambda: str(tmp_path))  # a directory
    assert timer.main(["--tick", "--no-watch"], now=NOW) != 0
    assert timer.main(["--list"], now=NOW) != 0


def test_no_write_transaction_spans_a_dispatch(db_path, conn, opened, monkeypatch, capsys):
    seen = []

    def handler(job):
        seen.append([c.in_transaction for c in opened])

    monkeypatch.setitem(timer.DISPATCH, "notify", (handler, 60))
    _add(conn, "job-1", "notify")
    _add(conn, "job-2", "notify")
    _add(conn, "job-send", "send")            # a gate write happens before the dispatches
    rc, _ = _run(capsys, ["--tick", "--no-watch"])
    assert rc == 0
    assert len(seen) == 2
    assert opened and all(flag is False for flags in seen for flag in flags)
    assert conn.in_transaction is False


# --- 4.8 no new channel ---------------------------------------------------------

def test_notify_job_reaches_the_three_layer_path(db_path, conn, notified, tmp_path):
    payload = tmp_path / "msg.md"
    payload.write_text("Check the deploy\n")
    _add(conn, "job-n", "notify", payload_ref=str(payload))
    timer.tick(conn, now=NOW)
    assert notified == [{"run_id": "job-n", "verdict": "due", "grade": "timer:alfred",
                         "report_path": str(payload), "digest": "Check the deploy"}]


def test_timer_opens_no_socket_and_defines_no_notifier():
    src = inspect.getsource(timer)
    assert "import socket" not in src and "socket." not in src
    assert "subprocess" not in src and "channels send" not in src
    names = {n for n, v in vars(timer).items() if inspect.isfunction(v) and v.__module__ == "timer"}
    assert not {n for n in names if "notif" in n.lower() and n != "_notify"}


def test_notify_unchanged_when_timer_never_imported():
    code = (
        "import sys, notify\n"
        "class T:\n"
        "    def discover_target(self): return {'target_session': 's'}\n"
        "    def push(self, argv, **kw):\n"
        "        class P: returncode = 0\n"
        "        return P()\n"
        "import tempfile\n"
        "s = notify.notify_completion('r', 'v', 'g', 'p', 'd', notice_dir=tempfile.mkdtemp(), transport=T())\n"
        "assert 'timer' not in sys.modules and 'schedule' not in sys.modules\n"
        "print(s)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "sent"


# --- 4.9 the best-effort watcher -------------------------------------------------

def test_watcher_never_changes_the_verdict(db_path, conn, capsys, notified):
    def raising():
        raise RuntimeError("watcher down")

    results = []
    for kw in ({"watcher": raising}, {"watcher": lambda: None}):
        _add(conn, f"job-{len(results)}", "notify")
        results.append(_run(capsys, ["--tick"], **kw))
    _add(conn, "job-x", "notify")
    results.append(_run(capsys, ["--tick", "--no-watch"]))
    assert [rc for rc, _ in results] == [0, 0, 0]
    assert len({out for _, out in results}) == 1   # identical summaries


# --- 4.11 the probe pass on every tick ----------------------------------------

class _FakeHerdr:
    """herdr double: `agent_get` answers the scripted status, or raises.

    `pane_read` is never reached by these cases — a non-`idle` status returns
    before it, and a raising `agent_get` returns before it — so reaching it is
    the test failing.
    """

    def __init__(self, status=None, exc=None):
        self.status, self.exc, self.calls = status, exc, []

    def agent_get(self, agent_id):
        self.calls.append(agent_id)
        if self.exc is not None:
            raise self.exc
        return {"agent_status": self.status, "pane_id": "p1"}

    def pane_read(self, pane, **kw):
        raise AssertionError("pane_read must not be reached by these cases")


def _notifier_double():
    calls = []

    def rec(*a, **kw):
        calls.append((a, kw))
        return "sent"

    return rec, calls


def _probe_row(conn, job_id):
    """A `due`, not-yet-due row — older than both probe tiers, thresholds on the row."""
    schedule.add_job(conn, job_id, action="notify", caller_agent_id="alfred",
                     agent_id=f"task-{job_id}", due_at=FUTURE, warn_s=1, stuck_s=2)
    with conn:
        conn.execute("UPDATE jobs SET created_at=? WHERE job_id=?", (STALE, job_id))


def test_main_runs_the_probe_pass_and_no_watch_scopes_to_the_watcher(
        db_path, conn, monkeypatch, capsys):
    import watch
    _probe_row(conn, "job-probe")
    fake, seen, watched = _FakeHerdr(exc=TimeoutError("pane unreadable")), [], []
    real = schedule.watchdog
    rec, sent = _notifier_double()

    def spy(c, n, **kw):
        seen.append((c, n))
        return real(c, n, client=fake, notifier=rec)

    monkeypatch.setattr(schedule, "watchdog", spy)
    monkeypatch.setattr(watch, "watch_once", lambda: watched.append(1))

    rc, out = _run(capsys, ["--tick", "--no-watch"])        # no double injected by the test
    assert rc == 0
    assert len(seen) == 1                                   # main() wired the real pass
    assert json.loads(out)["probe_miss"] == 1
    row = _row(conn, "job-probe")
    assert (row["status"], row["attempts"]) == ("due", 0)
    assert row["last_error"].startswith("probe_undecidable ")
    assert watched == []                                    # --no-watch held the watcher back

    rc, _ = _run(capsys, ["--tick"])                        # without --no-watch it runs
    assert rc == 0 and len(seen) == 2 and watched == [1]
    assert len(sent) == 1                                   # the ladder reached the double


def test_second_consecutive_miss_raises_exactly_one_stuck_candidate(db_path, conn, capsys):
    _probe_row(conn, "job-probe")
    fake = _FakeHerdr(exc=TimeoutError("pane unreadable"))
    rec, sent = _notifier_double()
    ladder = partial(schedule.watchdog, client=fake, notifier=rec)

    runs = [_run(capsys, ["--tick", "--no-watch"], watchdog=ladder) for _ in range(3)]
    assert [rc for rc, _ in runs] == [0, 0, 0]
    assert [json.loads(out)["stuck_candidates"] for _, out in runs] == [0, 1, 0]
    assert len(sent) == 1                                   # latched: it cannot notify twice
    (args, _kw), = sent
    assert args[0] == "stuck-job-probe" and args[1] == schedule.STUCK_LATCH
    assert _row(conn, "job-probe")["last_error"].startswith("stuck_candidate ")


def test_alive_probe_clears_the_counter_and_notifies_nobody(db_path, conn, capsys):
    _probe_row(conn, "job-probe")
    rec, sent = _notifier_double()
    dead = _FakeHerdr(exc=TimeoutError("pane unreadable"))
    _run(capsys, ["--tick", "--no-watch"],
         watchdog=partial(schedule.watchdog, client=dead, notifier=rec))
    assert _row(conn, "job-probe")["last_error"].startswith("probe_undecidable ")

    alive = _FakeHerdr(status="working")
    rc, out = _run(capsys, ["--tick", "--no-watch"],
                   watchdog=partial(schedule.watchdog, client=alive, notifier=rec))
    assert rc == 0
    assert json.loads(out)["probe_miss"] == 0
    assert json.loads(out)["stuck_candidates"] == 0
    assert _row(conn, "job-probe")["last_error"] is None
    assert sent == []


#: The positional shape of every `schedule.py` callable `timer.py` touches.
#: Keyword-only seams may be added; a positional change is a freeze violation.
PINNED_SIGNATURES = {
    "_connect": ["path"],
    "ensure_schema": ["conn"],
    "recover_leases": ["conn", "now"],
    "due_jobs": ["conn", "now"],
    "try_claim": ["conn", "job_id", "summary"],
    "list_jobs": ["conn"],
    "status_counts": ["conn"],
    "mark_fired": ["conn", "job_id", "now"],
    "mark_failed": ["job_id", "error"],
    "lease_s": ["action"],
    "watchdog": ["conn", "now"],
}


def _positional(func):
    return [p.name for p in inspect.signature(func).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]


def test_schedule_callables_timer_touches_are_pinned():
    for name, params in PINNED_SIGNATURES.items():
        assert _positional(getattr(schedule, name)) == params, name


def test_every_schedule_name_timer_references_exists():
    refs = set(re.findall(r"\bschedule\.([A-Za-z_]\w*)", inspect.getsource(timer)))
    assert refs >= set(PINNED_SIGNATURES)
    assert {n for n in refs if not hasattr(schedule, n)} == set()
