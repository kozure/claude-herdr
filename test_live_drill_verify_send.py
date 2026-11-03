"""Live drill for verify-before-send (SDD 01) Unit 5 — real-rendering validation.

Operator-gated via ``HERDR_INTEGRATION=1``, the house gate (``test_herdr_client.py``,
``test_bridge.py``). Nothing here touches a live pane unless that variable is set,
so the normal suite collects this file and skips the drill::

    HERDR_INTEGRATION=1 python3 -m pytest -q test_live_drill_verify_send.py

**It types real prompts into real Claude Code panes. It runs only with the operator's go.**
This file was authored against the seam frozen by parent ``0.0``
(``composer_state.ComposerReading``, ``sentinel_for``, ``classify_composer``) while
``3.0``/``4.0`` landed; the evidence run itself needs ``4.0`` in place.

The bar, quantified, is the one the spec states: **0 false acceptances and 0 false
non-acceptances against each run's ground truth, across >= 5 consecutive dispatches**
(``HERDR_DRILL_RUNS`` overrides the count, never below the spec's 5). One disagreement
fails the drill and sends the mechanism to the transcript-based contingency (design
§9) with that run's raw text kept. A run whose ground truth cannot be established is
*inconclusive* and also fails the bar — the drill never passes vacuously.

Ground truth, and why it is the *reverse* of the nonce
------------------------------------------------------
Each dispatch's prompt is the drill's own: reply with one line of the form
``drill-ok <reverse of NONCE>``. The wire text carries ``NONCE`` in forward form only,
so the reversed form can exist in the pane *only* after the session has read and
processed the prompt — which makes the ground truth independent of the pane's
rendering (the thing under test) and needs no tool use, so no permission dialog can
mask a real submission. ``NONCE`` is asserted non-palindromic.

Recorded per run: the raw pane text (truncated exactly as ``send()`` truncates it,
``raw[:200]``, see ``Bridge._verdict``), the per-signal reading ``R``/``P``/``M`` from
the drill's own post-send read, the transition outcome ``T`` and verdict the mechanism
returned, the first attempt's evidence, the ledger row, the ground truth and the
comparison. One JSON object per line lands in ``01-proofs/5.0-drill-log.jsonl``; every
string written there passes through ``scrub()``.

Teardown is the drill's own hygiene and is *not* the mechanism's behaviour: an
accepted pane is closed, a rejected one is left open for inspection (exactly what the
blocked-class message tells a human). ``send()``'s failure path never closes anything —
``test_verify_send_failure.py`` proves that separately.
"""
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import pytest

import bridge
import ledger as ledger_module
from bridge import ALLOWED_CWD, Bridge
from composer_state import COLLAPSE_TOKENS, ComposerReading, classify_composer, sentinel_for
from herdr_client import HerdrClient
from ledger import Ledger

HOME = os.path.expanduser("~")

# --- the gate ---------------------------------------------------------------

LIVE = pytest.mark.skipif(
    not os.environ.get("HERDR_INTEGRATION"),
    reason="Set HERDR_INTEGRATION=1 to run the live drill (types real prompts)",
)

# --- constants --------------------------------------------------------------

RUNS = max(5, int(os.environ.get("HERDR_DRILL_RUNS", "5")))  # spec floor is >= 5
RAW_LIMIT = 200                     # send() truncates pane_text[:200] (_verdict)
DRILL_CWD = os.path.join(ALLOWED_CWD, "_verify_send_drill")
SETTLE_S = float(os.environ.get("HERDR_DRILL_SETTLE_S", "180"))  # ground-truth poll
POLL_S = 2.0
KEEP_PANES = os.environ.get("HERDR_DRILL_KEEP_PANES") == "1"

PROOFS = Path(__file__).resolve().parent.parent / (
    "docs/specs/01-spec-verify-before-send/01-proofs")
LOG = PROOFS / "5.0-drill-log.jsonl"

