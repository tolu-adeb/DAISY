"""The desk's own small SQLite database (docs/22): pitches, the watchlist, news, sentiment, alerts, notes.

Kept apart from the bar store (``stocks.sqlite3``), which is large and rewritten every session. This one
is small, and everything in it has a history. One file: ``<data_dir>/stocks/desk.sqlite3``.

No secrets are ever written here: no keys, no webhooks, no tokens.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pitch (            -- one row per idea the desk pitched
  id TEXT PRIMARY KEY, symbol TEXT NOT NULL, book TEXT NOT NULL, as_of TEXT NOT NULL,
  status TEXT NOT NULL,                       -- new | active | closed
  rating TEXT, closed_at TEXT, close_reason TEXT, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pitch_symbol ON pitch(symbol, status);

CREATE TABLE IF NOT EXISTS history (          -- every status change, for pitches and watchlist names
  kind TEXT NOT NULL, ref TEXT NOT NULL, at TEXT NOT NULL, status TEXT NOT NULL, note TEXT
);
CREATE INDEX IF NOT EXISTS history_ref ON history(kind, ref);

CREATE TABLE IF NOT EXISTS watch (            -- the morning watchlist: one row per name, current state
  symbol TEXT PRIMARY KEY, status TEXT NOT NULL,   -- watching | triggered | in_play | exited | dropped
  added TEXT NOT NULL, updated TEXT NOT NULL, drank INTEGER, data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS screen (           -- the whole-market indicator snapshot from the last close
  symbol TEXT PRIMARY KEY, day TEXT NOT NULL, data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news (             -- headlines and filings, deduplicated by id
  id TEXT PRIMARY KEY, symbol TEXT, at TEXT NOT NULL, source TEXT, kind TEXT,
  title TEXT, url TEXT, sent REAL, rel REAL, data TEXT
);
CREATE INDEX IF NOT EXISTS news_symbol ON news(symbol, at);
CREATE INDEX IF NOT EXISTS news_at ON news(at);

CREATE TABLE IF NOT EXISTS sentiment (        -- daily score per name; symbol '*' is the whole market
  symbol TEXT NOT NULL, day TEXT NOT NULL, score REAL, n INTEGER NOT NULL DEFAULT 0, data TEXT,
  PRIMARY KEY (symbol, day)
);

CREATE TABLE IF NOT EXISTS event (            -- alerts already sent; the key makes posting idempotent
  key TEXT PRIMARY KEY, at TEXT NOT NULL, kind TEXT, symbol TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS event_at ON event(at);

CREATE TABLE IF NOT EXISTS note (             -- team notes and votes on a pitch (display name only)
  id INTEGER PRIMARY KEY AUTOINCREMENT, pitch_id TEXT NOT NULL, at TEXT NOT NULL,
  name TEXT NOT NULL, vote TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS note_pitch ON note(pitch_id);

CREATE TABLE IF NOT EXISTS holding (          -- the fund's recorded portfolio: real positions, logged by an admin
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, shares REAL NOT NULL, cost REAL NOT NULL,
  opened TEXT NOT NULL, note TEXT, pitch_id TEXT, status TEXT NOT NULL DEFAULT 'open',
  closed TEXT, exit_price REAL, updated TEXT
);

CREATE TABLE IF NOT EXISTS flow (             -- money put into or taken out of the fund (so returns are time-weighted)
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, amount REAL NOT NULL, note TEXT
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def now_iso() -> str:
    return datetime.now(NY).isoformat(timespec="seconds")


class DeskDB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA journal_size_limit=16777216")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._db.close()

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.rowcount

    def many(self, sql: str, rows: list[tuple]) -> int:
        if not rows:
            return 0
        with self._lock:
            cur = self._db.executemany(sql, rows)
            self._db.commit()
            return cur.rowcount

    # ------------------------------------------------------------------ small helpers
    def kv_get(self, k: str, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        if not r:
            return default
        try:
            return json.loads(r[0]["v"])
        except ValueError:
            return default

    def kv_set(self, k: str, v) -> None:
        self.x("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (k, json.dumps(v, default=str)))

    def log(self, kind: str, ref: str, status: str, note: str | None = None, at: str | None = None) -> None:
        self.x("INSERT INTO history(kind, ref, at, status, note) VALUES(?,?,?,?,?)",
               (kind, ref, at or now_iso(), status, note))

    def history(self, kind: str, ref: str) -> list[dict]:
        return [dict(r) for r in self.q("SELECT at, status, note FROM history WHERE kind=? AND ref=? ORDER BY rowid",
                                        (kind, ref))]
