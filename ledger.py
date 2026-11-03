"""ledger.py — thin SQLite lookup table for bridge tasks.

Not a state machine — herdr owns state. This is a lookup table + cache.
Schema per the v4 plan with pending_result, collected_at, cached_result.
"""
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone


# Module default session timeout (minutes) — used by Ledger.register_session.
DEFAULT_TIMEOUT_MIN = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    herdr_pane_id TEXT,
    herdr_workspace_id TEXT,
    claude_session_id TEXT,
    agent_id TEXT DEFAULT 'legacy',
    prompt TEXT,
    cwd TEXT,
    created_at TEXT,
    timeout_min INTEGER DEFAULT 30,
    last_state TEXT,
    last_polled TEXT,
    pending_result BOOLEAN DEFAULT 0,
    collected_at TEXT,
    cached_result TEXT,
    mcp_config_path TEXT DEFAULT NULL,
    mcp_target TEXT DEFAULT NULL,
    mcp_session_prefix TEXT DEFAULT NULL,
    notified_at TEXT DEFAULT NULL,
    notified_state TEXT DEFAULT NULL
);
"""


class Ledger:
    """Thin SQLite lookup table for bridge tasks."""

    def __init__(self, db_path=None):
        self.db_path = db_path or _default_db_path()
        self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # 5 s busy_timeout: the timer, the watcher and guest agents all
            # write this one file, and a bare connect means Python's implicit
            # default — a `database is locked` raised into code that does not
            # handle it (2026-11-03 review, finding 11).
            self._conn = sqlite3.connect(self.db_path, timeout=5.0)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def init_schema(self):
        """Create the tasks table if it doesn't exist.

        If a legacy (pre-agent_id) table exists, back up the file first
        (critic-catch: backup before ALTER), then ALTER the column in.
        If MCP columns are missing, ALTER them in (backup-first)."""
        if self.table_exists("tasks") and "agent_id" not in self.get_columns("tasks"):
            self._backup_and_add_agent_id()
        if self.table_exists("tasks") and "mcp_config_path" not in self.get_columns("tasks"):
            self._backup_and_add_mcp_columns()
        if self.table_exists("tasks") and "notified_at" not in self.get_columns("tasks"):
            self._backup_and_add_notified_columns()
        # WAL: readers (the watcher, a poll) no longer block the writer (the
        # timer). `:memory:` reports "memory" and ignores the request.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def _backup_and_add_agent_id(self):
        """Add agent_id to a legacy table; back up the DB file first."""
        if self.db_path != ":memory:":
            backup = f"{self.db_path}.pre-agentid-backup"
            if not os.path.exists(backup):
                shutil.copy2(self.db_path, backup)
        self.conn.execute(
            "ALTER TABLE tasks ADD COLUMN agent_id TEXT DEFAULT 'legacy'"
        )
        self.conn.commit()

    def _backup_and_add_mcp_columns(self):
        """Add MCP pairing columns; back up the DB file first."""
        if self.db_path != ":memory:":
            backup = f"{self.db_path}.pre-mcp-backup"
            if not os.path.exists(backup):
                shutil.copy2(self.db_path, backup)
        for col in ("mcp_config_path", "mcp_target", "mcp_session_prefix"):
            self.conn.execute(
                f"ALTER TABLE tasks ADD COLUMN {col} TEXT DEFAULT NULL"
            )
        self.conn.commit()

    def _backup_and_add_notified_columns(self):
        """Add the watcher columns (notified_at / notified_state); back up first."""
        if self.db_path != ":memory:":
            backup = f"{self.db_path}.pre-notify-backup"
            if not os.path.exists(backup):
                shutil.copy2(self.db_path, backup)
        for col in ("notified_at", "notified_state"):
            self.conn.execute(
                f"ALTER TABLE tasks ADD COLUMN {col} TEXT DEFAULT NULL"
            )
        self.conn.commit()

    def table_exists(self, name):
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def get_columns(self, table):
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [r["name"] for r in rows]

    def needs_migration(self):
        """Check if the table exists but is missing v4 columns."""
        if not self.table_exists("tasks"):
            return False  # fresh db, no migration needed
        cols = self.get_columns("tasks")
        required = {"pending_result", "collected_at", "cached_result", "herdr_pane_id"}
        return not required.issubset(set(cols))

    def migrate(self):
        """Archive old data and create fresh schema. Returns archive path or None."""
        if not self.needs_migration():
            self.init_schema()
            return None
        # Archive old table
        archive_path = f"{self.db_path}.archive-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        self.conn.execute(f"ATTACH DATABASE '{archive_path}' AS archive")
        self.conn.execute("CREATE TABLE archive.tasks AS SELECT * FROM tasks")
        self.conn.execute("DROP TABLE tasks")
        self.conn.execute("DETACH DATABASE archive")
        self.init_schema()
        self.conn.commit()
        return archive_path

    def insert_task(self, task_id, pane_id, workspace_id, session_id,
                    prompt, cwd, timeout_min, pending_result=False,
                    agent_id="legacy",
                    mcp_config_path=None, mcp_target=None,
                    mcp_session_prefix=None):
        """Insert a new task row. agent_id tags the originating agent
        ('legacy' for pre-multi-agent rows). MCP columns are NULL
        for non-paired sessions."""
        self.conn.execute(
            """INSERT INTO tasks 
               (task_id, herdr_pane_id, herdr_workspace_id, claude_session_id,
                agent_id, prompt, cwd, created_at, timeout_min, last_state,
                last_polled, pending_result, collected_at, cached_result,
                mcp_config_path, mcp_target, mcp_session_prefix)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)""",
            (task_id, pane_id, workspace_id, session_id,
             agent_id,
             prompt, cwd, datetime.now(timezone.utc).isoformat(),
             timeout_min, "idle", None, 1 if pending_result else 0,
             mcp_config_path, mcp_target, mcp_session_prefix)
        )
        self.conn.commit()

    def get_task(self, task_id):
        """Get a single task row, or None."""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_all(self):
        """Get all task rows."""
        rows = self.conn.execute("SELECT * FROM tasks").fetchall()
        return [dict(r) for r in rows]

    #: `last_state` values that mean the session is finished and has nothing
    #: left to announce.
    TERMINAL_STATES = ("done", "blocked", "lost", "collected")

    def list_unsettled(self):
        """Rows a periodic watcher still needs to look at.

        A row is settled once it is in a terminal state *and* that state has
        already been announced: there is nothing left to learn about it, and
        re-polling it forever is what made each watcher tick grow with the
        ledger's whole history (2026-11-03 review, finding 9). A new prompt
        resets `last_state` to `working`, which makes the row unsettled again.

        `killed` is settled unconditionally: the watcher never announces it
        (a deliberate kill has nothing to report), so waiting for a
        `notified_state` that will never be written would poll it forever.
        """
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE last_state != 'killed' AND NOT ("
            "  notified_state IS NOT NULL"
            "  AND notified_state = last_state"
            "  AND last_state IN ({})"
            ")".format(", ".join("?" for _ in self.TERMINAL_STATES)),
            self.TERMINAL_STATES,
        ).fetchall()
        return [dict(r) for r in rows]

    def update_last_state(self, task_id, state):
        """Update last_state and last_polled timestamp."""
        self.conn.execute(
            "UPDATE tasks SET last_state=?, last_polled=? WHERE task_id=?",
            (state, datetime.now(timezone.utc).isoformat(), task_id)
        )
        self.conn.commit()

    def update_prompt_sent(self, task_id):
        """Mark that a prompt was sent: set last_state=working + pending_result=true."""
        self.conn.execute(
            "UPDATE tasks SET last_state='working', pending_result=1, collected_at=NULL WHERE task_id=?",
            (task_id,)
        )
        self.conn.commit()

    def update_send_result(self, task_id, prompt, session_id):
        """Record what the handshake resolved: the wire prompt and the session id.

        The row itself is written right after `agent_start` (before the
        readiness loop) so a failed dispatch never leaves an unregistered pane;
        these two values are only known once the pane is ready.
        """
        self.conn.execute(
            "UPDATE tasks SET prompt=?, claude_session_id=? WHERE task_id=?",
            (prompt or "", session_id or "", task_id)
        )
        self.conn.commit()

    def reset_for_prompt(self, task_id):
        """Reset collection state for a new prompt (multi-turn)."""
        self.conn.execute(
            "UPDATE tasks SET last_state='working', pending_result=1, collected_at=NULL, cached_result=NULL WHERE task_id=?",
            (task_id,)
        )
        self.conn.commit()

    def clear_mcp_path(self, task_id):
        """Clear the MCP config path for a task (best-effort cleanup marker)."""
        self.conn.execute(
            "UPDATE tasks SET mcp_config_path = NULL WHERE task_id = ?",
            (task_id,),
        )
        self.conn.commit()

    def mark_notified(self, task_id, state, at=None):
        """Record that the watcher announced this task's terminal state.

        Keyed on state as well as time: a task that was announced `blocked`
        and later reaches `done` is announced again instead of being
        swallowed by the first announcement.
        """
        self.conn.execute(
            "UPDATE tasks SET notified_at=?, notified_state=? WHERE task_id=?",
            (at or datetime.now(timezone.utc).isoformat(), state, task_id),
        )
        self.conn.commit()

    def kill_task(self, task_id):
        """Mark task as killed."""
        self.conn.execute(
            "UPDATE tasks SET last_state='killed', pending_result=0 WHERE task_id=?",
            (task_id,)
        )
        self.conn.commit()

    def cas_collect(self, task_id, collected_at, cached_result, agent_id=None):
        """Compare-and-set: only collect if collected_at is still NULL.
        
        Returns number of rows affected (1=success, 0=already collected).
        The row's agent_id is preserved through collection; passing agent_id
        explicitly (re)attributes the row at collect time.
        """
        if agent_id is not None:
            cursor = self.conn.execute(
                """UPDATE tasks 
                   SET collected_at=?, cached_result=?, pending_result=0, agent_id=? 
                   WHERE task_id=? AND collected_at IS NULL""",
                (collected_at, cached_result, agent_id, task_id)
            )
        else:
            cursor = self.conn.execute(
                """UPDATE tasks 
                   SET collected_at=?, cached_result=?, pending_result=0 
                   WHERE task_id=? AND collected_at IS NULL""",
                (collected_at, cached_result, task_id)
            )
        self.conn.commit()
        return cursor.rowcount

    def sessions_by_agent(self, agent_id):
        """All task rows tagged with the given agent_id (newest first)."""
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE agent_id=? ORDER BY created_at DESC",
            (agent_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def register_session(self, agent_id, pane_id, purpose, cwd=None,
                         timeout_min=None):
        """Thin public wrapper (B3/M2): register a herdr session in the ledger
        under a caller's agent identity. Delegates to insert_task.

        Delegation mapping (pinned, M2 fix):
          - task_id: fresh uuid4
          - purpose -> prompt column
          - cwd: caller's cwd (os.getcwd()) unless passed
          - timeout_min: module default (DEFAULT_TIMEOUT_MIN=30) unless passed
          - pane_id: nullable — a pane-less trivial herdr command still registers

        Returns the new task_id.
        """
        task_id = str(uuid.uuid4())
        self.insert_task(
            task_id,
            pane_id,
            workspace_id=None,
            session_id=None,
            prompt=purpose,
            cwd=cwd if cwd is not None else os.getcwd(),
            timeout_min=timeout_min if timeout_min is not None else DEFAULT_TIMEOUT_MIN,
            agent_id=agent_id,
        )
        return task_id

    def get_by_pane_id(self, pane_id):
        """Find a task by herdr pane_id."""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE herdr_pane_id=?", (pane_id,)
        ).fetchone()
        return dict(row) if row else None

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


def _default_db_path():
    return os.path.expanduser("~/.local/share/claude_bridge/ledger.db")