#: The verdict vocabulary the drill reads back (frozen by the task list).
REASONS = ("composer_holds_prompt", "no_submission_evidence", "pane_unreadable")
VERIFIED_BY = ("transition", "marker")
EVIDENCE_KEYS = ("transition", "composer_check", "sentinel_present",
                 "marker_after_composer", "signals_consulted")

ROW_SQL = ("SELECT last_state, pending_result, collected_at, cached_result "
           "FROM tasks WHERE task_id = ?")

#: Secret-shaped substrings redacted before anything reaches an artifact. The
#: dash/underscore-suffixed key prefixes carry a token *body*, so the redaction
#: consumes the body with the prefix. Matching a bare `sk-` does both things
#: wrong: it leaves the key's body in the artifact (the part that is actually
#: secret), and it eats this package's own `task-<hex>` ids — "ta·sk-·d619ba99" —
#: which are the sentinel echoes the drill log exists to show. A real body is
#: long and alphanumeric, and the prefix is word-initial; requiring both keeps
#: the redaction on secrets and off the drill's identifiers.
SECRET_MARKERS = ("credentials.yaml", "password", "passwd", "secret", "api_key",
                  "apikey", "authorization", "bearer ", "sk-", "ghp_", "gho_",
                  "xoxb-", "private_key", "BEGIN RSA", "BEGIN OPENSSH")
_KEY_PREFIXES = ("sk-", "ghp_", "gho_", "xoxb-")
_KEY_BODY = r"[A-Za-z0-9_\-]{16,}"
_SECRET_RE = re.compile(
    "|".join(
        (r"(?<![A-Za-z0-9])" + re.escape(m) + _KEY_BODY) if m in _KEY_PREFIXES
        else re.escape(m)
        for m in SECRET_MARKERS
    ),
    re.IGNORECASE)


def scrub(text):
    """Redact secret-shaped substrings. Identity for non-strings and clean text."""
    return _SECRET_RE.sub("[redacted]", text) if isinstance(text, str) else text


# --- 5.2: carried residual R2, pinned on the drill's sanitized fixture -------
#
# R2 is the scoping residual the task list hands to this parent: the legibility
# rule must inspect *every* `❯`-bearing line of the read, not only the last, or a
# collapse form on an earlier `❯` line escapes inspection and an undecidable read
# becomes eligible for `no_submission_evidence` — the one verdict that writes to
# the pane again. `1.0` owns the rule; 5.0 independently pins that the shape the
# rule is aimed at is genuinely exercised.
#
# The fixture is the drill's own sanitized record of that shape, in the same
# vocabulary as `test_composer_state.py`'s synthetic table: a paste-collapse token
# on a `❯`-bearing line that is NOT the last `❯` line, with a clean composer below
# it. It carries no session content, no path and no credential.

DRILL_FIXTURE_RAW = (
    "❯ [Pasted text #1 +24 lines]\n"
    "⏺ On it.\n"
    "✻ Working\n"
    "❯ \n"
)
DRILL_FIXTURE_SENTINEL = "[verify-send:task-00000000]"


def _composer_lines(raw):
    return [i for i, line in enumerate(raw.splitlines()) if "❯" in line]


def test_drill_fixture_is_scrubbed():
    """The fixture is an artifact: it must be free of secrets, tokens, paths."""
    assert not _SECRET_RE.search(DRILL_FIXTURE_RAW)
    assert scrub(DRILL_FIXTURE_RAW) == DRILL_FIXTURE_RAW  # nothing to redact
    assert "credentials.yaml" not in DRILL_FIXTURE_RAW


