"""Tests for verify-before-send (SDD 01) — `Bridge.send()` must observe acceptance.

Unit 1 (parent 2.0): the TICKET-209 stall of 2026-10-09, reproduced as a
regression test *before* the fix exists. The pane echoed the prompt into the
composer and never submitted it; `agent_status` stayed `idle`; the ledger still
read `last_state: working`. The test encodes what the spec requires and is red
against the pre-fix `send()`, which returns no verdict and records `working`
unconditionally.

No live herdr: the double is `MagicMock(spec=HerdrClient)`, the house style of
`test_bridge.py`.
"""
import sqlite3
from unittest.mock import MagicMock

import pytest

import bridge
from bridge import Bridge
from herdr_client import HerdrClient
from ledger import Ledger

# Under ALLOWED_CWD so send() reaches the double instead of the cwd guard.
# Never created on disk: workspace_create is mocked and preseed_trust stubbed.
CWD = bridge.ALLOWED_CWD + "/_verify_send_test"

PROMPT = "Implement parent 1.0 of the verify-before-send task list."

LEDGER_SQL = "SELECT task_id, last_state, pending_result FROM tasks WHERE task_id = ?"


def composer_echo_herdr():
    """A herdr whose pane types the prompt into the composer and never submits.

    Whatever `agent_prompt` is given (sentinel and all, once 3.0 injects one)
    sits verbatim on the last `❯` line; no `⏺`/`✻` follows it; `agent_status`
    stays `idle` on every read. That is the TICKET-209 signature.
    """
    m = MagicMock(spec=HerdrClient)
    m.workspace_create.return_value = {"workspace_id": "w1F", "pane_id": "w1F:p1"}
    m.agent_start.return_value = {"type": "agent_started"}
    m.agent_get.side_effect = lambda *a, **k: {
        "agent_status": "idle", "agent_session": {"value": "sess-209"},
        "pane_id": "w1F:p1"}
    typed = []
    m.agent_prompt.side_effect = lambda task_id, text: typed.append(text) or {"type": "ok"}

    def screen(*a, **k):
        if not typed:
            return "╭──────────────╮\n│ Claude Code  │\n╰──────────────╯\n❯ \n"
        first, *rest = typed[-1].splitlines()
        return ("╭──────────────╮\n│ Claude Code  │\n╰──────────────╯\n"
                + "❯ " + first + "\n" + "".join(f"  {line}\n" for line in rest))

    m.pane_read.side_effect = screen
    m.agent_read.side_effect = screen
    return m


