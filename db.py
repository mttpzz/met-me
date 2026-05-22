"""SQLite persistence layer.

Replaces the earlier in-memory chat history and JSON state file. All blocking
sqlite3 calls are dispatched to a worker thread to keep the asyncio loop free.

Tables:
  users              - one row per Telegram user, refreshed on every message
  messages           - full conversation log; history is reconstructed from this
  rock_bottom_scores - per-message 0..10 score from the rock-bottom tracker
  consents           - GDPR consent records, one row per (user_id, version)
  settings           - key/value store (e.g. active_model)
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    last_name  TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT    NOT NULL,
    model      TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id, id);

CREATE TABLE IF NOT EXISTS rock_bottom_scores (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    message_id INTEGER,
    score      REAL    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE INDEX IF NOT EXISTS idx_rock_bottom_user ON rock_bottom_scores(user_id, id);

CREATE TABLE IF NOT EXISTS consents (
    user_id     INTEGER NOT NULL,
    version     TEXT    NOT NULL,
    granted_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, version),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: we serialize access through asyncio.to_thread,
        # and sqlite3 itself is internally locked for writes.
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)

    # ---- Internal helpers ---------------------------------------------------

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    async def _run(self, fn, *args, **kwargs) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ---- Users --------------------------------------------------------------

    def _upsert_user_sync(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        self._execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                updated_at = datetime('now')
            """,
            (user_id, username, first_name, last_name),
        )

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        await self._run(self._upsert_user_sync, user_id, username, first_name, last_name)

    def _list_users_sync(self) -> list[sqlite3.Row]:
        # LEFT JOIN so users with zero messages still appear (e.g. only did /start).
        return list(
            self._execute(
                """
                SELECT u.user_id,
                       u.username,
                       u.first_name,
                       u.last_name,
                       u.created_at,
                       COUNT(m.id) AS msg_count,
                       MAX(m.created_at) AS last_msg_at
                FROM users u
                LEFT JOIN messages m ON m.user_id = u.user_id
                GROUP BY u.user_id
                ORDER BY u.created_at
                """
            )
        )

    async def list_users(self) -> list[sqlite3.Row]:
        return await self._run(self._list_users_sync)

    # ---- Stats --------------------------------------------------------------

    def _get_stats_sync(self) -> dict:
        cur = self._execute
        total_users = cur("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        total_messages = cur("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        per_role = {
            r["role"]: r["n"]
            for r in cur("SELECT role, COUNT(*) AS n FROM messages GROUP BY role")
        }
        per_model = {
            (r["model"] or "unknown"): r["n"]
            for r in cur(
                "SELECT model, COUNT(*) AS n FROM messages GROUP BY model ORDER BY n DESC"
            )
        }
        top_users = list(
            cur(
                """
                SELECT u.user_id, u.username, u.first_name, COUNT(m.id) AS msg_count
                FROM users u
                LEFT JOIN messages m ON m.user_id = u.user_id
                GROUP BY u.user_id
                HAVING msg_count > 0
                ORDER BY msg_count DESC
                LIMIT 5
                """
            )
        )
        return {
            "total_users": total_users,
            "total_messages": total_messages,
            "per_role": per_role,
            "per_model": per_model,
            "top_users": top_users,
        }

    async def get_stats(self) -> dict:
        return await self._run(self._get_stats_sync)

    # ---- Messages -----------------------------------------------------------

    def _add_message_sync(self, user_id: int, role: str, content: str, model: str | None) -> int:
        cur = self._execute(
            "INSERT INTO messages (user_id, role, content, model) VALUES (?, ?, ?, ?)",
            (user_id, role, content, model),
        )
        return int(cur.lastrowid)

    async def add_message(self, user_id: int, role: str, content: str, model: str | None) -> int:
        return await self._run(self._add_message_sync, user_id, role, content, model)

    def _get_history_sync(self, user_id: int, max_turns: int) -> list[dict[str, str]]:
        # 1 turn = 1 user + 1 assistant message. Pull the most recent 2*max_turns,
        # newest first, then reverse to chronological order for the LLM.
        rows = self._execute(
            """
            SELECT role, content
            FROM messages
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, 2 * max_turns),
        ).fetchall()
        rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    async def get_history(self, user_id: int, max_turns: int) -> list[dict[str, str]]:
        return await self._run(self._get_history_sync, user_id, max_turns)

    def _clear_history_sync(self, user_id: int) -> int:
        # rock_bottom_scores.message_id FKs into messages.id, so wipe the children
        # first (no ON DELETE CASCADE on the existing schema). Reset is a fresh
        # start: orphan score entries would skew rolling averages, so drop them too.
        self._execute("DELETE FROM rock_bottom_scores WHERE user_id = ?", (user_id,))
        cur = self._execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        return cur.rowcount

    async def clear_history(self, user_id: int) -> int:
        return await self._run(self._clear_history_sync, user_id)

    def _delete_user_sync(self, user_id: int) -> dict[str, int]:
        # Children first (no ON DELETE CASCADE on the existing schema), then the
        # user row itself. Returns counts so the caller can log what was wiped.
        rb = self._execute(
            "DELETE FROM rock_bottom_scores WHERE user_id = ?", (user_id,)
        ).rowcount
        msgs = self._execute(
            "DELETE FROM messages WHERE user_id = ?", (user_id,)
        ).rowcount
        cons = self._execute(
            "DELETE FROM consents WHERE user_id = ?", (user_id,)
        ).rowcount
        usr = self._execute(
            "DELETE FROM users WHERE user_id = ?", (user_id,)
        ).rowcount
        return {
            "users": usr,
            "messages": msgs,
            "rock_bottom_scores": rb,
            "consents": cons,
        }

    async def delete_user(self, user_id: int) -> dict[str, int]:
        return await self._run(self._delete_user_sync, user_id)

    # ---- Consent (GDPR art. 9 — special-category data) --------------------

    def _has_consent_sync(self, user_id: int, version: str) -> bool:
        row = self._execute(
            "SELECT 1 FROM consents WHERE user_id = ? AND version = ?",
            (user_id, version),
        ).fetchone()
        return row is not None

    async def has_consent(self, user_id: int, version: str) -> bool:
        return await self._run(self._has_consent_sync, user_id, version)

    def _record_consent_sync(self, user_id: int, version: str) -> None:
        # INSERT OR IGNORE so re-tapping the accept button is a no-op rather
        # than a constraint error. The original granted_at is preserved.
        self._execute(
            "INSERT OR IGNORE INTO consents (user_id, version) VALUES (?, ?)",
            (user_id, version),
        )

    async def record_consent(self, user_id: int, version: str) -> None:
        await self._run(self._record_consent_sync, user_id, version)

    # ---- Rock-bottom tracker -----------------------------------------------

    def _add_rock_bottom_score_sync(
        self, user_id: int, score: float, message_id: int | None
    ) -> None:
        self._execute(
            "INSERT INTO rock_bottom_scores (user_id, message_id, score) VALUES (?, ?, ?)",
            (user_id, message_id, float(score)),
        )

    async def add_rock_bottom_score(
        self, user_id: int, score: float, message_id: int | None = None
    ) -> None:
        await self._run(self._add_rock_bottom_score_sync, user_id, score, message_id)

    def _recent_rock_bottom_avg_sync(
        self, user_id: int, window: int
    ) -> tuple[float | None, int]:
        rows = self._execute(
            "SELECT score FROM rock_bottom_scores WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, window),
        ).fetchall()
        if not rows:
            return (None, 0)
        scores = [r["score"] for r in rows]
        return (sum(scores) / len(scores), len(scores))

    async def recent_rock_bottom_avg(
        self, user_id: int, window: int
    ) -> tuple[float | None, int]:
        """Return (avg, count) of the most recent `window` rock-bottom scores.
        avg is None when no scores exist yet."""
        return await self._run(self._recent_rock_bottom_avg_sync, user_id, window)

    def _lowest_recent_user_msgs_sync(
        self, user_id: int, window: int, limit: int
    ) -> list[sqlite3.Row]:
        # Pick the lowest-scoring entries within the rolling window, joining
        # back to the user message that produced each score so admins see what
        # the user actually wrote.
        return list(
            self._execute(
                """
                WITH recent AS (
                    SELECT id, message_id, score, created_at
                    FROM rock_bottom_scores
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                SELECT r.score, r.created_at, m.content
                FROM recent r
                LEFT JOIN messages m ON m.id = r.message_id
                WHERE m.content IS NOT NULL
                ORDER BY r.score ASC, r.id DESC
                LIMIT ?
                """,
                (user_id, window, limit),
            )
        )

    async def lowest_recent_user_msgs(
        self, user_id: int, window: int, limit: int
    ) -> list[sqlite3.Row]:
        return await self._run(self._lowest_recent_user_msgs_sync, user_id, window, limit)

    def _get_user_sync(self, user_id: int) -> sqlite3.Row | None:
        return self._execute(
            "SELECT user_id, username, first_name, last_name, created_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    async def get_user(self, user_id: int) -> sqlite3.Row | None:
        return await self._run(self._get_user_sync, user_id)

    # ---- Settings -----------------------------------------------------------

    def _get_setting_sync(self, key: str, default: str | None) -> str | None:
        row = self._execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        return await self._run(self._get_setting_sync, key, default)

    def get_setting_sync(self, key: str, default: str | None = None) -> str | None:
        """Synchronous variant for startup, before the event loop runs."""
        return self._get_setting_sync(key, default)

    def _set_setting_sync(self, key: str, value: str) -> None:
        self._execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    async def set_setting(self, key: str, value: str) -> None:
        await self._run(self._set_setting_sync, key, value)

    def set_setting_sync(self, key: str, value: str) -> None:
        self._set_setting_sync(key, value)

    # ---- Lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()
