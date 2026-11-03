"""Tests for swarm.py — multi-Claude orchestration with MCP pairing."""

from __future__ import annotations

import os
import time
import unittest
from unittest.mock import MagicMock, patch, call

from swarm import (
    SwarmHandle,
    SwarmError,
    launch,
    coordinate,
    collect,
    teardown,
    route_peer_message,
    POLL_INTERVAL,
    MAX_PEER_POLL_SECS,
)

HOME = os.path.expanduser("~")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_mock_bridge():
    """A mock Bridge with controllable herdr + ledger."""
    b = MagicMock()
    # herdr
    b.herdr = MagicMock()
    b.herdr.workspace_create.return_value = {
        "workspace_id": "wS", "pane_id": "wS:p0",
    }
    b.herdr.agent_start.return_value = {"type": "agent_started"}
    b.herdr.agent_get.return_value = {
        "agent_status": "idle",
        "agent_session": {"value": "sess-1"},
    }
    b.herdr.agent_prompt.return_value = {"type": "ok"}
    b.herdr.pane_read.return_value = ""
    b.herdr.pane_send_keys.return_value = {"type": "ok"}
    b.herdr.pane_close.return_value = {"type": "ok"}
    b.herdr.agent_list.return_value = []

    # ledger
    from ledger import Ledger
    b.ledger = Ledger(":memory:")
    b.ledger.init_schema()

    # send() behavior
    task_counter = [0]

    def fake_send(prompt="", cwd=None, pane_id=None, mcp_pair=False,
                  agent_id=None, session_id=None, timeout_min=30):
        task_counter[0] += 1
        tid = f"task-s{task_counter[0]:03d}"
        b.ledger.insert_task(
            task_id=tid,
            pane_id=pane_id or "wS:pX",
            workspace_id="wS",
            session_id=f"sess-{task_counter[0]}",
            prompt=prompt,
            cwd=cwd or f"{HOME}/DEV",
            timeout_min=timeout_min,
            pending_result=bool(prompt),
            agent_id=agent_id or "legacy",
            mcp_config_path=f"/tmp/herdr-mcp-{tid}.json" if mcp_pair else None,
            mcp_target=agent_id if mcp_pair else None,
            mcp_session_prefix=f"swarm-TEST-{tid}" if mcp_pair else None,
        )
        return {
            "task_id": tid,
            "pane_id": pane_id or "wS:pX",
            "mcp_paired": mcp_pair,
            "mcp_target": agent_id if mcp_pair else None,
            "mcp_session_prefix": f"swarm-TEST-{tid}" if mcp_pair else None,
        }

    b.send = fake_send

    # poll() behavior: controllable via b._poll_results dict
    b._poll_results = {}
    b._poll_calls = {}

    def fake_poll(task_id):
        b._poll_calls[task_id] = b._poll_calls.get(task_id, 0) + 1
        if task_id in b._poll_results:
            return b._poll_results[task_id]
        return {"task_id": task_id, "state": "running"}

    b.poll = fake_poll

    # prompt()
    b._prompts = []

    def fake_prompt(task_id, text):
        b._prompts.append({"task_id": task_id, "text": text})
        return {"task_id": task_id, "state": "working"}

    b.prompt = fake_prompt

    # kill()
    b._killed = set()

    def fake_kill(task_id):
        b._killed.add(task_id)
        b.ledger.kill_task(task_id)
        return {"task_id": task_id, "state": "killed"}

    b.kill = fake_kill

    return b


@patch("swarm.preseed_trust",
       return_value={"key": f"{HOME}/DEV", "changed": []})
