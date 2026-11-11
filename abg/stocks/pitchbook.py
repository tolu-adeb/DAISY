"""The pitch book (docs/22.1). The desk pitches ideas; it does not run a portfolio.

Every pitch is one row with a lifecycle:

    new      pitched at the close; the tested entry is the next open
    active   the entry session has happened
    closed   at a target, the stop, the time limit or a rule exit

Every status change is kept in ``history``. Returns are measured from the tested entry (the next open
after the signal) to the close price of the pitch, or to the latest close while it is still open, and
compared with SPY and with the name's sector ETF over the same days. Nothing here is a position: there is
no equity, no P&L in dollars and no holdings.
"""
from __future__ import annotations

import json

import pandas as pd

from .deskdb import DeskDB, now_iso

OPEN = ("new", "active")
LEGACY_HOLD = 21            # sessions a migrated pitch with no recorded plan stays open
NO_STOP_HOLD = 252          # books with no stop close on a rule exit; this is the backstop

SECTOR_ETF = {
    "technology": "XLK", "information technology": "XLK", "financial services": "XLF", "financials": "XLF",
    "financial": "XLF", "healthcare": "XLV", "health care": "XLV", "consumer cyclical": "XLY",
    "consumer discretionary": "XLY", "consumer defensive": "XLP", "consumer staples": "XLP",
    "industrials": "XLI", "energy": "XLE", "utilities": "XLU", "basic materials": "XLB", "materials": "XLB",
    "real estate": "XLRE", "communication services": "XLC", "communications": "XLC",
}


def sector_etf(sector: str | None) -> str | None:
    return SECTOR_ETF.get((sector or "").strip().lower())


def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _r(x, d=5):
    return None if x is None else round(float(x), d)


# --------------------------------------------------------------------------- lifecycle (pure)
def resolve(row: dict, px: pd.DataFrame, mark: dict | None = None) -> dict:
    """Where a pitch stands, from its bars: ``{"status", "reason", "closed_at", "close_price", "entry_open",
    "entry_date", "close_basis"}``. The earliest of stop / target / time / rule exit closes it; a day that
    touches the stop and something else counts as the stop."""
    out = {"status": "new", "reason": None, "closed_at": None, "close_price": None, "close_basis": None,
           "entry_open": None, "entry_date": None}
    if px is None or px.empty:
        return out
    after = px[px.index > pd.Timestamp(row["as_of"])]
    if after.empty:
        return out
    entry = _f(after["open"].iloc[0]) or _f(after["close"].iloc[0])
    out.update({"status": "active", "entry_open": _r(entry, 4), "entry_date": str(after.index[0].date())})
    plan = row.get("plan") or {}
    stop, t1, t2 = _f(plan.get("stop")), _f(plan.get("t1")), _f(plan.get("t2"))
    hold = int(plan.get("max_hold") or 0) or (0 if plan else LEGACY_HOLD)
    ends: list[tuple[str, int, str, float | None, str]] = []      # (day, order, reason, price, basis)
    m = mark or {}
    if m.get("stop_hit"):
        ends.append((m["stop_hit"], 0, "stop", stop, "the stop level"))
    if m.get("t2_hit"):
        ends.append((m["t2_hit"], 1, "target", t2, "T2"))
    elif m.get("t1_hit") and not t2:
        ends.append((m["t1_hit"], 1, "target", t1, "T1"))
    limit = hold or (NO_STOP_HOLD if not stop else 0)
    if limit and len(after) >= limit:
        bar = after.iloc[limit - 1]
        ends.append((str(after.index[limit - 1].date()), 2, "time", _f(bar["close"]),
                     f"the close after {limit} sessions"))
    ex = row.get("exit_signal")
    if ex and ex.get("as_of") and ex["as_of"] > row["as_of"]:
        nxt = px[px.index > pd.Timestamp(ex["as_of"])]
        if len(nxt):
            ends.append((str(nxt.index[0].date()), 3, "rule exit", _f(nxt["open"].iloc[0]) or _f(nxt["close"].iloc[0]),
                         "the open after the rule's exit"))
        else:
            ends.append((ex["as_of"], 3, "rule exit", _f(ex.get("price")),
                         "the close on the exit signal; the tested exit is the next open"))
    if ends:
        day, _, reason, price, basis = min(ends, key=lambda e: (e[0], e[1]))
        out.update({"status": "closed", "reason": reason, "closed_at": day, "close_price": _r(price, 4),
                    "close_basis": basis})
    return out


