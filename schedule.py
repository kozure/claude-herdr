"""schedule.py — the timer substrate: one durable `jobs` table and the two-path rule.

Two decisions live here and they must never be confused with each other:

* **Firing is a *due-time* decision.** `due_jobs()` selects the rows whose
  `due_at` has passed, `claim_due()` takes exactly one of them with a guarded
  `UPDATE ... WHERE status='due'` whose `rowcount` is the only oracle, and only
  a successful claim may dispatch an action.
* **Escalating is a *probe-result* decision.** A probe that does not come back
  `alive` re-arms the row and eventually raises a stuck candidate. Elapsed time
  on its own never escalates: silence is not evidence.

`classify()` sits on the second path: it may re-arm or raise, and it **never
fires**. No callable in this module dispatches an action to the world.

Frozen **by shape** in parent `0.0`, before the writers that meet here start.
The row shape, the probe record and its vocabularies, the action table and the
tick-summary keys, and the callable signatures below are the contract that
`1.0`–`4.0` are written against. Each of those surfaces is asserted in
`test_schedule.py` by **exact** equality (set or signature string), so a parent
that widens one without a spec revision fails loudly instead of drifting.

The two-path split is also what makes the module importable on its own: it
reads the shared `liveness.toml` **by injected path** (`LIVENESS_TOML_ENV`),
never by importing another workspace, and it needs nothing from `herdr_client`
at module level.
"""
import dataclasses
import json
import os
import sqlite3
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import ledger
from composer_state import ComposerReading, classify_composer, sentinel_for

# --- The `jobs` row shape --------------------------------------------------

#: The 16 carried columns, in row order. The design's row (§6) carries 16; the
#: one deliberate omission is `run_id` — there is no run identity in the ledger
#: today (Non-Goal 6 / D3), so the column is absent rather than nullable-and-unused.
#:
#: `agent_id` and `caller_agent_id` are deliberately separate (2026-11-03):
#: `agent_id` is the **herdr target** the probe ladder watches — a ledger
#: `task_id`, or "" when the job has not fired and has no session yet —
#: while `caller_agent_id` is the **owning agent**, the identity `send`
#: hands to `Bridge.send()`. One column could not mean both: a `send` job
#: created ahead of its due time was being probed as `agent_get("alfred")`,
#: which reads as undecidable and raises a stuck-candidate for a job that has
#: not run yet.
JOBS_COLUMNS = (
    "job_id",
    "due_at",
    "created_at",
    "status",
    "action",
    "payload_ref",
    "agent_id",
    "caller_agent_id",
    "attempts",
    "max_attempts",
    "backoff_s",
    "fired_at",
    "last_error",
    "probe",
    "warn_s",
    "stuck_s",
)

#: The complete status vocabulary. `due` is the only status a tick may claim
#: from; `fired` and `failed` are both terminal, in opposite directions.
STATUS_VOCABULARY = ("due", "firing", "fired", "failed", "cancelled")

# R-1 (carried residual): `cancelled` is vocabulary here — no parent in this
# spec produces or tests it. Nothing may re-arm or probe a `cancelled` row.

#: The lease floor: 2 x the 60 s `StartInterval`. A row stranded in `firing`
#: by a crashed tick re-arms once its lease has elapsed.
LEASE_MIN_S = 120


# --- The action surface ----------------------------------------------------

class _Unbounded(str):
    """Sentinel for an action whose worst-case duration has no upper bound.

    It subclasses `str` so it reads and serialises cleanly, and so that
    `max(LEASE_MIN_S, UNBOUNDED)` **raises** instead of silently computing a
    number: the rule for this class is *stated* (the lease **is**
    `LEASE_MIN_S`), never derived.
    """

    def __repr__(self) -> str:  # pragma: no cover - trivial repr
        return "UNBOUNDED"


#: The declared sentinel. `send` carries it: it is a real Claude session, and
#: a real session has no upper bound. Its value is the word itself so it reads
#: identically in a repr, a log line and a JSON summary.
UNBOUNDED = _Unbounded("UNBOUNDED")

#: One entry per action in the dispatch table, and no entry may be missing:
#: `add_job` refuses an action with no declared worst case. The lease a row
#: carries is `max(LEASE_MIN_S, worst_case_s)` for the bounded entries, and
#: `LEASE_MIN_S` for any entry declaring `UNBOUNDED`.
#:
#: The three bounded values are the implementer's declaration at `0.0` (the
#: spec pins the sentinel and the arithmetic, not these numbers) and are
#: recorded in `02-proofs/0.0-proof.md` for review:
#:   * `notify` 60 s — the three-layer push + marker write, bounded inside one tick.
#:   * `snapshot` 30 s — a DB copy plus a provenance-fragment write.
#:   * `resume` 30 s — the row write plus the pre-dispatch probe.
ACTION_WORST_CASE_S = {
    "notify": 60,
    "send": UNBOUNDED,
    "snapshot": 30,
    "resume": 30,
}

