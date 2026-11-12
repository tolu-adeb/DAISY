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

    # ---- money in and out (deposits are positive, withdrawals negative)
    def flows(self) -> list[dict]:
        return [dict(r) for r in self.db.q("SELECT id, day, amount, note FROM flow ORDER BY day, id")]

    def add_flow(self, day, amount, note=None) -> dict:
        try:
            amt = float(str(amount).replace("$", "").replace(",", ""))
        except (TypeError, ValueError):
            raise ValueError("the amount must be a number (negative for a withdrawal)")
        if amt != amt or amt == 0:
            raise ValueError("the amount cannot be zero")
        d = _day(day or date.today().isoformat(), "the date")
        note = " ".join(str(note or "").split())[:200] or None
        self.db.x("INSERT INTO flow(day, amount, note) VALUES(?,?,?)", (d, amt, note))
        self.db.log("holding", "cash", "open", f"{'deposit' if amt > 0 else 'withdrawal'} of ${abs(amt):,.2f} on {d}")
        return dict(self.db.q("SELECT id, day, amount, note FROM flow ORDER BY id DESC LIMIT 1")[0])

    def delete_flow(self, fid: int) -> bool:
        if not self.db.q("SELECT 1 FROM flow WHERE id=?", (int(fid),)):
            raise KeyError(fid)
        self.db.x("DELETE FROM flow WHERE id=?", (int(fid),))
        self.db.log("holding", "cash", "open", "removed a deposit/withdrawal entry")
        return True

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