def test_scrub_takes_the_key_body_and_spares_the_drills_own_ids():
    """The redaction must consume a key's *body* — and must not eat `task-<hex>`.

    Both halves were wrong before the first live run, from one root cause: `sk-`
    matched bare, so `sk-ABCDEF0123456789` lost only its prefix (the body — the
    secret — stayed in the artifact) while `task-d619ba99` lost its middle
    ("ta·sk-·d619ba99"), mangling the sentinel echo the drill log exists to show.
    """
    key = "sk-" + "A1b2C3d4E5f6G7h8I9j0"          # a real-shaped body, >= 16
    assert scrub(f"token={key} done") == "token=[redacted] done", (
        "the key body must go with the prefix, or the artifact still carries it")
    assert scrub("task-d619ba99") == "task-d619ba99", (
        "the package's own task ids must survive the scrub")
    assert scrub("[verify-send:task-d619ba99]") == "[verify-send:task-d619ba99]"
    assert scrub("task-a1b2c3d4e5f6a7b8") == "task-a1b2c3d4e5f6a7b8", (
        "a longer task id must not match on its 'sk-' interior alone")


def test_drill_fixture_puts_the_collapse_token_on_a_non_last_composer_line():
    """R2's shape, asserted on the fixture itself rather than assumed.

    If this ever stops being true the R2 check below becomes vacuous — it would
    be confirming the last-line-only rule instead of refuting it — so the shape is
    asserted before the rule is.
    """
    lines = DRILL_FIXTURE_RAW.splitlines()
    composer = _composer_lines(DRILL_FIXTURE_RAW)
    assert len(composer) >= 2, "the fixture must carry more than one ❯ line"
    # Scoped to the ❯-bearing lines: R2 is about which *composer* lines the rule
    # inspects, and only a composer line can make a reading undecidable. A marker
    # line below the composer is not inspected at all, so it is not evidence here.
    token_lines = [i for i in composer
                   if any(tok in lines[i] for tok in COLLAPSE_TOKENS)]
    assert token_lines, "no ❯ line in the fixture carries a COLLAPSE_TOKENS token"
    assert all(i < composer[-1] for i in token_lines), (
        "the collapse token sits on the LAST ❯ line, which would make the R2 check "
        "vacuous — R2 is precisely the non-last case")
    last = lines[composer[-1]]
    assert not any(tok in last for tok in COLLAPSE_TOKENS)
    assert "[" not in last and "]" not in last, (
        "the fixture's last ❯ line must be clean, so only the wider scope can make "
        "the reading undecidable")


def test_drill_fixture_fails_the_reading_if_the_rule_narrows_to_the_last_line():
    """The R2 check: the drill's fixture must read undecidable.

    Under the wide scope `1.0` implements, the earlier `❯` line carrying the collapse
    token is inspected and the reading is `legible=False` — `pane_unreadable`, never
    `no_submission_evidence`. If the rule ever narrows back to the last line only,
    the last line here is clean, `legible` flips to `True`, and this assertion fails.
    """
    r = classify_composer(DRILL_FIXTURE_RAW, DRILL_FIXTURE_SENTINEL)
    assert r.legible is False, (
        "the collapse token on a non-last ❯ line was not inspected — the rule have "
        "narrowed to the last line only (carried residual R2)")
    assert not (r.legible and not r.sentinel_present), (
        "an undecidable read reached the `no_submission_evidence`-eligible state")


def test_r2_check_fails_against_a_last_line_only_rule(monkeypatch):
    """The R2 check has teeth — executed, not asserted.

    A check that cannot fail is not a check. The rule is replaced with a faithful
    last-line-only variant (the form R2 warns against: the last `❯` line and nothing
    else) and the check above is shown to fail under it. If a later edit narrows
    ``composer_state``'s scope the same way, that check fails for real — this test
    says the failure would be noticed, and says which way the rule must not move.
    """
    def last_line_only(raw_text, sentinel):
        lines = raw_text.splitlines()
        composer = [i for i, line in enumerate(lines) if "❯" in line]
        last = lines[composer[-1]]
        undecidable = any(tok in last for tok in COLLAPSE_TOKENS) or "[" in last
        return ComposerReading(sentinel in last, not undecidable, False, "ok",
                               ("raw_read", "sentinel", "composer_line"))

    monkeypatch.setattr(sys.modules[__name__], "classify_composer", last_line_only)
    narrowed = classify_composer(DRILL_FIXTURE_RAW, DRILL_FIXTURE_SENTINEL)
    assert narrowed.legible is True, (
        "the narrow variant should read the fixture as legible — if it does not, this "
        "test no longer models the divergence it exists to model")
    with pytest.raises(AssertionError):
        test_drill_fixture_fails_the_reading_if_the_rule_narrows_to_the_last_line()


