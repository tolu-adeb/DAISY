"""Daily bar store for the stock desk.

One SQLite file holds every daily bar the desk has ever seen, plus when each symbol was last
refreshed and which provider served it.  From it we build the ``Panel`` the vendored books read, so
nothing has to round-trip through per-ticker CSVs.

    store = BarStore.from_settings(settings)
    store.upsert("AAPL", price_history.df, provider="twelvedata")
    panel = store.panel(universe.all(), start="2015-01-01", benchmark="SPY")

Bars are expected split- and dividend-adjusted; ``adjusted`` records whether the provider said so,
because an unadjusted series silently breaks every momentum signal in the book.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from abg.tradebook.stocks.data import Panel

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
  symbol   TEXT NOT NULL,
  day      TEXT NOT NULL,              -- ISO date, the bar's session
  open     REAL, high REAL, low REAL, close REAL, volume REAL,
  adjusted INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (symbol, day)
);
CREATE INDEX IF NOT EXISTS bars_day ON bars(day);

CREATE TABLE IF NOT EXISTS symbols (
  symbol      TEXT PRIMARY KEY,
  first_day   TEXT,
  last_day    TEXT,
  bars        INTEGER NOT NULL DEFAULT 0,
  refreshed   REAL,                    -- unix seconds of the last successful fetch
  provider    TEXT,
  error       TEXT,                    -- last failure, cleared on success
  errors      INTEGER NOT NULL DEFAULT 0,
  delisted    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS membership (     -- point-in-time index membership; empty = everything eligible
  symbol TEXT NOT NULL,
  start  TEXT NOT NULL,
  end    TEXT,                              -- NULL = still a member
  PRIMARY KEY (symbol, start)
);

CREATE TABLE IF NOT EXISTS sentiment (      -- daily news score per symbol, in [-1, 1]
  symbol TEXT NOT NULL,
  day    TEXT NOT NULL,
  score  REAL NOT NULL,
  n      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (symbol, day)
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

FIELDS = ("open", "high", "low", "close", "volume")


@dataclass
class Coverage:
    symbol: str
    bars: int
    first_day: str | None
    last_day: str | None
    refreshed: float | None
    provider: str | None
    error: str | None
    errors: int
    delisted: bool

    @property
    def age_h(self) -> float | None:
        return None if not self.refreshed else (time.time() - self.refreshed) / 3600.0


def _day(x) -> str:
    """Normalise anything date-like to an ISO day string."""
    if isinstance(x, str):
        return x[:10]
    if isinstance(x, (pd.Timestamp, datetime)):
        return x.date().isoformat() if hasattr(x, "date") else str(x)[:10]
    if isinstance(x, date):
        return x.isoformat()
    return str(x)[:10]


class BarStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    @classmethod
    def from_settings(cls, settings) -> "BarStore":
        return cls(Path(settings.data_dir).expanduser() / "stocks.sqlite3")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ writing
    def upsert(self, symbol: str, df: pd.DataFrame, provider: str | None = None,
               adjusted: bool = True) -> int:
        """Insert or replace the bars in ``df`` (DatetimeIndex, open/high/low/close/volume).

        Returns the number of rows written.  Rows with no close are dropped: a NaN close would be
        carried into every indicator.
        """
        sym = symbol.upper().strip()
        if df is None or len(df) == 0:
            return 0
        d = df.copy()
        d.columns = [str(c).strip().lower() for c in d.columns]
        missing = [c for c in FIELDS if c not in d.columns]
        if missing:
            # volume is optional for indexes (^VIX has none); the rest is not
            if missing != ["volume"]:
                raise ValueError(f"{sym}: bars missing {missing}")
            d["volume"] = np.nan
        if not isinstance(d.index, pd.DatetimeIndex):
            raise ValueError(f"{sym}: bars need a DatetimeIndex")
        d = d[~d.index.duplicated(keep="last")].sort_index()
        d = d.dropna(subset=["close"])
        if d.empty:
            return 0
        rows = [(sym, _day(ts), _f(r.get("open")), _f(r.get("high")), _f(r.get("low")),
                 _f(r.get("close")), _f(r.get("volume")), int(bool(adjusted)))
                for ts, r in d.iterrows()]
        with self._lock:
            self._db.executemany(
                "INSERT INTO bars (symbol, day, open, high, low, close, volume, adjusted) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(symbol, day) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
                "volume=excluded.volume, adjusted=excluded.adjusted", rows)
            self._touch(sym, provider=provider, ok=True)
            self._db.commit()
        return len(rows)

    def mark_error(self, symbol: str, error: str) -> None:
        sym = symbol.upper().strip()
        with self._lock:
            self._db.execute(
                "INSERT INTO symbols (symbol, error, errors) VALUES (?,?,1) ON CONFLICT(symbol) DO UPDATE SET "
                "error=excluded.error, errors=symbols.errors+1", (sym, str(error)[:300]))
            self._db.commit()

    def set_delisted(self, symbol: str, delisted: bool = True) -> None:
        with self._lock:
            self._db.execute("INSERT INTO symbols (symbol, delisted) VALUES (?,?) "
                             "ON CONFLICT(symbol) DO UPDATE SET delisted=excluded.delisted",
                             (symbol.upper().strip(), int(delisted)))
            self._db.commit()

    def _touch(self, sym: str, provider: str | None, ok: bool) -> None:
        agg = self._db.execute("SELECT COUNT(*) n, MIN(day) a, MAX(day) b FROM bars WHERE symbol=?",
                               (sym,)).fetchone()
        self._db.execute(
            "INSERT INTO symbols (symbol, first_day, last_day, bars, refreshed, provider, error, errors) "
            "VALUES (?,?,?,?,?,?,NULL,0) ON CONFLICT(symbol) DO UPDATE SET "
            "first_day=excluded.first_day, last_day=excluded.last_day, bars=excluded.bars, "
            "refreshed=excluded.refreshed, provider=COALESCE(excluded.provider, symbols.provider), "
            "error=NULL, errors=0",
            (sym, agg["a"], agg["b"], agg["n"], time.time() if ok else None, provider))

    def set_sentiment(self, symbol: str, day, score: float, n: int = 0) -> None:
        with self._lock:
            self._db.execute("INSERT INTO sentiment (symbol, day, score, n) VALUES (?,?,?,?) "
                             "ON CONFLICT(symbol, day) DO UPDATE SET score=excluded.score, n=excluded.n",
                             (symbol.upper().strip(), _day(day), float(score), int(n)))
            self._db.commit()

    def set_membership(self, rows: list[tuple[str, str, str | None]]) -> int:
        """rows: (symbol, start ISO day, end ISO day or None)."""
        with self._lock:
            self._db.executemany(
                "INSERT INTO membership (symbol, start, end) VALUES (?,?,?) "
                "ON CONFLICT(symbol, start) DO UPDATE SET end=excluded.end",
                [(s.upper().strip(), _day(a), _day(b) if b else None) for s, a, b in rows])
            self._db.commit()
        return len(rows)

    def replace_membership(self, rows: list[tuple[str, str, str | None]]) -> int:
        """Swap in a complete membership history in one transaction (a refresh, not an append)."""
        with self._lock:
            self._db.execute("DELETE FROM membership")
            self._db.executemany(
                "INSERT OR REPLACE INTO membership (symbol, start, end) VALUES (?,?,?)",
                [(s.upper().strip(), _day(a), _day(b) if b else None) for s, a, b in rows])
            self._db.commit()
        return len(rows)

    def kv_set(self, k: str, v: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
            self._db.commit()

    def kv_get(self, k: str, default: str | None = None) -> str | None:
        r = self._q("SELECT v FROM kv WHERE k=?", (k,))
        return r[0]["v"] if r else default

    # ------------------------------------------------------------------ reading
    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def symbols(self) -> list[str]:
        return [r["symbol"] for r in self._q("SELECT symbol FROM symbols WHERE bars > 0 ORDER BY symbol")]

    def last_day(self, symbol: str) -> str | None:
        r = self._q("SELECT last_day FROM symbols WHERE symbol=?", (symbol.upper().strip(),))
        return r[0]["last_day"] if r else None

    def coverage(self, symbols: list[str] | None = None) -> dict[str, Coverage]:
        sql = ("SELECT symbol, bars, first_day, last_day, refreshed, provider, error, errors, delisted "
               "FROM symbols")
        rows = self._q(sql)
        out = {}
        for r in rows:
            if symbols is not None and r["symbol"] not in symbols:
                continue
            out[r["symbol"]] = Coverage(r["symbol"], r["bars"], r["first_day"], r["last_day"],
                                        r["refreshed"], r["provider"], r["error"], r["errors"],
                                        bool(r["delisted"]))
        return out

    def frame(self, symbol: str, start=None) -> pd.DataFrame:
        """One symbol's bars as a DataFrame (DatetimeIndex, open/high/low/close/volume)."""
        sql = "SELECT day, open, high, low, close, volume FROM bars WHERE symbol=?"
        args: tuple = (symbol.upper().strip(),)
        if start is not None:
            sql += " AND day >= ?"
            args += (_day(start),)
        rows = self._q(sql + " ORDER BY day", args)
        if not rows:
            return pd.DataFrame(columns=list(FIELDS))
        df = pd.DataFrame([dict(r) for r in rows])
        df["day"] = pd.to_datetime(df["day"])
        return df.set_index("day").rename_axis("date")[list(FIELDS)].astype(float)

    def panel(self, symbols: list[str], benchmark: str = "SPY", start=None,
              min_bars: int = 250, warn: bool = True) -> Panel:
        """Build the ``Panel`` the books read.

        The benchmark's own sessions are the trading calendar, exactly as the vendored CSV loader does,
        so a symbol that didn't trade on a given day is NaN rather than forward-filled.  Symbols with
        fewer than ``min_bars`` bars are dropped: the books need 200-plus bars before any indicator is
        valid, and a short series only produces NaN candidates.
        """
        from .universe import GAUGE_ALIAS            # ^VIX is stored as VIX: ask for what is stored
        bench = benchmark.upper().strip()
        want = {GAUGE_ALIAS.get(s.upper().strip(), s.upper().strip()) for s in symbols} | {bench}
        have = self.coverage()
        use = sorted(s for s in want if s in have and have[s].bars >= min_bars)
        if bench not in use:
            raise FileNotFoundError(
                f"benchmark {bench} has {have[bench].bars if bench in have else 0} bars in the store "
                f"(need {min_bars}); run `abg stocks backfill` first")
        dropped = sorted(want - set(use))

        sql = "SELECT symbol, day, open, high, low, close, volume FROM bars WHERE symbol IN (%s)" % \
              ",".join("?" * len(use))
        args = tuple(use)
        if start is not None:
            sql += " AND day >= ?"
            args += (_day(start),)
        rows = self._q(sql, args)
        if not rows:
            raise ValueError("no bars in the store for the requested symbols")
        raw = pd.DataFrame([dict(r) for r in rows])
        raw["day"] = pd.to_datetime(raw["day"])

        cal = pd.DatetimeIndex(sorted(raw.loc[raw["symbol"] == bench, "day"].unique()))
        fields = {}
        for f in FIELDS:
            wide = raw.pivot_table(index="day", columns="symbol", values=f, aggfunc="last")
            fields[f] = wide.reindex(cal).astype(float)
        close = fields["close"]

        elig = self._eligibility(close, cal, warn=warn)
        sent = self._sentiment_frame(cal, close.columns)
        if dropped and warn:
            log.info("panel: %d symbol(s) skipped for too few bars (%s%s)", len(dropped),
                     ", ".join(dropped[:8]), "..." if len(dropped) > 8 else "")
        return Panel(fields["open"], fields["high"], fields["low"], close, fields["volume"],
                     elig, self.sectors(), sent)

    def _eligibility(self, close: pd.DataFrame, cal: pd.DatetimeIndex, warn: bool) -> pd.DataFrame:
        rows = self._q("SELECT symbol, start, end FROM membership")
        if not rows:
            if warn:
                warnings.warn(
                    "No point-in-time membership in the store: every symbol is eligible on every day. "
                    "If the universe is today's index members, backtests over the past carry "
                    "survivorship bias. Load dated index membership into the store's membership table "
                    "(BarStore.set_membership) before trusting a backtest.",
                    stacklevel=3)
            return close.notna()
        elig = pd.DataFrame(False, index=cal, columns=close.columns)
        for r in rows:
            t = r["symbol"]
            if t not in elig.columns:
                continue
            a = pd.Timestamp(r["start"])
            b = pd.Timestamp(r["end"]) if r["end"] else cal[-1]
            elig.loc[(cal >= a) & (cal <= b), t] = True
        # The benchmark, the ETFs and any name the index has never held have no membership rows at all:
        # keep those eligible. A stock that *was* a member, just not inside this window (dropped in
        # 2012, or not added until 2024), stays ineligible - that is the whole point of membership.
        listed = {r["symbol"] for r in rows}
        for t in close.columns:
            if t not in listed:
                elig[t] = True
        return elig & close.notna()

    def _sentiment_frame(self, cal: pd.DatetimeIndex, cols) -> pd.DataFrame | None:
        rows = self._q("SELECT symbol, day, score FROM sentiment")
        if not rows:
            return None
        s = pd.DataFrame([dict(r) for r in rows])
        s["day"] = pd.to_datetime(s["day"])
        wide = s.pivot_table(index="day", columns="symbol", values="score", aggfunc="mean")
        return wide.reindex(cal).reindex(columns=[c for c in cols], fill_value=np.nan)

    def sectors(self) -> dict[str, str]:
        rows = self._q("SELECT v FROM kv WHERE k='sectors'")
        if not rows:
            return {}
        import json
        try:
            return json.loads(rows[0]["v"])
        except Exception:
            return {}

    def set_sectors(self, mapping: dict[str, str]) -> None:
        import json
        self.kv_set("sectors", json.dumps({k.upper(): v for k, v in mapping.items()}))

    # ------------------------------------------------------------------ health
    def stats(self) -> dict:
        r = self._q("SELECT COUNT(*) bars, COUNT(DISTINCT symbol) syms, MIN(day) a, MAX(day) b FROM bars")[0]
        bad = self._q("SELECT COUNT(*) n FROM symbols WHERE error IS NOT NULL")[0]["n"]
        unadj = self._q("SELECT COUNT(DISTINCT symbol) n FROM bars WHERE adjusted=0")[0]["n"]
        stale = self._q("SELECT COUNT(*) n FROM symbols WHERE bars>0 AND (refreshed IS NULL OR refreshed < ?)",
                        (time.time() - 36 * 3600,))[0]["n"]
        return {"bars": r["bars"], "symbols": r["syms"], "first_day": r["a"], "last_day": r["b"],
                "errors": bad, "unadjusted_symbols": unadj, "stale_symbols": stale,
                "membership_rows": self._q("SELECT COUNT(*) n FROM membership")[0]["n"],
                "path": str(self.path)}


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(v) else v
