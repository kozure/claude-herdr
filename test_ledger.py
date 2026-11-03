"""Tests for ledger.py — thin SQLite lookup table for bridge tasks.

Tests verify: schema creation, CRUD operations, state updates,
collection guard (compare-and-set), and migration from old schema.
"""
import os
import tempfile
import pytest
from ledger import Ledger


@pytest.fixture
def ledger():
    """Create a fresh in-memory ledger for each test."""
    db = Ledger(":memory:")
    db.init_schema()
    yield db
    db.close()


class TestSchema:
    def test_creates_table(self, ledger):
        assert ledger.table_exists("tasks")

    def test_pending_result_column_exists(self, ledger):
        cols = ledger.get_columns("tasks")
        assert "pending_result" in cols

    def test_collected_at_column_exists(self, ledger):
        cols = ledger.get_columns("tasks")
        assert "collected_at" in cols

    def test_cached_result_column_exists(self, ledger):
        cols = ledger.get_columns("tasks")
        assert "cached_result" in cols


class TestInsertAndRetrieve:
    def test_insert_and_get(self, ledger):
        ledger.insert_task(
            task_id="t1", pane_id="w1:p1", workspace_id="w1",
            session_id="sess-1", prompt="hello", cwd="/tmp",
            timeout_min=30
        )
        row = ledger.get_task("t1")
        assert row is not None
        assert row["task_id"] == "t1"
        assert row["herdr_pane_id"] == "w1:p1"
        assert row["prompt"] == "hello"
        assert row["last_state"] == "idle"
        assert row["pending_result"] == 0  # default false

    def test_insert_with_prompt_sets_pending(self, ledger):
        ledger.insert_task(
            task_id="t1", pane_id="w1:p1", workspace_id="w1",
            session_id="sess-1", prompt="hello", cwd="/tmp",
            timeout_min=30, pending_result=True
        )
        row = ledger.get_task("t1")
        assert row["pending_result"] == 1

    def test_get_nonexistent(self, ledger):
        assert ledger.get_task("nonexistent") is None

    def test_list_all(self, ledger):
        for i in range(3):
            ledger.insert_task(
                task_id=f"t{i}", pane_id=f"w1:p{i}", workspace_id="w1",
                session_id=f"sess-{i}", prompt=f"p{i}", cwd="/tmp",
                timeout_min=30
            )
        rows = ledger.list_all()
        assert len(rows) == 3


class TestStateUpdates:
    def test_update_last_state(self, ledger):
        ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30)
        ledger.update_last_state("t1", "working")
        row = ledger.get_task("t1")
        assert row["last_state"] == "working"

    def test_update_last_state_and_pending(self, ledger):
        ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30)
        ledger.update_prompt_sent("t1")  # sets last_state=working + pending_result=true
        row = ledger.get_task("t1")
        assert row["last_state"] == "working"
        assert row["pending_result"] == 1

    def test_kill_sets_killed(self, ledger):
        ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30)
        ledger.kill_task("t1")
        row = ledger.get_task("t1")
        assert row["last_state"] == "killed"
        assert row["pending_result"] == 0


class TestCollectionGuard:
    """The compare-and-set on collected_at prevents double collection."""

    def test_cas_collect_success(self, ledger):
        """First collection succeeds — collected_at was null, now set."""
        ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        affected = ledger.cas_collect("t1", collected_at="2026-01-01T00:00:00Z", cached_result="the result")
        assert affected == 1  # one row updated
        row = ledger.get_task("t1")
        assert row["collected_at"] == "2026-01-01T00:00:00Z"
        assert row["cached_result"] == "the result"
        assert row["pending_result"] == 0  # cleared

    def test_cas_collect_already_collected(self, ledger):
        """Second collection fails — collected_at already set."""
        ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        # First collect
        ledger.cas_collect("t1", "time1", "result1")
        # Second collect attempt
        affected = ledger.cas_collect("t1", "time2", "result2")
        assert affected == 0  # no rows updated — already collected
        row = ledger.get_task("t1")
        assert row["collected_at"] == "time1"  # unchanged
        assert row["cached_result"] == "result1"  # unchanged

    def test_cas_clears_pending_result(self, ledger):
        """Collection clears pending_result flag."""
        ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        ledger.cas_collect("t1", "time1", "result1")
        row = ledger.get_task("t1")
        assert row["pending_result"] == 0


class TestResetForPrompt:
    """prompt() resets collected_at and sets pending_result for new collection."""

    def test_reset_clears_collected(self, ledger):
        ledger.insert_task("t1", "w1:p1", "w1", "s1", "p", "/tmp", 30, pending_result=True)
        ledger.cas_collect("t1", "time1", "result1")
        # Now reset for new prompt
        ledger.reset_for_prompt("t1")
        row = ledger.get_task("t1")
        assert row["collected_at"] is None
        assert row["pending_result"] == 1
        assert row["last_state"] == "working"