class TestSwarmLaunch(unittest.TestCase):
    """Unit 1: swarm.launch() — shared workspace, N sessions, MCP paired."""

    def test_launch_default_size_3(self, _):
        b = make_mock_bridge()
        handle = launch("alfred", "test task", cwd=f"{HOME}/DEV",
                         bridge=b)
        self.assertEqual(len(handle.task_ids), 3)
        self.assertEqual(len(handle.roles), 3)
        self.assertEqual(handle.roles["claude-1"], "task-s001")
        self.assertEqual(handle.roles["claude-2"], "task-s002")
        self.assertEqual(handle.roles["claude-3"], "task-s003")
        self.assertEqual(handle.state, "launched")
        # Workspace created once
        b.herdr.workspace_create.assert_called_once()

    def test_launch_size_2(self, _):
        b = make_mock_bridge()
        handle = launch("alfred", "test", swarm_size=2,
                         cwd=f"{HOME}/DEV", bridge=b)
        self.assertEqual(len(handle.task_ids), 2)
        self.assertIn("claude-1", handle.roles)
        self.assertIn("claude-2", handle.roles)
        self.assertNotIn("claude-3", handle.roles)

    def test_launch_mcp_paired(self, _):
        b = make_mock_bridge()
        handle = launch("alfred", "test", cwd=f"{HOME}/DEV",
                         bridge=b)
        for task_id in handle.task_ids:
            row = b.ledger.get_task(task_id)
            self.assertIsNotNone(row["mcp_config_path"])
            self.assertEqual(row["mcp_target"], "alfred")

    def test_launch_mcp_config_paths_tracked(self, _):
        b = make_mock_bridge()
        handle = launch("alfred", "test", cwd=f"{HOME}/DEV",
                         bridge=b)
        self.assertEqual(len(handle.mcp_config_paths), 3)
        for task_id in handle.task_ids:
            self.assertIn(task_id, handle.mcp_config_paths)

    def test_launch_cwd_enforcement(self, _):
        b = make_mock_bridge()
        with self.assertRaises(ValueError):
            launch("alfred", "test", cwd="/tmp/evil", bridge=b)

    def test_partial_launch_failure(self, _):
        b = make_mock_bridge()
        call_count = [0]

        def fail_on_third(prompt="", **kw):
            call_count[0] += 1
            if call_count[0] == 3:
                return {"error": "herdr_down", "message": "no"}
            return b.send.__wrapped__(prompt, **kw) if hasattr(b.send, "__wrapped__") else {
                "task_id": f"task-s{call_count[0]:03d}",
                "pane_id": "wS:pX",
                "mcp_paired": True,
            }

        # Simpler: just patch the third send call
        real_send = b.send
        send_count = [0]

        def patched_send(**kw):
            send_count[0] += 1
            if send_count[0] == 3:
                return {"error": "herdr_down", "message": "simulated failure"}
            return real_send(**kw)

        b.send = patched_send

        with self.assertRaises(SwarmError) as ctx:
            launch("alfred", "test", cwd=f"{HOME}/DEV", bridge=b)

        self.assertIn("Partial launch", str(ctx.exception))
        self.assertIn("2/3", str(ctx.exception))
        # All launched tasks should have been killed
        self.assertIn("task-s001", b._killed)
        self.assertIn("task-s002", b._killed)


@patch("swarm.preseed_trust",
       return_value={"key": f"{HOME}/DEV", "changed": []})
class TestSwarmCoordinate(unittest.TestCase):
    """Unit 1: swarm.coordinate() — peer discovery prompts."""

    def test_coordinate_sends_prompts(self, _):
        b = make_mock_bridge()
        handle = launch("alfred", "test", cwd=f"{HOME}/DEV",
                         bridge=b)
        coordinate(handle)
        self.assertEqual(handle.state, "working")
        self.assertEqual(len(b._prompts), 3)

    def test_coordinate_peer_list_includes_others(self, _):
        b = make_mock_bridge()
        handle = launch("alfred", "test", cwd=f"{HOME}/DEV",
                         bridge=b)
        coordinate(handle)
        # Each prompt should mention the OTHER two claudes and NOT itself as reachable
        for p in b._prompts:
            text = p["text"]
            # Each claude-N should see the other two, not itself
            for role in ("claude-1", "claude-2", "claude-3"):
                if role in text.split("You are ")[1].split(" in swarm")[0]:
                    # This is the prompt TO this claude — it should NOT list itself as a peer
                    self.assertNotIn(f"{role} — reach via qwenpaw_chat", text)
                else:
                    # It SHOULD list the others
                    self.assertIn(f"{role} — reach via qwenpaw_chat", text)

    def test_coordinate_bad_state(self, _):
        b = make_mock_bridge()
        handle = launch("alfred", "test", cwd=f"{HOME}/DEV",
                         bridge=b)
        handle.state = "working"
        with self.assertRaises(SwarmError):
            coordinate(handle)


