"""Contract tests for `schedule.py` — the surfaces frozen in parent `0.0`.

Every assertion in this section is an **exact** equality (set equality or a
frozen signature string). That is deliberate: these are the surfaces three
files share, so the failure mode worth catching is a later parent *widening* a
frozen set or silently re-shaping a signature. A subset check would let that
through; equality fails loudly and points at the parent that moved.

Sections below this one belong to the parents that implement the surfaces
(`1.0` substrate, `2.0` watchdog, `3.0` failure path).
"""
import dataclasses
import inspect
import sqlite3

import pytest

import schedule
from schedule import (
    ACTION_WORST_CASE_S,
    LIVENESS_TOML_ENV,
    LEASE_MIN_S,
    ProbeResult,
    REASONS,
    RESTATED_LIVENESS,
    STATUS_VOCABULARY,
    TICK_SUMMARY_KEYS,
    UNBOUNDED,
    VERDICTS,
)

NOW = "2026-10-14T00:00:00+00:00"

# --- 0.1 the row shape ------------------------------------------------------

EXPECTED_JOBS_COLUMNS = (
    "job_id",
    "due_at",
    "created_at",
    "status",
    "action",
    "payload_ref",
    "agent_id",
    "attempts",
    "max_attempts",
    "backoff_s",
    "fired_at",
    "last_error",
    "probe",
    "warn_s",
    "stuck_s",
)


def test_jobs_columns_are_exactly_fifteen_and_in_order():
    assert schedule.JOBS_COLUMNS == EXPECTED_JOBS_COLUMNS
    assert len(schedule.JOBS_COLUMNS) == 15
    # duplicates would still "count" as 15 only if one name were dropped
    assert len(set(schedule.JOBS_COLUMNS)) == 15


def test_run_id_is_the_one_deliberate_omission():
    assert "run_id" not in schedule.JOBS_COLUMNS


def test_status_vocabulary_and_lease_floor():
    assert STATUS_VOCABULARY == ("due", "firing", "fired", "failed", "cancelled")
    assert set(STATUS_VOCABULARY) == {"due", "firing", "fired", "failed", "cancelled"}
    assert LEASE_MIN_S == 120


# --- 0.2 the action surface -------------------------------------------------

EXPECTED_TICK_SUMMARY_KEYS = (
    "claimed",
    "fired",
    "rearmed_lease",
    "spend_gate",
    "locked",
    "probe_miss",
    "stuck_candidates",
    "lease_rearm_count",
)


def test_action_worst_case_covers_exactly_the_dispatch_table():
    # The dispatch table is what makes the lease derivable, so a declared
    # worst case per action is not optional, and an extra action here without
    # a dispatch handler is just as wrong as a missing one.
    assert set(ACTION_WORST_CASE_S) == {"notify", "send", "snapshot", "resume"}


def test_send_declares_the_unbounded_sentinel():
    assert ACTION_WORST_CASE_S["send"] is UNBOUNDED
    assert str(UNBOUNDED) == "UNBOUNDED"


def test_the_unbounded_class_is_stated_not_computed():
    # `max(LEASE_MIN_S, UNBOUNDED)` must not produce a number: the rule for
    # that class is that the lease *is* LEASE_MIN_S, stated rather than derived.
    with pytest.raises(TypeError):
        max(LEASE_MIN_S, UNBOUNDED)


def test_bounded_actions_declare_a_positive_worst_case():
    for action, worst in ACTION_WORST_CASE_S.items():
        if action == "send":
            continue
        assert isinstance(worst, int) and worst > 0, action


def test_tick_summary_keys_exact_set_and_order():
    assert tuple(TICK_SUMMARY_KEYS) == EXPECTED_TICK_SUMMARY_KEYS
    assert set(TICK_SUMMARY_KEYS) == set(EXPECTED_TICK_SUMMARY_KEYS)
    assert len(TICK_SUMMARY_KEYS) == 8


# --- 0.3 the observation surface -------------------------------------------

def test_probe_result_is_frozen_with_exactly_four_fields_in_order():
    assert [f.name for f in dataclasses.fields(ProbeResult)] == [
        "agent_status",
        "read_error",
        "composer",
        "signals_consulted",
    ]
    assert dataclasses.fields(ProbeResult)[0].name == "agent_status"


def test_probe_result_mutation_raises():
    # The probe record carries the ladder's evidence verbatim; a mutable bag
    # would let a later stage edit an observation it is reporting.
    reading = ProbeResult(
        agent_status="idle", read_error=None, composer=None, signals_consulted=()
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        reading.agent_status = "working"


def test_verdict_and_reason_vocabularies_are_exact():
    assert VERDICTS == ("alive", "miss", "undecidable")
    assert set(VERDICTS) == {"alive", "miss", "undecidable"}
    assert set(REASONS) == {
        "composer_holds_prompt",
        "pane_unreadable",
        "blocked_prompt",
        "pane_gone",
        "agent_not_ready",
        "unknown_status",
    }
    assert len(REASONS) == 6


def test_restated_liveness_defaults_and_env_constant():
    assert RESTATED_LIVENESS["warn_after_s"] == 900
    assert RESTATED_LIVENESS["stuck_after_s"] == 1800
    assert RESTATED_LIVENESS["probe"]["deadline_s"] == 5
    assert LIVENESS_TOML_ENV == "CLAUDE_BRIDGE_LIVENESS_TOML"


# --- 0.4 the callable surface, pinned by signature string -------------------

FROZEN_SIGNATURES = {
    "add_job": (
        "(conn: sqlite3.Connection, job_id: str, *, action: str, agent_id: str, "
        "due_at: str, payload_ref: str | None = None, max_attempts: int = 3, "
        "backoff_s: int = 60, probe: int | None = None, warn_s: int | None = None, "
        "stuck_s: int | None = None) -> None"
    ),
    "due_jobs": "(conn: sqlite3.Connection, now: str) -> list[sqlite3.Row]",
    "claim_due": "(conn: sqlite3.Connection, job_id: str) -> bool",
    "mark_fired": "(conn: sqlite3.Connection, job_id: str, now: str) -> bool",
    "mark_failed": "(job_id: str, error: str) -> None",
    "list_jobs": "(conn: sqlite3.Connection) -> list[sqlite3.Row]",
    "status_counts": "(conn: sqlite3.Connection) -> dict[str, int]",
    "classify": (
        "(probe_result: schedule.ProbeResult, now: str, job: sqlite3.Row) "
        "-> tuple[str, str]"
    ),
}

#: One minimal, type-valid call per stub. The bodies are unimplemented at
#: `0.0`, so every one of these must raise `NotImplementedError` — a return
#: value here would mean a body was quietly filled in outside its owner parent.
STUB_CALLS = {
    "add_job": lambda: schedule.add_job(
        None, "job-00000000", action="notify", agent_id="alfred", due_at=NOW
    ),
    "due_jobs": lambda: schedule.due_jobs(None, NOW),
    "claim_due": lambda: schedule.claim_due(None, "job-00000000"),
    "mark_fired": lambda: schedule.mark_fired(None, "job-00000000", NOW),
    "mark_failed": lambda: schedule.mark_failed("job-00000000", "boom"),
    "list_jobs": lambda: schedule.list_jobs(None),
    "status_counts": lambda: schedule.status_counts(None),
    "classify": lambda: schedule.classify(
        ProbeResult(
            agent_status="idle", read_error=None, composer=None, signals_consulted=()
        ),
        NOW,
        None,
    ),
}

# Owners, recorded here so a stub that is still unimplemented at validation
# time names the parent that owes it (parent `0.0`'s own note).
STUB_OWNERS = {
    "add_job": "1.0",
    "due_jobs": "1.0",
    "claim_due": "1.0",
    "mark_fired": "3.0",
    "mark_failed": "3.0",
    "list_jobs": "1.0",
    "status_counts": "1.0",
    "classify": "2.0",
}


@pytest.mark.parametrize("name", sorted(FROZEN_SIGNATURES))
def test_frozen_signature_matches_to_the_character(name):
    assert str(inspect.signature(getattr(schedule, name))) == FROZEN_SIGNATURES[name]


#: Owner parents whose bodies have landed. The task list (0.0 proof artifacts)
#: makes `NotImplementedError` "acceptable only until that callable's owner
#: parent lands"; past that point the same call must reach a real body.
LANDED_OWNERS = {"1.0", "2.0", "3.0"}


@pytest.mark.parametrize("name", sorted(STUB_CALLS))
def test_stub_body_raises_not_implemented(name):
    if STUB_OWNERS[name] in LANDED_OWNERS:
        # `conn=None` makes the real body fail — but never as a stub.
        with pytest.raises(Exception) as exc:
            STUB_CALLS[name]()
        assert not isinstance(exc.value, NotImplementedError)
        return
    with pytest.raises(NotImplementedError):
        STUB_CALLS[name]()


def test_every_frozen_callable_has_an_owner_parent():
    assert set(STUB_OWNERS) == set(FROZEN_SIGNATURES)


def test_classify_returns_a_verdict_and_a_reason():
    # `2.0`'s mapping must record *why*, not only which of the three verdicts:
    # the reason is the discriminator the escalation ladder carries.
    ann = inspect.signature(schedule.classify).return_annotation
    assert ann == tuple[str, str]
    assert str(ann) == "tuple[str, str]"


def test_stub_annotation_types_resolve():
    # A string annotation that cannot be resolved would only surface at
    # `typing.get_type_hints` time, i.e. in a later parent's tooling.
    hints = {n: inspect.signature(getattr(schedule, n)).parameters for n in FROZEN_SIGNATURES}
    assert hints["add_job"]["conn"].annotation is sqlite3.Connection


# =============================================================================
# 1.0 — the substrate: additive `jobs` table, catch-up, atomic claim, lease
# =============================================================================

import os

import ledger

T0 = "2026-10-14T00:00:00+00:00"


def _at(seconds):
    """T0 + seconds, in the stored ISO-8601 UTC form."""
    from datetime import datetime, timedelta

    return schedule.utc_iso(datetime.fromisoformat(T0) + timedelta(seconds=seconds))


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "ledger.db")
    conn = schedule._connect(path)
    schedule.ensure_schema(conn)
    yield conn
    conn.close()