class TestMigration:
    def test_detects_old_schema(self, tmp_path):
        """Old schema without pending_result should be detected."""
        import sqlite3
        db_path = str(tmp_path / "old_ledger.db")
        # Create old-style schema
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            pane_id TEXT,
            state TEXT,
            created_at TEXT
        )""")
        conn.execute("INSERT INTO tasks VALUES ('old1', 'w1:p1', 'done', '2026-01-01')")
        conn.commit()
        conn.close()

        # Now open with new Ledger — should detect old schema
        ledger = Ledger(db_path)
        assert ledger.needs_migration()
        ledger.close()

    def test_migration_archives_and_recreates(self, tmp_path):
        """Migration should archive old data and create fresh schema."""
        import sqlite3
        db_path = str(tmp_path / "old_ledger.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            pane_id TEXT,
            state TEXT,
            created_at TEXT
        )""")
        conn.execute("INSERT INTO tasks VALUES ('old1', 'w1:p1', 'done', '2026-01-01')")
        conn.commit()
        conn.close()

        ledger = Ledger(db_path)
        archive_path = ledger.migrate()
        assert archive_path is not None
        assert os.path.exists(archive_path)
        assert not ledger.needs_migration()
        # New schema is empty
        assert ledger.get_task("old1") is None
        # But old data is in archive
        archive_conn = sqlite3.connect(archive_path)
        row = archive_conn.execute("SELECT * FROM tasks WHERE task_id='old1'").fetchone()
        assert row is not None
        archive_conn.close()
        ledger.close()

# ---------------------------------------------------------------------------
# Task 6 / D5 find: Ledger must create its parent directory (the default
# path ~/.local/share/claude_bridge/ does not exist on a fresh machine).
# ---------------------------------------------------------------------------

def test_ledger_creates_missing_parent_dir(tmp_path):
    from ledger import Ledger
    db = tmp_path / "deep" / "nested" / "ledger.db"
    led = Ledger(str(db))
    led.init_schema()                      # must not raise
    assert db.exists()


# ---------------------------------------------------------------------------
# 2026-11-03 review, finding 11 + finding 9 (settled rows)
# ---------------------------------------------------------------------------

def test_file_ledger_uses_wal(tmp_path):
    """The timer, the watcher and guest agents share one file."""
    db = Ledger(str(tmp_path / "ledger.db"))
    db.init_schema()
    mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    db.close()
    assert mode.lower() == "wal"


def test_connect_waits_on_a_locked_database(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.init_schema()
    busy = db.conn.execute("PRAGMA busy_timeout").fetchone()[0]
    db.close()
    assert busy >= 5000


def _row(db, task_id, last_state, collected=None, notified=None):
    db.insert_task(task_id=task_id, pane_id="p1", workspace_id="w1", session_id="s",
                   prompt="p", cwd="/tmp", timeout_min=30)
    db.update_last_state(task_id, last_state)
    if collected:
        db.conn.execute("UPDATE tasks SET collected_at = ? WHERE task_id = ?",
                        (collected, task_id))
    if notified:
        db.mark_notified(task_id, notified)
    db.conn.commit()


def test_list_unsettled_skips_announced_terminal_rows(ledger):
    _row(ledger, "t-settled", "done", collected="2026-11-03T00:00:00+00:00", notified="done")
    _row(ledger, "t-working", "working")
    _row(ledger, "t-unannounced-done", "done", collected="2026-11-03T00:00:00+00:00")
    ids = {r["task_id"] for r in ledger.list_unsettled()}
    assert "t-settled" not in ids
    assert {"t-working", "t-unannounced-done"} <= ids


def test_list_unsettled_skips_announced_lost_and_any_killed(ledger):
    """`lost` settles on announcement; `killed` never announces at all."""
    _row(ledger, "t-lost", "lost", notified="lost")
    _row(ledger, "t-killed", "killed")
    _row(ledger, "t-live-lost", "lost")            # not yet announced: keep it
    ids = {r["task_id"] for r in ledger.list_unsettled()}
    assert "t-lost" not in ids
    assert "t-killed" not in ids
    assert "t-live-lost" in ids


def test_list_all_still_returns_everything(ledger):
    _row(ledger, "t-settled", "done", collected="2026-11-03T00:00:00+00:00", notified="done")
    assert {r["task_id"] for r in ledger.list_all()} == {"t-settled"}