@patch("swarm.preseed_trust",
       return_value={"key": f"{HOME}/DEV", "changed": []})
class TestSwarmCollect(unittest.TestCase):
    """Unit 1: swarm.collect() — blocking poll of all N."""

    def test_collect_all_done(self, _):
        b = make_mock_bridge()
        handle = launch("alfred", "test", cwd=f"{HOME}/DEV",
                         bridge=b)
        coordinate(handle)

        # All three sessions report done immediately
        for tid in handle.task_ids:
            b._poll_results[tid] = {
                "task_id": tid, "state": "done",
                "result": {"text": f"result from {tid}"},
            }

        results = collect(handle, timeout_min=1)
        self.assertEqual(len(results), 3)
        for role in ("claude-1", "claude-2", "claude-3"):
            self.assertEqual(results[role]["status"], "completed")

    def test_collect_partial_failure(self, _):
        b = make_mock_bridge()
        handle = launch("alfred", "test", cwd=f"{HOME}/DEV",
                         bridge=b)
        coordinate(handle)

        # Claude-1 done, Claude-2 killed, Claude-3 running (→ timeout)
        b._poll_results["task-s001"] = {"task_id": "task-s001", "state": "done",
                                         "result": {"text": "ok"}}
        b._poll_results["task-s002"] = {"task_id": "task-s002", "state": "killed"}
        # task-s003 stays "running" → will timeout

        results = collect(handle, timeout_min=0.01)
        self.assertEqual(results["claude-1"]["status"], "completed")
        self.assertEqual(results["claude-2"]["status"], "failed")
        self.assertEqual(results["claude-3"]["status"], "timeout")


@patch("swarm.preseed_trust",
       return_value={"key": f"{HOME}/DEV", "changed": []})
class TestSwarmTeardown(unittest.TestCase):
    """Unit 1: swarm.teardown() — kill all, cleanup."""

    def test_teardown_kills_all(self, _):
        b = make_mock_bridge()
        handle = launch("alfred", "test", cwd=f"{HOME}/DEV",
                         bridge=b)
        result = teardown(handle)
        self.assertEqual(len(result["closed"]), 3)
        self.assertEqual(len(result["errors"]), 0)
        # All should be killed
        for tid in handle.task_ids:
            self.assertIn(tid, b._killed)

    def test_teardown_clears_pending_requests(self, _):
        b = make_mock_bridge()
        handle = launch("alfred", "test", cwd=f"{HOME}/DEV",
                         bridge=b)
        handle._pending_peer_requests.add(("claude-1", "claude-2"))
        teardown(handle)
        self.assertEqual(len(handle._pending_peer_requests), 0)



