"""Persistent portfolio store (SQLite).

Unlike the cache, this is *durable user data*: it lives in ``ABG_DATA_DIR/portfolio.sqlite3``
(default ``~/.abg-terminal``) and is never expired or cleared by ``abg cache clear``.

Schema
------
portfolios     name, created_at
transactions   id, portfolio, symbol, side (BUY/SELL), shares, price, fees, ts, notes
position_meta  portfolio, symbol, stop_loss, take_profit, notes           (per-holding settings)
watchlist      portfolio, symbol, added_at, notes
alert_rules    id, portfolio, symbol, kind, value, enabled, one_shot, note, created_at
signals        id, ts, portfolio, symbol, kind, key, severity, direction, title, message, data, delivered, acknowledged
signal_state   portfolio, symbol, key, value, updated_at                   (edge-trigger memory)

Positions are *derived* from transactions with the average-cost method, so the
transaction log is the single source of truth (edit history, get realized P&L).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import ABGError
from ..utils import normalize_symbol

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolios (name TEXT PRIMARY KEY, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio TEXT NOT NULL, symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('BUY','SELL')), shares REAL NOT NULL CHECK (shares > 0),
  price REAL NOT NULL CHECK (price >= 0), fees REAL NOT NULL DEFAULT 0, ts REAL NOT NULL, notes TEXT);
CREATE INDEX IF NOT EXISTS tx_pf ON transactions(portfolio, symbol, ts);
CREATE TABLE IF NOT EXISTS position_meta (
  portfolio TEXT NOT NULL, symbol TEXT NOT NULL, stop_loss REAL, take_profit REAL, notes TEXT,
  PRIMARY KEY (portfolio, symbol));
CREATE TABLE IF NOT EXISTS watchlist (
  portfolio TEXT NOT NULL, symbol TEXT NOT NULL, added_at REAL NOT NULL, notes TEXT, PRIMARY KEY (portfolio, symbol));
CREATE TABLE IF NOT EXISTS alert_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL,
  value REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, one_shot INTEGER NOT NULL DEFAULT 1, note TEXT,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, portfolio TEXT NOT NULL, symbol TEXT NOT NULL,
  kind TEXT NOT NULL, key TEXT NOT NULL, severity TEXT NOT NULL, direction TEXT NOT NULL, title TEXT NOT NULL,
  message TEXT NOT NULL, data TEXT, delivered TEXT, acknowledged INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS sig_ts ON signals(portfolio, ts);
CREATE INDEX IF NOT EXISTS sig_key ON signals(portfolio, symbol, kind, key, ts);
CREATE TABLE IF NOT EXISTS signal_state (
  portfolio TEXT NOT NULL, symbol TEXT NOT NULL, key TEXT NOT NULL, value TEXT, updated_at REAL NOT NULL,
  PRIMARY KEY (portfolio, symbol, key));
"""

RULE_KINDS = {
    "price_above": "Price rises above value",
    "price_below": "Price falls below value",
    "change_above": "Day change % rises above value",
    "change_below": "Day change % falls below value (use a negative number)",
    "rsi_above": "RSI(14) rises above value",
    "rsi_below": "RSI(14) falls below value",
    "score_above": "Composite signal score rises above value",
    "score_below": "Composite signal score falls below value",
}


class PortfolioError(ABGError):
    code = "portfolio_error"


@dataclass
class Transaction:
    id: int
    portfolio: str
    symbol: str
    side: str
    shares: float
    price: float
    fees: float
    ts: float
    notes: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["date"] = datetime.fromtimestamp(self.ts, timezone.utc).isoformat()
        return d


@dataclass
class Position:
    symbol: str
    shares: float
    avg_cost: float
    cost_basis: float
    realized_pnl: float
    first_bought: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    notes: str | None = None
    transactions: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AlertRule:
    id: int
    portfolio: str
    symbol: str
    kind: str
    value: float
    enabled: bool = True
    one_shot: bool = True
    note: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {**asdict(self), "description": RULE_KINDS.get(self.kind, self.kind)}


