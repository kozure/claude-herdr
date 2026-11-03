"""Tests for bridge.py — the herdr-backed bridge.

Unit tests mock the HerdrClient and Ledger to test state mapping, 
collection logic, trust-dialog handling, herdr-down propagation, etc.
Integration tests (gated) run against a live herdr server.
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

import pytest

from bridge import Bridge
from herdr_client import (
    HerdrClient, HerdrServerDown, HerdrTimeout, AgentNotReady, HerdrAPIError
)
from result_parser import parse_result

HOME = os.path.expanduser("~")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_herdr():
    """A mock HerdrClient."""
    m = MagicMock(spec=HerdrClient)
    m.ping.return_value = {"type": "pong", "version": "0.8.2"}
    m.workspace_create.return_value = {
        "workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"
    }
    m.agent_start.return_value = {"type": "agent_started"}
    m.agent_get.return_value = {"agent_status": "idle", "agent_session": {"value": "sess-1"}, "pane_id": "w1:p1"}
    m.agent_list.return_value = []
    m.agent_read.return_value = "❯ prompt\n⏺ HERDR_OK\n"
    m.agent_prompt.return_value = {"type": "ok"}
    m.pane_send_keys.return_value = {"type": "ok"}
    m.pane_read.return_value = ""
    m.pane_close.return_value = {"type": "ok"}
    return m


@pytest.fixture
def in_memory_ledger():
    """A real in-memory ledger."""
    from ledger import Ledger
    db = Ledger(":memory:")
    db.init_schema()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def no_real_config_writes():
    """Never let a test touch the developer's real ~/.claude.json."""
    with patch("bridge.preseed_trust",
               return_value={"key": f"{HOME}/DEV", "changed": []}) as m:
        yield m


@pytest.fixture
def bridge(mock_herdr, in_memory_ledger):
    """A Bridge with mock herdr client and real in-memory ledger."""
    b = Bridge(herdr=mock_herdr, ledger=in_memory_ledger, default_cwd=f"{HOME}/DEV")
    return b


# ---------------------------------------------------------------------------
# State mapping tests
# ---------------------------------------------------------------------------

class TestStateMapping:
    """Test all rows in the state mapping table."""

    def test_working_state(self, bridge, mock_herdr, in_memory_ledger):
        """working → running, update last_state=working."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        mock_herdr.agent_get.return_value = {"agent_status": "working"}
        result = bridge.poll("t1")
        assert result["state"] == "running"
        row = in_memory_ledger.get_task("t1")
        assert row["last_state"] == "working"

    def test_blocked_state(self, bridge, mock_herdr, in_memory_ledger):
        """blocked → blocked, surface to the operator."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        mock_herdr.agent_get.return_value = {"agent_status": "blocked"}
        result = bridge.poll("t1")
        assert result["state"] == "blocked"
        row = in_memory_ledger.get_task("t1")
        assert row["last_state"] == "blocked"

    def test_idle_after_working_collects(self, bridge, mock_herdr, in_memory_ledger):
        """idle + last_state=working + collected_at=null → done, collect."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        in_memory_ledger.update_last_state("t1", "working")
        mock_herdr.agent_get.return_value = {"agent_status": "idle"}
        mock_herdr.agent_read.return_value = "❯ prompt\n⏺ result text\n"
        result = bridge.poll("t1")
        assert result["state"] == "done"
        assert "result text" in result["result"]["text"]
        row = in_memory_ledger.get_task("t1")
        assert row["collected_at"] is not None
        assert row["pending_result"] == 0

    def test_idle_after_working_cached(self, bridge, mock_herdr, in_memory_ledger):
        """idle + last_state=working + collected_at=set → done (cached), no agent_read."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        in_memory_ledger.update_last_state("t1", "working")
        in_memory_ledger.cas_collect("t1", "2026-01-01T00:00:00Z", "cached result")
        mock_herdr.agent_get.return_value = {"agent_status": "idle"}
        result = bridge.poll("t1")
        assert result["state"] == "done"
        assert result["result"] == "cached result"
        mock_herdr.agent_read.assert_not_called()

    def test_idle_pending_result_collects(self, bridge, mock_herdr, in_memory_ledger):
        """idle + last_state=idle + pending_result=true + collected_at=null → done, collect (missed working window)."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        # last_state is still "idle" (default) — simulating poll missed the working window
        mock_herdr.agent_get.return_value = {"agent_status": "idle"}
        mock_herdr.agent_read.return_value = "❯ prompt\n⏺ result text\n"
        result = bridge.poll("t1")
        assert result["state"] == "done"
        assert "result text" in result["result"]["text"]

    def test_idle_no_pending_no_working(self, bridge, mock_herdr, in_memory_ledger):
        """idle + last_state=idle + pending_result=false + collected_at=null → idle (waiting)."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=False)
        mock_herdr.agent_get.return_value = {"agent_status": "idle"}
        result = bridge.poll("t1")
        assert result["state"] == "idle"
        mock_herdr.agent_read.assert_not_called()

    def test_idle_after_blocked(self, bridge, mock_herdr, in_memory_ledger):
        """idle + last_state=blocked → blocked (still blocked)."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        in_memory_ledger.update_last_state("t1", "blocked")
        mock_herdr.agent_get.return_value = {"agent_status": "idle"}
        result = bridge.poll("t1")
        assert result["state"] == "blocked"

    def test_idle_after_killed(self, bridge, mock_herdr, in_memory_ledger):
        """idle + last_state=killed → killed."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30)
        in_memory_ledger.kill_task("t1")
        mock_herdr.agent_get.return_value = {"agent_status": "idle"}
        result = bridge.poll("t1")
        assert result["state"] == "killed"

    def test_idle_unknown_post_restart_with_pending(self, bridge, mock_herdr, in_memory_ledger):
        """idle + last_state=unknown + pending_result=true → done (collect)."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        in_memory_ledger.update_last_state("t1", "unknown")
        mock_herdr.agent_get.return_value = {"agent_status": "idle"}
        mock_herdr.agent_read.return_value = "❯ prompt\n⏺ result\n"
        result = bridge.poll("t1")
        assert result["state"] == "done"

    def test_idle_unknown_post_restart_no_pending(self, bridge, mock_herdr, in_memory_ledger):
        """idle + last_state=unknown + pending_result=false → idle (resumed, waiting)."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=False)
        in_memory_ledger.update_last_state("t1", "unknown")
        mock_herdr.agent_get.return_value = {"agent_status": "idle"}
        result = bridge.poll("t1")
        assert result["state"] == "idle"

    def test_working_after_killed(self, bridge, mock_herdr, in_memory_ledger):
        """working + last_state=killed → running (kill in transit)."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30)
        in_memory_ledger.kill_task("t1")
        mock_herdr.agent_get.return_value = {"agent_status": "working"}
        result = bridge.poll("t1")
        assert result["state"] == "running"

    def test_done_status_collects(self, bridge, mock_herdr, in_memory_ledger):
        """herdr 'done' status + last_state=working → done, collect (same as idle)."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        in_memory_ledger.update_last_state("t1", "working")
        mock_herdr.agent_get.return_value = {"agent_status": "done"}
        mock_herdr.agent_read.return_value = "❯ prompt\n⏺ result text\n"
        result = bridge.poll("t1")
        assert result["state"] == "done"
        assert "result text" in result["result"]["text"]

    def test_done_status_pending_result(self, bridge, mock_herdr, in_memory_ledger):
        """herdr 'done' + pending_result=true + last_state=idle → done (collect)."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        mock_herdr.agent_get.return_value = {"agent_status": "done"}
        mock_herdr.agent_read.return_value = "❯ prompt\n⏺ result\n"
        result = bridge.poll("t1")
        assert result["state"] == "done"


class TestUnknownStateRetry:
    """Test the unknown-state retry policy (every 2s, max 5 attempts)."""

    def test_unknown_then_idle(self, bridge, mock_herdr, in_memory_ledger):
        """unknown 3 times then idle → eventually collects."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        # Return unknown 3 times, then idle
        mock_herdr.agent_get.side_effect = [
            {"agent_status": "unknown"},
            {"agent_status": "unknown"},
            {"agent_status": "unknown"},
            {"agent_status": "idle"},
        ]
        mock_herdr.agent_read.return_value = "❯ prompt\n⏺ result\n"
        with patch("bridge.time.sleep") as mock_sleep:
            result = bridge.poll("t1")
        assert result["state"] == "done"
        assert mock_herdr.agent_get.call_count == 4

    def test_unknown_exceeds_retries(self, bridge, mock_herdr, in_memory_ledger):
        """unknown for 6+ attempts → returns unknown state to the operator."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        mock_herdr.agent_get.return_value = {"agent_status": "unknown"}
        with patch("bridge.time.sleep"):
            result = bridge.poll("t1")
        assert result["state"] == "unknown"
        assert "message" in result


# ---------------------------------------------------------------------------
# Collection guard tests
# ---------------------------------------------------------------------------

class TestCollectionGuard:
    """Test poll-collects-once-then-caches and double-collect guard."""

    def test_collects_once_then_caches(self, bridge, mock_herdr, in_memory_ledger):
        """First poll collects, second poll returns cached."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        in_memory_ledger.update_last_state("t1", "working")
        mock_herdr.agent_get.return_value = {"agent_status": "idle"}
        mock_herdr.agent_read.return_value = "❯ prompt\n⏺ result\n"
        # First poll
        result1 = bridge.poll("t1")
        assert result1["state"] == "done"
        assert mock_herdr.agent_read.call_count == 1
        # Second poll
        result2 = bridge.poll("t1")
        assert result2["state"] == "done"
        assert mock_herdr.agent_read.call_count == 1  # still 1 — no re-read