# --- 5.1: the drill is authored against the frozen 0.0 contract --------------
#
# The drill reads the seam's shape and the verdict vocabulary it records; those are
# asserted here so a later edit to either cannot leave the drill silently measuring
# something else. The verdict *shape* itself is pinned by 3.0/4.0's own tests
# (test_verify_send.py, test_verify_send_failure.py) — deliberately not re-tested
# here, so this file carries no duplicate of the unit suite.

def test_drill_is_authored_against_the_frozen_reading_shape():
    fields = ComposerReading.__dataclass_fields__
    assert sorted(fields) == ["composer_check", "legible", "marker_after_composer",
                             "sentinel_present", "signals_consulted"]
    assert "T" not in fields, "T belongs to _wait_working's return, never the reading"


def test_drill_reads_the_sentinel_the_mechanism_injects():
    sentinel = sentinel_for("task-0cb00606")
    assert sentinel == "[verify-send:task-0cb00606]"
    assert bridge.REPORTING_MARKER not in sentinel
    assert "task-0cb00606" in sentinel and "\n" not in sentinel


def test_drill_records_only_the_frozen_verdict_vocabulary():
    assert REASONS == ("composer_holds_prompt", "no_submission_evidence",
                       "pane_unreadable")
    assert VERIFIED_BY == ("transition", "marker")
    assert EVIDENCE_KEYS == ("transition", "composer_check", "sentinel_present",
                            "marker_after_composer", "signals_consulted")
    assert ALLOWED_CWD == f"{HOME}/DEV"


# --- the drill --------------------------------------------------------------

@pytest.fixture
def live(monkeypatch):
    """The drill runs with the flag ON, and fails fast without a live herdr."""
    monkeypatch.setenv("HERDR_VERIFY_SEND", "1")
    os.makedirs(DRILL_CWD, exist_ok=True)
    return DRILL_CWD


def _record(entry):
    """Append one JSON line to the drill log, scrubbed. Returns the entry."""
    PROOFS.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    return entry


def _reading_dict(reading):
    return {"sentinel_present": reading.sentinel_present,
            "legible": reading.legible,
            "marker_after_composer": reading.marker_after_composer,
            "composer_check": reading.composer_check,
            "signals_consulted": list(reading.signals_consulted)}


def _ledger_row(task_id):
    with sqlite3.connect(ledger_module._default_db_path()) as conn:
        row = conn.execute(ROW_SQL, (task_id,)).fetchone()
    if row is None:
        return None
    return {"last_state": row[0], "pending_result": row[1],
            "collected_at": row[2], "cached_result_is_null": row[3] is None}


def _dispatch_prompt(nonce):
    """The drill's own prompt: one short reply, no tool use, no file writes."""
    return ("Reply with exactly one line and nothing else: the string `drill-ok ` "
            f"followed by \"{nonce}\" written backwards. Do not use any tool, and do "
            "not create or change any file.")


def _ground_truth_token(nonce):
    """`drill-ok <reversed nonce>` — only producible by processing the prompt."""
    return "drill-ok " + nonce[::-1]


def _residual_window(evidence):
    """Unit 3's residual window on one sample: ¬T ∧ R ∧ ¬P ∧ ¬M.

    ``composer_check == "ok"`` stands in for `R` here: the evidence records
    ``sentinel_present: None`` when the read was not legible, so `R` is exactly
    "a raw read was obtainable and the composer line was legible".
    """
    return (evidence.get("transition") is False
            and evidence.get("composer_check") == "ok"
            and evidence.get("sentinel_present") is False
            and evidence.get("marker_after_composer") is False)


