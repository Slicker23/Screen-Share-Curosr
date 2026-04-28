"""SQLite state for the bridge.

Stores:
- conversations: conversation_id -> last seen project root, last activity time
- followup_queue: pending phone -> Cursor messages, FIFO per conversation
- allowlist: user-added shell command regex patterns (extends config patterns)
- approval_log: audit trail (every approval request + decision)

In-memory:
- pending_approvals: approval_id -> asyncio.Future (lives in Bridge, not here)
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    project_root    TEXT,
    last_seen_ts    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS followup_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    message         TEXT NOT NULL,
    created_ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_followup_conv ON followup_queue(conversation_id, id);

CREATE TABLE IF NOT EXISTS allowlist (
    pattern    TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,         -- 'allow' or 'deny'
    created_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    tool            TEXT NOT NULL,
    payload         TEXT NOT NULL,    -- JSON
    decision        TEXT,             -- 'allow' / 'deny' / 'timeout'
    decided_by      TEXT,             -- 'phone' / 'allowlist' / 'denylist' / 'timeout'
    created_ts      REAL NOT NULL,
    decided_ts      REAL
);

-- Latest todos snapshot per Cursor plan file. Cursor writes plans to
-- ~/.cursor/plans/<slug>.plan.md whenever the agent updates step status.
-- We poll the directory and diff against this snapshot to drive Telegram
-- notifications. plan_id == file basename without ".plan.md".
CREATE TABLE IF NOT EXISTS plan_state (
    plan_id         TEXT PRIMARY KEY,
    plan_name       TEXT,
    plan_path       TEXT NOT NULL,
    todos_json      TEXT NOT NULL,    -- JSON array
    file_mtime      REAL NOT NULL,
    updated_ts      REAL NOT NULL
);
"""


class State:
    def __init__(self, path: Path):
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def _cur(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    # --- conversations ---

    def touch_conversation(self, conversation_id: str, project_root: Optional[str]) -> None:
        with self._cur() as c:
            c.execute(
                """INSERT INTO conversations(conversation_id, project_root, last_seen_ts)
                   VALUES (?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                       project_root = COALESCE(excluded.project_root, conversations.project_root),
                       last_seen_ts = excluded.last_seen_ts""",
                (conversation_id, project_root, time.time()),
            )

    def most_recent_conversation(self) -> Optional[tuple[str, Optional[str]]]:
        with self._cur() as c:
            row = c.execute(
                "SELECT conversation_id, project_root FROM conversations ORDER BY last_seen_ts DESC LIMIT 1"
            ).fetchone()
        return (row[0], row[1]) if row else None

    def list_conversations(self, limit: int = 10) -> list[tuple[str, Optional[str], float]]:
        with self._cur() as c:
            return c.execute(
                "SELECT conversation_id, project_root, last_seen_ts FROM conversations ORDER BY last_seen_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()

    # --- followup queue ---

    def enqueue_followup(self, conversation_id: str, message: str) -> int:
        """Append to the queue. Returns 0 if it's an exact duplicate of the most
        recent pending message for this conversation (dedup to handle the user
        retrying the same prompt while Cursor is idle)."""
        with self._cur() as c:
            row = c.execute(
                "SELECT id, message FROM followup_queue WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if row and row[1] == message:
                return 0
            c.execute(
                "INSERT INTO followup_queue(conversation_id, message, created_ts) VALUES (?, ?, ?)",
                (conversation_id, message, time.time()),
            )
            return c.lastrowid or 0

    def pop_followup(self, conversation_id: str) -> Optional[str]:
        with self._cur() as c:
            row = c.execute(
                "SELECT id, message FROM followup_queue WHERE conversation_id = ? ORDER BY id LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if not row:
                return None
            c.execute("DELETE FROM followup_queue WHERE id = ?", (row[0],))
            return row[1]

    def queue_depth(self, conversation_id: str) -> int:
        with self._cur() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM followup_queue WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return row[0] if row else 0

    def list_queued(self, limit: int = 50) -> list[tuple[int, str, str, float]]:
        with self._cur() as c:
            return c.execute(
                "SELECT id, conversation_id, message, created_ts FROM followup_queue ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def clear_queue(self, conversation_id: Optional[str] = None) -> int:
        with self._cur() as c:
            if conversation_id is None:
                c.execute("DELETE FROM followup_queue")
            else:
                c.execute("DELETE FROM followup_queue WHERE conversation_id = ?", (conversation_id,))
            return c.rowcount

    # --- allowlist / denylist ---

    def add_pattern(self, pattern: str, kind: str) -> None:
        assert kind in ("allow", "deny")
        with self._cur() as c:
            c.execute(
                """INSERT INTO allowlist(pattern, kind, created_ts) VALUES (?, ?, ?)
                   ON CONFLICT(pattern) DO UPDATE SET kind = excluded.kind""",
                (pattern, kind, time.time()),
            )

    def remove_pattern(self, pattern: str) -> bool:
        with self._cur() as c:
            c.execute("DELETE FROM allowlist WHERE pattern = ?", (pattern,))
            return c.rowcount > 0

    def list_patterns(self) -> list[tuple[str, str]]:
        with self._cur() as c:
            return c.execute("SELECT pattern, kind FROM allowlist ORDER BY kind, pattern").fetchall()

    # --- approval log ---

    def log_approval_request(self, conversation_id: Optional[str], tool: str, payload_json: str) -> int:
        with self._cur() as c:
            c.execute(
                """INSERT INTO approval_log(conversation_id, tool, payload, created_ts)
                   VALUES (?, ?, ?, ?)""",
                (conversation_id, tool, payload_json, time.time()),
            )
            return c.lastrowid or 0

    def log_approval_decision(self, approval_id: int, decision: str, decided_by: str) -> None:
        with self._cur() as c:
            c.execute(
                "UPDATE approval_log SET decision = ?, decided_by = ?, decided_ts = ? WHERE id = ?",
                (decision, decided_by, time.time(), approval_id),
            )

    # --- plan state (Cursor plan-file snapshots) ---

    def get_plan(self, plan_id: str) -> Optional[tuple[str, str, float]]:
        """Returns (todos_json, plan_path, file_mtime) or None."""
        with self._cur() as c:
            row = c.execute(
                "SELECT todos_json, plan_path, file_mtime FROM plan_state WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def set_plan(self, plan_id: str, plan_name: str, plan_path: str,
                 todos_json: str, file_mtime: float) -> None:
        with self._cur() as c:
            c.execute(
                """INSERT INTO plan_state(plan_id, plan_name, plan_path,
                                          todos_json, file_mtime, updated_ts)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(plan_id) DO UPDATE SET
                       plan_name  = excluded.plan_name,
                       plan_path  = excluded.plan_path,
                       todos_json = excluded.todos_json,
                       file_mtime = excluded.file_mtime,
                       updated_ts = excluded.updated_ts""",
                (plan_id, plan_name, plan_path, todos_json, file_mtime, time.time()),
            )

    def list_plans(self) -> list[tuple[str, str, str, float]]:
        """Returns [(plan_id, plan_name, plan_path, updated_ts), ...] newest first."""
        with self._cur() as c:
            return [(r[0], r[1], r[2], r[3]) for r in c.execute(
                "SELECT plan_id, plan_name, plan_path, updated_ts FROM plan_state "
                "ORDER BY updated_ts DESC"
            )]

    def close(self) -> None:
        self._conn.close()