# ---------------------------------------------------------------------------
# Prompt multi-turn tests
# ---------------------------------------------------------------------------

class TestPromptMultiTurn:
    """Test prompt resets collection and the multi-turn race fix."""

    def test_prompt_sets_working_and_pending(self, bridge, mock_herdr, in_memory_ledger):
        """After prompt: last_state=working, pending_result=true, collected_at=null."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        in_memory_ledger.cas_collect("t1", "time1", "old result")
        with patch("bridge.time.sleep"):
            bridge.prompt("t1", "new prompt")
        row = in_memory_ledger.get_task("t1")
        assert row["last_state"] == "working"
        assert row["pending_result"] == 1
        assert row["collected_at"] is None
        mock_herdr.agent_prompt.assert_called_once_with("t1", "new prompt")

    def test_prompt_then_poll_collects_new_result(self, bridge, mock_herdr, in_memory_ledger):
        """prompt → poll collects fresh result (verifies the fix)."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        in_memory_ledger.cas_collect("t1", "time1", "old result")
        with patch("bridge.time.sleep"):
            bridge.prompt("t1", "follow up")
        mock_herdr.agent_get.return_value = {"agent_status": "idle"}
        mock_herdr.agent_read.return_value = "❯ follow up\n⏺ new result\n"
        result = bridge.poll("t1")
        assert result["state"] == "done"
        assert "new result" in result["result"]["text"]


# ---------------------------------------------------------------------------
# Trust dialog tests
# ---------------------------------------------------------------------------

class TestTrustDialog:
    """Test trust-dialog detection + content guard in send()."""

    def test_trust_dialog_detected_and_confirmed(self, bridge, mock_herdr, in_memory_ledger):
        """blocked + "trust" in pane text → sends Down+Enter."""
        mock_herdr.agent_start.side_effect = AgentNotReady("blocked")
        mock_herdr.agent_get.side_effect = [
            {"agent_status": "blocked"},  # first check
            {"agent_status": "idle"},     # after trust confirmation
            {"agent_status": "idle"},     # 2nd consecutive idle
            {"agent_status": "idle"},     # 3rd consecutive idle
            {"agent_status": "working"},  # wait-for-working loop
            {"agent_status": "idle", "agent_session": {"value": "sess-1"}},  # post-prompt read
        ]
        mock_herdr.pane_read.return_value = "Do you trust the files in this folder?\n 1. Yes, proceed\n 2. No, exit Claude Code"
        with patch("bridge.time.sleep"):
            result = bridge.send("test prompt", cwd=f"{HOME}/DEV")
        assert result["task_id"] is not None
        mock_herdr.pane_send_keys.assert_any_call("w1:p1", "Down")
        mock_herdr.pane_send_keys.assert_any_call("w1:p1", "Enter")

    def test_trust_dialog_not_present(self, bridge, mock_herdr, in_memory_ledger):
        """blocked + no "trust" in pane text → surface to the operator, no keys sent."""
        mock_herdr.agent_start.side_effect = AgentNotReady("blocked")
        mock_herdr.agent_get.return_value = {"agent_status": "blocked"}
        mock_herdr.pane_read.return_value = "Permission required to run command"
        result = bridge.send("test prompt", cwd=f"{HOME}/DEV")
        assert "error" in result or result.get("state") == "blocked"
        mock_herdr.pane_send_keys.assert_not_called()

    def test_trust_dialog_resume_launch_pending_false(self, bridge, mock_herdr):
        """blocked + launch_pending=false + "trust" text → sends Down+Enter (resume case)."""
        mock_herdr.agent_start.side_effect = AgentNotReady("blocked")
        mock_herdr.agent_get.side_effect = [
            {"agent_status": "blocked", "launch_pending": False},
            {"agent_status": "idle"},
            {"agent_status": "idle"},
            {"agent_status": "idle"},
            {"agent_status": "working"},  # wait-for-working
            {"agent_status": "idle", "agent_session": {"value": "sess-1"}},
        ]
        mock_herdr.pane_read.return_value = "Do you trust the files in this folder?"
        with patch("bridge.time.sleep"):
            result = bridge.send("test", cwd=f"{HOME}/DEV")
        assert result["task_id"] is not None
        mock_herdr.pane_send_keys.assert_any_call("w1:p1", "Down")