def compute_positions(txs: list[Transaction]) -> dict[str, Position]:
    """Average-cost accounting.  Fees are added to cost on buys and subtracted from proceeds on sells."""
    pos: dict[str, Position] = {}
    for t in sorted(txs, key=lambda t: (t.ts, t.id)):
        p = pos.setdefault(t.symbol, Position(t.symbol, 0.0, 0.0, 0.0, 0.0))
        p.transactions += 1
        if t.side == "BUY":
            new_shares = p.shares + t.shares
            p.cost_basis = p.cost_basis + t.shares * t.price + t.fees
            p.avg_cost = p.cost_basis / new_shares
            p.shares = new_shares
            if p.first_bought is None:
                p.first_bought = t.ts
        else:
            if t.shares > p.shares + 1e-9:
                raise PortfolioError(f"Cannot sell {t.shares:g} {t.symbol}: only {p.shares:g} held at that time")
            p.realized_pnl += t.shares * (t.price - p.avg_cost) - t.fees
            p.shares -= t.shares
            p.cost_basis = p.shares * p.avg_cost
            if p.shares < 1e-9:
                p.shares, p.cost_basis, p.avg_cost, p.first_bought = 0.0, 0.0, 0.0, None
    return pos


class PortfolioStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._db.commit()

    @classmethod
    def from_settings(cls, settings) -> "PortfolioStore":
        return cls(Path(settings.data_dir).expanduser() / "portfolio.sqlite3")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ helpers
    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid if cur.lastrowid else cur.rowcount

    def ensure(self, portfolio: str) -> str:
        name = (portfolio or "main").strip().lower()[:40] or "main"
        self._x("INSERT OR IGNORE INTO portfolios (name, created_at) VALUES (?, ?)", (name, time.time()))
        return name

    def portfolios(self) -> list[str]:
        return [r["name"] for r in self._q("SELECT name FROM portfolios ORDER BY created_at")]

    # ------------------------------------------------------------------ transactions
    def add_transaction(self, portfolio: str, symbol: str, side: str, shares: float, price: float,
                        fees: float = 0.0, ts: float | None = None, notes: str | None = None) -> Transaction:
        pf = self.ensure(portfolio)
        sym = normalize_symbol(symbol)
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise PortfolioError("side must be BUY or SELL")
        if not shares or shares <= 0:
            raise PortfolioError("shares must be > 0")
        if price is None or price < 0:
            raise PortfolioError("price must be >= 0")
        ts = ts or time.time()
        if side == "SELL":   # validate against holdings at that point in time
            trial = self.transactions(pf, sym) + [Transaction(-1, pf, sym, side, shares, price, fees, ts, notes)]
            compute_positions(trial)
        tid = self._x("INSERT INTO transactions (portfolio, symbol, side, shares, price, fees, ts, notes) "
                      "VALUES (?,?,?,?,?,?,?,?)", (pf, sym, side, float(shares), float(price), float(fees), ts, notes))
        return Transaction(tid, pf, sym, side, float(shares), float(price), float(fees), ts, notes)

    def delete_transaction(self, portfolio: str, tx_id: int) -> bool:
        pf = self.ensure(portfolio)
        row = self._q("SELECT * FROM transactions WHERE id=? AND portfolio=?", (tx_id, pf))
        if not row:
            return False
        remaining = [t for t in self.transactions(pf, row[0]["symbol"]) if t.id != tx_id]
        compute_positions(remaining)          # refuse deletions that would leave a negative position
        self._x("DELETE FROM transactions WHERE id=? AND portfolio=?", (tx_id, pf))
        return True

    def transactions(self, portfolio: str, symbol: str | None = None) -> list[Transaction]:
        pf = self.ensure(portfolio)
        rows = self._q("SELECT * FROM transactions WHERE portfolio=? AND (? IS NULL OR symbol=?) ORDER BY ts, id",
                       (pf, symbol, symbol))
        return [Transaction(r["id"], r["portfolio"], r["symbol"], r["side"], r["shares"], r["price"], r["fees"],
                            r["ts"], r["notes"]) for r in rows]

    def positions(self, portfolio: str, include_closed: bool = False) -> list[Position]:
        pf = self.ensure(portfolio)
        pos = compute_positions(self.transactions(pf))
        meta = {r["symbol"]: r for r in self._q("SELECT * FROM position_meta WHERE portfolio=?", (pf,))}
        out = []
        for sym, p in sorted(pos.items()):
            if p.shares <= 1e-9 and not include_closed:
                continue
            m = meta.get(sym)
            if m is not None:
                p.stop_loss, p.take_profit, p.notes = m["stop_loss"], m["take_profit"], m["notes"]
            out.append(p)
        return out

    def realized_pnl(self, portfolio: str) -> float:
        return sum(p.realized_pnl for p in compute_positions(self.transactions(portfolio)).values())

    def set_position_meta(self, portfolio: str, symbol: str, stop_loss: float | None = None,
                          take_profit: float | None = None, notes: str | None = None, clear: bool = False) -> None:
        pf = self.ensure(portfolio)
        sym = normalize_symbol(symbol)
        cur = self._q("SELECT * FROM position_meta WHERE portfolio=? AND symbol=?", (pf, sym))
        old = cur[0] if cur else {"stop_loss": None, "take_profit": None, "notes": None}
        vals = (None, None, None) if clear else (
            stop_loss if stop_loss is not None else old["stop_loss"],
            take_profit if take_profit is not None else old["take_profit"],
            notes if notes is not None else old["notes"])
        self._x("INSERT OR REPLACE INTO position_meta (portfolio, symbol, stop_loss, take_profit, notes) "
                "VALUES (?,?,?,?,?)", (pf, sym, *vals))

    # ------------------------------------------------------------------ watchlist
    def watch(self, portfolio: str, symbol: str, notes: str | None = None) -> str:
        pf = self.ensure(portfolio)
        sym = normalize_symbol(symbol)
        self._x("INSERT OR IGNORE INTO watchlist (portfolio, symbol, added_at, notes) VALUES (?,?,?,?)",
                (pf, sym, time.time(), notes))
        return sym

    def unwatch(self, portfolio: str, symbol: str) -> bool:
        return self._x("DELETE FROM watchlist WHERE portfolio=? AND symbol=?",
                       (self.ensure(portfolio), normalize_symbol(symbol))) > 0

    def watchlist(self, portfolio: str) -> list[dict]:
        return [dict(r) for r in self._q("SELECT symbol, added_at, notes FROM watchlist WHERE portfolio=? ORDER BY symbol",
                                         (self.ensure(portfolio),))]

    def symbols(self, portfolio: str) -> list[str]:
        """Every symbol the monitor should track: open holdings + watchlist + symbols with alert rules."""
        held = [p.symbol for p in self.positions(portfolio)]
        watched = [w["symbol"] for w in self.watchlist(portfolio)]
        ruled = [r.symbol for r in self.rules(portfolio) if r.enabled]
        return sorted(set(held) | set(watched) | set(ruled))

    # ------------------------------------------------------------------ alert rules
    def add_rule(self, portfolio: str, symbol: str, kind: str, value: float, one_shot: bool = True,
                 note: str | None = None) -> AlertRule:
        if kind not in RULE_KINDS:
            raise PortfolioError(f"unknown rule kind '{kind}'. Choose from: {', '.join(RULE_KINDS)}")
        pf = self.ensure(portfolio)
        sym = normalize_symbol(symbol)
        now = time.time()
        rid = self._x("INSERT INTO alert_rules (portfolio, symbol, kind, value, enabled, one_shot, note, created_at) "
                      "VALUES (?,?,?,?,1,?,?,?)", (pf, sym, kind, float(value), int(one_shot), note, now))
        return AlertRule(rid, pf, sym, kind, float(value), True, one_shot, note, now)

    def rules(self, portfolio: str, symbol: str | None = None) -> list[AlertRule]:
        rows = self._q("SELECT * FROM alert_rules WHERE portfolio=? AND (? IS NULL OR symbol=?) ORDER BY id",
                       (self.ensure(portfolio), symbol, symbol))
        return [AlertRule(r["id"], r["portfolio"], r["symbol"], r["kind"], r["value"], bool(r["enabled"]),
                          bool(r["one_shot"]), r["note"], r["created_at"]) for r in rows]

    def set_rule_enabled(self, portfolio: str, rule_id: int, enabled: bool) -> None:
        self._x("UPDATE alert_rules SET enabled=? WHERE id=? AND portfolio=?", (int(enabled), rule_id, self.ensure(portfolio)))

    def delete_rule(self, portfolio: str, rule_id: int) -> bool:
        return self._x("DELETE FROM alert_rules WHERE id=? AND portfolio=?", (rule_id, self.ensure(portfolio))) > 0

    # ------------------------------------------------------------------ signals
    def save_signal(self, sig: dict) -> int:
        return self._x(
            "INSERT INTO signals (ts, portfolio, symbol, kind, key, severity, direction, title, message, data, delivered) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sig["ts"], sig["portfolio"], sig["symbol"], sig["kind"], sig["key"], sig["severity"], sig["direction"],
             sig["title"], sig["message"], json.dumps(sig.get("data") or {}, default=str), json.dumps({})))

    def mark_delivered(self, signal_id: int, channel: str, status: str) -> None:
        with self._lock:
            row = self._db.execute("SELECT delivered FROM signals WHERE id=?", (signal_id,)).fetchone()
            if row is None:
                return
            d = json.loads(row["delivered"] or "{}")
            d[channel] = status
            self._db.execute("UPDATE signals SET delivered=? WHERE id=?", (json.dumps(d), signal_id))
            self._db.commit()

    def signals(self, portfolio: str, limit: int = 100, symbol: str | None = None, since: float | None = None) -> list[dict]:
        rows = self._q("SELECT * FROM signals WHERE portfolio=? AND (? IS NULL OR symbol=?) AND (? IS NULL OR ts>?) "
                       "ORDER BY ts DESC, id DESC LIMIT ?",
                       (self.ensure(portfolio), symbol, symbol, since, since, int(limit)))
        return [_signal_row(r) for r in rows]

    def last_fired(self, portfolio: str, symbol: str, kind: str, key: str) -> float | None:
        r = self._q("SELECT MAX(ts) AS t FROM signals WHERE portfolio=? AND symbol=? AND kind=? AND key=?",
                    (portfolio, symbol, kind, key))
        return r[0]["t"] if r and r[0]["t"] is not None else None

    def acknowledge(self, portfolio: str, signal_id: int | None = None) -> int:
        if signal_id is None:
            return self._x("UPDATE signals SET acknowledged=1 WHERE portfolio=? AND acknowledged=0", (self.ensure(portfolio),))
        return self._x("UPDATE signals SET acknowledged=1 WHERE id=? AND portfolio=?", (signal_id, self.ensure(portfolio)))

    # ------------------------------------------------------------------ edge-trigger state
    def get_state(self, portfolio: str, symbol: str, key: str) -> Any:
        r = self._q("SELECT value FROM signal_state WHERE portfolio=? AND symbol=? AND key=?", (portfolio, symbol, key))
        return json.loads(r[0]["value"]) if r else None

    def set_state(self, portfolio: str, symbol: str, key: str, value: Any) -> None:
        self._x("INSERT OR REPLACE INTO signal_state (portfolio, symbol, key, value, updated_at) VALUES (?,?,?,?,?)",
                (portfolio, symbol, key, json.dumps(value, default=str), time.time()))

    # ------------------------------------------------------------------ backup
    def export(self, portfolio: str) -> dict:
        pf = self.ensure(portfolio)
        return {"format": "abg-portfolio-v1", "portfolio": pf, "exported_at": time.time(),
                "transactions": [t.to_dict() for t in self.transactions(pf)],
                "position_meta": [dict(r) for r in self._q("SELECT symbol, stop_loss, take_profit, notes FROM position_meta WHERE portfolio=?", (pf,))],
                "watchlist": self.watchlist(pf),
                "alert_rules": [r.to_dict() for r in self.rules(pf)]}

    def import_(self, data: dict, portfolio: str | None = None, replace: bool = False) -> dict:
        if data.get("format") != "abg-portfolio-v1":
            raise PortfolioError("not an abg-portfolio-v1 export")
        pf = self.ensure(portfolio or data.get("portfolio") or "main")
        with self._lock:
            if replace:
                for t in ("transactions", "position_meta", "watchlist", "alert_rules"):
                    self._db.execute(f"DELETE FROM {t} WHERE portfolio=?", (pf,))
                self._db.commit()
        for t in sorted(data.get("transactions", []), key=lambda t: t["ts"]):
            self.add_transaction(pf, t["symbol"], t["side"], t["shares"], t["price"], t.get("fees", 0), t["ts"], t.get("notes"))
        for m in data.get("position_meta", []):
            self.set_position_meta(pf, m["symbol"], m.get("stop_loss"), m.get("take_profit"), m.get("notes"))
        for w in data.get("watchlist", []):
            self.watch(pf, w["symbol"], w.get("notes"))
        for r in data.get("alert_rules", []):
            self.add_rule(pf, r["symbol"], r["kind"], r["value"], r.get("one_shot", True), r.get("note"))
        return {"portfolio": pf, "transactions": len(data.get("transactions", [])),
                "watchlist": len(data.get("watchlist", [])), "rules": len(data.get("alert_rules", []))}


def _signal_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["data"] = json.loads(d.get("data") or "{}")
    d["delivered"] = json.loads(d.get("delivered") or "{}")
    d["acknowledged"] = bool(d["acknowledged"])
    d["time"] = datetime.fromtimestamp(d["ts"], timezone.utc).isoformat()
    return d