@pytest.fixture
def stalled(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_VERIFY_SEND", "1")  # spec metric 3: flag enabled
    monkeypatch.setattr(bridge, "preseed_trust", lambda *a, **k: {"key": "x", "changed": []})
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    monkeypatch.setattr(Bridge, "_check_bare_flag", lambda self: False)
    db = tmp_path / "ledger.db"
    herdr = composer_echo_herdr()
    return Bridge(herdr=herdr, ledger=Ledger(db_path=str(db))), herdr, db


def test_mahq_209_composer_echo_stall_is_not_recorded_working(stalled):
    """TICKET-209: a prompt left in the composer must not be recorded as working."""
    b, herdr, db = stalled
    result = b.send(PROMPT, cwd=CWD)

    # The double really reproduced the stall: prompt typed once, sitting on
    # the composer line, status idle throughout.
    assert result.get("error") != "cwd_not_allowed"
    herdr.agent_prompt.assert_called_once()
    task_id = herdr.agent_prompt.call_args.args[0]
    assert PROMPT in herdr.pane_read(None)

    with sqlite3.connect(db) as conn:
        row = conn.execute(LEDGER_SQL, (task_id,)).fetchone()
    evidence = f"result={result!r} ledger_row={row!r}"

    # What the spec requires (pre-fix send() breaks all three):
    assert row is not None, "a non-accepted dispatch still leaves a durable row"
    assert result.get("reason") == "composer_holds_prompt", (
        "send() returned no verification verdict for a prompt left in the composer; "
        + evidence)
    assert row[1] != "working", (
        "ledger recorded last_state='working' for a dispatch that never left the composer; "
        + evidence)
    assert row[1:] == ("blocked", 0), evidence


# --- Parent 3.0: the acceptance predicate, one test per table row ----------
#
# T or (R and not P and M). T comes from _wait_working (agent_status), P/R/M
# from classify_composer over the raw read-back. Each double fixes the signals
# for its row; the test asserts the verdict, the ledger state and the evidence.

BANNER = "╭──────────────╮\n│ Claude Code  │\n╰──────────────╯\n"


def scripted_herdr(status_after_prompt, screen):
    """herdr double: `idle` until agent_prompt, then `status_after_prompt`.

    `screen(typed_text)` builds the raw pane text after the prompt; it may
    return "" to make both read paths come back empty.
    """
    m = MagicMock(spec=HerdrClient)
    m.workspace_create.return_value = {"workspace_id": "w1V", "pane_id": "w1V:p1"}
    m.agent_start.return_value = {"type": "agent_started"}
    typed = []
    m.agent_get.side_effect = lambda *a, **k: {
        "agent_status": status_after_prompt if typed else "idle",
        "agent_session": {"value": "sess-v"}, "pane_id": "w1V:p1"}
    m.agent_prompt.side_effect = lambda task_id, text: typed.append(text) or {"type": "ok"}
    m.pane_read.side_effect = lambda *a, **k: screen(typed[-1]) if typed else BANNER + "❯ \n"
    m.agent_read.side_effect = m.pane_read.side_effect
    return m


def submitted_screen(text):
    """Composer cleared, the assistant answered: ¬P and M.

    No transcript echo of the submitted prompt: classify_composer (1.0) reads P
    as "sentinel anywhere in the read", so an echo would read as P."""
    return BANNER + "❯ \n⏺ On it.\n✻ Working…\n"


def cleared_no_marker_screen(text):
    """Composer cleared, nothing after it: ¬P and ¬M."""
    return BANNER + "❯ \n"


def held_screen(text):
    """Prompt still sitting on the composer line: P."""
    first, *rest = text.splitlines()
    return BANNER + "❯ " + first + "\n" + "".join(f"  {line}\n" for line in rest)


def collapsed_screen(text):
    """Claude Code paste-collapse on the composer line: undecidable, R false."""
    return BANNER + "❯ [Pasted text #1 +3 lines]\n"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "preseed_trust", lambda *a, **k: {"key": "x", "changed": []})
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    monkeypatch.setattr(Bridge, "_check_bare_flag", lambda self: False)
    return monkeypatch, tmp_path / "ledger.db"


def dispatch(env, status, screen, flag="1", **send_kw):
    monkeypatch, db = env
    if flag is None:
        monkeypatch.delenv("HERDR_VERIFY_SEND", raising=False)
    else:
        monkeypatch.setenv("HERDR_VERIFY_SEND", flag)
    herdr = scripted_herdr(status, screen)
    result = Bridge(herdr=herdr, ledger=Ledger(db_path=str(db))).send(PROMPT, cwd=CWD, **send_kw)
    with sqlite3.connect(db) as conn:
        row = conn.execute(LEDGER_SQL, (result["task_id"],)).fetchone()
    return result, row, herdr


@pytest.mark.parametrize("status", ["working", "done"])
def test_transition_observed_is_accepted(env, status):
    """Row 1: T observed (working, or done inside the window from idle)."""
    result, row, herdr = dispatch(env, status, held_screen)
    assert result["verified"] is True
    assert result["verified_by"] == "transition"
    assert "reason" not in result
    assert row[1:] == ("working", 1)
    ev = result["evidence"]
    assert ev["transition"] is True
    # T is never overridden: the composer disagreeing is recorded, not acted on.
    assert ev["sentinel_present"] is True
    assert ev["signals_consulted"][0] == "transition"
    herdr.agent_prompt.assert_called_once()


def test_transition_stands_when_composer_unreadable(env):
    """Row 1 again: T stands even when the read-back is unavailable."""
    result, row, _ = dispatch(env, "working", lambda t: "")
    assert result["verified_by"] == "transition"
    assert result["evidence"]["composer_check"] == "unavailable"
    assert row[1:] == ("working", 1)


def test_marker_accept_when_transition_unsampled(env):
    """Row 2: ¬T, R, ¬P, M — a fast task finished before the first poll."""
    result, row, _ = dispatch(env, "idle", submitted_screen)
    assert result["verified"] is True
    assert result["verified_by"] == "marker"
    assert row[1:] == ("working", 1)
    ev = result["evidence"]
    assert ev == {"transition": False, "composer_check": "ok", "sentinel_present": False,
                  "marker_after_composer": True,
                  "signals_consulted": ("transition", "raw_read", "sentinel",
                                        "composer_line", "marker")}