def span_return(px: pd.DataFrame, start: str, end: str | None = None) -> float | None:
    """Open on ``start`` (or the first session after it) to the close on ``end`` (default: the latest)."""
    if px is None or px.empty or not start:
        return None
    seg = px[px.index >= pd.Timestamp(start)]
    if seg.empty:
        return None
    a = _f(seg["open"].iloc[0]) or _f(seg["close"].iloc[0])
    if end:
        seg = seg[seg.index <= pd.Timestamp(end)]
        if seg.empty:
            return None
    b = _f(seg["close"].iloc[-1])
    return _r(b / a - 1) if a and b else None


def track(row: dict, res: dict, px: pd.DataFrame, spy: pd.DataFrame | None, sec: pd.DataFrame | None,
          etf: str | None) -> dict | None:
    """The pitch's return from the tested entry, and the same days for SPY and the sector ETF."""
    entry = res.get("entry_open")
    if not entry:
        return None
    end = res.get("closed_at")
    last = res.get("close_price") if res["status"] == "closed" else _f(px["close"].iloc[-1])
    if not last:
        return None
    ret = _r(last / entry - 1)
    s = span_return(spy, res["entry_date"], end) if spy is not None else None
    e = span_return(sec, res["entry_date"], end) if sec is not None else None
    stop = _f((row.get("plan") or {}).get("stop"))
    return {"ret": ret, "spy": s, "sector": e, "sector_etf": etf if e is not None else None,
            "excess_spy": _r(ret - s) if s is not None else None,
            "excess_sector": _r(ret - e) if e is not None else None,
            "r": _r((last - entry) / (entry - stop), 3) if stop and entry > stop else None,
            "to": end or str(px.index[-1].date()), "open": res["status"] != "closed"}


def agg(rows: list[dict]) -> dict:
    tr = [r["track"] for r in rows if r.get("track") and r["track"].get("ret") is not None]
    closed = [r for r in rows if r.get("status") == "closed"]

    def mean(key):
        xs = [t[key] for t in tr if t.get(key) is not None]
        return _r(sum(xs) / len(xs)) if xs else None
    vs = [t["excess_spy"] for t in tr if t.get("excess_spy") is not None]
    why = {}
    for r in closed:
        why[r.get("close_reason") or "?"] = why.get(r.get("close_reason") or "?", 0) + 1
    return {"n": len(rows), "open": sum(1 for r in rows if r.get("status") in OPEN), "closed": len(closed),
            "marked": len(tr), "avg_ret": mean("ret"), "avg_excess_spy": mean("excess_spy"),
            "avg_excess_sector": mean("excess_sector"), "avg_r": mean("r"),
            "beat_spy": _r(sum(1 for v in vs if v > 0) / len(vs), 4) if vs else None,
            "win_rate": _r(sum(1 for t in tr if t["ret"] > 0) / len(tr), 4) if tr else None,
            "closed_by": why}


def track_record(rows: list[dict]) -> dict:
    """The scorecard: how the desk's pitches have done, cut every way the team asks about."""
    def by(key):
        keys = sorted({str(key(r)) for r in rows if key(r) is not None})
        return {k: agg([r for r in rows if str(key(r)) == k]) for k in keys}
    return {"all": agg(rows), "by_book": by(lambda r: r.get("book")),
            "by_bucket": by(lambda r: (r.get("plan") or {}).get("bucket")),
            "by_rank": by(lambda r: r.get("drank")), "by_sector": by(lambda r: r.get("sector"))}