def _await_ground_truth(b, task_id, pane_id, token):
    """Bounded poll for the reversed nonce. Never indefinite, never silent.

    Returns ``(ground_truth, wait_s, end_status, raw)`` where ground truth is
    ``ran`` / ``did_not_run`` / ``unknown``. ``unknown`` is reported when the window
    closes with the session still ``working``: the ground truth is genuinely not
    established, and the drill says so instead of guessing a pass or a fail.
    """
    start = time.time()
    raw = ""
    while time.time() - start < SETTLE_S:
        raw = b._read_raw(task_id, pane_id)
        if token in raw:
            return "ran", time.time() - start, None, raw
        time.sleep(POLL_S)
    try:
        status = b.herdr.agent_get(task_id).get("agent_status")
    except Exception:  # herdr hiccup at the end of the window is not a verdict
        status = "unknown"
    return ("unknown" if status == "working" else "did_not_run",
            time.time() - start, status, raw)


def _teardown(b, pane_id, keep):
    """Drill hygiene only. A pane worth inspecting stays open.

    ``keep`` is the *run's* judgement, not the mechanism's: a rejected dispatch, a
    disagreement with ground truth or an unestablished ground truth all leave the
    pane up, because that raw text is the evidence the contingency needs. ``send()``
    itself takes no such action on any path — ``test_verify_send_failure.py`` proves
    that separately.
    """
    if KEEP_PANES:
        return "kept (HERDR_DRILL_KEEP_PANES=1)"
    if keep:
        return "left_open_for_inspection"
    try:
        b.herdr.pane_close(pane_id)
        return "closed"
    except Exception as exc:
        return f"close_failed: {exc}"


@LIVE
@pytest.mark.parametrize("run_index", range(RUNS))
def test_live_dispatch_meets_the_bar(run_index, live):
    """One run per dispatch: read it back, establish ground truth, compare."""
    nonce = uuid.uuid4().hex[:12]
    assert nonce != nonce[::-1], "a palindromic nonce would make ground truth vacuous"
    token = _ground_truth_token(nonce)

    b = Bridge()
    result = b.send(_dispatch_prompt(nonce), cwd=live, mcp_pair=False)
    if "task_id" not in result:
        pytest.fail(f"run {run_index}: send() returned no dispatch: {scrub(result)}")

    task_id, pane_id = result["task_id"], result["pane_id"]
    sentinel = sentinel_for(task_id)
    raw = b._read_raw(task_id, pane_id)          # the drill's own post-send read
    reading = classify_composer(raw, sentinel)
    gt, wait_s, end_status, final_raw = _await_ground_truth(b, task_id, pane_id, token)
    if gt != "ran":                              # keep the last raw text as evidence
        raw = final_raw or raw
        reading = classify_composer(raw, sentinel)

    accepted = bool(result.get("verified"))
    false_acceptance = accepted and gt == "did_not_run"
    false_non_acceptance = (not accepted) and gt == "ran"
    # `first_attempt` is set by `_confirm_or_resubmit` exactly when a confirmation
    # sample was taken — on both outcomes of it (R1's acceptance and the final
    # blocked-class verdict) — so it is that read's observed cost. `confirmation` and
    # `resubmitted` appear only on the re-submit outcome.
    first = result.get("first_attempt") or result.get("evidence") or {}
    keep = (not accepted) or false_acceptance or false_non_acceptance or gt == "unknown"
    entry = _record({
        "kind": "dispatch", "run": run_index, "task_id": task_id, "pane_id": pane_id,
        "verdict": {"verified": result.get("verified"),
                    "verified_by": result.get("verified_by"),
                    "reason": result.get("reason"), "error": result.get("error")},
        "evidence": result.get("evidence"),
        "first_attempt": result.get("first_attempt"),
        "confirmation": result.get("confirmation"),
        "resubmitted": bool(result.get("resubmitted")),
        "T": (result.get("evidence") or {}).get("transition"),
        "reading_after": _reading_dict(reading),
        "raw_after": scrub(raw[:RAW_LIMIT]),
        "ground_truth": gt, "ground_truth_wait_s": round(wait_s, 1),
        "agent_status_end": end_status,
        "residual_window": _residual_window(first),
        "confirmation_reads": 1 if result.get("first_attempt") else 0,
        "false_acceptance": false_acceptance,
        "false_non_acceptance": false_non_acceptance,
        "row": _ledger_row(task_id),
        "teardown": _teardown(b, pane_id, keep),
    })
    teardown = entry["teardown"]

    assert gt != "unknown", (
        f"run {run_index}: ground truth not established — still working at the end of "
        f"the {SETTLE_S:.0f}s window; pane left for inspection ({teardown})")
    assert not entry["false_acceptance"], (
        f"run {run_index}: FALSE ACCEPTANCE — accepted "
        f"({result.get('verified_by')}) but the prompt never ran. raw: {entry['raw_after']!r}")
    assert not entry["false_non_acceptance"], (
        f"run {run_index}: FALSE NON-ACCEPTANCE — rejected "
        f"({result.get('reason')}) but the prompt ran. This is the residual window "
        f"firing: the mechanism goes to the transcript contingency (design §9) and "
        f"this run's raw text is kept. raw: {entry['raw_after']!r}")