# ---------------------------------------------------------------------------
# Trust pre-seeding tests
# ---------------------------------------------------------------------------

class TestPreseedOnLaunch:
    """send() must mark the launch directory trusted BEFORE creating a pane."""

    def test_preseed_called_with_resolved_cwd(self, bridge, no_real_config_writes):
        with patch("bridge.time.sleep"):
            bridge.send("hi", cwd=f"{HOME}/DEV")
        no_real_config_writes.assert_called_once()
        arg = no_real_config_writes.call_args[0][0]
        import os
        assert arg == os.path.realpath(f"{HOME}/DEV")

    def test_preseed_happens_before_workspace_create(self, bridge, mock_herdr,
                                                     no_real_config_writes):
        """Ordering is the whole point — pre-seed must precede launch."""
        order = []
        no_real_config_writes.side_effect = lambda *a, **k: order.append("preseed") or {"changed": []}
        mock_herdr.workspace_create.side_effect = lambda *a, **k: (
            order.append("workspace_create") or
            {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"})
        with patch("bridge.time.sleep"):
            bridge.send("hi", cwd=f"{HOME}/DEV")
        assert order == ["preseed", "workspace_create"]

    def test_preseed_failure_is_non_fatal(self, bridge, mock_herdr, no_real_config_writes):
        """An unwritable config must not stop the launch — keys still work."""
        no_real_config_writes.return_value = {"error": "config_unreadable",
                                              "message": "boom"}
        with patch("bridge.time.sleep"):
            result = bridge.send("test prompt", cwd=f"{HOME}/DEV")
        assert result.get("task_id") is not None

    def test_no_keystrokes_sent_on_clean_launch(self, bridge, mock_herdr,
                                                no_real_config_writes):
        """Happy path must never poke the pane — blind Enter exits Claude."""
        with patch("bridge.time.sleep"):
            bridge.send("test prompt", cwd=f"{HOME}/DEV")
        mock_herdr.pane_send_keys.assert_not_called()

    def test_blocked_without_dialog_returns_error(self, bridge, mock_herdr,
                                                  no_real_config_writes):
        """Genuinely blocked on something unrecognised -> surface, don't poke."""
        mock_herdr.agent_start.side_effect = AgentNotReady("blocked")
        mock_herdr.agent_get.return_value = {"agent_status": "blocked"}
        mock_herdr.pane_read.return_value = "Permission required to run command"
        result = bridge.send("hi", cwd=f"{HOME}/DEV")
        assert result.get("error") == "blocked"
        mock_herdr.pane_send_keys.assert_not_called()

    def test_external_imports_dialog_selects_yes(self, bridge, mock_herdr,
                                                 no_real_config_writes):
        """The imports dialog's focused default is 'Yes, allow' (1st item).

        Sending Down would move focus to 'No'; the correct answer for a
        pre-seeded directory is Enter alone.
        """
        mock_herdr.agent_start.side_effect = AgentNotReady("blocked")
        mock_herdr.agent_get.side_effect = [
            {"agent_status": "blocked"},
            {"agent_status": "idle"}, {"agent_status": "idle"}, {"agent_status": "idle"},
            {"agent_status": "working"},
            {"agent_status": "idle", "agent_session": {"value": "sess-1"}},
        ]
        mock_herdr.pane_read.return_value = (
            "Allow external CLAUDE.md file imports?\n"
            " 1. Yes, allow\n 2. No\n")
        with patch("bridge.time.sleep"):
            result = bridge.send("hi", cwd=f"{HOME}/DEV")
        assert result.get("task_id") is not None
        mock_herdr.pane_send_keys.assert_called_once_with("w1:p1", "Enter")

    def test_residual_trust_dialog_is_confirmed_safely(self, bridge, mock_herdr,
                                                       no_real_config_writes):
        """If pre-seeding missed (new repo), answer Yes — never blind Enter.

        The trust dialog's default focus is 'No, exit Claude Code', so a bare
        Enter would kill the session. Down (-> 'Yes') then Enter is required.
        """
        mock_herdr.agent_start.side_effect = AgentNotReady("blocked")
        mock_herdr.agent_get.side_effect = [
            {"agent_status": "blocked"},
            {"agent_status": "idle"}, {"agent_status": "idle"}, {"agent_status": "idle"},
            {"agent_status": "working"},
            {"agent_status": "idle", "agent_session": {"value": "sess-1"}},
        ]
        mock_herdr.pane_read.return_value = (
            "Do you trust the files in this folder?\n"
            " 1. Yes, proceed\n 2. No, exit Claude Code\n")
        with patch("bridge.time.sleep"):
            result = bridge.send("hi", cwd=f"{HOME}/DEV")
        assert result.get("task_id") is not None
        assert call("w1:p1", "Down") in mock_herdr.pane_send_keys.call_args_list
        assert call("w1:p1", "Enter") in mock_herdr.pane_send_keys.call_args_list


# ---------------------------------------------------------------------------
# Overtime tests
# ---------------------------------------------------------------------------

class TestOvertime:
    """Test overtime math (advisory only — no auto-kill)."""

    def test_overtime_false_within_timeout(self, bridge, mock_herdr, in_memory_ledger):
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", timeout_min=30)
        mock_herdr.agent_get.return_value = {"agent_status": "working"}
        result = bridge.poll("t1")
        assert result["overtime"] is False

    def test_overtime_true_past_timeout(self, bridge, mock_herdr, in_memory_ledger):
        # Insert a task with created_at 60 minutes ago
        from datetime import datetime, timezone, timedelta
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", timeout_min=30)
        # Manually set created_at to 60 min ago
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        in_memory_ledger.conn.execute(
            "UPDATE tasks SET created_at=? WHERE task_id=?", (old_time, "t1")
        )
        in_memory_ledger.conn.commit()
        mock_herdr.agent_get.return_value = {"agent_status": "working"}
        result = bridge.poll("t1")
        assert result["overtime"] is True
        assert result["elapsed_min"] >= 59  # approximately 60


# ---------------------------------------------------------------------------
# Reconcile tests
# ---------------------------------------------------------------------------

class TestReconcile:
    """Test reconcile drift detection."""

    def test_resumed_task(self, bridge, mock_herdr, in_memory_ledger):
        """Task in ledger + matching pane in herdr → resumed (unknown)."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "sess-1", "p", "/tmp", 30)
        mock_herdr.agent_list.return_value = [
            {"name": "t1", "agent_status": "unknown", "pane_id": "w1:p1"}
        ]
        result = bridge.reconcile()
        row = in_memory_ledger.get_task("t1")
        assert row["last_state"] == "unknown"
        assert any(r["status"] == "resumed" for r in result["tasks"])

    def test_lost_task(self, bridge, mock_herdr, in_memory_ledger):
        """Task in ledger + no match in herdr → lost."""
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "sess-1", "p", "/tmp", 30)
        mock_herdr.agent_list.return_value = []  # no agents in herdr
        result = bridge.reconcile()
        assert any(r["status"] == "lost" for r in result["tasks"])
        lost = [r for r in result["tasks"] if r["status"] == "lost"][0]
        assert lost["claude_session_id"] == "sess-1"

    def test_orphaned_task(self, bridge, mock_herdr, in_memory_ledger):
        """Agent in herdr + no match in ledger → orphaned."""
        mock_herdr.agent_list.return_value = [
            {"name": "unknown-agent", "agent_status": "idle", "pane_id": "w9:p1"}
        ]
        result = bridge.reconcile()
        assert any(r["status"] == "orphaned" for r in result["tasks"])


# ---------------------------------------------------------------------------
# herdr-down propagation tests
# ---------------------------------------------------------------------------

class TestHerdrDown:
    """Test herdr-down error propagation through all commands."""

    def test_send_herdr_down(self, bridge, mock_herdr):
        mock_herdr.workspace_create.side_effect = HerdrServerDown("down")
        result = bridge.send("prompt", cwd=f"{HOME}/DEV")
        assert result["error"] == "herdr_server_down"
        assert "message" in result

    def test_poll_herdr_down(self, bridge, mock_herdr, in_memory_ledger):
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30)
        mock_herdr.agent_get.side_effect = HerdrServerDown("down")
        result = bridge.poll("t1")
        assert result["state"] == "daemon_down"

    def test_poll_all_herdr_down(self, bridge, mock_herdr, in_memory_ledger):
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30)
        in_memory_ledger.insert_task("t2", "w1:p2", "w1", "s2", "p", "/tmp", 30)
        mock_herdr.agent_list.side_effect = HerdrServerDown("down")
        result = bridge.poll_all()
        assert len(result) == 2
        assert all(r["state"] == "daemon_down" for r in result)

    def test_read_herdr_down(self, bridge, mock_herdr, in_memory_ledger):
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30)
        mock_herdr.agent_read.side_effect = HerdrServerDown("down")
        result = bridge.read("t1")
        assert result["error"] == "herdr_server_down"

    def test_prompt_herdr_down(self, bridge, mock_herdr, in_memory_ledger):
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30)
        mock_herdr.agent_prompt.side_effect = HerdrServerDown("down")
        result = bridge.prompt("t1", "new text")
        assert result["error"] == "herdr_server_down"

    def test_kill_herdr_down(self, bridge, mock_herdr, in_memory_ledger):
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30)
        mock_herdr.pane_close.side_effect = HerdrServerDown("down")
        result = bridge.kill("t1")
        assert result["error"] == "herdr_server_down"

    def test_reconcile_herdr_down(self, bridge, mock_herdr):
        mock_herdr.agent_list.side_effect = HerdrServerDown("down")
        with patch("bridge.time.sleep"):  # speed up the retry
            result = bridge.reconcile()
        assert result["error"] == "herdr_server_down"

    def test_reconcile_herdr_down_retry(self, bridge, mock_herdr):
        """reconcile retries once on HerdrServerDown."""
        mock_herdr.agent_list.side_effect = [HerdrServerDown("down"), []]
        with patch("bridge.time.sleep"):
            result = bridge.reconcile()
        # Second call succeeded
        assert "error" not in result
        assert mock_herdr.agent_list.call_count == 2


# ---------------------------------------------------------------------------
# F11 guard tests
# ---------------------------------------------------------------------------

class TestF11Guard:
    """Test the --bare tripwire (LOW priority)."""

    def test_f11_no_bare_flag(self, bridge, mock_herdr):
        """Normal start — no --bare flag → no warning."""
        mock_herdr.agent_get.return_value = {"agent_status": "idle"}
        with patch("subprocess.run") as mock_run, patch("bridge.time.sleep"):
            mock_run.return_value = MagicMock(
                stdout="claude --resume sess-1\n",
                returncode=0
            )
            result = bridge.send("test", cwd=f"{HOME}/DEV")
        assert result.get("parse_warning") != "--bare detected"

    def test_f11_bare_detected(self, bridge, mock_herdr):
        """--bare flag present → warning logged."""
        mock_herdr.agent_get.return_value = {"agent_status": "idle"}
        with patch("subprocess.run") as mock_run, patch("bridge.time.sleep"):
            mock_run.return_value = MagicMock(
                stdout="claude --bare --resume sess-1\n",
                returncode=0
            )
            result = bridge.send("test", cwd=f"{HOME}/DEV")
        # The warning should be in the result
        assert result.get("f11_warning") is True or "--bare" in str(result)


# ---------------------------------------------------------------------------
# Send tests
# ---------------------------------------------------------------------------

class TestSend:
    """Test the send command."""

    def test_send_success(self, bridge, mock_herdr, in_memory_ledger):
        mock_herdr.agent_get.return_value = {"agent_status": "idle", "agent_session": {"value": "sess-new"}}
        with patch("bridge.time.sleep"):
            result = bridge.send("do something", cwd=f"{HOME}/DEV")
        assert result["task_id"] is not None
        assert result["pane_id"] == "w1:p1"
        row = in_memory_ledger.get_task(result["task_id"])
        assert row is not None
        assert row["prompt"] == "do something"
        assert row["pending_result"] == 1
        mock_herdr.agent_prompt.assert_called_once()

    def test_send_no_prompt(self, bridge, mock_herdr, in_memory_ledger):
        """send without a prompt — just starts the agent, no pending_result."""
        mock_herdr.agent_get.return_value = {"agent_status": "idle", "agent_session": {"value": "sess-new"}}
        with patch("bridge.time.sleep"):
            result = bridge.send(None, cwd=f"{HOME}/DEV")
        assert result["task_id"] is not None
        row = in_memory_ledger.get_task(result["task_id"])
        assert row["pending_result"] == 0
        mock_herdr.agent_prompt.assert_not_called()

    def test_send_cwd_enforcement(self, bridge, mock_herdr):
        """cwd outside ~/DEV → refused."""
        result = bridge.send("test", cwd="/etc")
        assert "error" in result
        assert "cwd" in result["error"].lower() or "approved" in result.get("message", "").lower()
        mock_herdr.workspace_create.assert_not_called()

    def test_send_with_session_resume(self, bridge, mock_herdr, in_memory_ledger):
        """send --session should pass --resume to agent_start."""
        mock_herdr.agent_get.return_value = {"agent_status": "idle", "agent_session": {"value": "sess-old"}}
        with patch("bridge.time.sleep"):
            result = bridge.send("continue", cwd=f"{HOME}/DEV", session_id="sess-old")
        # Check agent_start was called with --resume args
        _, kwargs = mock_herdr.agent_start.call_args
        assert "--resume" in (kwargs.get("args") or [])

    def test_send_idle_confirmations(self, bridge, mock_herdr):
        """send requires 3 consecutive idle readings before sending prompt."""
        mock_herdr.agent_get.side_effect = [
            {"agent_status": "unknown"},
            {"agent_status": "idle"},
            {"agent_status": "idle"},
            {"agent_status": "idle"},  # 3rd consecutive idle
            {"agent_status": "working"},  # wait-for-working loop
            {"agent_status": "idle", "agent_session": {"value": "sess-1"}},  # post-prompt read
        ]
        with patch("bridge.time.sleep"):
            result = bridge.send("test", cwd=f"{HOME}/DEV")
        assert result["task_id"] is not None
        mock_herdr.agent_prompt.assert_called_once()


# ---------------------------------------------------------------------------
# Poll --all tests
# ---------------------------------------------------------------------------

class TestPollAll:
    def test_poll_all_mixed_states(self, bridge, mock_herdr, in_memory_ledger):
        in_memory_ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        in_memory_ledger.insert_task("t2", "w1:p2", "w1", "s2", "p", "/tmp", 30, pending_result=True)
        in_memory_ledger.update_last_state("t1", "working")
        mock_herdr.agent_list.return_value = [
            {"name": "t1", "agent_status": "idle", "pane_id": "w1:p1"},
            {"name": "t2", "agent_status": "working", "pane_id": "w1:p2"},
        ]
        # agent_get returns per-task: t1 idle (done), t2 working (running)
        def agent_get_side_effect(name, *args, **kwargs):
            if name == "t1":
                return {"agent_status": "idle"}
            elif name == "t2":
                return {"agent_status": "working"}
            return {"agent_status": "idle"}
        mock_herdr.agent_get.side_effect = agent_get_side_effect
        mock_herdr.agent_read.return_value = "❯ p\n⏺ result\n"
        results = bridge.poll_all()
        assert len(results) == 2
        states = {r["task_id"]: r["state"] for r in results}
        assert states["t1"] == "done"
        assert states["t2"] == "running"


# ---------------------------------------------------------------------------
# Integration tests (gated)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("HERDR_INTEGRATION"),
    reason="Set HERDR_INTEGRATION=1 to run integration tests"
)
class TestBridgeIntegration:
    """Integration tests against a live herdr server."""

    SOCKET_PATH = os.path.expanduser("~/.config/herdr/herdr.sock")
    DEFAULT_CWD = f"{HOME}/DEV"

    @pytest.fixture
    def live_bridge(self):
        from herdr_client import HerdrClient
        from ledger import Ledger
        herdr = HerdrClient(socket_path=self.SOCKET_PATH)
        ledger = Ledger(":memory:")
        ledger.init_schema()
        b = Bridge(herdr=herdr, ledger=ledger, default_cwd=self.DEFAULT_CWD)
        yield b
        # Cleanup: kill all tasks
        for row in ledger.list_all():
            try:
                b.kill(row["task_id"])
            except Exception:
                pass

    def test_send_poll_done(self, live_bridge):
        """send trivial task → poll until done → result matches."""
        result = live_bridge.send("Reply with exactly: HERDR_OK and nothing else.", cwd=self.DEFAULT_CWD)
        assert result["task_id"] is not None
        task_id = result["task_id"]
        
        # Poll until done (agent starts in working state after send)
        for _ in range(120):
            poll_result = live_bridge.poll(task_id)
            if poll_result["state"] == "done":
                result_text = poll_result["result"]
                if isinstance(result_text, dict):
                    result_text = result_text.get("text", "")
                assert "HERDR_OK" in str(result_text), f"Expected HERDR_OK, got: {str(result_text)[:200]}"
                break
            elif poll_result["state"] == "blocked":
                pass  # surface but keep waiting
            time.sleep(1)
        else:
            pytest.fail("Task did not complete within 120s")

    def test_poll_cached(self, live_bridge):
        """poll again after done → cached result, no re-read."""
        result = live_bridge.send("Reply with exactly: CACHED_TEST", cwd=self.DEFAULT_CWD)
        task_id = result["task_id"]
        
        # Wait for done
        for _ in range(120):
            poll1 = live_bridge.poll(task_id)
            if poll1["state"] == "done":
                break
            time.sleep(1)
        
        # Poll again — should be cached
        poll2 = live_bridge.poll(task_id)
        assert poll2["state"] == "done"
        result_text = poll2["result"]
        if isinstance(result_text, dict):
            result_text = result_text.get("text", "")
        assert "CACHED_TEST" in str(result_text)

    def test_prompt_multiturn(self, live_bridge):
        """prompt → poll → new result (verifies the multi-turn fix)."""
        result = live_bridge.send("Reply with exactly: FIRST", cwd=self.DEFAULT_CWD)
        task_id = result["task_id"]
        
        # Wait for done
        for _ in range(120):
            poll = live_bridge.poll(task_id)
            if poll["state"] == "done":
                break
            time.sleep(1)
        
        # Multi-turn prompt
        live_bridge.prompt(task_id, "Now reply with exactly: SECOND")
        
        # Poll until done
        for _ in range(120):
            poll = live_bridge.poll(task_id)
            if poll["state"] == "done":
                result_text = poll["result"]
                if isinstance(result_text, dict):
                    result_text = result_text.get("text", "")
                assert "SECOND" in str(result_text), f"Expected SECOND, got: {str(result_text)[:200]}"
                break
            time.sleep(1)
        else:
            pytest.fail("Multi-turn task did not complete within 120s")

    def test_kill(self, live_bridge):
        """kill mid-run → poll shows killed."""
        result = live_bridge.send("Write a very long essay about the history of computing.", cwd=self.DEFAULT_CWD)
        task_id = result["task_id"]
        time.sleep(2)  # let it start
        live_bridge.kill(task_id)
        poll = live_bridge.poll(task_id)
        assert poll["state"] in ("killed", "lost")  # killed if ledger state, lost if agent gone

    def test_herdr_down(self, live_bridge):
        """stop herdr → poll returns daemon_down → restart → poll recovers (or agent lost)."""
        import subprocess
        result = live_bridge.send("Reply with exactly: DOWNTEST", cwd=self.DEFAULT_CWD)
        task_id = result["task_id"]
        
        # Stop herdr
        subprocess.run(["launchctl", "unload", os.path.expanduser("~/Library/LaunchAgents/com.luther.herdr.plist")],
                       capture_output=True)
        time.sleep(2)
        
        # Poll — should get daemon_down
        poll = live_bridge.poll(task_id)
        assert poll["state"] == "daemon_down"
        
        # Restart herdr
        subprocess.run(["launchctl", "load", os.path.expanduser("~/Library/LaunchAgents/com.luther.herdr.plist")],
                       capture_output=True)
        time.sleep(5)
        
        # Poll — should recover (done/lost/killed are all valid after restart)
        poll = live_bridge.poll(task_id)
        assert poll["state"] != "daemon_down"


# ---------------------------------------------------------------------------
# MCP Pairing tests (Unit 2 — Bridge.send() extension)
# ---------------------------------------------------------------------------

class TestMCPPairing:
    """Tests for the mcp_pair parameter on Bridge.send()."""

    def test_default_no_mcp_pair(self, bridge, mock_herdr, in_memory_ledger):
        """Default send (no agent_id) produces no MCP args — auto-pair stays off."""
        result = bridge.send("hi")
        assert "error" not in result
        call_args = mock_herdr.agent_start.call_args
        args_passed = call_args.kwargs.get("args")
        if args_passed:
            assert "--mcp-config" not in args_passed
        row = in_memory_ledger.get_task(result["task_id"])
        assert row["mcp_config_path"] is None
        assert row["mcp_target"] is None
        assert row["mcp_session_prefix"] is None

    def test_auto_pair_when_agent_id_set(self, bridge, mock_herdr, in_memory_ledger):
        """Send with agent_id auto-pairs without explicit mcp_pair=True."""
        result = bridge.send("hi", agent_id="alfred")
        assert "error" not in result
        call_args = mock_herdr.agent_start.call_args
        args_passed = call_args.kwargs.get("args")
        assert args_passed is not None
        assert "--mcp-config" in args_passed
        assert "--strict-mcp-config" in args_passed
        assert result.get("mcp_paired") is True
        # Cleanup temp file
        import os as _os
        mcp_idx = args_passed.index("--mcp-config")
        _os.unlink(args_passed[mcp_idx + 1])

    def test_explicit_false_overrides_auto(self, bridge, mock_herdr, in_memory_ledger):
        """mcp_pair=False overrides the auto-pair default when agent_id is set."""
        result = bridge.send("hi", agent_id="alfred", mcp_pair=False)
        assert "error" not in result
        call_args = mock_herdr.agent_start.call_args
        args_passed = call_args.kwargs.get("args")
        if args_passed:
            assert "--mcp-config" not in args_passed
        assert "mcp_paired" not in result

    def test_mcp_pair_produces_config(self, bridge, mock_herdr, in_memory_ledger):
        """mcp_pair=True generates config and passes --mcp-config to agent_start."""
        result = bridge.send("hi", mcp_pair=True, agent_id="alfred")
        assert "error" not in result
        # Verify agent_start was called with MCP args
        call_args = mock_herdr.agent_start.call_args
        args_passed = call_args.kwargs.get("args")
        assert args_passed is not None
        assert "--mcp-config" in args_passed
        assert "--strict-mcp-config" in args_passed
        # Find the config path in the args
        mcp_idx = args_passed.index("--mcp-config")
        config_path = args_passed[mcp_idx + 1]
        assert os.path.isfile(config_path)
        assert "herdr-mcp-" in config_path
        # Verify ledger has MCP fields
        row = in_memory_ledger.get_task(result["task_id"])
        assert row["mcp_config_path"] == config_path
        assert row["mcp_target"] == "alfred"
        assert "mcp-bridge-herdr-" in row["mcp_session_prefix"]
        # Verify result dict has MCP info
        assert result.get("mcp_paired") is True
        assert result["mcp_target"] == "alfred"
        # Cleanup
        import os as _os
        _os.unlink(config_path)

    def test_mcp_pair_respects_session_id(self, bridge, mock_herdr, in_memory_ledger):
        """mcp_pair=True with session_id still passes --resume."""
        result = bridge.send("hi", mcp_pair=True, session_id="my-session", agent_id="alfred")
        call_args = mock_herdr.agent_start.call_args
        args_passed = call_args.kwargs.get("args")
        assert "--resume" in args_passed
        assert "my-session" in args_passed
        assert "--mcp-config" in args_passed

    def test_kill_cleans_up_mcp_config(self, bridge, mock_herdr, in_memory_ledger):
        """kill() removes the temp MCP config file."""
        result = bridge.send("hi", mcp_pair=True, agent_id="alfred")
        row = in_memory_ledger.get_task(result["task_id"])
        config_path = row["mcp_config_path"]
        assert os.path.isfile(config_path)
        # Kill the task
        bridge.kill(result["task_id"])
        # Config file should be gone
        assert not os.path.isfile(config_path)
        # Ledger should be cleared
        row_after = in_memory_ledger.get_task(result["task_id"])
        assert row_after["mcp_config_path"] is None

    def test_kill_tolerates_missing_config(self, bridge, mock_herdr, in_memory_ledger):
        """kill() doesn't raise if the MCP config file was already removed."""
        result = bridge.send("hi", mcp_pair=True, agent_id="alfred")
        row = in_memory_ledger.get_task(result["task_id"])
        config_path = row["mcp_config_path"]
        # Remove the file manually (simulating external cleanup)
        import os as _os
        _os.unlink(config_path)
        # kill() should not raise
        kill_result = bridge.kill(result["task_id"])
        assert kill_result["state"] == "killed"


# ---------------------------------------------------------------------------
# Unit 0: pane_id parameter (shared workspace for swarm)
# ---------------------------------------------------------------------------

class TestPaneIdSharing:
    """Tests for Bridge.send(pane_id=...) workspace-sharing extension."""

    def test_pane_id_skips_workspace_create(self, bridge, mock_herdr, in_memory_ledger):
        """When pane_id is provided, workspace_create is NOT called."""
        result = bridge.send("hi", pane_id="w1:shared-pane", agent_id="alfred")
        assert "error" not in result
        # workspace_create must NOT have been called
        mock_herdr.workspace_create.assert_not_called()
        # agent_start must use the provided pane_id
        agent_start_call = mock_herdr.agent_start.call_args
        assert agent_start_call.kwargs.get("pane_id") == "w1:shared-pane"

    def test_default_without_pane_id_calls_workspace_create(self, bridge, mock_herdr, in_memory_ledger):
        """Without pane_id, workspace_create IS called (existing behavior)."""
        result = bridge.send("hi")
        assert "error" not in result
        mock_herdr.workspace_create.assert_called_once()

    def test_pane_id_with_mcp_pair(self, bridge, mock_herdr, in_memory_ledger):
        """pane_id works with mcp_pair=True for swarm sessions."""
        result = bridge.send("hi", pane_id="w1:swarm-pane", mcp_pair=True, agent_id="alfred")
        assert "error" not in result
        assert result.get("mcp_paired") is True
        mock_herdr.workspace_create.assert_not_called()
        # Cleanup temp config
        import os as _os
        row = in_memory_ledger.get_task(result["task_id"])
        if row and row.get("mcp_config_path"):
            _os.unlink(row["mcp_config_path"])

# ---------------------------------------------------------------------------
# Reporting contract — brief fix (2026-10-10)
# ---------------------------------------------------------------------------

class TestReportingContract:
    """A paired prompt always carries the report block.

    Root cause: TICKET-444 parent 2.0 finished its work, the ledger saw the
    pane go idle, and nobody was told — the hand-written brief never said
    "report back". The block is appended structurally so that omission is
    no longer possible.
    """

    def test_block_names_task_and_target(self):
        from bridge import REPORTING_MARKER, _with_reporting_contract
        out = _with_reporting_contract("do the thing", "task-abc", "alfred")
        assert out.startswith("do the thing")
        assert REPORTING_MARKER in out
        assert "task-abc" in out
        assert "alfred" in out
        assert "qwenpaw_chat" in out

    def test_block_is_idempotent(self):
        from bridge import _with_reporting_contract
        once = _with_reporting_contract("x", "t", "alfred")
        assert _with_reporting_contract(once, "t", "alfred") == once

    def test_no_block_for_empty_prompt(self):
        from bridge import _with_reporting_contract
        assert _with_reporting_contract(None, "t", "alfred") is None
        assert _with_reporting_contract("", "t", "alfred") == ""

    def test_unpaired_send_is_unchanged(self, bridge, mock_herdr,
                                       in_memory_ledger):
        mock_herdr.agent_get.return_value = {
            "agent_status": "idle", "agent_session": {"value": "sess-new"}}
        with patch("bridge.time.sleep"):
            result = bridge.send("hi", cwd=f"{HOME}/DEV")
        row = in_memory_ledger.get_task(result["task_id"])
        assert row["prompt"] == "hi"

    def test_paired_send_carries_block_to_pane_and_ledger(
            self, bridge, mock_herdr, in_memory_ledger):
        import os as _os
        from bridge import REPORTING_MARKER
        mock_herdr.agent_get.return_value = {
            "agent_status": "idle", "agent_session": {"value": "sess-new"}}
        with patch("bridge.time.sleep"):
            result = bridge.send("hi", mcp_pair=True, agent_id="alfred")
        assert "error" not in result
        sent = mock_herdr.agent_prompt.call_args[0][1]
        assert REPORTING_MARKER in sent
        assert result["task_id"] in sent
        row = in_memory_ledger.get_task(result["task_id"])
        # The ledger records what the pane was actually told.
        assert row["prompt"] == sent
        if row.get("mcp_config_path"):
            _os.unlink(row["mcp_config_path"])


# ---------------------------------------------------------------------------
# Review findings (2026-11-03) — regression tests
# ---------------------------------------------------------------------------

class TestCwdContainment:
    """Finding 1: the folder check must survive `..` and a shared prefix."""

    @pytest.mark.parametrize("escape", [
        f"{HOME}/DEV/../.ssh",
        f"{HOME}/DEVIOUS",
        f"{HOME}/qwenpaw/workspaces/workspaces/../../../.config",
        f"{HOME}/DEV/../qwenpaw",
    ])
    def test_traversal_and_prefix_neighbours_are_refused(self, escape):
        from bridge import cwd_allowed
        assert cwd_allowed(escape) is False

    @pytest.mark.parametrize("allowed", [
        f"{HOME}/DEV",
        f"{HOME}/DEV/project",
        f"{HOME}/qwenpaw/workspaces/workspaces/alfred",
    ])
    def test_real_roots_are_still_allowed(self, allowed):
        from bridge import cwd_allowed
        assert cwd_allowed(allowed) is True

    def test_send_refuses_a_traversal_cwd(self, bridge, mock_herdr):
        result = bridge.send("x", cwd=f"{HOME}/DEV/../.ssh")
        assert result["error"] == "cwd_not_allowed"
        mock_herdr.workspace_create.assert_not_called()


class TestStartupDeadline:
    """Finding 2: the startup loop must not run forever."""

    def test_a_never_ready_pane_returns_not_ready(self, bridge, mock_herdr, monkeypatch):
        import bridge as bridge_module
        mock_herdr.agent_get.return_value = {"agent_status": "working", "pane_id": "w1:p1"}
        monkeypatch.setattr(bridge_module, "STARTUP_DEADLINE_S", 0.0)
        with patch("bridge.time.sleep"):
            result = bridge.send("x", cwd=f"{HOME}/DEV")
        assert result["error"] == "not_ready"
        assert result["task_id"] and result["pane_id"]

    def test_an_unknown_status_also_hits_the_deadline(self, bridge, mock_herdr, monkeypatch):
        import bridge as bridge_module
        mock_herdr.agent_get.return_value = {"agent_status": "weird"}
        monkeypatch.setattr(bridge_module, "STARTUP_DEADLINE_S", 0.0)
        with patch("bridge.time.sleep"):
            result = bridge.send("x", cwd=f"{HOME}/DEV")
        assert result["error"] == "not_ready"

    def test_agent_not_found_during_startup_returns_ids(self, bridge, mock_herdr):
        mock_herdr.agent_get.side_effect = HerdrAPIError("agent_not_found", "gone")
        with patch("bridge.time.sleep"):
            result = bridge.send("x", cwd=f"{HOME}/DEV")
        assert result["error"] == "not_ready"
        assert result["task_id"] and result["pane_id"]


class TestPartialSendLeavesNoOrphan:
    """Finding 3: a failed send must still be registered and cleaned up."""

    def test_blocked_error_carries_ids_and_a_ledger_row(
            self, bridge, mock_herdr, in_memory_ledger, no_real_config_writes):
        mock_herdr.agent_get.return_value = {"agent_status": "blocked"}
        mock_herdr.pane_read.return_value = "an unrecognised dialog nobody handles"
        with patch("bridge.time.sleep"):
            result = bridge.send("x", cwd=f"{HOME}/DEV", agent_id="alfred")
        assert result["error"] == "blocked"
        assert result["task_id"] and result["pane_id"]
        row = in_memory_ledger.get_task(result["task_id"])
        assert row is not None, "a live pane with no ledger row is an orphan"
        assert row["herdr_pane_id"] == result["pane_id"]

    def test_blocked_error_cleans_the_mcp_temp_config(
            self, bridge, mock_herdr, tmp_path, no_real_config_writes):
        from mcp_pair import PairingConfig
        cfg_file = tmp_path / "mcp.json"
        cfg_file.write_text("{}")
        fake = PairingConfig(target_agent="default", allowlist="default",
                             session_prefix="p", config_path=str(cfg_file),
                             config_json={})
        mock_herdr.agent_get.return_value = {"agent_status": "blocked"}
        mock_herdr.pane_read.return_value = "an unrecognised dialog nobody handles"
        with patch("mcp_pair.write_config", return_value=fake), patch("bridge.time.sleep"):
            result = bridge.send("x", cwd=f"{HOME}/DEV", agent_id="alfred")
        assert result["error"] == "blocked"
        assert not cfg_file.exists(), "temp MCP config leaked after a failed send"


class TestPromptReportingContract:
    """Finding 4: follow-up prompts must carry the reporting block too."""

    def _seed_paired(self, ledger, mcp_target="default"):
        ledger.insert_task(task_id="task-p", pane_id="w1:p1", workspace_id="w1",
                           session_id="s", prompt="first", cwd=f"{HOME}/DEV",
                           timeout_min=30, agent_id="alfred",
                           mcp_target=mcp_target, mcp_session_prefix="p")

    def test_prompt_adds_the_block_when_the_task_is_paired(
            self, bridge, mock_herdr, in_memory_ledger):
        from bridge import REPORTING_MARKER
        self._seed_paired(in_memory_ledger)
        with patch("bridge.time.sleep"):
            bridge.prompt("task-p", "follow up")
        sent = mock_herdr.agent_prompt.call_args[0][1]
        assert REPORTING_MARKER in sent

    def test_prompt_leaves_an_unpaired_task_alone(self, bridge, mock_herdr, in_memory_ledger):
        from bridge import REPORTING_MARKER
        in_memory_ledger.insert_task(task_id="task-u", pane_id="w1:p1", workspace_id="w1",
                                     session_id="s", prompt="first", cwd=f"{HOME}/DEV",
                                     timeout_min=30, agent_id="alfred")
        with patch("bridge.time.sleep"):
            bridge.prompt("task-u", "follow up")
        sent = mock_herdr.agent_prompt.call_args[0][1]
        assert REPORTING_MARKER not in sent


class TestKillIsIdempotent:
    """Finding 7: a pane that is already gone is a killed task, not an error."""

    def test_missing_agent_is_treated_as_killed(self, bridge, mock_herdr, in_memory_ledger):
        in_memory_ledger.insert_task(task_id="task-k", pane_id="w1:p1", workspace_id="w1",
                                     session_id="s", prompt="p", cwd=f"{HOME}/DEV",
                                     timeout_min=30, agent_id="alfred")
        mock_herdr.pane_close.side_effect = HerdrAPIError("agent_not_found", "gone")
        result = bridge.kill("task-k")
        assert result["state"] == "killed"
        assert in_memory_ledger.get_task("task-k")["last_state"] == "killed"


def test_server_down_message_names_the_installed_plist():
    """Finding 8: the hint must name the plist that is actually installed."""
    import bridge as bridge_module
    assert "com.alfred.herdr.plist" in bridge_module.HERDR_DOWN_MSG
    assert "com.luther" not in bridge_module.HERDR_DOWN_MSG
