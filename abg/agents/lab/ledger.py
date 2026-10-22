"""Trial ledger and proposals queue (SQLite, in the terminal's data dir).

Every hypothesis tested is recorded - winners and losers - so the number of tries behind any "winner" is always
visible (the more variants you test, the more likely the best one is luck).  Holdout looks are counted per
holdout window; proposals wait for a person to approve or reject them.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, source TEXT, name TEXT, rationale TEXT, changes TEXT,
  phash TEXT, base_phash TEXT, fingerprint TEXT, holdout_start TEXT, verdict TEXT, summary TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS peeks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, trial_id INTEGER, holdout_start TEXT, fingerprint TEXT,
  passed INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, trial_id INTEGER, name TEXT, changes TEXT, params TEXT,
  evidence TEXT, status TEXT, decided_ts REAL, note TEXT);
"""


def _j(x) -> str:
    return json.dumps(x, default=lambda o: list(o) if isinstance(o, tuple) else str(o))


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.executescript(SCHEMA)

    def _c(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    # ------------------------------------------------------------------ trials
    def add_trial(self, **kw) -> int:
        with self._c() as c:
            cur = c.execute("INSERT INTO trials (ts, source, name, rationale, changes, phash, base_phash, fingerprint, "
                            "holdout_start, verdict, summary, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (time.time(), kw.get("source", "cli"), kw["name"], kw["rationale"], _j(kw["changes"]),
                             kw["phash"], kw["base_phash"], kw["fingerprint"], kw["holdout_start"], kw["verdict"],
                             _j(kw.get("summary", {})), _j(kw.get("detail", {}))))
            return int(cur.lastrowid)

    def set_verdict(self, trial_id: int, verdict: str) -> None:
        with self._c() as c:
            c.execute("UPDATE trials SET verdict=? WHERE id=?", (verdict, trial_id))

    def trial(self, trial_id: int) -> dict | None:
        with self._c() as c:
            r = c.execute("SELECT * FROM trials WHERE id=?", (trial_id,)).fetchone()
        return self._row(r) if r else None

    def trials(self, limit: int = 50) -> list[dict]:
        with self._c() as c:
            rows = c.execute("SELECT * FROM trials ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def count_trials(self, fingerprint: str | None = None) -> int:
        with self._c() as c:
            if fingerprint:
                return int(c.execute("SELECT COUNT(*) FROM trials WHERE fingerprint=?", (fingerprint,)).fetchone()[0])
            return int(c.execute("SELECT COUNT(*) FROM trials").fetchone()[0])

    # ------------------------------------------------------------------ holdout peeks
    def add_peek(self, trial_id: int, holdout_start: str, fingerprint: str, passed: bool, detail: dict) -> None:
        with self._c() as c:
            c.execute("INSERT INTO peeks (ts, trial_id, holdout_start, fingerprint, passed, detail) VALUES (?,?,?,?,?,?)",
                      (time.time(), trial_id, holdout_start, fingerprint, int(passed), _j(detail)))

    def peeks(self, holdout_start: str, holdout_end: str | None = None, min_overlap: float = 0.5) -> int:
        """Looks at this holdout.  A look at an earlier window counts too when the two windows overlap by at least
        ``min_overlap`` of the current one - sliding the window forward a few days doesn't buy a fresh holdout."""
        from datetime import date
        with self._c() as c:
            rows = c.execute("SELECT holdout_start, detail FROM peeks").fetchall()
        if holdout_end is None:
            return sum(1 for r in rows if r["holdout_start"] == holdout_start)
        h0, e = date.fromisoformat(holdout_start), date.fromisoformat(holdout_end)
        span = max(1, (e - h0).days)
        n = 0
        for r in rows:
            try:
                w = json.loads(r["detail"]).get("window") or [r["holdout_start"], holdout_end]
                a, b = date.fromisoformat(w[0]), date.fromisoformat(w[1])
            except (ValueError, TypeError, AttributeError):
                continue
            if (min(b, e) - max(a, h0)).days / span >= min_overlap:
                n += 1
        return n

    # ------------------------------------------------------------------ proposals
    def add_proposal(self, trial_id: int, name: str, changes: dict, params: dict, evidence: dict) -> int:
        with self._c() as c:
            cur = c.execute("INSERT INTO proposals (ts, trial_id, name, changes, params, evidence, status) "
                            "VALUES (?,?,?,?,?,?, 'pending')", (time.time(), trial_id, name, _j(changes), _j(params), _j(evidence)))
            return int(cur.lastrowid)

    def proposals(self, status: str | None = None) -> list[dict]:
        with self._c() as c:
            q = "SELECT * FROM proposals" + (" WHERE status=?" if status else "") + " ORDER BY id DESC"
            rows = c.execute(q, (status,) if status else ()).fetchall()
        return [self._row(r) for r in rows]

    def proposal(self, pid: int) -> dict | None:
        with self._c() as c:
            r = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        return self._row(r) if r else None

    def decide(self, pid: int, status: str, note: str = "") -> None:
        with self._c() as c:
            c.execute("UPDATE proposals SET status=?, decided_ts=?, note=? WHERE id=?", (status, time.time(), note, pid))

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        for k in ("changes", "summary", "detail", "params", "evidence"):
            if k in d and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        return d