def _row(conn, job_id):
    return tuple(conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone())


def _add(conn, job_id="job-1", action="notify", due_at=T0, **kw):
    schedule.add_job(conn, job_id, action=action, agent_id="alfred", due_at=due_at, **kw)


def _set_firing(conn, job_id, fired_at):
    with conn:
        conn.execute(
            "UPDATE jobs SET status = 'firing', fired_at = ? WHERE job_id = ?",
            (fired_at, job_id),
        )


# --- 1.1 connection posture, additive schema, path discipline ----------------

def test_every_connection_uses_timeout_five(tmp_path, monkeypatch):
    seen = []
    real = sqlite3.connect

    def spy(*args, **kwargs):
        seen.append(kwargs.get("timeout"))
        return real(*args, **kwargs)

    monkeypatch.setattr(schedule.sqlite3, "connect", spy)
    conn = schedule._connect(str(tmp_path / "a.db"))
    assert conn.row_factory is sqlite3.Row
    conn.close()
    monkeypatch.setattr(ledger, "_default_db_path", lambda: str(tmp_path / "b.db"))
    schedule._connect().close()
    assert seen == [5.0, 5.0]


def test_ensure_schema_twice_leaves_tasks_byte_identical(tmp_path):
    path = str(tmp_path / "ledger.db")
    led = ledger.Ledger(path)
    led.init_schema()
    with led.conn:
        for i in range(3):
            led.conn.execute(
                "INSERT INTO tasks (task_id, agent_id, prompt, created_at) VALUES (?, ?, ?, ?)",
                (f"t{i}", "alfred", f"fixture prompt {i}", T0),
            )
    dump = lambda c: (
        [tuple(r) for r in c.execute("SELECT * FROM tasks ORDER BY task_id")],
        c.execute("SELECT sql FROM sqlite_master WHERE name = 'tasks'").fetchone()[0],
    )
    before = dump(led.conn)
    conn = schedule._connect(path)
    schedule.ensure_schema(conn)
    schedule.ensure_schema(conn)
    after = dump(conn)
    assert before == after
    assert len(after[0]) == 3
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
        "tasks",
        "jobs",
    }
    led.close()
    conn.close()


def test_jobs_table_has_the_fifteen_columns_and_agent_id_not_null(db):
    info = db.execute("PRAGMA table_info(jobs)").fetchall()
    assert tuple(r["name"] for r in info) == schedule.JOBS_COLUMNS
    assert len(info) == 15
    notnull = {r["name"]: r["notnull"] for r in info}
    assert notnull["agent_id"] == 1


def test_path_discipline_repo_ledger_db_untouched(tmp_path):
    repo_db = os.path.join(os.path.dirname(schedule.__file__), "ledger.db")
    if not os.path.exists(repo_db):
        pytest.skip("repo-local ledger.db absent")
    stat = lambda: os.stat(repo_db).st_mtime_ns
    count = lambda: sqlite3.connect(f"file:{repo_db}?mode=ro", uri=True).execute(
        "SELECT COUNT(*) FROM tasks"
    ).fetchone()[0]
    before = (stat(), count())
    conn = schedule._connect(str(tmp_path / "ledger.db"))
    schedule.ensure_schema(conn)
    _add(conn)
    schedule.claim_due(conn, "job-1")
    schedule.recover_leases(conn, _at(10_000))
    conn.close()
    assert (stat(), count()) == before


# --- 1.2 add_job ------------------------------------------------------------

def test_add_job_round_trips_every_column(db):
    schedule.add_job(
        db, "job-rt", action="snapshot", agent_id="luther", due_at=T0,
        payload_ref="/var/tmp/payload.json", max_attempts=5, backoff_s=90,
        probe=1, warn_s=600, stuck_s=1200,
    )
    row = dict(db.execute("SELECT * FROM jobs WHERE job_id = 'job-rt'").fetchone())
    created = row.pop("created_at")
    assert created.endswith("+00:00")
    assert row == {
        "job_id": "job-rt", "due_at": T0, "status": "due", "action": "snapshot",
        "payload_ref": "/var/tmp/payload.json", "agent_id": "luther", "attempts": 0,
        "max_attempts": 5, "backoff_s": 90, "fired_at": None, "last_error": None,
        "probe": 1, "warn_s": 600, "stuck_s": 1200,
    }


@pytest.mark.parametrize("agent_id", [None, ""])
def test_add_job_refuses_missing_agent_id_without_inserting(db, agent_id):
    with pytest.raises(ValueError, match="agent_id"):
        schedule.add_job(db, "job-x", action="notify", agent_id=agent_id, due_at=T0)
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_add_job_refuses_undeclared_action(db):
    with pytest.raises(ValueError, match="'teleport'"):
        _add(db, action="teleport")
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "inline", ['{"prompt": "do the thing"}', "line one\nline two", "relative/path.json"]
)
def test_add_job_refuses_inline_payload_naming_it(db, inline):
    with pytest.raises(ValueError) as exc:
        _add(db, payload_ref=inline)
    assert repr(inline) in str(exc.value)
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_payload_ref_path_is_byte_identical(db):
    ref = "/Users/someone/payloads/" + "x" * 900 + ".json"
    _add(db, payload_ref=ref)
    assert db.execute("SELECT payload_ref FROM jobs").fetchone()[0] == ref


def test_add_job_normalises_due_at_to_utc_and_refuses_naive(db):
    _add(db, due_at="2026-10-14T02:00:00+02:00")
    assert db.execute("SELECT due_at FROM jobs").fetchone()[0] == T0
    with pytest.raises(ValueError, match="due_at"):
        _add(db, job_id="job-2", due_at="2026-10-14T00:00:00")