def test_composer_holds_prompt(env):
    """Row 3: ¬T, R, P — typed but never submitted (TICKET-209)."""
    result, row, herdr = dispatch(env, "idle", held_screen)
    assert result["verified"] is False
    assert result["reason"] == "composer_holds_prompt"
    assert "verified_by" not in result
    assert row[1:] == ("blocked", 0)
    assert result["evidence"]["sentinel_present"] is True
    assert result["evidence"]["composer_check"] == "ok"
    herdr.agent_prompt.assert_called_once()


def test_no_submission_evidence(env):
    """Row 4: ¬T, R, ¬P, ¬M — sentinel absent alone is never acceptance."""
    result, row, herdr = dispatch(env, "idle", cleared_no_marker_screen)
    assert result["verified"] is False
    assert result["reason"] == "no_submission_evidence"
    assert row[1:] == ("blocked", 0)
    ev = result["evidence"]
    assert (ev["sentinel_present"], ev["marker_after_composer"]) == (False, False)
    assert "marker" in ev["signals_consulted"]
    # Unit 3 (4.0): this row is the residual-window shape — two consecutive
    # all-clear reads (¬T ∧ R ∧ ¬P ∧ ¬M) ⇒ exactly one re-submit, so the
    # bounded write count is 2. Still blocked; only the count moved (was 1 in 3.0).
    assert herdr.agent_prompt.call_count == 2


@pytest.mark.parametrize("screen", [lambda t: "", collapsed_screen],
                         ids=["no_read", "collapse_token"])
def test_pane_unreadable(env, screen):
    """Row 5: ¬T, ¬R — no raw read, or an undecidable composer line."""
    result, row, herdr = dispatch(env, "idle", screen)
    assert result["verified"] is False
    assert result["reason"] == "pane_unreadable"
    assert row[1:] == ("blocked", 0)
    assert result["evidence"]["composer_check"] == "unavailable"
    assert result["evidence"]["sentinel_present"] is None
    herdr.agent_prompt.assert_called_once()


def test_read_back_falls_back_to_agent_read(env):
    """pane_read failing → agent_read's raw text decides, never parse_result."""
    monkeypatch, db = env
    monkeypatch.setenv("HERDR_VERIFY_SEND", "1")
    herdr = scripted_herdr("idle", submitted_screen)
    herdr.pane_read.side_effect = lambda *a, **k: ""
    result = Bridge(herdr=herdr, ledger=Ledger(db_path=str(db))).send(PROMPT, cwd=CWD)
    assert result["verified_by"] == "marker"
    herdr.agent_read.assert_called_with(result["task_id"], source="recent_unwrapped",
                                        format="text")


def test_sentinel_is_line_one_after_reporting_contract(env):
    """Sentinel on line 1 of the wire text, after the reporting block is appended,
    carrying the full task_id; the stored prompt is the wire text."""
    result, _, herdr = dispatch(env, "working", held_screen, agent_id="alfred")
    try:
        task_id = result["task_id"]
        wire = herdr.agent_prompt.call_args.args[1]
        lines = wire.splitlines()
        assert lines[0] == f"[verify-send:{task_id}]"
        assert lines[1] == PROMPT
        assert bridge.REPORTING_MARKER in wire          # reporting block not dropped
        assert wire.count(bridge.REPORTING_MARKER) == 1
        with sqlite3.connect(env[1]) as conn:
            stored = conn.execute("SELECT prompt FROM tasks WHERE task_id=?",
                                  (task_id,)).fetchone()[0]
        assert stored == wire
    finally:
        with sqlite3.connect(env[1]) as conn:
            path = conn.execute("SELECT mcp_config_path FROM tasks").fetchone()[0]
        if path:
            import os
            os.unlink(path)


# --- Default-off parity (spec metric 7) ------------------------------------

@pytest.mark.parametrize("flag", [None, "0", "false"], ids=["unset", "zero", "false"])
def test_flag_off_is_todays_behaviour(env, flag):
    """Flag unset or any non-"1" value: no sentinel, no read-back, working
    recorded unconditionally, today's return shape — even for the stall."""
    result, row, herdr = dispatch(env, "idle", held_screen, flag=flag)
    assert set(result) == {"task_id", "pane_id"}
    assert row[1:] == ("working", 1)
    herdr.agent_prompt.assert_called_once_with(result["task_id"], PROMPT)
    herdr.pane_read.assert_not_called()
    herdr.agent_read.assert_not_called()
    # Pre-send path unchanged: same launch calls as today.
    herdr.workspace_create.assert_called_once_with(cwd=CWD)
    herdr.agent_start.assert_called_once()
