"""Tests for verify-before-send (SDD 01) Unit 3 — the failure path.

Parent 4.0: exactly one retry whose shape is fixed by the recorded `reason`,
the confirmation-sample guard in front of the single re-submit (carried
residual R1 pinned), the `blocked`-class row + error dict, and no destructive
call on the way. The `agent_prompt` call count is the thing under test: it is
what proves the retry cannot become an unbounded write path.

No live herdr: the double is `MagicMock(spec=HerdrClient)`, the house style of
`test_verify_send.py`, with faults injected per post-prompt read and status.
"""
import sqlite3
from unittest.mock import MagicMock

import pytest

import bridge
from bridge import Bridge
from herdr_client import HerdrClient
from ledger import Ledger

CWD = bridge.ALLOWED_CWD + "/_verify_send_failure_test"
PROMPT = "Implement parent 4.0 of the verify-before-send task list."
BANNER = "╭──────────────╮\n│ Claude Code  │\n╰──────────────╯\n"
WINDOW = 20  # _wait_working's samples per wait

ROW_SQL = ("SELECT last_state, pending_result, collected_at, cached_result "
           "FROM tasks WHERE task_id = ?")


def cleared(text):
    """All-clear: composer empty, nothing after it (¬P, ¬M, R)."""
    return BANNER + "❯ \n"


def held(text):
    """The prompt still sits on the composer line (P)."""
    first, *rest = text.splitlines()
    return BANNER + "❯ " + first + "\n" + "".join(f"  {line}\n" for line in rest)


def collapsed(text):
    """Paste-collapse placeholder: undecidable, R false."""
    return BANNER + "❯ [Pasted text #1 +3 lines]\n"


def unread(text):
    return ""


def answered(text):
    """Composer cleared, response markers below it (¬P, M) — spec row-2 fixture."""
    return BANNER + "❯ \n⏺ On it.\n"


def faulted_herdr(screens, status_at=lambda n: "idle"):
    """herdr double with faults injected after the first `agent_prompt`.

    `screens[i]` renders the i-th post-prompt raw read (the last one repeats);
    `status_at(n)` is the status of the n-th post-prompt `agent_get` (1-based).
    Before any prompt the pane is an idle, empty composer, so launch is today's.
    """
    m = MagicMock(spec=HerdrClient)
    m.workspace_create.return_value = {"workspace_id": "w1R", "pane_id": "w1R:p1"}
    m.agent_start.return_value = {"type": "agent_started"}
    typed, gets, reads = [], [0], [0]

    def get(*a, **k):
        if typed:
            gets[0] += 1
        status = status_at(gets[0]) if typed else "idle"
        return {"agent_status": status, "agent_session": {"value": "sess-r"},
                "pane_id": "w1R:p1"}

    def read(*a, **k):
        if not typed:
            return BANNER + "❯ \n"
        reads[0] += 1
        return screens[min(reads[0], len(screens)) - 1](typed[-1])

    m.agent_get.side_effect = get
    m.agent_prompt.side_effect = lambda task_id, text: typed.append(text) or {"type": "ok"}
    m.pane_read.side_effect = read
    m.agent_read.side_effect = read
    return m


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_VERIFY_SEND", "1")
    monkeypatch.setattr(bridge, "preseed_trust", lambda *a, **k: {"key": "x", "changed": []})
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    monkeypatch.setattr(Bridge, "_check_bare_flag", lambda self: False)
    return tmp_path / "ledger.db"  # own temp ledger — never the live one


def dispatch(db, screens, status_at=lambda n: "idle"):
    herdr = faulted_herdr(screens, status_at)
    b = Bridge(herdr=herdr, ledger=Ledger(db_path=str(db)))
    result = b.send(PROMPT, cwd=CWD)
    with sqlite3.connect(db) as conn:
        row = conn.execute(ROW_SQL, (result["task_id"],)).fetchone()
    return result, row, herdr, b


def assert_blocked_class(result, row, reason):
    assert result["verified"] is False
    assert result["error"] == "blocked"
    assert result["reason"] == reason
    assert reason in result["message"]
    assert result["agent_status"] == "idle"
    assert "verified_by" not in result
    assert set(result["evidence"]) == {"transition", "composer_check", "sentinel_present",
                                       "marker_after_composer", "signals_consulted"}
    assert result["evidence"]["signals_consulted"][0] == "transition"
    assert len(result["pane_text"]) <= 200
    # Row: blocked, not pending, never collected (no fabricated result).
    assert row == ("blocked", 0, None, None)


# --- The three retry shapes -------------------------------------------------

def test_composer_holds_prompt_is_terminal(env):
    """(a) P true ⇒ no re-submit, no confirmation sample: one write, ever."""
    result, row, herdr, _ = dispatch(env, [held])
    assert_blocked_class(result, row, "composer_holds_prompt")
    assert herdr.agent_prompt.call_count == 1
    assert result["evidence"]["sentinel_present"] is True
    assert result["pane_text"].startswith(BANNER + "❯ [verify-send:" + result["task_id"])
    assert "first_attempt" not in result and "resubmitted" not in result