# --- 1.3 due_jobs: catch-up by string comparison ----------------------------

@pytest.mark.parametrize(
    "due_at, now, expected",
    [
        (T0, _at(-1), False),       # future
        (T0, T0, True),             # boundary-equal
        (T0, _at(1), True),
        (T0, _at(60 * 10), True),   # ten missed 60 s ticks: still returned
        (_at(0.5), T0, False),      # fractional seconds order correctly
        (T0, _at(0.5), True),
    ],
)
def test_due_jobs_comparison_table(db, due_at, now, expected):
    _add(db, due_at=due_at)
    assert [r["job_id"] for r in schedule.due_jobs(db, now)] == (["job-1"] if expected else [])


def test_due_jobs_catches_up_then_drops_once_claimed(db):
    _add(db)
    for missed in (60, 120, 600, 3600):
        assert [r["job_id"] for r in schedule.due_jobs(db, _at(missed))] == ["job-1"]
    assert schedule.claim_due(db, "job-1") is True
    assert schedule.due_jobs(db, _at(7200)) == []


def test_due_jobs_parses_no_date_at_query_time(db):
    _add(db)
    sql = []
    db.set_trace_callback(sql.append)
    schedule.due_jobs(db, _at(60))
    db.set_trace_callback(None)
    assert sql
    for stmt in sql:
        assert "datetime(" not in stmt.lower()
        assert "strftime(" not in stmt.lower()
        assert "julianday(" not in stmt.lower()


# --- 1.4 claim_due: rowcount is the only oracle -----------------------------

def test_claim_due_true_then_false(db):
    _add(db)
    assert schedule.claim_due(db, "job-1") is True
    assert schedule.claim_due(db, "job-1") is False
    row = db.execute("SELECT status, fired_at FROM jobs").fetchone()
    assert row["status"] == "firing" and row["fired_at"].endswith("+00:00")


def test_claim_due_on_unknown_or_non_due_row_is_false(db):
    assert schedule.claim_due(db, "nope") is False
    for status in ("fired", "failed", "cancelled"):
        _add(db, job_id=f"j-{status}")
        with db:
            db.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (status, f"j-{status}"))
        assert schedule.claim_due(db, f"j-{status}") is False


def test_two_tick_interleave_fires_exactly_once(tmp_path):
    path = str(tmp_path / "ledger.db")
    a, b = schedule._connect(path), schedule._connect(path)
    schedule.ensure_schema(a)
    _add(a)
    fired = []
    # Both ticks read the due set before either claims.
    seen_a = [r["job_id"] for r in schedule.due_jobs(a, _at(60))]
    seen_b = [r["job_id"] for r in schedule.due_jobs(b, _at(60))]
    assert seen_a == seen_b == ["job-1"]
    for tick, conn, seen in (("a", a, seen_a), ("b", b, seen_b)):
        for job_id in seen:
            if schedule.claim_due(conn, job_id):
                fired.append((tick, job_id))
    assert fired == [("a", "job-1")]
    a.close()
    b.close()


# --- 1.5 lease recovery, bounded class --------------------------------------

def test_stale_bounded_firing_row_returns_to_due_and_fires_once(db):
    _add(db)
    _set_firing(db, "job-1", T0)
    now = _at(schedule.lease_s("notify"))
    assert schedule.recover_leases(db, now) == 1
    row = db.execute("SELECT status, last_error, attempts FROM jobs").fetchone()
    assert (row["status"], row["last_error"], row["attempts"]) == ("due", "lease_rearm", 0)
    assert [r["job_id"] for r in schedule.due_jobs(db, now)] == ["job-1"]
    assert [schedule.claim_due(db, "job-1"), schedule.claim_due(db, "job-1")] == [True, False]


def test_fresh_firing_row_is_left_alone(db):
    _add(db)
    _set_firing(db, "job-1", T0)
    before = _row(db, "job-1")
    assert schedule.recover_leases(db, _at(schedule.lease_s("notify") - 1)) == 0
    assert _row(db, "job-1") == before


def test_visible_lease_is_max_of_floor_and_worst_case(monkeypatch):
    monkeypatch.setitem(schedule.ACTION_WORST_CASE_S, "long_bounded", 300)
    for action, worst in schedule.ACTION_WORST_CASE_S.items():
        if worst is UNBOUNDED:
            continue
        assert schedule.lease_s(action) == max(LEASE_MIN_S, worst)
    assert schedule.lease_s("long_bounded") == 300
    assert schedule.lease_s("notify") == LEASE_MIN_S


def test_lease_rearm_count_increments_per_rearm(db):
    summary = dict.fromkeys(TICK_SUMMARY_KEYS, 0)
    for i in range(3):
        _add(db, job_id=f"j{i}")
        _set_firing(db, f"j{i}", T0)
    _set_firing(db, "j2", _at(500))  # fresh at the recovery instant
    summary["lease_rearm_count"] += schedule.recover_leases(db, _at(LEASE_MIN_S))
    assert summary["lease_rearm_count"] == 2
    summary["lease_rearm_count"] += schedule.recover_leases(db, _at(LEASE_MIN_S))
    assert summary["lease_rearm_count"] == 2  # already re-armed rows are not recounted


def test_recovery_touches_only_firing_rows(db):
    for status in ("fired", "failed", "cancelled"):
        _add(db, job_id=status)
        with db:
            db.execute(
                "UPDATE jobs SET status = ?, fired_at = ? WHERE job_id = ?", (status, T0, status)
            )
    before = [tuple(r) for r in schedule.list_jobs(db)]
    assert schedule.recover_leases(db, _at(10_000)) == 0
    assert [tuple(r) for r in schedule.list_jobs(db)] == before


# --- 1.6 the UNBOUNDED class (r3 normative) ---------------------------------

def test_unbounded_lease_is_the_floor_not_a_maximum():
    assert ACTION_WORST_CASE_S["send"] is UNBOUNDED
    assert schedule.lease_s("send") == LEASE_MIN_S
    with pytest.raises(TypeError):
        max(LEASE_MIN_S, UNBOUNDED)


def test_unbounded_firing_row_rearms_with_no_zombie_and_stays_spend_deferred(db):
    _add(db, job_id="send-1", action="send", payload_ref="/var/tmp/prompt.md")
    assert schedule.claim_due(db, "send-1") is True
    _set_firing(db, "send-1", T0)  # the tick that claimed it died here
    assert schedule.recover_leases(db, _at(LEASE_MIN_S - 1)) == 0
    summary = dict.fromkeys(TICK_SUMMARY_KEYS, 0)
    summary["lease_rearm_count"] += schedule.recover_leases(db, _at(LEASE_MIN_S))
    assert summary["lease_rearm_count"] == 1
    assert schedule.status_counts(db)["firing"] == 0  # no zombie `firing` row
    row = db.execute("SELECT status, last_error, attempts FROM jobs").fetchone()
    assert (row["status"], row["last_error"], row["attempts"]) == ("due", "lease_rearm", 0)

    # Stand-in for 4.0's spend gate, checked *before* the claim: an unattended
    # tick never claims `send`. 4.0 owns the gate and its `last_error` write;
    # this asserts the re-armed row stays deferrable — due, unclaimed, attempts 0.
    def unattended_tick(now):
        for r in schedule.due_jobs(db, now):
            if ACTION_WORST_CASE_S[r["action"]] is UNBOUNDED:
                summary["spend_gate"] += 1
                continue
            schedule.claim_due(db, r["job_id"])

    for n in range(1, 6):
        unattended_tick(_at(LEASE_MIN_S + 60 * n))
        row = db.execute("SELECT status, attempts FROM jobs").fetchone()
        assert (row["status"], row["attempts"]) == ("due", 0)
    assert summary["spend_gate"] == 5
    assert schedule.recover_leases(db, _at(10_000)) == 0  # no re-arm loop


