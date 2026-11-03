"""Tests for mcp_pair.py — MCP config generation + validation."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest

from mcp_pair import (
    PairingConfig,
    PairingError,
    pairing_config,
    write_config,
    cleanup_config,
    QWEN_PYTHON,
    SERVER_SCRIPT,
)


class TestPairingConfig(unittest.TestCase):
    """Pure tests for pairing_config() — no filesystem, no herdr."""

    def test_resolution_explicit_wins(self):
        """Explicit target_agent overrides caller_agent_id."""
        cfg = pairing_config(
            caller_agent_id="alfred",
            target_agent="default",
            task_id="task123",
        )
        self.assertEqual(cfg.target_agent, "default")
        self.assertIn("default", cfg.allowlist)

    def test_resolution_falls_back_to_caller(self):
        """Without explicit target, caller_agent_id is used."""
        cfg = pairing_config(caller_agent_id="alfred", task_id="task123")
        self.assertEqual(cfg.target_agent, "alfred")
        self.assertIn("alfred", cfg.allowlist)

    def test_resolution_default_when_none(self):
        """None caller + None target → 'default'."""
        cfg = pairing_config(caller_agent_id="", task_id="task123")
        self.assertEqual(cfg.target_agent, "default")

    def test_allowlist_passed_through(self):
        """Allowlist is passed through to config — no auto-include magic."""
        cfg = pairing_config(
            caller_agent_id="alfred",
            allowlist="alfred,default",
            task_id="task123",
        )
        self.assertIn("alfred", cfg.allowlist.split(","))
        self.assertIn("default", cfg.allowlist.split(","))

    def test_allowlist_refuses_outsider(self):
        """A target outside the allowlist raises PairingError."""
        with self.assertRaises(PairingError) as ctx:
            pairing_config(
                caller_agent_id="critic_agent",
                allowlist="default,alfred",
                task_id="task123",
            )
        self.assertIn("critic_agent", str(ctx.exception))
        self.assertIn("allowlist", str(ctx.exception))

    def test_session_prefix_with_task_id(self):
        """Session prefix follows herdr convention."""
        cfg = pairing_config(
            caller_agent_id="alfred",
            task_id="task-abc123",
            depth=1,
        )
        self.assertEqual(cfg.session_prefix, "mcp-bridge-herdr-task-abc123-d1")

    def test_session_prefix_depth(self):
        """Depth marker increments correctly."""
        cfg = pairing_config(
            caller_agent_id="alfred",
            task_id="task-xyz",
            depth=3,
        )
        self.assertEqual(cfg.session_prefix, "mcp-bridge-herdr-task-xyz-d3")

    def test_session_prefix_without_task_id(self):
        """Without a task_id, falls back to mcp-bridge-{agent}."""
        cfg = pairing_config(caller_agent_id="alfred")
        self.assertEqual(cfg.session_prefix, "mcp-bridge-alfred")

    def test_config_json_structure(self):
        """Generated JSON matches the schema Claude Code expects."""
        cfg = pairing_config(
            caller_agent_id="alfred",
            task_id="task123",
        )
        self.assertIn("mcpServers", cfg.config_json)
        self.assertIn("qwenpaw-bridge", cfg.config_json["mcpServers"])
        server = cfg.config_json["mcpServers"]["qwenpaw-bridge"]
        self.assertEqual(server["command"], QWEN_PYTHON)
        self.assertIn(SERVER_SCRIPT, server["args"])
        self.assertIn("--agent", server["args"])
        self.assertIn("alfred", server["args"])
        self.assertIn("--session-prefix", server["args"])
        self.assertIn("mcp-bridge-herdr-task123-d1", server["args"])
        self.assertIn("--agents", server["args"])

    def test_config_json_no_placeholder_in_bridge_prefix(self):
        """Bridge-generated prefixes NEVER contain {agent}."""
        cfg = pairing_config(
            caller_agent_id="alfred",
            task_id="task123",
        )
        self.assertNotIn("{agent}", cfg.session_prefix)
        self.assertNotIn("{agent}", cfg.config_json["mcpServers"]["qwenpaw-bridge"]["args"][-1])


class TestWriteConfig(unittest.TestCase):
    """Filesystem tests for write_config() — uses real temp files."""

    def test_write_creates_file_with_correct_contents(self):
        cfg = pairing_config(caller_agent_id="alfred", task_id="task123")
        written = write_config(cfg)
        try:
            self.assertTrue(os.path.isfile(written.config_path))
            with open(written.config_path) as f:
                data = json.load(f)
            self.assertEqual(data, cfg.config_json)
        finally:
            cleanup_config(written.config_path)

    def test_write_file_has_0600_mode(self):
        cfg = pairing_config(caller_agent_id="alfred", task_id="task123")
        written = write_config(cfg)
        try:
            mode = stat.S_IMODE(os.stat(written.config_path).st_mode)
            self.assertEqual(mode, 0o600,
                             f"expected 0600, got {oct(mode)}")
        finally:
            cleanup_config(written.config_path)

    def test_config_path_is_set_after_write(self):
        cfg = pairing_config(caller_agent_id="alfred", task_id="task123")
        self.assertEqual(cfg.config_path, "")
        written = write_config(cfg)
        try:
            self.assertTrue(written.config_path)
            self.assertIn("herdr-mcp-", written.config_path)
            self.assertTrue(written.config_path.endswith(".json"))
        finally:
            cleanup_config(written.config_path)

    def test_write_preserves_immutability_of_input(self):
        """write_config returns a new object; input is unchanged."""
        cfg = pairing_config(caller_agent_id="alfred", task_id="task123")
        written = write_config(cfg)
        self.assertEqual(cfg.config_path, "")  # original unchanged
        cleanup_config(written.config_path)


class TestCleanupConfig(unittest.TestCase):
    """Tests for cleanup_config()."""

    def test_cleanup_removes_file(self):
        cfg = pairing_config(caller_agent_id="alfred", task_id="task123")
        written = write_config(cfg)
        path = written.config_path
        self.assertTrue(os.path.isfile(path))
        cleanup_config(path)
        self.assertFalse(os.path.isfile(path))

    def test_cleanup_idempotent(self):
        """Calling cleanup twice doesn't raise."""
        cfg = pairing_config(caller_agent_id="alfred", task_id="task123")
        written = write_config(cfg)
        path = written.config_path
        cleanup_config(path)
        cleanup_config(path)  # second call, file already gone
        self.assertFalse(os.path.isfile(path))

    def test_cleanup_nonexistent_noop(self):
        """Cleaning up a nonexistent file doesn't raise."""
        cleanup_config("/tmp/does-not-exist-herdr-mcp-xxxxxxxx.json")


if __name__ == "__main__":
    unittest.main()