class TestPeerRouting(unittest.TestCase):
    """Unit 2: route_peer_message() — deadlock detection, timeout, success."""

    @classmethod
    def setUpClass(cls):
        cls._preseed_patcher = patch("swarm.preseed_trust",
                                      return_value={"key": f"{HOME}/DEV", "changed": []})
        cls._preseed_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls._preseed_patcher.stop()

    def setUp(self):
        self.b = make_mock_bridge()
        self.handle = launch("alfred", "test", cwd=f"{HOME}/DEV",
                              bridge=self.b)

    def test_route_peer_success(self):
        """Claude-1 → Claude-2: prompt fires, poll returns done."""
        target_tid = self.handle.roles["claude-2"]
        self.b._poll_results[target_tid] = {
            "task_id": target_tid, "state": "done",
            "result": {"text": "hello from claude-2"},
        }

        result = route_peer_message(
            self.handle,
            session_id=f"swarm-{self.handle.swarm_id}-claude-1-to-claude-2",
            text="howdy",
        )
        self.assertEqual(result, "hello from claude-2")
        self.assertEqual(len(self.b._prompts), 1)
        self.assertIn("howdy", self.b._prompts[0]["text"])

    def test_route_peer_deadlock_detected(self):
        """Mutual wait: claude-2 waiting on claude-1 and vice versa."""
        self.handle._pending_peer_requests.add(("claude-2", "claude-1"))

        result = route_peer_message(
            self.handle,
            session_id=f"swarm-{self.handle.swarm_id}-claude-1-to-claude-2",
            text="hey",
        )
        self.assertIn("deadlock", result.lower())
        self.assertIn("claude-2", result)

    def test_route_peer_timeout(self):
        """Target never responds — timeout after MAX_PEER_POLL_SECS."""
        # Monkey-patch MAX_PEER_POLL_SECS to 1 for test speed
        import swarm as swarm_mod
        orig = swarm_mod.MAX_PEER_POLL_SECS
        swarm_mod.MAX_PEER_POLL_SECS = 1
        try:
            result = route_peer_message(
                self.handle,
                session_id=f"swarm-{self.handle.swarm_id}-claude-1-to-claude-3",
                text="hello?",
            )
            self.assertIn("did not respond", result)
        finally:
            swarm_mod.MAX_PEER_POLL_SECS = orig

    def test_route_peer_invalid_session_id(self):
        """Malformed session_id returns error."""
        result = route_peer_message(
            self.handle,
            session_id="not-a-valid-id",
            text="hi",
        )
        self.assertIn("Error", result)

    def test_route_peer_unknown_target(self):
        """Target role not in swarm."""
        result = route_peer_message(
            self.handle,
            session_id=f"swarm-{self.handle.swarm_id}-claude-1-to-claude-99",
            text="hi",
        )
        self.assertIn("not found", result)

    def test_route_peer_target_killed(self):
        """Target session was killed mid-routing."""
        target_tid = self.handle.roles["claude-2"]
        self.b._poll_results[target_tid] = {
            "task_id": target_tid, "state": "killed",
        }

        result = route_peer_message(
            self.handle,
            session_id=f"swarm-{self.handle.swarm_id}-claude-1-to-claude-2",
            text="hey",
        )
        self.assertIn("killed", result.lower())

    def test_route_peer_clears_pending_on_exit(self):
        """Pending request is cleared after routing completes."""
        target_tid = self.handle.roles["claude-2"]
        self.b._poll_results[target_tid] = {
            "task_id": target_tid, "state": "done",
            "result": {"text": "ok"},
        }

        route_peer_message(
            self.handle,
            session_id=f"swarm-{self.handle.swarm_id}-claude-1-to-claude-2",
            text="hey",
        )
        # Pending set should be empty
        self.assertEqual(len(self.handle._pending_peer_requests), 0)


class TestSwarmIntegration(unittest.TestCase):
    """End-to-end: launch → coordinate → collect → teardown."""

    @classmethod
    def setUpClass(cls):
        cls._preseed_patcher = patch("swarm.preseed_trust",
                                      return_value={"key": f"{HOME}/DEV", "changed": []})
        cls._preseed_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls._preseed_patcher.stop()

    def test_full_lifecycle(self):
        b = make_mock_bridge()
        handle = launch("alfred", "integration test",
                         cwd=f"{HOME}/DEV", bridge=b)
        self.assertEqual(handle.state, "launched")

        coordinate(handle)
        self.assertEqual(handle.state, "working")

        # All sessions done
        for tid in handle.task_ids:
            b._poll_results[tid] = {"task_id": tid, "state": "done",
                                     "result": {"text": f"done {tid}"}}

        results = collect(handle, timeout_min=1)
        self.assertEqual(handle.state, "done")
        self.assertEqual(len(results), 3)
        for r in results.values():
            self.assertEqual(r["status"], "completed")

        teardown_result = teardown(handle)
        self.assertEqual(len(teardown_result["closed"]), 3)


if __name__ == "__main__":
    unittest.main()
