"""SQLite persistence for external trade ideas, their event timeline and ingestion cursors.

File: ``ABG_DATA_DIR/signals.sqlite3`` (next to portfolio.sqlite3).  Ideas are stored as a JSON
document plus indexed columns (status, symbol, source message id) so the schema can grow
without migrations.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .lifecycle import OPEN_STATES, Idea

SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
  id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL, symbol TEXT NOT NULL, source TEXT,
  channel_id TEXT, message_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ideas_status ON ideas(status, symbol);
CREATE UNIQUE INDEX IF NOT EXISTS ideas_msg ON ideas(channel_id, message_id) WHERE message_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, idea_id INTEGER NOT NULL, ts REAL NOT NULL, type TEXT NOT NULL,
  price REAL, title TEXT, text TEXT, data TEXT, relayed TEXT);
CREATE INDEX IF NOT EXISTS events_idea ON events(idea_id, ts);
CREATE TABLE IF NOT EXISTS messages (
  channel_id TEXT NOT NULL, message_id TEXT NOT NULL, ts REAL NOT NULL, author TEXT, text TEXT, outcome TEXT,
  idea_id INTEGER, PRIMARY KEY (channel_id, message_id));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class ExtSignalStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    @classmethod
    def from_settings(cls, settings) -> "ExtSignalStore":
        return cls(Path(settings.data_dir).expanduser() / "signals.sqlite3")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ ideas
    def save(self, idea: Idea) -> Idea:
        idea.updated_at = time.time()
        data = json.dumps(idea.to_dict(), default=str)
        with self._lock:
            if idea.id is None:
                cur = self._db.execute(
                    "INSERT INTO ideas (status, symbol, source, channel_id, message_id, created_at, updated_at, data) "
                    "VALUES (?,?,?,?,?,?,?,?)", (idea.status, idea.symbol, idea.source, idea.channel_id, idea.message_id,
                                                 idea.created_at, idea.updated_at, data))
                idea.id = cur.lastrowid
                self._db.execute("UPDATE ideas SET data=? WHERE id=?", (json.dumps(idea.to_dict(), default=str), idea.id))
            else:
                self._db.execute("UPDATE ideas SET status=?, symbol=?, updated_at=?, data=? WHERE id=?",
                                 (idea.status, idea.symbol, idea.updated_at, data, idea.id))
            self._db.commit()
        return idea

    def get(self, idea_id: int) -> Idea | None:
        with self._lock:
            r = self._db.execute("SELECT data FROM ideas WHERE id=?", (idea_id,)).fetchone()
        return Idea.from_dict(json.loads(r["data"])) if r else None

    def by_message(self, channel_id: str, message_id: str) -> Idea | None:
        with self._lock:
            r = self._db.execute("SELECT data FROM ideas WHERE channel_id=? AND message_id=?", (channel_id, message_id)).fetchone()
        return Idea.from_dict(json.loads(r["data"])) if r else None

    def ideas(self, status: set[str] | None = None, symbol: str | None = None, limit: int = 500) -> list[Idea]:
        q = "SELECT data FROM ideas WHERE 1=1"
        args: list = []
        if status:
            q += f" AND status IN ({','.join('?' * len(status))})"
            args += list(status)
        if symbol:
            q += " AND symbol=?"
            args.append(symbol)
        q += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [Idea.from_dict(json.loads(r["data"])) for r in rows]

    def open_ideas(self) -> list[Idea]:
        return self.ideas(OPEN_STATES)

    def latest_open_for(self, symbol: str, source_key: str | None = None) -> Idea | None:
        for i in self.ideas(OPEN_STATES, symbol=symbol, limit=20):
            if source_key is None or source_key in (i.author, i.channel_id):
                return i
        return None

    # ------------------------------------------------------------------ events
    def add_event(self, idea_id: int, ev: dict, title: str, text: str, data: dict | None = None) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO events (idea_id, ts, type, price, title, text, data, relayed) VALUES "
                                   "(?,?,?,?,?,?,?,?)", (idea_id, ev.get("ts", time.time()), ev["type"], ev.get("price"),
                                                         title, text, json.dumps(data or {}, default=str), "{}"))
            self._db.commit()
            return cur.lastrowid

    def mark_relayed(self, event_id: int, channel: str, status: str) -> None:
        with self._lock:
            r = self._db.execute("SELECT relayed FROM events WHERE id=?", (event_id,)).fetchone()
            if r is None:
                return
            d = json.loads(r["relayed"] or "{}")
            d[channel] = status
            self._db.execute("UPDATE events SET relayed=? WHERE id=?", (json.dumps(d), event_id))
            self._db.commit()

    def events(self, idea_id: int | None = None, limit: int = 200, since: float | None = None) -> list[dict]:
        q, args = "SELECT * FROM events WHERE 1=1", []
        if idea_id is not None:
            q += " AND idea_id=?"
            args.append(idea_id)
        if since is not None:
            q += " AND ts>?"
            args.append(since)
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"] or "{}")
            d["relayed"] = json.loads(d["relayed"] or "{}")
            out.append(d)
        return out

    # ------------------------------------------------------------------ ingestion bookkeeping
    def seen(self, channel_id: str, message_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM messages WHERE channel_id=? AND message_id=?",
                                    (channel_id, message_id)).fetchone() is not None

    def record_message(self, channel_id: str, message_id: str, author: str | None, text: str, outcome: str,
                       idea_id: int | None = None) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?)",
                             (channel_id, message_id, time.time(), author, text[:4000], outcome, idea_id))
            self._db.commit()

    def messages(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM messages ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()]

    def get_kv(self, key: str) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_kv(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))
            self._db.commit()