#: The tick summary carries exactly these keys, always all of them — a key set
#: asserted by equality, so "add a counter later" is a deliberate spec change.
TICK_SUMMARY_KEYS = (
    "claimed",
    "fired",
    "rearmed_lease",
    "spend_gate",
    "locked",
    "probe_miss",
    "stuck_candidates",
    "lease_rearm_count",
)


# --- The observation surface -----------------------------------------------

@dataclass(frozen=True)
class ProbeResult:
    """One read-only observation of a pane, and nothing inferred from it.

    A record, not a mutable bag: the ladder's evidence is carried verbatim so
    a later second miss can report both observations in order.
    """

    agent_status: str | None
    read_error: str | None
    composer: ComposerReading | None
    signals_consulted: tuple[str, ...]


#: The three verdicts. `undecidable` is the fail-safe direction: it is the one
#: verdict that never writes to the pane again and *is* escalatable on repeat.
VERDICTS = ("alive", "miss", "undecidable")

#: c1's six reason names — the discriminator recorded beside a verdict. Nine
#: rows of the spec's input mapping resolve into these six names.
REASONS = (
    "composer_holds_prompt",
    "pane_unreadable",
    "blocked_prompt",
    "pane_gone",
    "agent_not_ready",
    "unknown_status",
)

#: The liveness defaults restated here so the module never imports the shared
#: config to read them; resolution order is `row ?? injected toml ?? these`.
RESTATED_LIVENESS = {
    "warn_after_s": 900,
    "stuck_after_s": 1800,
    "probe": {"deadline_s": 5},
}

#: Env var naming the shared `liveness.toml` when an operator overrides the
#: restated defaults. Read **by path**, following `job_queue.load_worker_limit`.
LIVENESS_TOML_ENV = "CLAUDE_BRIDGE_LIVENESS_TOML"


# --- The substrate (`1.0`) -------------------------------------------------

#: Column declarations, keyed by `JOBS_COLUMNS` so the table cannot drift from
#: the frozen row shape. `caller_agent_id` is NOT NULL as a backstop only:
#: `add_job` refuses a missing one first, by name. `agent_id` may be "" — a job
#: scheduled ahead of its run has no session for the probe ladder to watch.
_COLUMN_DECLS = {
    "job_id": "TEXT PRIMARY KEY",
    "due_at": "TEXT NOT NULL",
    "created_at": "TEXT NOT NULL",
    "status": "TEXT NOT NULL DEFAULT 'due' CHECK (status IN ({}))".format(
        ", ".join(f"'{s}'" for s in STATUS_VOCABULARY)
    ),
    "action": "TEXT NOT NULL",
    "payload_ref": "TEXT",
    "agent_id": "TEXT NOT NULL DEFAULT ''",
    "caller_agent_id": "TEXT NOT NULL DEFAULT ''",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "max_attempts": "INTEGER NOT NULL",
    "backoff_s": "INTEGER NOT NULL",
    "fired_at": "TEXT",
    "last_error": "TEXT",
    "probe": "INTEGER",
    "warn_s": "INTEGER",
    "stuck_s": "INTEGER",
}

#: The lock string SQLite raises once `timeout` is exhausted.
_LOCKED = "database is locked"