def performance(rows: list[dict], flows: list[dict], cash_now: float, closes_fn, bench) -> dict:
    """The fund's value day by day, rebuilt from the position log, and its time-weighted return.

    ``closes_fn(symbol)`` -> a close series (DatetimeIndex) or None; ``bench``: the benchmark's close series.
    Value on a day = every position held at that day's close, at that close, plus cash. Cash is walked
    backwards from today's logged cash: a buy took its cost out, a sale put its proceeds in, a deposit or
    withdrawal moved it. The return is time-weighted: money moving in or out never counts as performance.
    """
    import pandas as pd
    out = {"available": False, "series": [], "stats": {}, "assumptions": [], "contributions": []}
    rows = [r for r in rows if r.get("opened")]
    if not rows or bench is None or not len(bench):
        out["reason"] = "log at least one position (and run a daily pass) to see performance"
        return out
    start = pd.Timestamp(min(r["opened"] for r in rows))
    days = bench.index[bench.index >= start]
    if len(days) < 2:
        out["reason"] = "performance appears once two sessions have passed since the first position"
        return out
    value = pd.Series(0.0, index=days)
    cash_moves = pd.Series(0.0, index=days)              # + into cash (sales), - out of cash (buys)
    unpriced = []

    def snap(d):                                         # the first session on or after a date
        i = days.searchsorted(pd.Timestamp(d))
        return days[min(i, len(days) - 1)]
    for r in rows:
        px = closes_fn(r["symbol"])
        o = snap(r["opened"])
        c = snap(r["closed"]) if r.get("status") == "closed" and r.get("closed") else None
        if px is None or not len(px):
            unpriced.append(r["symbol"])
            p = pd.Series(float(r["cost"]), index=days)  # held at cost: it adds no gain and no loss
        else:
            p = px.reindex(days, method="ffill").fillna(float(r["cost"]))
        held = (days >= o) & ((days < c) if c is not None else True)
        value = value.add(p.where(held, 0.0) * float(r["shares"]), fill_value=0.0)
        cash_moves[o] -= float(r["shares"]) * float(r["cost"])
        if c is not None and r.get("exit_price") is not None:
            cash_moves[c] += float(r["shares"]) * float(r["exit_price"])
    ext = pd.Series(0.0, index=days)
    for f in flows or []:
        if pd.Timestamp(f["day"]) >= days[0]:
            ext[snap(f["day"])] += float(f["amount"])
    # cash at each close, walked back from today's logged cash
    change = cash_moves + ext
    cash = float(cash_now or 0.0) - change[::-1].cumsum()[::-1] + change
    cash_before_first = float(cash.iloc[0] - change.iloc[0])
    total = value + cash
    prev = total.shift(1)
    prev.iloc[0] = cash_before_first
    ret = ((total - ext) / prev - 1).where(prev > 0, 0.0).fillna(0.0)
    idx = (1 + ret).cumprod()
    b = bench.reindex(days, method="ffill")
    bidx = b / float(b.iloc[0])
    dd = idx / idx.cummax() - 1
    bret = b.pct_change().fillna(0.0)
    var = float(bret.var())
    step = max(1, len(days) // 500)
    out.update({"available": True, "start": str(days[0].date()), "end": str(days[-1].date()), "days": int(len(days)),
                "series": [{"t": str(d.date()), "fund": round(float(idx[d]), 5), "bench": round(float(bidx[d]), 5),
                            "dd": round(float(dd[d]), 5), "value": round(float(total[d]), 2)} for d in days[::step]],
                "stats": {"total_return": round(float(idx.iloc[-1] - 1), 5), "bench_return": round(float(bidx.iloc[-1] - 1), 5),
                          "excess": round(float(idx.iloc[-1] - bidx.iloc[-1]), 5),
                          "ann_vol": round(float(ret.std() * (252 ** 0.5)), 5) if len(ret) > 5 else None,
                          "bench_vol": round(float(bret.std() * (252 ** 0.5)), 5) if len(bret) > 5 else None,
                          "max_drawdown": round(float(dd.min()), 5), "current_drawdown": round(float(dd.iloc[-1]), 5),
                          "beta": round(float(ret.cov(bret) / var), 3) if var > 0 and len(ret) > 20 else None,
                          "value_now": round(float(total.iloc[-1]), 2), "best_day": round(float(ret.max()), 5),
                          "worst_day": round(float(ret.min()), 5)}})
    if (cash < -1e-6).any():
        out["assumptions"].append("The cash log does not cover every purchase (cash would have been negative on some days): "
                                  "log the fund's deposits so the return is exact.")
    if not flows:
        out["assumptions"].append("No deposits or withdrawals are logged, so the money behind each purchase is assumed to have "
                                  "been in the fund as cash from the first day.")
    if unpriced:
        out["assumptions"].append(f"No stored prices for {', '.join(sorted(set(unpriced)))}: held at cost in the chart.")
    return out


def breakdown(view: dict, sectors: dict, cap: float = 0.10) -> dict:
    """Where the money is: contribution by holding, sector weights and concentration."""
    op = view.get("open") or []
    tot_gain = sum(r["gain"] for r in op if r.get("gain") is not None) + sum(r["gain"] for r in view.get("closed") or [] if r.get("gain") is not None)
    contrib = sorted(({"symbol": r["symbol"], "gain": r["gain"], "gain_pct": r.get("gain_pct"), "status": r["status"],
                       "share": round(r["gain"] / tot_gain, 4) if tot_gain else None}
                      for r in op + (view.get("closed") or []) if r.get("gain") is not None), key=lambda x: -x["gain"])
    sec: dict = {}
    for r in op:
        k = sectors.get(r["symbol"]) or "not in data"
        sec[k] = sec.get(k, 0.0) + (r.get("weight") or 0.0)
    ws = sorted((r.get("weight") or 0.0 for r in op), reverse=True)
    by_sym: dict = {}
    for r in op:
        by_sym[r["symbol"]] = by_sym.get(r["symbol"], 0.0) + (r.get("weight") or 0.0)
    return {"contributions": contrib, "sectors": [{"sector": k, "weight": round(v, 4)} for k, v in sorted(sec.items(), key=lambda kv: -kv[1])],
            "concentration": {"top1": round(ws[0], 4) if ws else None, "top3": round(sum(ws[:3]), 4) if ws else None,
                              "hhi": round(sum(w * w for w in by_sym.values()), 4) if by_sym else None,
                              "over_cap": sorted(s for s, w in by_sym.items() if w > cap), "cap": cap}}


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def review(view: dict, pitches: list[dict], screen: dict, cap: float = 0.10) -> list[dict]:
    """What the log shows about how the fund invests, next to what DAISY pitched. Observations with their
    evidence and one testable suggestion each - never an instruction."""
    op, obs = view.get("open") or [], []
    by_sym: dict = {}
    for p in sorted(pitches, key=lambda p: p["as_of"]):
        by_sym.setdefault(p["symbol"], []).append(p)

    def pitch_for(h):
        if h.get("pitch_id"):
            got = next((p for p in pitches if p["id"] == h["pitch_id"] and p["symbol"] == h["symbol"]), None)
            if got:
                return got
        return (by_sym.get(h["symbol"]) or [None])[-1]           # the latest pitch on the name
    if not op:
        return obs
    linked = [(h, pitch_for(h)) for h in op]
    with_p = [(h, p) for h, p in linked if p]
    obs.append({"title": "How much of the portfolio came from DAISY's ideas",
                "finding": f"{len(with_p)} of {_n(len(op), 'open position')} {'is a name' if len(with_p) == 1 else 'are names'} DAISY has pitched.",
                "evidence": [f"{h['symbol']}: pitched {p['as_of']} ({p.get('rating') or 'BUY'}, {'analyst' if p['book'] == 'analyst' else 'rule'})" for h, p in with_p][:8]
                            + [f"{h['symbol']}: no DAISY pitch on record" for h, p in linked if not p][:8],
                "suggestion": "Link each position to its pitch when logging it, so the two records can be compared name by name."})
    past = [(h, p) for h, p in with_p if (p.get("plan") or {}).get("stop") and h.get("last") is not None and h["last"] < float(p["plan"]["stop"])]
    if past:
        avg = sum(h["last"] / float(p["plan"]["stop"]) - 1 for h, p in past) / len(past)
        obs.append({"title": "Positions held below their pitch's stop",
                    "finding": f"{_n(len(past), 'position trades', 'positions trade')} under the stop their pitch set, on average {avg:+.1%} beyond it.",
                    "evidence": [f"{h['symbol']}: last ${h['last']:,.2f} against a stop of ${float(p['plan']['stop']):,.2f} ({h['last'] / float(p['plan']['stop']) - 1:+.1%})" for h, p in past],
                    "suggestion": "Decide each one on purpose: either the stop is the exit, or write down why this position is an exception."})
    stale = [(h, p) for h, p in with_p if p.get("status") == "closed"]
    if stale:
        obs.append({"title": "Still held after DAISY closed the pitch",
                    "finding": f"{_n(len(stale), 'position is', 'positions are')} open although their pitch has closed.",
                    "evidence": [f"{h['symbol']}: pitch closed {p.get('closed_at')} ({p.get('close_reason')}); position {('%+.1f%%' % (h['gain_pct'] * 100)) if h.get('gain_pct') is not None else 'not priced'} against cost" for h, p in stale],
                    "suggestion": "Check whether holding past the plan has helped: compare each position's return since the pitch closed with SPY's."})
    weak = [h for h in op if (screen.get(h["symbol"]) or {}).get("close") and (screen[h["symbol"]].get("sma200") or 0) and screen[h["symbol"]]["close"] < screen[h["symbol"]]["sma200"]]
    if weak:
        obs.append({"title": "Holdings below their 200-day average",
                    "finding": f"{len(weak)} of {len(op)} holdings trade below their 200-day average, where none of DAISY's rule books would buy.",
                    "evidence": [f"{h['symbol']}: {screen[h['symbol']]['close'] / screen[h['symbol']]['sma200'] - 1:+.1%} from its 200-day average" for h in weak],
                    "suggestion": "Review these first: a long-term thesis can justify one, but each should have a written reason."})
    by_w: dict = {}
    for h in op:
        by_w[h["symbol"]] = by_w.get(h["symbol"], 0.0) + (h.get("weight") or 0.0)
    big = {s: w for s, w in by_w.items() if w > cap}
    if big:
        obs.append({"title": "Positions larger than the pitch size cap",
                    "finding": f"{_n(len(big), 'position is', 'positions are')} above {cap:.0%} of the fund, the largest size a DAISY pitch will suggest.",
                    "evidence": [f"{s}: {w:.1%} of the fund" for s, w in sorted(big.items(), key=lambda kv: -kv[1])],
                    "suggestion": f"Check whether a {cap:.0%} cap would have changed the result. If the larger sizes are deliberate, raise the pitch size cap so the plans match how the fund sizes."})
    marked = [h for h in op if h.get("gain_pct") is not None]
    win, lose = [h for h in marked if h["gain_pct"] > 0], [h for h in marked if h["gain_pct"] <= 0]
    if win and lose:
        aw, al = sum(h["gain_pct"] for h in win) / len(win), sum(h["gain_pct"] for h in lose) / len(lose)
        worst = min(lose, key=lambda h: h["gain_pct"])
        obs.append({"title": "Winners against losers",
                    "finding": f"{_n(len(win), 'holding is', 'holdings are')} up (average {aw:+.1%}) and {_n(len(lose), 'is', 'are')} down (average {al:+.1%}).",
                    "evidence": [f"Largest loss: {worst['symbol']} {worst['gain_pct']:+.1%}", f"Average gain to average loss: {abs(aw / al):.2f} to 1" if al else "No losing average"],
                    "suggestion": "If the average loss is larger than the average gain, test a rule that cuts a position at a fixed loss."})
    held = {h["symbol"] for h in op}
    taken = [p for p in pitches if p["symbol"] in held and (p.get("track") or {}).get("ret") is not None]
    passed = [p for p in pitches if p["symbol"] not in held and (p.get("track") or {}).get("ret") is not None and p["book"] != "dual_momentum"]
    if taken and passed:
        mt, mp = sum(p["track"]["ret"] for p in taken) / len(taken), sum(p["track"]["ret"] for p in passed) / len(passed)
        obs.append({"title": "Pitches the fund took against those it passed on",
                    "finding": f"Pitches on names the fund holds have returned {mt:+.1%} on average; the {len(passed)} it did not take, {mp:+.1%}.",
                    "evidence": [f"Taken: {_n(len(taken), 'pitch', 'pitches')}", f"Passed on: {_n(len(passed), 'pitch', 'pitches')}"],
                    "suggestion": "If the passed-on pitches keep doing better, look at how the fund chooses which pitches to act on."})
    return obs
