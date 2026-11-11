"""The fund's recorded portfolio (docs/22.12): what the fund actually holds, logged by hand.

This is not a simulation and DAISY never trades it. An admin records each position (ticker, shares,
average cost, the day it was bought, a note, optionally the pitch it came from) and closes it with an exit
price when it is sold. Everyone with access can read it. It lives in the server's desk database, never
in the repo, and it is what "Why this fits our fund" compares a new pitch against.

Values use the last stored close, so a figure here is as fresh as the last daily pass.
"""
from __future__ import annotations

import re
from datetime import date

from .deskdb import DeskDB, now_iso

SYMBOL = re.compile(r"^[A-Z0-9.^-]{1,12}$")
FIELDS = ("symbol", "shares", "cost", "opened", "note", "pitch_id")


def _num(x, name: str, positive: bool = True) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if v != v or v < 0 or (positive and v == 0):
        raise ValueError(f"{name} must be {'above zero' if positive else 'zero or more'}")
    return v


def _day(x, name: str) -> str:
    try:
        return date.fromisoformat(str(x)[:10]).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a date like 2026-11-10")


def clean(body: dict, partial: bool = False) -> dict:
    """Validate what an admin typed. ``partial``: only the fields present (an edit)."""
    out: dict = {}
    if "symbol" in body or not partial:
        s = str(body.get("symbol") or "").strip().upper()
        if not SYMBOL.match(s):
            raise ValueError("ticker is missing or not a valid symbol")
        out["symbol"] = s
    if "shares" in body or not partial:
        out["shares"] = _num(body.get("shares"), "shares")
    if "cost" in body or not partial:
        out["cost"] = _num(body.get("cost"), "average cost")
    if "opened" in body or not partial:
        out["opened"] = _day(body.get("opened") or date.today().isoformat(), "the date bought")
    if "note" in body:
        out["note"] = " ".join(str(body.get("note") or "").split())[:300] or None
    if "pitch_id" in body:
        p = str(body.get("pitch_id") or "").strip()[:80]
        if p and not re.fullmatch(r"[\w.^-]+", p):
            raise ValueError("pitch id has characters it should not")
        out["pitch_id"] = p or None
    return out