@pytest.mark.parametrize("screen", [unread, collapsed], ids=["no_read", "collapse_token"])
def test_pane_unreadable_is_terminal(env, screen):
    """(c) R false ⇒ no re-submit, composer_check unavailable, one write."""
    result, row, herdr, _ = dispatch(env, [screen])
    assert_blocked_class(result, row, "pane_unreadable")
    assert herdr.agent_prompt.call_count == 1
    assert result["evidence"]["composer_check"] == "unavailable"
    assert result["evidence"]["sentinel_present"] is None
    assert "first_attempt" not in result and "resubmitted" not in result


def test_no_submission_evidence_resubmits_once_and_accepts(env):
    """(b) all-clear twice ⇒ one re-submit; its verdict is final; first
    attempt's evidence carried forward unchanged."""
    # Post-prompt gets: 1..20 first wait, 21 confirmation, 22.. second wait.
    result, row, herdr, _ = dispatch(env, [cleared],
                                     status_at=lambda n: "working" if n > WINDOW + 1 else "idle")
    assert herdr.agent_prompt.call_count == 2
    first_wire, second_wire = (c.args for c in herdr.agent_prompt.call_args_list)
    assert first_wire == second_wire  # same task, same sentinel, same text
    assert result["verified"] is True and result["verified_by"] == "transition"
    assert result["resubmitted"] is True
    assert result["first_attempt"] == {
        "transition": False, "composer_check": "ok", "sentinel_present": False,
        "marker_after_composer": False,
        "signals_consulted": ("transition", "raw_read", "sentinel", "composer_line", "marker")}
    assert result["confirmation"] == result["first_attempt"]
    assert "error" not in result
    assert row == ("working", 1, None, None)


def test_residual_window_resubmits_exactly_once_then_blocks(env):
    """Residual window shape: every read all-clear, no transition ever ⇒
    exactly one re-submit, then blocked — never a third write."""
    result, row, herdr, _ = dispatch(env, [cleared])
    assert herdr.agent_prompt.call_count == 2
    assert_blocked_class(result, row, "no_submission_evidence")
    assert result["resubmitted"] is True
    assert result["first_attempt"]["sentinel_present"] is False
    assert result["first_attempt"]["marker_after_composer"] is False
    assert result["confirmation"] == result["first_attempt"]


# --- The confirmation-sample guard and R1 -----------------------------------

@pytest.mark.parametrize("second, reason", [(held, "composer_holds_prompt"),
                                            (collapsed, "pane_unreadable")],
                         ids=["P_flips_true", "R_flips_false"])
def test_confirmation_not_all_clear_blocks_without_resubmit(env, second, reason):
    """All-clear then not: the stricter verdict stands, no re-submit."""
    result, row, herdr, _ = dispatch(env, [cleared, second])
    assert herdr.agent_prompt.call_count == 1
    assert_blocked_class(result, row, reason)
    assert result["first_attempt"]["sentinel_present"] is False  # first read was all-clear
    assert "resubmitted" not in result


def test_r1_transition_on_confirmation_is_accepted(env):
    """R1: T seen on the confirmation sample ⇒ accepted, never a failure,
    and no re-submit (the fast accepted submission M cannot catch live)."""
    result, row, herdr, _ = dispatch(env, [cleared],
                                     status_at=lambda n: "working" if n > WINDOW else "idle")
    assert herdr.agent_prompt.call_count == 1
    assert result["verified"] is True and result["verified_by"] == "transition"
    assert result["evidence"]["transition"] is True
    assert result["first_attempt"]["transition"] is False
    assert "error" not in result and "reason" not in result
    assert row == ("working", 1, None, None)


def test_marker_on_confirmation_is_accepted(env):
    """Same predicate on the confirmation sample: ¬T, R, ¬P, M ⇒ marker accept."""
    result, row, herdr, _ = dispatch(env, [cleared, answered])
    assert herdr.agent_prompt.call_count == 1
    assert result["verified_by"] == "marker"
    assert row == ("working", 1, None, None)


# --- No destructive action, no ghost ----------------------------------------

@pytest.mark.parametrize("screens", [[held], [unread], [cleared], [cleared, held]],
                         ids=["holds", "unreadable", "residual", "guard"])
def test_failure_path_issues_no_kill_or_close(env, screens):
    result, _, herdr, _ = dispatch(env, screens)
    assert result["error"] == "blocked"
    herdr.pane_close.assert_not_called()
    herdr.pane_send_keys.assert_not_called()
    herdr.workspace_create.assert_called_once()
    herdr.agent_start.assert_called_once()  # no restart


@pytest.mark.parametrize("screens", [[held], [cleared]], ids=["holds", "residual"])
def test_poll_on_rejected_row_is_blocked_and_never_collects(env, screens):
    result, _, herdr, b = dispatch(env, screens)
    herdr.agent_read.reset_mock()
    polled = b.poll(result["task_id"])
    assert polled["state"] == "blocked"
    assert "result" not in polled
    herdr.agent_read.assert_not_called()
    with sqlite3.connect(env) as conn:
        row = conn.execute(ROW_SQL, (result["task_id"],)).fetchone()
    assert row == ("blocked", 0, None, None)