def _connect(path: str | None = None) -> sqlite3.Connection:
    """Open the ledger DB the way every `schedule.py` connection is opened.

    `timeout=5.0` (SQLite `busy_timeout` 5000 ms) is a c2-local posture: the
    live `ledger.py` writer uses a bare connect. The path defaults to
    `ledger._default_db_path()` and is overridable per call — never the
    stale repo copy `claude_bridge/ledger.db`.
    """
    path = path or ledger._default_db_path()
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create `jobs` if absent. Additive only: no migration of `tasks`, and
    nothing in `tasks` is read or written.

    One additive ALTER is applied to a `jobs` table created before
    `caller_agent_id` existed (2026-11-03). Existing rows are backfilled from
    the old overloaded `agent_id`, and `send` rows lose that value as a target
    because a `send` job's target is unknown until it fires.
    """
    cols = ", ".join(f"{c} {_COLUMN_DECLS[c]}" for c in JOBS_COLUMNS)
    with conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS jobs ({cols})")
        have = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        if "caller_agent_id" not in have:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN caller_agent_id TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "UPDATE jobs SET caller_agent_id = agent_id"
                " WHERE caller_agent_id = '' AND agent_id != ''")
            conn.execute(
                "UPDATE jobs SET agent_id = '' WHERE action = 'send' AND agent_id != ''")


def utc_iso(when: datetime | None = None) -> str:
    """The one timestamp form `jobs` stores: ISO-8601 UTC, `+00:00` suffix
    (the ledger's `datetime.now(timezone.utc).isoformat()` idiom). Every stored
    time and every `now` takes this form, so `due_at <= now` orders as strings."""
    return (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def lease_s(action: str) -> int:
    """The lease a row of `action` carries — the value `--list` shows.

    Bounded: `max(LEASE_MIN_S, worst_case_s)`. `UNBOUNDED`: the lease **is**
    `LEASE_MIN_S` — stated, never computed (`max()` on the sentinel raises).
    No column holds it: the 15-column row is frozen, and the action fully
    determines it.
    """
    worst = ACTION_WORST_CASE_S[action]
    if worst is UNBOUNDED:
        return LEASE_MIN_S
    return max(LEASE_MIN_S, worst)


def _validate_ref(payload_ref: str | None) -> None:
    # ponytail: "a path" = absolute and single-line; content that happens to
    # look like an absolute path still passes. Tighten if inline blobs appear.
    if payload_ref is None:
        return
    if (
        not isinstance(payload_ref, str)
        or "\n" in payload_ref
        or "\0" in payload_ref
        or not os.path.isabs(payload_ref)
    ):
        raise ValueError(
            f"payload_ref must name an absolute path, not inline content: {payload_ref!r}"
        )


# --- The frozen callable surface -------------------------------------------
#
# Owner parent is named on every stub, and every body raises. Nothing here is
# implemented at `0.0`: the point is that `4.0` may be written against these
# signatures while `1.0`–`3.0` fill them in.

def add_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    action: str,
    caller_agent_id: str,
    due_at: str,
    agent_id: str = "",
    payload_ref: str | None = None,
    max_attempts: int = 3,
    backoff_s: int = 60,
    probe: int | None = None,
    warn_s: int | None = None,
    stuck_s: int | None = None,
) -> None:
    """Insert one `status='due'` row. Owner: `1.0` (sub-task 1.2).

    Refuses an action with no declared `ACTION_WORST_CASE_S`, an inline
    payload (`payload_ref` names a path, it never carries content), and a
    missing `caller_agent_id`. `agent_id` is the herdr target the probe
    ladder watches and defaults to "" — a job that has not fired yet has no
    session, and probing the caller's name as if it were a `task_id` is what
    produced false stuck-candidates (2026-11-03 review, finding 5).
    """
    if not caller_agent_id:
        raise ValueError(
            f"caller_agent_id is required on every job row, got {caller_agent_id!r}")
    if action not in ACTION_WORST_CASE_S:
        raise ValueError(
            f"action {action!r} declares no ACTION_WORST_CASE_S; refusing to schedule it"
        )
    _validate_ref(payload_ref)
    try:
        parsed = datetime.fromisoformat(due_at)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ValueError(f"due_at must be a timezone-aware ISO-8601 string: {due_at!r}")
    with conn:
        conn.execute(
            "INSERT INTO jobs (job_id, due_at, created_at, status, action, payload_ref,"
            " agent_id, caller_agent_id, attempts, max_attempts, backoff_s, probe,"
            " warn_s, stuck_s)"
            " VALUES (?, ?, ?, 'due', ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (job_id, utc_iso(parsed), utc_iso(), action, payload_ref,
             agent_id or "", caller_agent_id,
             max_attempts, backoff_s, probe, warn_s, stuck_s),
        )


def due_jobs(conn: sqlite3.Connection, now: str) -> list[sqlite3.Row]:
    """Rows with `status='due'` and `due_at <= now`. Owner: `1.0` (1.3).

    `now` is an ISO-8601 UTC **string**, so the comparison is a string
    comparison and no date is parsed at query time. A job missed by several
    ticks is still returned: catch-up is a property of the query.
    """
    return conn.execute(
        "SELECT * FROM jobs WHERE status = 'due' AND due_at <= ? ORDER BY due_at, job_id",
        (now,),
    ).fetchall()


def claim_due(conn: sqlite3.Connection, job_id: str) -> bool:
    """Take one due row atomically. Owner: `1.0` (1.4).

    Returns `True` when this caller won the row and `False` when another tick
    already owns it; the guarded `UPDATE`'s `rowcount` is the only oracle.
    """
    # One short statement, committed by `with conn:` before returning: no write
    # transaction outlives the claim, so none is held across a dispatch.
    # `fired_at` here is the claim time the lease is measured from.
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'firing', fired_at = ? WHERE job_id = ? AND status = 'due'",
            (utc_iso(), job_id),
        )
    return cur.rowcount == 1


def try_claim(conn: sqlite3.Connection, job_id: str, summary: dict) -> bool:
    """`claim_due`, with the lock posture: a `database is locked` outliving the
    5 s busy timeout is caught, counted into `summary['locked']`, and the row is
    left exactly as found (the `with conn:` rollback means no partial write).
    Any other `OperationalError` propagates."""
    try:
        return claim_due(conn, job_id)
    except sqlite3.OperationalError as e:
        if _LOCKED not in str(e):
            raise
        summary["locked"] += 1
        return False


def recover_leases(conn: sqlite3.Connection, now: str) -> int:
    """Return every `firing` row older than its lease to `due`; the count is the
    tick's `lease_rearm_count`.

    Applies to `firing` only — `fired`/`failed`/`cancelled` are never touched.
    An `UNBOUNDED` row re-arms after `LEASE_MIN_S` like any other (no zombie
    `firing` row); its re-dispatch is then held by the spend gate, which `4.0`
    checks before the claim. `last_error='lease_rearm'` records that the
    previous `mark_fired` did not land — not that the action never ran.
    """
    cutoff = datetime.fromisoformat(now)
    rearmed = 0
    rows = conn.execute(
        "SELECT job_id, action, fired_at FROM jobs WHERE status = 'firing'"
    ).fetchall()
    for r in rows:
        if r["fired_at"] is not None and (
            datetime.fromisoformat(r["fired_at"]) + timedelta(seconds=lease_s(r["action"]))
            > cutoff
        ):
            continue
        # Guarded on the observed `fired_at`, so a row re-claimed between the
        # SELECT and here is left to its new owner.
        with conn:
            cur = conn.execute(
                "UPDATE jobs SET status = 'due', last_error = 'lease_rearm'"
                " WHERE job_id = ? AND status = 'firing' AND fired_at IS ?",
                (r["job_id"], r["fired_at"]),
            )
        rearmed += cur.rowcount
    return rearmed


def mark_fired(conn: sqlite3.Connection, job_id: str, now: str) -> bool:
    """Move a `firing` row to terminal `fired`. Owner: `3.0` (3.5).

    Idempotent: an already-`fired` row is written not at all and returns
    `False`. A caught-and-skipped return is itself a re-fire source and is
    counted exactly like a lease re-arm.
    """
    # Guarded on `firing`, like `claim_due`: `rowcount` is the only oracle, and
    # a miss (already `fired`, re-armed by lease recovery, unknown) writes nothing.
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'fired', fired_at = ? WHERE job_id = ? AND status = 'firing'",
            (now, job_id),
        )
    return cur.rowcount == 1


def mark_failed(job_id: str, error: str) -> None:
    """Record an action failure and re-arm or terminate the row. Owner: `3.0` (3.1).

    Increments `attempts`, writes `last_error` from the caught exception, and
    re-arms through `next_due(...)` while `attempts < max_attempts`; past the
    bound the row is left terminally `failed`.
    """
    # The frozen seam carries no `conn` and no `now` (0.0 decision 3): one
    # connection of its own, and `utc_iso()` is the one clock read. No
    # `ensure_schema` here — a stray call must never create a table.
    now = utc_iso()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT status, attempts, max_attempts, backoff_s FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"mark_failed: no job {job_id!r}")
        if row["status"] != "firing":
            return  # another tick owns the row now (lease re-arm); leave it as found
        attempts = row["attempts"] + 1
        if attempts < row["max_attempts"]:
            status, due_at = "due", next_due(now, attempts, row["backoff_s"])
        else:
            status, due_at = "failed", None
        # CAS on the observed `attempts`: a row re-armed and re-claimed between
        # the SELECT and here is left byte-for-byte to its new owner.
        with conn:
            conn.execute(
                "UPDATE jobs SET status = ?, attempts = ?, last_error = ?,"
                " due_at = COALESCE(?, due_at)"
                " WHERE job_id = ? AND status = 'firing' AND attempts = ?",
                (status, attempts, error, due_at, job_id, row["attempts"]),
            )
    finally:
        conn.close()


def next_due(now: str, attempts: int, backoff_s: int) -> str:
    """The re-arm time after failure number `attempts`: `now + backoff_s x attempts`.

    Pure — no connection, no clock; the caller injects `now`. Linear and
    monotonic in `attempts`; the bound is `mark_failed`'s, which calls this only
    while `attempts < max_attempts`. Returns the stored ISO-8601 UTC form so the
    re-armed `due_at` orders as a string against `now`.
    """
    return utc_iso(datetime.fromisoformat(now) + timedelta(seconds=backoff_s * attempts))


def list_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every job row, for `timer.py --list`. Owner: `1.0` (read side of `--list`)."""
    return conn.execute("SELECT * FROM jobs ORDER BY due_at, job_id").fetchall()


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts per status, including terminally `failed`. Owner: `1.0`."""
    counts = dict.fromkeys(STATUS_VOCABULARY, 0)
    for status, n in conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status"):
        counts[status] = n
    return counts


def classify(probe_result: ProbeResult, now: str, job: sqlite3.Row) -> tuple[str, str]:
    """Map one probe observation to `(verdict, reason)`. Owner: `2.0` (2.2).

    The mapping is **total** over its declared input: a status string the spec
    does not list resolves to `("undecidable", "unknown_status")`, never to
    `alive`. `now` is injectable, and no branch consults elapsed time.

    It re-arms or raises; it never fires. Alive rows carry the empty reason
    (the spec's "—"). A `fired`/`cancelled` job is refused: those rows are
    never probed, so classifying one is a caller bug, not a verdict.
    """
    if job["status"] in ("fired", "cancelled"):
        raise ValueError(f"{job['status']} rows are never probed: {job['job_id']}")
    status = probe_result.agent_status
    if status in ("working", "done"):                       # rows 1, 2
        return "alive", ""
    if status in _MISS_REASON:                              # rows 3, 4
        return "miss", _MISS_REASON[status]
    if status == "idle":                                    # rows 5, 6, 7
        c = probe_result.composer
        if c is None or not c.legible:
            return "undecidable", "pane_unreadable"
        return ("miss", "composer_holds_prompt") if c.sentinel_present else ("alive", "")
    if status is None or status == "unknown":               # row 8
        return "undecidable", "agent_not_ready"
    return "undecidable", "unknown_status"                  # row 9: total


# --- The watchdog (`2.0`) --------------------------------------------------
#
# Escalation is a probe-result decision. Thresholds decide only *when to look*;
# `classify` decides what was seen; `escalate` walks the ladder. Nothing here
# fires a job or touches a session: the only writes are to `jobs` rows.

#: `blocked` is a herdr status; `killed` is the **ledger's** task state.
_MISS_REASON = {"blocked": "blocked_prompt", "killed": "pane_gone"}

#: Where the shared liveness config lives when nothing overrides it.
DEFAULT_LIVENESS_TOML = os.path.expanduser(
    "~/qwenpaw/workspaces/workspaces/default/liveness/liveness.toml"
)

#: The ladder's durable state lives in `last_error` (the 15-column row has no
#: counter column): `probe_miss`/`probe_undecidable` = counter at 1,
#: `stuck_candidate` = the per-incident latch. Each is followed by JSON evidence.
PROBE_ANNOTATION = {"miss": "probe_miss", "undecidable": "probe_undecidable"}
STUCK_LATCH = "stuck_candidate"

#: What a human may do about a stuck-candidate. The watchdog does none of them.
STUCK_OPTIONS = (
    "inspect the pane",
    "submit or clear the composer by hand",
    "cancel the job",
    "kill the session (only on the operator's explicit approval)",
)


def resolve_thresholds(job=None, toml_path: str | None = None) -> dict:
    """Thresholds in force for `job`: `row ?? injected liveness.toml ?? RESTATED_LIVENESS`.

    The toml is read **by path** (`toml_path`, else `$CLAUDE_BRIDGE_LIVENESS_TOML`,
    else `DEFAULT_LIVENESS_TOML`), following `job_queue.load_worker_limit`. An
    absent or unreadable file degrades to the restated defaults and says so in
    `fallback` — never silently, never by raising. A row's `probe` NULL means
    probed; `0` means never probed.
    """
    path = toml_path or os.environ.get(LIVENESS_TOML_ENV) or DEFAULT_LIVENESS_TOML
    fallback = None
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        tiers, probe_cfg = data.get("tiers", {}), data.get("probe", {})
        toml = {
            "warn_after_s": tiers.get("warn_after_s"),
            "stuck_after_s": tiers.get("stuck_after_s"),
            "probe_deadline_s": probe_cfg.get("deadline_s"),
        }
        toml = {k: None if v is None else int(v) for k, v in toml.items()}
    except (OSError, tomllib.TOMLDecodeError, ValueError, TypeError, AttributeError) as e:
        toml, fallback = {}, f"{type(e).__name__}: {path}"
    default = {
        "warn_after_s": RESTATED_LIVENESS["warn_after_s"],
        "stuck_after_s": RESTATED_LIVENESS["stuck_after_s"],
        "probe_deadline_s": RESTATED_LIVENESS["probe"]["deadline_s"],
    }
    row = {
        "warn_after_s": job["warn_s"] if job is not None else None,
        "stuck_after_s": job["stuck_s"] if job is not None else None,
        "probe_deadline_s": None,
    }
    out = {}
    for k in default:
        for v in (row[k], toml.get(k), default[k]):
            if v is not None:
                out[k] = v
                break
    out["probe"] = job is None or job["probe"] is None or bool(job["probe"])
    out["source"] = path
    out["fallback"] = fallback
    return out


def probe(
    agent_id: str,
    pane: str | None,
    now: str,
    *,
    client=None,
    deadline_s: float | None = None,
    socket_path: str | None = None,
    ledger_state: str | None = None,
) -> ProbeResult:
    """One read-only look at `agent_id`'s session. Never raises.

    `agent_id` is the herdr target — the ledger's `task_id`, not its owning
    agent. A ledger `killed` is authoritative (`bridge.py`) and needs no herdr
    call. Otherwise one `agent_get`, plus one `pane_read` + composer reading
    only when the status is `idle`. The probe owns its deadline: a client it
    builds gets `timeout=deadline_s` (resolved default 5 s), and any client
    exception — `HerdrTimeout` included — becomes `read_error`.
    """
    if ledger_state == "killed":
        return ProbeResult("killed", None, None, ("ledger_state",))
    if client is None:
        import herdr_client  # lazy: the module imports nothing from herdr at load

        if deadline_s is None:
            deadline_s = resolve_thresholds()["probe_deadline_s"]
        client = herdr_client.HerdrClient(socket_path=socket_path, timeout=deadline_s)
    try:
        agent = client.agent_get(agent_id)
    except Exception as e:  # noqa: BLE001 — a failed read is evidence, not an alarm
        return ProbeResult(None, type(e).__name__, None, ("agent_get",))
    status = agent.get("agent_status") if isinstance(agent, dict) else None
    if status is not None and not isinstance(status, str):
        status = str(status)
    if status != "idle":
        return ProbeResult(status, None, None, ("agent_get",))
    pane = pane or agent.get("pane_id")
    raw, read_error = "", None
    if pane:
        try:
            raw = client.pane_read(pane, source="visible")
        except Exception as e:  # noqa: BLE001
            read_error = type(e).__name__
    else:
        read_error = "NoPane"
    reading = classify_composer(raw or "", sentinel_for(agent_id))
    return ProbeResult("idle", read_error, reading, ("agent_get", "pane_read", "composer"))


def should_probe(job, now: str, thresholds: dict) -> bool:
    """Elapsed time decides *when to look*, never what to conclude: first look
    once the job is `warn_after_s` old, a confirming look (row already on the
    ladder) once it is `stuck_after_s` old. `probe = 0` is never looked at."""
    if not thresholds["probe"]:
        return False
    elapsed = (datetime.fromisoformat(now) - datetime.fromisoformat(job["created_at"])).total_seconds()
    on_ladder = _annotation(job["last_error"]) in (*PROBE_ANNOTATION.values(), STUCK_LATCH)
    return elapsed >= thresholds["stuck_after_s" if on_ladder else "warn_after_s"]


def _annotation(last_error: str | None) -> str | None:
    return last_error.split(" ", 1)[0] if last_error else None


def _evidence(result: ProbeResult, verdict: str, reason: str, now: str) -> dict:
    return {"at": now, "verdict": verdict, "reason": reason, "probe": dataclasses.asdict(result)}


def escalate(conn, job, result: ProbeResult, now: str, thresholds: dict, notifier=None) -> str:
    """Walk the ladder for one probed row; return the step taken.

    `alive` clears the counter and the latch. A first non-`alive` re-arms
    (`status='due'`, `probe_miss`/`probe_undecidable` + evidence) and notifies
    nobody. A second consecutive one raises **one** stuck-candidate through
    `notify.notify_completion` — latch written first, guarded, so two ticks
    cannot both notify. Latched rows add nothing until an `alive` clears them.
    Every write is guarded on the observed `last_error`; `attempts` is never touched.
    """
    verdict, reason = classify(result, now, job)
    prev = job["last_error"]
    kind = _annotation(prev)
    guard = "WHERE job_id = ? AND status = 'due' AND last_error IS ?"
    if verdict == "alive":
        if kind in (*PROBE_ANNOTATION.values(), STUCK_LATCH):
            with conn:
                conn.execute(f"UPDATE jobs SET last_error = NULL {guard}", (job["job_id"], prev))
        return "alive"
    evidence = _evidence(result, verdict, reason, now)
    if kind == STUCK_LATCH:
        return "latched"
    if kind not in PROBE_ANNOTATION.values():
        note = f"{PROBE_ANNOTATION[verdict]} {json.dumps(evidence)}"
        with conn:
            conn.execute(f"UPDATE jobs SET status = 'due', last_error = ? {guard}",
                         (note, job["job_id"], prev))
        return "probe_miss"
    candidate = {
        "job_id": job["job_id"],
        "agent_id": job["agent_id"],
        "reason": reason,
        "probes": [json.loads(prev.split(" ", 1)[1]), evidence],
        "thresholds": thresholds,
        "options": list(STUCK_OPTIONS),
        "raised_at": now,
    }
    latch = f"{STUCK_LATCH} {json.dumps(candidate)}"
    with conn:
        cur = conn.execute(f"UPDATE jobs SET last_error = ? {guard}", (latch, job["job_id"], prev))
    if cur.rowcount != 1:
        return "latched"  # another tick moved the row first; it owns the notify
    if notifier is None:
        import notify

        notifier = notify.notify_completion
    sent = notifier(f"stuck-{job['job_id']}", STUCK_LATCH, reason,
                    f"jobs/{job['job_id']}#last_error", json.dumps(candidate))
    candidate["notify"] = sent
    with conn:
        conn.execute(f"UPDATE jobs SET last_error = ? {guard}",
                     (f"{STUCK_LATCH} {json.dumps(candidate)}", job["job_id"], latch))
    return "stuck_candidate"


def _ledger_task(conn, task_id: str):
    """`(last_state, herdr_pane_id)` for the probed session, or `(None, None)`
    when the ledger has no such task or no `tasks` table."""
    try:
        r = conn.execute(
            "SELECT last_state, herdr_pane_id FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None, None
    return (r[0], r[1]) if r else (None, None)


#: Ledger `last_state` values the watchdog probes as live sessions. A session
#: started by `Bridge.send()` lands in `tasks` (the ledger), never in `jobs`, so
#: the jobs-only SELECT would never see it. This pass closes that gap.
WATCHED_LEDGER_STATES = ("working",)

#: A `read_error` that means the herdr *daemon* is unreachable, not that one
#: session is stuck. Flagging every working session on a daemon blip would be a
#: false-positive storm, so the ledger pass skips it (the completion watcher
#: already treats `daemon_down` the same way).
DAEMON_DOWN_READ_ERRORS = ("HerdrServerDown",)


def _working_ledger_sessions(conn):
    """Ledger `tasks` rows that are live sessions the jobs pass never covers:
    `last_state` in `WATCHED_LEDGER_STATES`, with a pane, and no `jobs` row
    already probing the same task (a session dispatched through the timer has
    both; its jobs row owns the ladder). Empty when `tasks` is absent."""
    states = ", ".join(f"'{s}'" for s in WATCHED_LEDGER_STATES)
    try:
        return conn.execute(
            f"SELECT task_id, agent_id, herdr_pane_id, created_at, notified_state"
            f" FROM tasks WHERE last_state IN ({states}) AND herdr_pane_id IS NOT NULL"
            f" AND task_id NOT IN (SELECT agent_id FROM jobs WHERE agent_id IS NOT NULL)"
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _ledger_stale(row, now: str, thresholds: dict) -> bool:
    """A working ledger session is probe-eligible once it is `stuck_after_s`
    old — the same "when to look" discipline as `should_probe`, keyed on the
    ledger's `created_at` (there is no jobs row to carry `warn_s`/`stuck_s`).
    Elapsed time decides *when to look*, never what to conclude."""
    if not thresholds["probe"]:
        return False
    try:
        elapsed = (datetime.fromisoformat(now) - datetime.fromisoformat(row["created_at"])).total_seconds()
    except (TypeError, ValueError):
        return False
    return elapsed >= thresholds["stuck_after_s"]


def _flag_ledger_stuck(conn, row, result: ProbeResult, now: str, notifier=None) -> str:
    """Surface a stuck working-ledger-session, at most once per incident.

    `alive` is skipped (and clears a prior latch); a `miss`/`undecidable`
    raises **one** stuck-candidate, de-duplicated on `tasks.notified_state` so
    two ticks cannot both notify. A session already past startup has no
    slow-start ambiguity, so a single conclusive miss is enough — there is no
    two-probe ladder here, unlike the jobs path. Returns the step taken,
    mirroring `escalate`'s vocabulary.
    """
    verdict, reason = classify(result, now, {"status": "due"})
    if verdict == "alive":
        if row["notified_state"] == STUCK_LATCH:
            with conn:
                conn.execute(
                    "UPDATE tasks SET notified_state = NULL, notified_at = NULL"
                    " WHERE task_id = ? AND notified_state = ?",
                    (row["task_id"], STUCK_LATCH),
                )
        return "alive"
    if row["notified_state"] == STUCK_LATCH:
        return "latched"
    candidate = {
        "task_id": row["task_id"],
        "agent_id": row["agent_id"],
        "reason": reason,
        "probes": [_evidence(result, verdict, reason, now)],
        "options": list(STUCK_OPTIONS),
        "raised_at": now,
    }
    if notifier is None:
        import notify

        notifier = notify.notify_completion
    # Latch first, guarded, exactly as `escalate` does: the UPDATE is the point
    # of mutual exclusion. Notifying first let two concurrent ticks both send —
    # the docstring's "two ticks cannot both notify" only holds if the write
    # that claims the row happens before the side effect.
    with conn:
        cur = conn.execute(
            "UPDATE tasks SET notified_state = ?, notified_at = ?"
            " WHERE task_id = ? AND notified_state IS ?",
            (STUCK_LATCH, now, row["task_id"], row["notified_state"]),
        )
    if cur.rowcount != 1:
        return "latched"
    sent = notifier(f"stuck-{row['task_id']}", STUCK_LATCH, reason,
                    f"tasks/{row['task_id']}#working", json.dumps(candidate))
    candidate["notify"] = sent
    return "stuck_candidate"


def watchdog(conn, now: str, *, toml_path=None, client=None, socket_path=None, notifier=None) -> dict:
    """The probe pass for one tick: `{"probe_miss": n, "stuck_candidates": n}`.

    Two sources are watched: `jobs` rows not yet due (timer-scheduled sessions,
    whose ladder lives in `jobs.last_error`), and — the gap this closes —
    working ledger sessions started by `Bridge.send()` that have no `jobs` row
    at all. `fired`/`cancelled`/`failed`/`firing` rows are never probed, and a
    ledger session that is alive, already latched, or behind a daemon-down is
    left exactly as found.
    """
    counts = {"probe_miss": 0, "stuck_candidates": 0}
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'due' AND due_at > ? ORDER BY due_at, job_id", (now,)
    ).fetchall()
    for job in rows:
        th = resolve_thresholds(job, toml_path)
        if not should_probe(job, now, th):
            continue
        if not job["agent_id"]:
            # No session yet (a `send` job ahead of its due time has no herdr
            # target). Probing the caller's name as a task_id read as
            # undecidable and raised a stuck-candidate for a job that had not
            # run — skip it until it fires and the target is recorded.
            continue
        state, pane = _ledger_task(conn, job["agent_id"])
        result = probe(job["agent_id"], pane, now, client=client,
                       deadline_s=th["probe_deadline_s"], socket_path=socket_path,
                       ledger_state=state)
        step = escalate(conn, job, result, now, th, notifier)
        if step != "alive":
            counts["probe_miss"] += 1
        if step == "stuck_candidate":
            counts["stuck_candidates"] += 1

    # Bridge.send ledger sessions live in `tasks`, never in `jobs`: probe them
    # directly so a session stuck in `working` whose pane is gone still
    # surfaces as a stuck-candidate instead of vanishing into the empty table.
    for row in _working_ledger_sessions(conn):
        th = resolve_thresholds(None, toml_path)
        if not _ledger_stale(row, now, th):
            continue
        result = probe(row["task_id"], row["herdr_pane_id"], now, client=client,
                       deadline_s=th["probe_deadline_s"], socket_path=socket_path)
        if result.read_error in DAEMON_DOWN_READ_ERRORS:
            continue
        step = _flag_ledger_stuck(conn, row, result, now, notifier)
        if step != "alive":
            counts["probe_miss"] += 1
        if step == "stuck_candidate":
            counts["stuck_candidates"] += 1
    return counts
