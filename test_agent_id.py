"""Tests for agent_id schema migration + Ledger.register_session (Task 2, plan M2).

Covers:
- fresh schema carries agent_id
- legacy DBs (pre-agent_id) migrate; existing rows read back as 'legacy'
- cas_collect/insert paths accept and store agent_id
- sessions_by_agent filters correctly
- register_session wrapper: pinned delegation mapping (M2 fix):
  task_id=fresh uuid4, purpose->prompt column, cwd/workspace, timeout_min=module
  default, pane_id nullable (pane-less trivial herdr command still registers)
"""
import os
import sqlite3

import pytest
from ledger import Ledger, SCHEMA

HOME = os.path.expanduser("~")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger():
    """Fresh in-memory ledger with current schema."""
    db = Ledger(":memory:")
    db.init_schema()
    yield db
    db.close()


@pytest.fixture
def legacy_db_path(tmp_path):
    """A legacy DB file with the pre-agent_id schema + one existing row."""
    path = str(tmp_path / "legacy_ledger.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            herdr_pane_id TEXT,
            herdr_workspace_id TEXT,
            claude_session_id TEXT,
            prompt TEXT,
            cwd TEXT,
            created_at TEXT,
            timeout_min INTEGER DEFAULT 30,
            last_state TEXT,
            last_polled TEXT,
            pending_result BOOLEAN DEFAULT 0,
            collected_at TEXT,
            cached_result TEXT
        )
    """)
    conn.execute(
        """INSERT INTO tasks (task_id, herdr_pane_id, claude_session_id, prompt, cwd,
           created_at, timeout_min, last_state, pending_result)
           VALUES ('legacy-task-1', 'paneA', 'sessA', 'old prompt', '/tmp',
                   '2026-08-29T00:00:00+00:00', 30, 'done', 0)"""
    )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# Schema: agent_id column present
# ---------------------------------------------------------------------------

class TestAgentIdSchema:
    def test_fresh_schema_has_agent_id(self, ledger):
        cols = ledger.get_columns("tasks")
        assert "agent_id" in cols

    def test_legacy_db_gains_agent_id_and_rows_marked_legacy(self, legacy_db_path):
        db = Ledger(legacy_db_path)
        db.init_schema()
        cols = db.get_columns("tasks")
        assert "agent_id" in cols
        row = db.get_task("legacy-task-1")
        assert row is not None
        assert row["agent_id"] == "legacy"
        db.close()

    def test_backup_created_before_column_add(self, legacy_db_path, tmp_path):
        """init_schema on a legacy file DB must create a .pre-agentid-backup copy."""
        db = Ledger(legacy_db_path)
        db.init_schema()
        assert os.path.exists(legacy_db_path + ".pre-agentid-backup")
        # backup must still be readable and contain the legacy row pre-migration
        chk = sqlite3.connect(legacy_db_path + ".pre-agentid-backup")
        cols = [r[1] for r in chk.execute("PRAGMA table_info(tasks)")]
        assert "agent_id" not in cols
        n = chk.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert n == 1
        chk.close()
        db.close()


# ---------------------------------------------------------------------------
# insert_task / cas_collect carry agent_id
# ---------------------------------------------------------------------------

class TestAgentIdStorage:
    def test_insert_task_stores_agent_id(self, ledger):
        ledger.insert_task("t1", "pane1", "ws1", "sess1", "p", "/tmp",
                           30, agent_id="alfred")
        row = ledger.get_task("t1")
        assert row["agent_id"] == "alfred"

    def test_insert_task_defaults_agent_id_legacy(self, ledger):
        """Old positional callers (bridge.py) keep working; default 'legacy'."""
        ledger.insert_task("t2", "pane2", "ws2", "sess2", "p", "/tmp", 30)
        row = ledger.get_task("t2")
        assert row["agent_id"] == "legacy"

    def test_cas_collect_preserves_agent_id(self, ledger):
        ledger.insert_task("t3", "pane3", "ws3", "sess3", "p", "/tmp", 30,
                           agent_id="deep_researcher")
        n = ledger.cas_collect("t3", "2026-08-30T12:00:00+00:00", "result text")
        assert n == 1
        row = ledger.get_task("t3")
        assert row["agent_id"] == "deep_researcher"
        assert row["cached_result"] == "result text"


# ---------------------------------------------------------------------------
# sessions_by_agent
# ---------------------------------------------------------------------------

class TestSessionsByAgent:
    def test_filters_by_agent(self, ledger):
        ledger.insert_task("a1", "p1", "w1", "s1", "x", "/tmp", 30, agent_id="alfred")
        ledger.insert_task("a2", "p2", "w2", "s2", "x", "/tmp", 30, agent_id="alfred")
        ledger.insert_task("d1", "p3", "w3", "s3", "x", "/tmp", 30,
                           agent_id="deep_researcher")
        rows = ledger.sessions_by_agent("alfred")
        assert sorted(r["task_id"] for r in rows) == ["a1", "a2"]
        rows = ledger.sessions_by_agent("deep_researcher")
        assert [r["task_id"] for r in rows] == ["d1"]

    def test_unknown_agent_returns_empty(self, ledger):
        assert ledger.sessions_by_agent("nobody") == []

    def test_legacy_rows_queryable(self, legacy_db_path):
        db = Ledger(legacy_db_path)
        db.init_schema()
        rows = db.sessions_by_agent("legacy")
        assert [r["task_id"] for r in rows] == ["legacy-task-1"]
        db.close()


# ---------------------------------------------------------------------------
# register_session wrapper (M2 fix: pinned delegation mapping)
# ---------------------------------------------------------------------------

class TestRegisterSession:
    def test_creates_row_with_agent_id(self, ledger):
        tid = ledger.register_session(agent_id="default", pane_id="paneX",
                                      purpose="trivial herdr smoke")
        # fresh uuid4
        assert isinstance(tid, str) and len(tid) == 36 and tid.count("-") == 4
        row = ledger.get_task(tid)
        assert row["agent_id"] == "default"
        assert row["prompt"] == "trivial herdr smoke"   # purpose -> prompt column
        assert row["herdr_pane_id"] == "paneX"
        assert row["timeout_min"] == 30                  # module default

    def test_pane_id_nullable(self, ledger):
        """Pane-less trivial herdr command still registers (the D5 case)."""
        tid = ledger.register_session(agent_id="default", pane_id=None,
                                      purpose="pane-less command")
        row = ledger.get_task(tid)
        assert row["herdr_pane_id"] is None
        assert row["agent_id"] == "default"

    def test_explicit_cwd_and_timeout(self, ledger):
        tid = ledger.register_session(agent_id="alfred", pane_id="p",
                                      purpose="job", cwd=f"{HOME}/DEV/x",
                                      timeout_min=15)
        row = ledger.get_task(tid)
        assert row["cwd"] == f"{HOME}/DEV/x"
        assert row["timeout_min"] == 15

    def test_returns_distinct_ids(self, ledger):
        t1 = ledger.register_session(agent_id="a", pane_id=None, purpose="one")
        t2 = ledger.register_session(agent_id="a", pane_id=None, purpose="two")
        assert t1 != t2