class Portfolio:
    def __init__(self, db: DeskDB):
        self.db = db

    def rows(self, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM holding", ()
        if status:
            sql, args = sql + " WHERE status=?", (status,)
        return [dict(r) for r in self.db.q(sql + " ORDER BY status DESC, symbol, opened, id", args)]

    def get(self, hid: int) -> dict | None:
        r = self.db.q("SELECT * FROM holding WHERE id=?", (int(hid),))
        return dict(r[0]) if r else None

    def add(self, body: dict) -> dict:
        v = clean(body)
        self.db.x("INSERT INTO holding(symbol, shares, cost, opened, note, pitch_id, status, updated) VALUES(?,?,?,?,?,?,?,?)",
                  (v["symbol"], v["shares"], v["cost"], v["opened"], v.get("note"), v.get("pitch_id"), "open", now_iso()))
        row = dict(self.db.q("SELECT * FROM holding ORDER BY id DESC LIMIT 1")[0])
        self.db.log("holding", str(row["id"]), "open", f"logged {v['shares']:g} {v['symbol']} at ${v['cost']:,.2f}")
        return row

    def update(self, hid: int, body: dict) -> dict:
        row = self.get(hid)
        if row is None:
            raise KeyError(hid)
        v = clean(body, partial=True)
        if not v:
            raise ValueError("nothing to change")
        sets = ", ".join(f"{k}=?" for k in v)
        self.db.x(f"UPDATE holding SET {sets}, updated=? WHERE id=?", tuple(v.values()) + (now_iso(), int(hid)))
        self.db.log("holding", str(hid), row["status"], "edited: " + ", ".join(f"{k} {row.get(k)!r} -> {val!r}" for k, val in v.items()))
        return self.get(hid)

    def close(self, hid: int, exit_price, closed=None) -> dict:
        row = self.get(hid)
        if row is None:
            raise KeyError(hid)
        px, day = _num(exit_price, "exit price"), _day(closed or date.today().isoformat(), "the date sold")
        self.db.x("UPDATE holding SET status='closed', exit_price=?, closed=?, updated=? WHERE id=?", (px, day, now_iso(), int(hid)))
        self.db.log("holding", str(hid), "closed", f"sold at ${px:,.2f} on {day}")
        return self.get(hid)

    def reopen(self, hid: int) -> dict:
        if self.get(hid) is None:
            raise KeyError(hid)
        self.db.x("UPDATE holding SET status='open', exit_price=NULL, closed=NULL, updated=? WHERE id=?", (now_iso(), int(hid)))
        self.db.log("holding", str(hid), "open", "reopened")
        return self.get(hid)

    def delete(self, hid: int) -> bool:
        row = self.get(hid)
        if row is None:
            raise KeyError(hid)
        self.db.x("DELETE FROM holding WHERE id=?", (int(hid),))
        self.db.log("holding", str(hid), "removed", f"removed the {row['symbol']} entry (logged by mistake)")
        return True

    def cash(self) -> float:
        return float(self.db.kv_get("portfolio_cash") or 0.0)

    def set_cash(self, x) -> float:
        v = _num(x, "cash", positive=False)
        self.db.kv_set("portfolio_cash", v)
        self.db.log("holding", "cash", "open", f"cash set to ${v:,.2f}")
        return v

    def log(self, limit: int = 60) -> list[dict]:
        return [dict(r) for r in self.db.q("SELECT ref, at, status, note FROM history WHERE kind='holding' ORDER BY rowid DESC LIMIT ?", (limit,))]


def value(rows: list[dict], last_fn, bench_fn=None, cash: float = 0.0) -> dict:
    """Mark the log: ``last_fn(symbol)`` -> (last close, its day) or None; ``bench_fn(day)`` -> the
    benchmark's return from that day's close to now, or None. Open positions get a value, a weight and
    the gain since cost; closed ones get what they made."""
    open_rows, closed_rows = [], []
    for r in rows:
        cost_val = r["shares"] * r["cost"]
        if r["status"] == "closed":
            px = r.get("exit_price")
            gain = (px - r["cost"]) * r["shares"] if px is not None else None
            closed_rows.append({**r, "cost_value": round(cost_val, 2), "gain": None if gain is None else round(gain, 2),
                                "gain_pct": round(px / r["cost"] - 1, 5) if px is not None and r["cost"] else None})
            continue
        got = last_fn(r["symbol"])
        last, as_of = (got if got else (None, None))
        val = r["shares"] * last if last is not None else None
        spy = bench_fn(r["opened"]) if bench_fn else None
        pct = (last / r["cost"] - 1) if last is not None and r["cost"] else None
        open_rows.append({**r, "last": last, "last_day": as_of, "cost_value": round(cost_val, 2),
                          "value": None if val is None else round(val, 2),
                          "gain": None if val is None else round(val - cost_val, 2),
                          "gain_pct": None if pct is None else round(pct, 5), "spy_pct": spy,
                          "excess": round(pct - spy, 5) if pct is not None and spy is not None else None})
    invested = sum(r["value"] if r["value"] is not None else r["cost_value"] for r in open_rows)
    total = invested + (cash or 0.0)
    for r in open_rows:
        v = r["value"] if r["value"] is not None else r["cost_value"]
        r["weight"] = round(v / total, 5) if total else None
    cost = sum(r["cost_value"] for r in open_rows)
    marked = [r for r in open_rows if r["value"] is not None]
    gain = sum(r["gain"] for r in marked)
    realized = [r["gain"] for r in closed_rows if r["gain"] is not None]
    return {"open": sorted(open_rows, key=lambda r: -(r["weight"] or 0)), "closed": sorted(closed_rows, key=lambda r: r.get("closed") or "", reverse=True),
            "totals": {"positions": len(open_rows), "invested": round(invested, 2), "cash": round(cash or 0.0, 2), "total": round(total, 2),
                       "cost": round(cost, 2), "gain": round(gain, 2) if marked else None,
                       "gain_pct": round(gain / sum(r["cost_value"] for r in marked), 5) if marked and sum(r["cost_value"] for r in marked) else None,
                       "unmarked": len(open_rows) - len(marked), "realized": round(sum(realized), 2) if realized else None,
                       "closed": len(closed_rows)}}


def weights(view: dict) -> dict | None:
    """``{symbol: weight}`` for the fund-fit section; lots of the same name are added up."""
    out: dict = {}
    for r in view["open"]:
        if r.get("weight"):
            out[r["symbol"]] = round(out.get(r["symbol"], 0.0) + r["weight"], 6)
    return out or None