# --- 1.7 lock posture and transaction scope ---------------------------------

class _LockedConn:
    """Delegates to a real connection; every UPDATE raises the busy error."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith("UPDATE"):
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_locked_claim_counts_locked_and_leaves_row_byte_identical(db):
    _add(db)
    before = _row(db, "job-1")
    summary = dict.fromkeys(TICK_SUMMARY_KEYS, 0)
    assert schedule.try_claim(_LockedConn(db), "job-1", summary) is False  # no raise
    assert summary["locked"] == 1
    assert summary["claimed"] == 0
    assert _row(db, "job-1") == before
    assert db.in_transaction is False
    assert schedule.try_claim(db, "job-1", summary) is True  # next tick claims it


def test_real_lock_is_caught_and_row_left_due(tmp_path):
    path = str(tmp_path / "ledger.db")
    holder = schedule._connect(path)
    schedule.ensure_schema(holder)
    _add(holder)
    before = _row(holder, "job-1")
    holder.execute("BEGIN EXCLUSIVE")
    tick = sqlite3.connect(path, timeout=0.05)  # same code path, short wait for the test
    tick.row_factory = sqlite3.Row
    summary = dict.fromkeys(TICK_SUMMARY_KEYS, 0)
    assert schedule.try_claim(tick, "job-1", summary) is False
    assert summary["locked"] == 1
    assert tick.in_transaction is False
    holder.rollback()
    assert _row(holder, "job-1") == before
    holder.close()
    tick.close()


def test_other_operational_errors_are_not_swallowed(db):
    class Broken(_LockedConn):
        def execute(self, sql, *args):
            raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        schedule.try_claim(Broken(db), "job-1", dict.fromkeys(TICK_SUMMARY_KEYS, 0))


def test_no_write_transaction_open_after_claim_or_recovery(db):
    _add(db)
    assert schedule.claim_due(db, "job-1") is True
    assert db.in_transaction is False
    _set_firing(db, "job-1", T0)
    schedule.recover_leases(db, _at(10_000))
    assert db.in_transaction is False
    schedule.claim_due(db, "job-1")
    assert db.in_transaction is False


# --- list_jobs / status_counts (read side of `--list`) ----------------------

def test_list_jobs_and_status_counts(db):
    _add(db, job_id="b", due_at=_at(60))
    _add(db, job_id="a")
    schedule.claim_due(db, "a")
    assert [r["job_id"] for r in schedule.list_jobs(db)] == ["a", "b"]
    assert schedule.status_counts(db) == {
        "due": 1, "firing": 1, "fired": 0, "failed": 0, "cancelled": 0,
    }


# =============================================================================
# 2.0 — the watchdog: probe, the 9-row mapping, thresholds, the ladder
# =============================================================================

import ast
import json
import socket
import tempfile
import time

import herdr_client
import notify
import watch
from composer_state import classify_composer, sentinel_for

TASK = "task-wd"
SENT = sentinel_for(TASK)
#: Composer fixtures, each a sanitized pane text read by the *shipped* recogniser.
PANE_CLEAR = "⏺ previous answer\n❯ \n"
PANE_SENTINEL = f"❯ {SENT} please continue the review\n"
PANE_UNREADABLE = "some scrollback with no composer line\n"
DESTRUCTIVE = {"pane_close", "agent_start", "kill", "pane_send_keys", "agent_prompt"}


class FakeClient:
    """A herdr double: returns `agent` (or raises it), serves `pane`, logs every call."""

    def __init__(self, agent=None, pane=PANE_CLEAR, pane_exc=None):
        self.agent, self.pane, self.pane_exc, self.calls = agent, pane, pane_exc, []

    def agent_get(self, target):
        self.calls.append("agent_get")
        if isinstance(self.agent, Exception):
            raise self.agent
        return self.agent

    def pane_read(self, pane_id, source="visible", format="text"):
        self.calls.append("pane_read")
        if self.pane_exc:
            raise self.pane_exc
        return self.pane

    def __getattr__(self, name):  # any other client method: logged, never served
        def call(*a, **k):
            self.calls.append(name)
        return call


def _notifier():
    calls = []
    return calls, lambda *a, **k: calls.append(a) or "sent"


def _job(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()


def _watched(conn, job_id="job-w", **kw):
    """A pending watched row: due far in the future, created at T0."""
    schedule.add_job(conn, job_id, action="notify", agent_id=TASK, due_at=_at(86_400), **kw)
    with conn:
        conn.execute("UPDATE jobs SET created_at = ? WHERE job_id = ?", (T0, job_id))


@pytest.fixture
def no_toml(monkeypatch, tmp_path):
    monkeypatch.setenv(schedule.LIVENESS_TOML_ENV, str(tmp_path / "absent.toml"))


# --- 2.1 / 2.2 the 9-row mapping, driven through probe() ---------------------

#: (row, client agent / exception, pane text, ledger_state, verdict, reason, signals)
ROWS = [
    (1, {"agent_status": "working"}, None, None, "alive", "", ("agent_get",)),
    (2, {"agent_status": "done"}, None, None, "alive", "", ("agent_get",)),
    (3, {"agent_status": "blocked"}, None, None, "miss", "blocked_prompt", ("agent_get",)),
    (4, {"agent_status": "working"}, None, "killed", "miss", "pane_gone", ("ledger_state",)),
    (5, {"agent_status": "idle", "pane_id": "p1"}, PANE_CLEAR, None, "alive", "",
     ("agent_get", "pane_read", "composer")),
    (6, {"agent_status": "idle", "pane_id": "p1"}, PANE_SENTINEL, None, "miss",
     "composer_holds_prompt", ("agent_get", "pane_read", "composer")),
    (7, {"agent_status": "idle", "pane_id": "p1"}, PANE_UNREADABLE, None, "undecidable",
     "pane_unreadable", ("agent_get", "pane_read", "composer")),
    (8, herdr_client.AgentNotReady("trust dialog"), None, None, "undecidable",
     "agent_not_ready", ("agent_get",)),
    # Row 9: a status string the spec does not list.
    (9, {"agent_status": "hibernating-v7"}, None, None, "undecidable", "unknown_status",
     ("agent_get",)),
]
DUE_JOB = {"job_id": "job-x", "status": "due"}


@pytest.mark.parametrize("row, agent, pane, ledger_state, verdict, reason, signals", ROWS,
                         ids=[f"row{r[0]}" for r in ROWS])
def test_probe_input_mapping_row(row, agent, pane, ledger_state, verdict, reason, signals):
    client = FakeClient(agent, pane)
    result = schedule.probe(TASK, None, NOW, client=client, ledger_state=ledger_state)
    assert schedule.classify(result, NOW, DUE_JOB) == (verdict, reason)
    assert result.signals_consulted == signals
    if pane is not None:  # composer read through the shipped recogniser, not re-implemented
        assert result.composer == classify_composer(pane, SENT)
    else:  # not idle: the composer was not consulted
        assert result.composer is None and "pane_read" not in client.calls
    print(f"row {row}: {verdict:<11} {reason or '—':<22} {signals}")


@pytest.mark.parametrize("agent", [{"agent_status": "unknown"}, {}, {"agent_status": None}])
def test_row8_unknown_or_absent_status(agent):
    result = schedule.probe(TASK, None, NOW, client=FakeClient(agent))
    assert schedule.classify(result, NOW, DUE_JOB) == ("undecidable", "agent_not_ready")


def test_mapping_is_total_and_never_alive_for_novel_strings():
    seen = set()
    for _, agent, pane, ls, verdict, reason, _ in ROWS:
        r = schedule.probe(TASK, None, NOW, client=FakeClient(agent, pane), ledger_state=ls)
        v, why = schedule.classify(r, NOW, DUE_JOB)
        assert v in VERDICTS and (why in REASONS or (v == "alive" and why == ""))
        seen.add((v, why))
    assert len(seen) == 7  # 9 rows: rows 1, 2, 5 share ("alive", ""), the 6 reasons are distinct
    assert {why for _, why in seen} - {""} == set(REASONS)
    for novel in ("", "WORKING", "paused", "42", "done!"):
        r = ProbeResult(novel, None, None, ("agent_get",))
        assert schedule.classify(r, NOW, DUE_JOB) == ("undecidable", "unknown_status")
    assert schedule.classify(ProbeResult(7, None, None, ()), NOW, DUE_JOB)[0] == "undecidable"


def test_classify_consults_no_elapsed_time():
    r = ProbeResult("working", None, None, ("agent_get",))
    for now in (T0, _at(10**8)):
        assert schedule.classify(r, now, {"job_id": "j", "status": "due"}) == ("alive", "")


@pytest.mark.parametrize("status", ["fired", "cancelled"])
def test_classify_refuses_unprobeable_rows(status):
    with pytest.raises(ValueError, match=status):
        schedule.classify(ProbeResult("working", None, None, ()), NOW,
                          {"job_id": "j", "status": status})


def test_unreadable_pane_sets_read_error_and_is_row7():
    client = FakeClient({"agent_status": "idle", "pane_id": "p1"},
                        pane_exc=herdr_client.HerdrTimeout("stalled pane_read"))
    result = schedule.probe(TASK, None, NOW, client=client)
    assert result.read_error == "HerdrTimeout"
    assert schedule.classify(result, NOW, DUE_JOB) == ("undecidable", "pane_unreadable")


def test_idle_with_no_pane_is_row7_not_a_crash():
    result = schedule.probe(TASK, None, NOW, client=FakeClient({"agent_status": "idle"}))
    assert result.read_error == "NoPane"
    assert schedule.classify(result, NOW, DUE_JOB) == ("undecidable", "pane_unreadable")


# --- 2.1 the probe owns its deadline (audit R1) ------------------------------

def test_hung_socket_returns_within_deadline_without_raising():
    d = tempfile.mkdtemp()  # short path: AF_UNIX paths are capped near 104 bytes
    path = os.path.join(d, "h.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)  # connects succeed via the backlog; nothing ever replies
    try:
        start = time.monotonic()
        result = schedule.probe(TASK, None, NOW, deadline_s=0.3, socket_path=path)
        took = time.monotonic() - start
    finally:
        server.close()
        os.unlink(path)
        os.rmdir(d)
    assert result.read_error == "HerdrTimeout" and result.agent_status is None
    assert took < 0.3 + 1.0, took
    assert schedule.classify(result, NOW, DUE_JOB) == ("undecidable", "agent_not_ready")  # row 8


def test_client_is_built_with_the_resolved_probe_deadline(monkeypatch, tmp_path):
    built = []

    class Recorder:
        def __init__(self, socket_path=None, timeout=30):
            built.append(timeout)

        def agent_get(self, target):
            raise herdr_client.HerdrTimeout("never answers")

    monkeypatch.setattr(herdr_client, "HerdrClient", Recorder)
    toml = tmp_path / "liveness.toml"
    toml.write_text("[probe]\ndeadline_s = 7\n")
    monkeypatch.setenv(schedule.LIVENESS_TOML_ENV, str(toml))
    assert schedule.probe(TASK, None, NOW).read_error == "HerdrTimeout"
    assert schedule.probe(TASK, None, NOW, deadline_s=2).read_error == "HerdrTimeout"
    monkeypatch.setenv(schedule.LIVENESS_TOML_ENV, str(tmp_path / "absent.toml"))
    schedule.probe(TASK, None, NOW)
    assert built == [7, 2, 5]


# --- 2.4 thresholds: row ?? toml ?? restated default -------------------------

def _toml(tmp_path, body):
    p = tmp_path / "liveness.toml"
    p.write_text(body)
    return str(p)


def test_threshold_resolution_order(db, tmp_path):
    toml = _toml(tmp_path, "[tiers]\nwarn_after_s = 60\nstuck_after_s = 120\n[probe]\ndeadline_s = 3\n")
    _watched(db, "j-null")
    _watched(db, "j-row", warn_s=10, stuck_s=20)
    null_row, row = _job(db, "j-null"), _job(db, "j-row")
    th = schedule.resolve_thresholds(null_row, toml)
    assert (th["warn_after_s"], th["stuck_after_s"], th["probe_deadline_s"], th["fallback"]) == (60, 120, 3, None)
    th = schedule.resolve_thresholds(row, toml)
    assert (th["warn_after_s"], th["stuck_after_s"]) == (10, 20)  # row beats toml
    th = schedule.resolve_thresholds(null_row, _toml(tmp_path, "[queue]\nworker_limit = 3\n"))
    assert (th["warn_after_s"], th["stuck_after_s"], th["probe_deadline_s"]) == (900, 1800, 5)
    assert th["fallback"] is None  # file read fine; keys absent, defaults fill in


@pytest.mark.parametrize("body", [None, "this is = = not toml", "[tiers]\nwarn_after_s = 'soon'\n"])
def test_missing_or_unreadable_toml_falls_back_and_records_it(tmp_path, body):
    path = _toml(tmp_path, body) if body else str(tmp_path / "absent.toml")
    th = schedule.resolve_thresholds(None, path)
    assert (th["warn_after_s"], th["stuck_after_s"], th["probe_deadline_s"]) == (900, 1800, 5)
    assert th["fallback"] and path in th["fallback"]


def test_env_var_names_the_toml(monkeypatch, tmp_path):
    monkeypatch.setenv(schedule.LIVENESS_TOML_ENV, _toml(tmp_path, "[tiers]\nwarn_after_s = 42\n"))
    assert schedule.resolve_thresholds()["warn_after_s"] == 42


def test_toml_moves_the_look_boundary(db, tmp_path):
    _watched(db)
    job = _job(db, "job-w")
    default = schedule.resolve_thresholds(job, str(tmp_path / "absent.toml"))
    moved = schedule.resolve_thresholds(job, _toml(tmp_path, "[tiers]\nwarn_after_s = 300\nstuck_after_s = 600\n"))
    assert [schedule.should_probe(job, _at(s), default) for s in (299, 899, 900)] == [False, False, True]
    assert [schedule.should_probe(job, _at(s), moved) for s in (299, 300)] == [False, True]
    with db:
        db.execute("UPDATE jobs SET last_error = 'probe_miss {}' WHERE job_id = 'job-w'")
    on_ladder = _job(db, "job-w")
    assert [schedule.should_probe(on_ladder, _at(s), moved) for s in (599, 600)] == [False, True]
    assert [schedule.should_probe(on_ladder, _at(s), default) for s in (1799, 1800)] == [False, True]


def test_row_stuck_s_overrides_the_toml_boundary(db, tmp_path):
    toml = _toml(tmp_path, "[tiers]\nstuck_after_s = 600\n")
    _watched(db, stuck_s=3000)
    with db:
        db.execute("UPDATE jobs SET last_error = 'probe_miss {}'")
    job = _job(db, "job-w")
    th = schedule.resolve_thresholds(job, toml)
    assert [schedule.should_probe(job, _at(s), th) for s in (600, 2999, 3000)] == [False, False, True]


def test_probe_zero_is_never_probed(db, no_toml, monkeypatch):
    _watched(db, probe=0)
    probed = []
    monkeypatch.setattr(schedule, "probe", lambda *a, **k: probed.append(a))
    for s in (1000, 2000, 10**6):
        schedule.watchdog(db, _at(s), client=FakeClient({"agent_status": "blocked"}))
    assert probed == []


# --- 2.5 / 2.6 the ladder, the latch, the incident reset ---------------------

MISS = ProbeResult("blocked", None, None, ("agent_get",))
UNDECIDABLE = ProbeResult(None, "AgentNotReady", None, ("agent_get",))
ALIVE = ProbeResult("working", None, None, ("agent_get",))
TH = {"warn_after_s": 900, "stuck_after_s": 1800, "probe_deadline_s": 5, "probe": True,
      "source": "/tmp/liveness.toml", "fallback": None}


def _step(db, result, now, notifier):
    return schedule.escalate(db, _job(db, "job-w"), result, now, TH, notifier)


def test_ladder_first_miss_rearms_quietly_second_raises_once(db):
    _watched(db)
    calls, notifier = _notifier()
    assert _step(db, MISS, _at(1000), notifier) == "probe_miss"
    row = _job(db, "job-w")
    assert (row["status"], row["attempts"], len(calls)) == ("due", 0, 0)
    assert row["last_error"].split(" ", 1)[0] == "probe_miss"

    assert _step(db, UNDECIDABLE, _at(2000), notifier) == "stuck_candidate"  # counted together
    assert len(calls) == 1
    run_id, verdict, grade, report_path, digest = calls[0]
    cand = json.loads(digest)
    assert (run_id, verdict, grade) == ("stuck-job-w", "stuck_candidate", "agent_not_ready")
    assert [p["probe"]["agent_status"] for p in cand["probes"]] == ["blocked", None]
    assert [p["reason"] for p in cand["probes"]] == ["blocked_prompt", "agent_not_ready"]
    assert [p["at"] for p in cand["probes"]] == [_at(1000), _at(2000)]
    assert cand["thresholds"] == TH and cand["options"] == list(schedule.STUCK_OPTIONS)
    latch = _job(db, "job-w")["last_error"]
    assert latch.startswith("stuck_candidate ") and json.loads(latch.split(" ", 1)[1])["notify"] == "sent"
    print("call log:", [(r, v, g) for r, v, g, *_ in calls])


def test_first_undecidable_is_annotated_probe_undecidable(db):
    _watched(db)
    calls, notifier = _notifier()
    _step(db, UNDECIDABLE, _at(1000), notifier)
    assert _job(db, "job-w")["last_error"].startswith("probe_undecidable ") and calls == []


def test_latch_holds_through_the_hundredth_miss(db):
    _watched(db)
    calls, notifier = _notifier()
    steps = [_step(db, MISS, _at(1000 + i), notifier) for i in range(100)]
    assert steps[:2] == ["probe_miss", "stuck_candidate"]
    assert set(steps[2:]) == {"latched"}
    assert len(calls) == 1


def test_alive_clears_counter_and_latch_and_a_new_incident_reports_again(db):
    _watched(db)
    calls, notifier = _notifier()
    _step(db, MISS, _at(1000), notifier)
    assert _step(db, ALIVE, _at(1100), notifier) == "alive"
    assert _job(db, "job-w")["last_error"] is None          # counter cleared
    _step(db, MISS, _at(1200), notifier)
    _step(db, MISS, _at(1300), notifier)
    _step(db, MISS, _at(1400), notifier)
    assert len(calls) == 1
    _step(db, ALIVE, _at(1500), notifier)
    assert _job(db, "job-w")["last_error"] is None          # latch cleared
    assert _step(db, MISS, _at(1600), notifier) == "probe_miss"
    assert len(calls) == 1                                  # first miss of incident 2: quiet
    assert _step(db, MISS, _at(1700), notifier) == "stuck_candidate"
    assert len(calls) == 2                                  # exactly one new call


def test_alive_does_not_clear_another_writers_note(db):
    _watched(db)
    with db:
        db.execute("UPDATE jobs SET last_error = 'lease_rearm'")
    _step(db, ALIVE, _at(5000), _notifier()[1])
    assert _job(db, "job-w")["last_error"] == "lease_rearm"


def test_latch_write_is_the_oracle_two_ticks_cannot_both_notify(db):
    _watched(db)
    calls, notifier = _notifier()
    _step(db, MISS, _at(1000), notifier)
    stale = _job(db, "job-w")  # both ticks read the row before either writes
    assert schedule.escalate(db, stale, MISS, _at(2000), TH, notifier) == "stuck_candidate"
    assert schedule.escalate(db, stale, MISS, _at(2000), TH, notifier) == "latched"
    assert len(calls) == 1


@pytest.mark.parametrize("agent, pane", [
    ({"agent_status": "working"}, None),                     # row 1
    ({"agent_status": "done"}, None),                        # row 2
    ({"agent_status": "idle", "pane_id": "p1"}, PANE_CLEAR),  # row 5
], ids=["row1", "row2", "row5"])
def test_no_escalation_from_silence(db, no_toml, agent, pane):
    _watched(db)
    with db:  # the job was already on the ladder before the long silence
        db.execute("UPDATE jobs SET last_error = 'probe_miss {}'")
    calls, notifier = _notifier()
    client = FakeClient(agent, pane)
    for s in (1800 * 10, 1800 * 100, 10**8):  # far past stuck_after_s, e.g. a machine asleep
        assert schedule.watchdog(db, _at(s), client=client, notifier=notifier) == {
            "probe_miss": 0, "stuck_candidates": 0}
    row = _job(db, "job-w")
    assert (calls, row["last_error"], row["status"]) == ([], None, "due")


# --- 2.3 TICKET-209: an idle pane still holding its prompt ---------------------

@pytest.mark.parametrize("pane, reason", [
    (PANE_SENTINEL, "composer_holds_prompt"),   # row 6
    (PANE_UNREADABLE, "pane_unreadable"),       # row 7
], ids=["row6-sentinel", "row7-unreadable"])
def test_mahq209_idle_composer_stall_reaches_stuck_candidate_on_second_probe(db, no_toml, pane, reason):
    _watched(db)
    calls, notifier = _notifier()
    client = FakeClient({"agent_status": "idle", "pane_id": "p1"}, pane)
    assert schedule.watchdog(db, _at(1000), client=client, notifier=notifier) == {
        "probe_miss": 1, "stuck_candidates": 0}
    assert calls == []                                   # never on the first
    assert schedule.watchdog(db, _at(1500), client=client, notifier=notifier)["stuck_candidates"] == 0
    assert calls == []                                   # not yet stuck_after_s: not looked at
    assert schedule.watchdog(db, _at(1800), client=client, notifier=notifier) == {
        "probe_miss": 1, "stuck_candidates": 1}
    assert len(calls) == 1 and calls[0][2] == reason     # on the second consecutive
    cand = json.loads(calls[0][4])
    assert [p["reason"] for p in cand["probes"]] == [reason, reason]
    assert all(p["probe"]["agent_status"] == "idle" for p in cand["probes"])


# --- 2.7 the failure boundary ------------------------------------------------

def test_probe_miss_is_not_an_action_failure(db, no_toml):
    _watched(db)
    client = FakeClient({"agent_status": "blocked"})
    for s in (1000, 2000, 3000):
        schedule.watchdog(db, _at(s), client=client, notifier=_notifier()[1])
        row = _job(db, "job-w")
        assert (row["status"], row["attempts"]) == ("due", 0)


def test_fired_cancelled_failed_firing_and_past_due_rows_are_never_probed(db, no_toml, monkeypatch):
    for jid in ("w-due", "w-fired", "w-cancelled", "w-failed", "w-firing"):
        _watched(db, jid)
    for jid, st in (("w-fired", "fired"), ("w-cancelled", "cancelled"),
                    ("w-failed", "failed"), ("w-firing", "firing")):
        with db:
            db.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (st, jid))
    _add(db, job_id="past-due")  # due_at T0: the firing path's row this tick
    probed = []
    real = schedule.probe
    monkeypatch.setattr(schedule, "probe", lambda *a, **k: probed.append(a[0]) or real(*a, **k))
    db.execute("UPDATE jobs SET agent_id = job_id")
    db.commit()
    before = [tuple(r) for r in schedule.list_jobs(db) if r["job_id"] != "w-due"]
    schedule.watchdog(db, _at(5000), client=FakeClient({"agent_status": "blocked"}),
                      notifier=_notifier()[1])
    assert probed == ["w-due"]
    assert [tuple(r) for r in schedule.list_jobs(db) if r["job_id"] != "w-due"] == before


def test_ledger_killed_task_is_row4_and_supplies_the_pane(db, no_toml, tmp_path):
    led = ledger.Ledger(str(tmp_path / "ledger.db"))
    led.init_schema()
    with led.conn:
        led.conn.execute(
            "INSERT INTO tasks (task_id, agent_id, prompt, created_at, herdr_pane_id, last_state)"
            " VALUES (?, 'alfred', 'fixture', ?, 'pane-9', 'killed')", (TASK, T0))
    assert schedule._ledger_task(db, TASK) == ("killed", "pane-9")
    assert schedule._ledger_task(db, "no-such-task") == (None, None)
    _watched(db)
    client = FakeClient({"agent_status": "working"})
    schedule.watchdog(db, _at(1000), client=client, notifier=_notifier()[1])
    assert client.calls == []  # ledger is authoritative: no herdr call for a gone session
    assert json.loads(_job(db, "job-w")["last_error"].split(" ", 1)[1])["reason"] == "pane_gone"


# --- 2.9 ledger-session probe: Bridge.send sessions have no jobs row ---------

def test_working_ledger_session_without_jobs_row_is_probed(db, no_toml, tmp_path):
    led = ledger.Ledger(str(tmp_path / "ledger.db"))
    led.init_schema()
    with led.conn:
        led.conn.execute(
            "INSERT INTO tasks (task_id, agent_id, prompt, created_at, herdr_pane_id, last_state)"
            " VALUES (?, 'alfred', 'fixture', ?, 'pane-9', 'working')", (TASK, T0))
    calls, notifier = _notifier()
    client = FakeClient({"agent_status": "killed"})
    # created_at T0, now T0+5000s > stuck_after_s 1800s → probe-eligible.
    assert schedule.watchdog(db, _at(5000), client=client, notifier=notifier)["stuck_candidates"] == 1
    assert client.calls == ["agent_get"]
    run_id, verdict, grade, _report_path, digest = calls[0]
    assert (run_id, verdict, grade) == (f"stuck-{TASK}", "stuck_candidate", "pane_gone")
    assert json.loads(digest)["reason"] == "pane_gone"
    # Latched: a second tick must not re-notify.
    assert schedule.watchdog(db, _at(5001), client=client, notifier=notifier)["stuck_candidates"] == 0
    assert len(calls) == 1
    # An alive probe clears the latch.
    schedule.watchdog(db, _at(5002), client=FakeClient({"agent_status": "working"}),
                      notifier=notifier)
    assert db.execute("SELECT notified_state FROM tasks WHERE task_id = ?",
                      (TASK,)).fetchone()[0] is None


def test_daemon_down_does_not_flag_working_ledger_sessions(db, no_toml, tmp_path):
    led = ledger.Ledger(str(tmp_path / "ledger.db"))
    led.init_schema()
    with led.conn:
        led.conn.execute(
            "INSERT INTO tasks (task_id, agent_id, prompt, created_at, herdr_pane_id, last_state)"
            " VALUES (?, 'alfred', 'fixture', ?, 'pane-9', 'working')", (TASK, T0))
    calls, notifier = _notifier()
    client = FakeClient(herdr_client.HerdrServerDown("down"))
    assert schedule.watchdog(db, _at(5000), client=client, notifier=notifier) == {
        "probe_miss": 0, "stuck_candidates": 0}
    assert calls == []  # a daemon blip is not a per-session stuck


# --- 2.8 guardrails: no destructive call, no coupling ------------------------

def test_watchdog_path_makes_no_destructive_call(db, no_toml):
    _watched(db, "j-hung")
    _watched(db, "j-idle")
    clients = [FakeClient(herdr_client.HerdrTimeout("hung")),
               FakeClient({"agent_status": "idle", "pane_id": "p1"}, PANE_SENTINEL),
               FakeClient({"agent_status": "blocked"}),
               FakeClient({"agent_status": "working"})]
    calls, notifier = _notifier()
    for i in range(100):
        client = clients[(i // 25) % len(clients)]
        schedule.watchdog(db, _at(1000 + 60 * i), client=client, notifier=notifier)
    logged = [c for cl in clients for c in cl.calls]
    assert set(logged) <= {"agent_get", "pane_read"}
    assert not DESTRUCTIVE & set(logged)
    assert len(calls) >= 1  # the run did reach the ladder's top


def _public_callables(mod):
    return [(n, o) for n, o in vars(mod).items()
            if not n.startswith("_") and callable(o) and getattr(o, "__module__", None) == mod.__name__]


@pytest.mark.parametrize("mod", [notify, watch], ids=["notify", "watch"])
def test_notify_and_watch_take_no_timer_shaped_parameter(mod):
    callables = _public_callables(mod)
    assert callables
    for name, obj in callables:
        for p in inspect.signature(obj).parameters:
            assert not any(w in p.lower() for w in ("timer", "due", "schedule")), (name, p)


def test_notify_imports_no_schedule_module():
    tree = ast.parse(open(notify.__file__).read())
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not {m for m in names if "schedule" in m or "timer" in m}


# =============================================================================
# 3.0 — the failure path: mark_failed, next_due, the terminal state, mark_fired
# =============================================================================

import timer

#: The pinned clock `mark_failed`'s internal `utc_iso()` reads (set per test).
_CLOCK = {"now": T0}


@pytest.fixture
def fdb(tmp_path, monkeypatch):
    """A tmp DB reached through the one path helper — `mark_failed` opens its
    own connection — with `utc_iso()`'s clock pinned (callers that pass a
    `when` still get the real conversion)."""
    path = str(tmp_path / "ledger.db")
    monkeypatch.setattr(ledger, "_default_db_path", lambda: path)
    _CLOCK["now"] = T0
    real = schedule.utc_iso
    monkeypatch.setattr(schedule, "utc_iso", lambda when=None: real(when) if when else _CLOCK["now"])
    conn = schedule._connect(path)
    schedule.ensure_schema(conn)
    yield conn
    conn.close()


def _dict(conn, job_id):
    return dict(conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone())


# --- 3.2 next_due: the backoff table, executed ------------------------------

@pytest.mark.parametrize("now, attempts, backoff_s, expected", [
    (T0, 1, 60, _at(60)),
    (T0, 2, 60, _at(120)),
    (T0, 3, 60, _at(180)),
    (T0, 1, 90, _at(90)),
    (T0, 2, 90, _at(180)),
    ("2026-10-14T02:00:00+02:00", 1, 60, _at(60)),  # any offset lands in UTC form
])
def test_next_due_table(now, attempts, backoff_s, expected):
    assert schedule.next_due(now, attempts, backoff_s) == expected


def test_next_due_is_monotonic_and_string_ordered():
    dues = [schedule.next_due(T0, a, 60) for a in range(1, 10)]
    assert dues == sorted(dues) and len(set(dues)) == len(dues)
    assert all(T0 < d for d in dues)  # the SQL `due_at <= now` string comparison holds


def test_next_due_is_pure():
    src = inspect.getsource(schedule.next_due)
    for impure in ("_connect", "utc_iso()", "datetime.now", "open("):
        assert impure not in src
    assert schedule.next_due(T0, 2, 60) == schedule.next_due(T0, 2, 60)


# --- 3.1 / 3.3 three due -> firing -> due cycles, then terminal failed --------

def test_three_failures_cycle_then_terminal_failed(fdb):
    _add(fdb, job_id="j", backoff_s=60)  # max_attempts default 3
    seen = []
    for attempt in (1, 2, 3):
        due_at = _dict(fdb, "j")["due_at"]
        _CLOCK["now"] = due_at
        assert [r["job_id"] for r in schedule.due_jobs(fdb, due_at)] == ["j"]
        assert schedule.claim_due(fdb, "j") is True
        assert _dict(fdb, "j")["status"] == "firing"
        assert schedule.claim_due(fdb, "j") is False  # no second claim mid-flight
        schedule.mark_failed("j", f"RuntimeError: boom {attempt}")
        row = _dict(fdb, "j")
        assert row["attempts"] == attempt
        assert row["last_error"] == f"RuntimeError: boom {attempt}"
        if attempt < 3:
            assert row["status"] == "due"
            assert row["due_at"] == schedule.next_due(due_at, attempt, 60)
            assert schedule.due_jobs(fdb, due_at) == []  # backoff: not due yet
        else:
            assert row["status"] == "failed"
            assert row["due_at"] == due_at  # terminal: not re-armed
        seen.append(row["status"])
    assert seen == ["due", "due", "failed"]
    # No fourth cycle: nothing is due, ever, and a stray call writes nothing.
    assert schedule.due_jobs(fdb, _at(10 ** 7)) == []
    before = _dict(fdb, "j")
    schedule.mark_failed("j", "late")
    assert _dict(fdb, "j") == before
    assert fdb.in_transaction is False


# --- 3.4 the bounds and the terminal state ----------------------------------

def test_max_attempts_one_fails_terminally_on_first_failure(fdb):
    _add(fdb, job_id="j", max_attempts=1)
    schedule.claim_due(fdb, "j")
    schedule.mark_failed("j", "ValueError: once")
    row = _dict(fdb, "j")
    assert (row["status"], row["attempts"], row["last_error"]) == ("failed", 1, "ValueError: once")


def test_terminal_failed_is_surfaced_and_never_rearmed(fdb):
    _add(fdb, job_id="j", max_attempts=1)
    schedule.claim_due(fdb, "j")
    schedule.mark_failed("j", "boom")
    assert [r["job_id"] for r in schedule.list_jobs(fdb)] == ["j"]
    assert schedule.status_counts(fdb)["failed"] == 1
    before = _dict(fdb, "j")
    assert schedule.recover_leases(fdb, _at(10 ** 7)) == 0
    assert _dict(fdb, "j") == before
    assert "failed" in timer.LIST_STATUSES and "j" in timer.render_list(fdb)


def test_lease_recovery_rearms_firing_but_not_failed(fdb):
    _add(fdb, job_id="dead", max_attempts=1)
    _add(fdb, job_id="stranded")
    for j in ("dead", "stranded"):
        schedule.claim_due(fdb, j)
    schedule.mark_failed("dead", "boom")
    assert schedule.recover_leases(fdb, _at(10 ** 7)) == 1
    assert _dict(fdb, "stranded")["status"] == "due"
    assert _dict(fdb, "dead")["status"] == "failed"


def test_mark_failed_guard_miss_leaves_row_byte_identical(fdb):
    _add(fdb, job_id="j")  # still `due`: lease recovery took it back
    before = _dict(fdb, "j")
    schedule.mark_failed("j", "late")
    assert _dict(fdb, "j") == before


def test_mark_failed_unknown_job_raises_and_writes_nothing(fdb):
    with pytest.raises(KeyError, match="nope"):
        schedule.mark_failed("nope", "boom")
    assert schedule.list_jobs(fdb) == []


def test_mark_failed_opens_one_connection_and_closes_it(fdb, monkeypatch):
    opened = []
    real = schedule._connect

    def spy(*a, **kw):
        c = real(*a, **kw)
        opened.append(c)
        return c

    monkeypatch.setattr(schedule, "_connect", spy)
    _add(fdb, job_id="j")
    schedule.claim_due(fdb, "j")
    schedule.mark_failed("j", "boom")
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):  # closed: no transaction left open
        opened[0].execute("SELECT 1")


# --- 3.5 mark_fired: terminal success, idempotent ---------------------------

def test_mark_fired_moves_firing_to_fired_once(fdb):
    _add(fdb, job_id="j")
    schedule.claim_due(fdb, "j")
    assert schedule.mark_fired(fdb, "j", _at(5)) is True
    row = _dict(fdb, "j")
    assert (row["status"], row["fired_at"], row["attempts"]) == ("fired", _at(5), 0)
    assert fdb.in_transaction is False
    before = _dict(fdb, "j")
    assert schedule.mark_fired(fdb, "j", _at(10)) is False  # idempotent
    assert _dict(fdb, "j") == before


@pytest.mark.parametrize("status", ["due", "failed", "cancelled"])
def test_mark_fired_on_non_firing_row_writes_nothing(fdb, status):
    _add(fdb, job_id="j")
    with fdb:
        fdb.execute("UPDATE jobs SET status = ? WHERE job_id = 'j'", (status,))
    before = _dict(fdb, "j")
    assert schedule.mark_fired(fdb, "j", _at(5)) is False
    assert _dict(fdb, "j") == before


def test_fired_is_never_rearmed_or_due(fdb):
    _add(fdb, job_id="j")
    schedule.claim_due(fdb, "j")
    schedule.mark_fired(fdb, "j", _at(1))
    assert schedule.recover_leases(fdb, _at(10 ** 7)) == 0
    assert schedule.due_jobs(fdb, _at(10 ** 7)) == []


def test_skipped_mark_fired_is_counted_like_a_lease_rearm(fdb, monkeypatch):
    monkeypatch.setitem(timer.DISPATCH, "notify", (lambda job: None, timer.DISPATCH["notify"][1]))
    _add(fdb, job_id="j")
    monkeypatch.setattr(schedule, "mark_fired", lambda c, j, n: False)
    summary = timer.tick(fdb, now=T0)
    assert (summary["claimed"], summary["fired"], summary["lease_rearm_count"]) == (1, 0, 1)
    monkeypatch.undo()


# --- 3.6 counting discipline: only an action failure counts -----------------

def test_only_an_action_failure_increments_attempts(fdb, monkeypatch, no_toml):
    def boom(job):
        raise RuntimeError("action exploded")

    monkeypatch.setitem(timer.DISPATCH, "notify", (boom, timer.DISPATCH["notify"][1]))
    _add(fdb, job_id="gated", action="send")          # spend-gate deferral
    _add(fdb, job_id="failing", action="notify", probe=0)  # action failure (unwatched:
    # a re-armed failure is otherwise probed and its `last_error` replaced — see below)
    _watched(fdb, "watched")                          # probe miss
    client = FakeClient({"agent_status": "blocked"})
    wd = lambda c, now: schedule.watchdog(c, now, client=client, notifier=_notifier()[1])
    _CLOCK["now"] = _at(1000)
    summary = timer.tick(fdb, now=_at(1000), watchdog=wd)
    assert (summary["spend_gate"], summary["claimed"], summary["probe_miss"]) == (1, 1, 1)
    got = {j: (_dict(fdb, j)["status"], _dict(fdb, j)["attempts"])
           for j in ("gated", "failing", "watched")}
    assert got == {"gated": ("due", 0), "failing": ("due", 1), "watched": ("due", 0)}
    assert _dict(fdb, "gated")["last_error"] == "spend_gate"
    assert _dict(fdb, "watched")["last_error"].startswith("probe_miss ")
    assert _dict(fdb, "failing")["last_error"] == "RuntimeError: action exploded"


def test_watchdog_on_a_rearmed_failure_never_touches_attempts(fdb, no_toml):
    # Named residual: a re-armed failure that is also watched gets its
    # `last_error` replaced by the probe ladder's note; `attempts` survives.
    _add(fdb, job_id="j")
    _CLOCK["now"] = _at(1000)  # fails past warn_after_s; re-armed due_at is in the future
    schedule.claim_due(fdb, "j")
    schedule.mark_failed("j", "RuntimeError: boom")
    schedule.watchdog(fdb, _at(1000), client=FakeClient({"agent_status": "blocked"}),
                      notifier=_notifier()[1])
    row = _dict(fdb, "j")
    assert (row["status"], row["attempts"]) == ("due", 1)
    assert row["last_error"].startswith("probe_miss ")  # the residual, pinned so a fix is visible