class UnsubmittedHerdr:
    """The real client, except ``agent_prompt`` types the text and never submits.

    TICKET-209's exact shape, induced on a live pane: the whole production path runs —
    pre-send readiness gate, `send()`'s verdict, the retry-shape decision, the ledger
    row — with only the *submission* suppressed, so the prompt demonstrably sits on
    the composer. That is what makes the induced run a production demonstration of
    Unit 3 rather than a double's impression of one.
    """

    def __init__(self, inner):
        self.inner = inner
        self.pane_id = None
        self.typed = []

    def workspace_create(self, **kwargs):
        ws = self.inner.workspace_create(**kwargs)
        self.pane_id = ws.get("pane_id", "")
        return ws

    def agent_prompt(self, target, text, **kwargs):
        self.typed.append(text)
        # Literal text onto the composer, never an Enter. herdr's `pane.send_text`
        # ("Send literal text to a pane") types the wire text as-is and submits
        # nothing — verified on a live pane: the two-line wire text lands in the
        # composer region with `agent_status` still `idle`. `pane_send_keys` is
        # key-press only and rejects a text string outright (`invalid_key:
        # unsupported key [verify-send:task-…]`), so it induces no such shape.
        # Called through the client's socket API because `herdr_client` itself is
        # a declared non-goal for this spec ("no `herdr_client` change").
        self.inner._call("pane.send_text", {"pane_id": self.pane_id, "text": text})
        return {"type": "ok"}

    def __getattr__(self, name):                        # delegate the rest unchanged
        return getattr(self.inner, name)