# --------------------------------------------------------------------------- storage
class PitchBook:
    def __init__(self, db: DeskDB):
        self.db = db

    def _row(self, r) -> dict:
        d = json.loads(r["data"])
        d.update({"id": r["id"], "symbol": r["symbol"], "book": r["book"], "as_of": r["as_of"],
                  "status": r["status"], "rating": r["rating"] or d.get("rating"),
                  "closed_at": r["closed_at"], "close_reason": r["close_reason"]})
        return d

    def rows(self, status: tuple | None = None, limit: int = 2000) -> list[dict]:
        sql, args = "SELECT * FROM pitch", ()
        if status:
            sql += f" WHERE status IN ({','.join('?' * len(status))})"
            args = tuple(status)
        return [self._row(r) for r in self.db.q(sql + " ORDER BY as_of, id LIMIT ?", args + (limit,))]

    def get(self, pid: str) -> dict | None:
        r = self.db.q("SELECT * FROM pitch WHERE id=?", (pid,))
        return self._row(r[0]) if r else None

    def has(self, as_of: str, book: str, symbol: str) -> bool:
        return bool(self.db.q("SELECT 1 FROM pitch WHERE as_of=? AND book=? AND symbol=?", (as_of, book, symbol)))

    def save(self, row: dict) -> None:
        self.db.x(
            "INSERT INTO pitch(id, symbol, book, as_of, status, rating, closed_at, close_reason, data) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET as_of=excluded.as_of, status=excluded.status, rating=excluded.rating, "
            "closed_at=excluded.closed_at, close_reason=excluded.close_reason, data=excluded.data",
            (row["id"], row["symbol"], row["book"], row["as_of"], row.get("status") or "new", row.get("rating"),
             row.get("closed_at"), row.get("close_reason"), json.dumps(row, default=str)))

    def add(self, row: dict, note: str = "pitched at the close") -> bool:
        if self.get(row["id"]) or self.has(row["as_of"], row["book"], row["symbol"]):
            return False
        row = {**row, "status": row.get("status") or "new", "opened_at": row.get("opened_at") or now_iso()}
        self.save(row)
        self.db.log("pitch", row["id"], row["status"], note, at=row["opened_at"])
        return True

    def set_status(self, row: dict, status: str, note: str | None = None) -> None:
        if row.get("status") == status:
            return
        row["status"] = status
        self.save(row)
        self.db.log("pitch", row["id"], status, note)

    def history(self, pid: str) -> list[dict]:
        return self.db.history("pitch", pid)

    def open_by_symbol(self) -> dict[str, dict[str, dict]]:
        out: dict[str, dict[str, dict]] = {}
        for r in self.rows(OPEN):
            out.setdefault(r["symbol"], {})[r["book"]] = r
        return out

    def exit_signal(self, symbol: str, book: str, as_of: str, price: float | None, reason: str) -> dict | None:
        """A rule exit on an open pitch: remember it; ``resolve`` closes the pitch at the next open."""
        row = self.open_by_symbol().get(symbol, {}).get(book)
        if not row or row.get("exit_signal") or as_of <= row["as_of"]:
            return None
        row["exit_signal"] = {"as_of": as_of, "price": price, "reason": reason}
        self.save(row)
        self.db.log("pitch", row["id"], row["status"], f"rule exit signalled: {reason.replace('_', ' ')}")
        return row

    def counts(self, as_of: str | None = None) -> dict:
        rows = self.db.q("SELECT status, COUNT(*) n FROM pitch GROUP BY status")
        c = {r["status"]: r["n"] for r in rows}
        out = {"new": c.get("new", 0), "active": c.get("active", 0), "closed": c.get("closed", 0)}
        if as_of:
            out["closed_today"] = self.db.q("SELECT COUNT(*) n FROM pitch WHERE closed_at=?", (as_of,))[0]["n"]
        return out

    def migrate(self, rows: list[dict]) -> int:
        """Carry the old ``pitches.json`` rows over, once. Nothing is dropped: every row becomes a pitch
        with its marks, and the history says where it came from."""
        n = 0
        for r in rows or []:
            if not r.get("symbol") or not r.get("as_of") or not r.get("book"):
                continue
            rid = r.get("id") or f"{r['as_of']}-{r['book']}-{r['symbol']}"
            row = {**r, "id": rid, "status": "new", "rating": (r.get("plan") or {}).get("rating") or "BUY",
                   "migrated": True, "opened_at": r["as_of"]}
            if self.add(row, note="carried over from pitches.json"):
                n += 1
        return n
