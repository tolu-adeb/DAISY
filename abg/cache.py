"""Two-tier cache: in-process LRU (microseconds) in front of a SQLite file (milliseconds).

Entries are stored with their write time, not an expiry.  The *reader* decides what is
fresh by passing a TTL, which enables **stale-if-error**: when every provider fails,
the router asks for anything younger than ``max_stale`` and serves it flagged as stale
instead of erroring out.  The disk tier makes that work across restarts.

Values are pickled.  The cache directory is private to the user; do not point
``ABG_CACHE_DIR`` at a location other people can write to.
"""
from __future__ import annotations

import logging
import pickle
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class CacheHit:
    value: Any
    stored_at: float
    fresh: bool

    @property
    def age(self) -> float:
        return time.time() - self.stored_at


class MemoryCache:
    def __init__(self, max_entries: int = 512):
        self.max_entries = max_entries
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[float, Any] | None:
        with self._lock:
            item = self._data.get(key)
            if item is not None:
                self._data.move_to_end(key)
            return item

    def set(self, key: str, value: Any, stored_at: float | None = None) -> None:
        with self._lock:
            self._data[key] = (stored_at or time.time(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def delete_prefix(self, prefix: str = "") -> int:
        with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                del self._data[k]
            return len(keys)

    def __len__(self) -> int:
        return len(self._data)


class DiskCache:
    """SQLite key/value store.  Any I/O or corruption error disables the tier instead of
    propagating - the cache must never be the reason a request fails."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=2.0)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, t REAL NOT NULL, v BLOB NOT NULL)")
            self._conn.commit()
        except Exception as e:  # pragma: no cover - environment dependent
            log.warning("disk cache disabled (%s)", e)
            self._conn = None

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def get(self, key: str) -> tuple[float, Any] | None:
        if not self._conn:
            return None
        try:
            with self._lock:
                row = self._conn.execute("SELECT t, v FROM kv WHERE k=?", (key,)).fetchone()
            if row is None:
                return None
            return row[0], pickle.loads(row[1])
        except Exception as e:
            log.debug("disk cache read failed for %s: %s", key, e)
            return None

    def set(self, key: str, value: Any, stored_at: float | None = None) -> None:
        if not self._conn:
            return
        try:
            blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            with self._lock:
                self._conn.execute("INSERT OR REPLACE INTO kv (k, t, v) VALUES (?, ?, ?)",
                                   (key, stored_at or time.time(), blob))
                self._conn.commit()
        except Exception as e:
            log.debug("disk cache write failed for %s: %s", key, e)

    def delete_prefix(self, prefix: str = "") -> int:
        if not self._conn:
            return 0
        with self._lock:
            cur = self._conn.execute("DELETE FROM kv WHERE k LIKE ?", (prefix.replace("%", "") + "%",))
            self._conn.commit()
            return cur.rowcount

    def stats(self) -> dict:
        if not self._conn:
            return {"enabled": False}
        with self._lock:
            n, size = self._conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(v)),0) FROM kv").fetchone()
        return {"enabled": True, "entries": n, "bytes": size, "path": str(self.path)}

    def close(self) -> None:
        if self._conn:
            with self._lock:
                self._conn.close()
            self._conn = None


class TieredCache:
    def __init__(self, cache_dir: Path | None = None, max_entries: int = 512, enabled: bool = True):
        self.enabled = enabled
        self.memory = MemoryCache(max_entries)
        self.disk = DiskCache(Path(cache_dir) / "cache.sqlite3") if (enabled and cache_dir) else None

    def get(self, key: str, ttl: float, max_stale: float = 0.0) -> CacheHit | None:
        """Return the entry if younger than ``max(ttl, max_stale)``; ``fresh`` says which."""
        if not self.enabled:
            return None
        item = self.memory.get(key)
        if item is None and self.disk is not None:
            item = self.disk.get(key)
            if item is not None:
                self.memory.set(key, item[1], stored_at=item[0])   # promote
        if item is None:
            return None
        stored_at, value = item
        age = time.time() - stored_at
        if age <= ttl:
            return CacheHit(value, stored_at, True)
        if age <= max_stale:
            return CacheHit(value, stored_at, False)
        return None

    def set(self, key: str, value: Any, persist: bool = True) -> None:
        if not self.enabled:
            return
        now = time.time()
        self.memory.set(key, value, now)
        if persist and self.disk is not None:
            self.disk.set(key, value, now)

    def clear(self, prefix: str = "") -> int:
        n = self.memory.delete_prefix(prefix)
        if self.disk is not None:
            n = max(n, self.disk.delete_prefix(prefix))
        return n

    def stats(self) -> dict:
        return {"enabled": self.enabled, "memory_entries": len(self.memory),
                "disk": self.disk.stats() if self.disk else {"enabled": False}}

    def close(self) -> None:
        if self.disk:
            self.disk.close()