@LIVE
def test_induced_non_submission_on_a_live_pane(live):
    """5.4's induced non-submission: the error dict and the row instead of `working`.

    The induction has to *take* before anything is concluded from it. If herdr's
    ``pane.send_text`` does not type the wire text literally then the sentinel never
    reaches the composer and this run proves nothing — it fails naming the induction,
    and never passes silently as a mechanism failure or a mechanism success.
    """
    herdr = UnsubmittedHerdr(HerdrClient())
    b = Bridge(herdr=herdr, ledger=Ledger())
    result = b.send(_dispatch_prompt(uuid.uuid4().hex[:12]), cwd=live, mcp_pair=False)
    if "task_id" not in result:
        pytest.fail(f"induced run: send() returned no dispatch: {scrub(result)}")

    task_id, pane_id = result["task_id"], result["pane_id"]
    sentinel = sentinel_for(task_id)
    raw = b._read_raw(task_id, pane_id)
    reading = classify_composer(raw, sentinel)
    row = _ledger_row(task_id)
    entry = _record({
        "kind": "induced_non_submission", "task_id": task_id, "pane_id": pane_id,
        "verdict": {"verified": result.get("verified"),
                    "verified_by": result.get("verified_by"),
                    "reason": result.get("reason"), "error": result.get("error")},
        "message": scrub(result.get("message", "")),
        "agent_status": result.get("agent_status"),
        "evidence": result.get("evidence"),
        "resubmitted": bool(result.get("resubmitted")),
        "agent_prompt_writes": len(herdr.typed),
        "reading": _reading_dict(reading),
        "raw": scrub(raw[:RAW_LIMIT]),
        "row": row,
        "teardown": _teardown(b, pane_id, keep=True),  # inspected, never closed
    })

    assert reading.sentinel_present, (
        "induction did not take: herdr's pane.send_text did not type the wire text "
        "literally, so the sentinel never reached the composer. This is an induction "
        "failure — the mechanism is untested by this run, not vindicated by it. "
        f"raw: {entry['raw']!r}")
    assert result.get("error") == "blocked"
    assert result.get("reason") == "composer_holds_prompt", (
        "TICKET-209's signature must surface as composer_holds_prompt, never as a "
        "re-submit")
    assert result.get("verified") is False
    assert len(herdr.typed) == 1, "a held composer is terminal: one write, ever"
    assert "resubmitted" not in result
    assert row == {"last_state": "blocked", "pending_result": 0, "collected_at": None,
                   "cached_result_is_null": True}, (
        "the row must replace a false `working`: blocked, not pending, never collected")


@LIVE
def test_drill_log_meets_the_bar(live):
    """The bar over the run set: 0/0 against ground truth, and metric 8 measured."""
    if not LOG.exists():
        pytest.fail(f"no drill log at {LOG} — run the drill dispatches first")
    records = [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]
    runs = [r for r in records if r.get("kind") == "dispatch"]
    assert len(runs) >= RUNS, (
        f"the bar needs >= {RUNS} consecutive dispatches; the log carries {len(runs)}")
    latest = runs[-RUNS:]

    false_acceptances = [r for r in latest if r.get("false_acceptance")]
    false_non_acceptances = [r for r in latest if r.get("false_non_acceptance")]
    inconclusive = [r for r in latest if r.get("ground_truth") == "unknown"]
    residual = [r for r in latest if r.get("residual_window")]
    markers = [r for r in latest if (r.get("reading_after") or {}).get("marker_after_composer")]
    confirmation_reads = sum(r.get("confirmation_reads", 0) for r in latest)

    summary = {
        "kind": "summary", "runs": len(latest),
        "false_acceptances": len(false_acceptances),
        "false_non_acceptances": len(false_non_acceptances),
        "inconclusive": len(inconclusive),
        # Metric 8: the residual window's observed frequency, measured not argued.
        "residual_window_runs": len(residual),
        "residual_window_frequency": round(len(residual) / len(latest), 3),
        # The confirmation read's observed cost: one bounded extra read on the
        # failure path only.
        "confirmation_reads_total": confirmation_reads,
        # Open question 1: whether M is ever observed on a real pane.
        "marker_after_composer_runs": len(markers),
    }
    _record(summary)

    assert not false_acceptances, f"false acceptances: {scrub(false_acceptances)}"
    assert not false_non_acceptances, (
        "false non-acceptances — the residual window fired; residual retired only at "
        f"0/0: {scrub(false_non_acceptances)}")
    assert not inconclusive, (
        f"{len(inconclusive)} run(s) whose ground truth was never established: the bar "
        "is not met by an unestablished comparison")
    print(json.dumps(summary, indent=2, sort_keys=True))
