"""Live book: grade new signals, track every one through its life, and emit alerts.

Design: replay is the source of truth. Each run re-simulates every tracked signal from
its post time over the latest daily bars (same engine as the research), then diffs the
result against the last saved state. Transitions become alerts. An intraday quote is
used only to say "price is in the zone now" before the daily bar exists.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from .engine import run
from .strategy import grade, load_config, plan_from_config

ROOT = Path(__file__).resolve().parents[1]
BOOK = ROOT / "state/book.json"

TERMINAL = {"WIN", "LOSS", "SCRATCH", "NOFILL_RAN", "NOFILL_INVALID", "SKIPPED", "EXPIRED"}


@dataclass
class Alert:
    kind: str           # GRADED | IN_ZONE | FILLED | TP_HIT | STOPPED | CANCELLED | EXPIRED
    key: str
    symbol: str
    text: str
    data: dict

    def as_dict(self):
        return asdict(self)


def sig_key(sig: dict) -> str:
    return f"{sig['symbol']}@{sig['posted_et']}"


def load_book() -> dict:
    return json.load(open(BOOK)) if BOOK.exists() else {}


def save_book(book: dict) -> None:
    BOOK.parent.mkdir(parents=True, exist_ok=True)
    json.dump(book, open(BOOK, "w"), indent=1, default=str)


def _fmt(x):
    return f"{x:,.2f}" if x is not None and x >= 1 else (f"{x:.4f}" if x is not None else "-")


def update(signals: pd.DataFrame, prices: dict, spy: pd.DataFrame, quotes: dict | None = None,
           cfg: dict | None = None, book: dict | None = None, plans: dict | None = None) -> tuple[dict, list[Alert]]:
    """``plans``: optional {author: Plan} overrides (spin-off PATCH 7: the wide scan's ideas may chase; the channel's
    ideas always use the configured plan)."""
    cfg = cfg or load_config()
    plan0 = plan_from_config(cfg)
    plans = plans or {}
    book = dict(book if book is not None else load_book())
    alerts: list[Alert] = []
    quotes = quotes or {}
    for _, row in signals.iterrows():
        sig = row.to_dict()
        sig["posted"] = datetime.strptime(sig["posted_et"], "%Y-%m-%d %H:%M")
        plan = plans.get(sig.get("author"), plan0)
        key = sig_key(sig)
        px = prices.get(sig["symbol"])
        if px is None or px.empty:
            continue
        prev = book.get(key, {})
        if prev.get("state") in TERMINAL:
            continue
        # grade once, at post time (that is what the research validated)
        if "verdict" not in prev:
            asof = px.index[px.index <= pd.Timestamp(sig["posted"].date())]
            if not len(asof):
                continue
            v = grade(sig, px, spy, cfg, asof=asof[-1], flags=sig.get("flags") or [])
            prev = {"symbol": sig["symbol"], "posted_et": sig["posted_et"], "author": sig.get("author"),
                    "verdict": v.as_dict(), "state": "WATCHING" if v.take else "SKIPPED"}
            head = f"{'✅ TAKE' if v.take else '⛔ SKIP'} {sig['symbol']} (tier {v.tier}) from {sig.get('author')}"
            body = (f"limit {_fmt(v.entry_px)} | stop {_fmt(v.stop_px)} ({sig['stop_basis']}) | "
                    f"TP {_fmt(v.tp_px)} (0.75R; posted {_fmt(v.posted_target)}) | risk {v.risk_pct}% "
                    f"→ {v.shares_per_10k} sh per $10k" if v.take else "; ".join(v.reasons))
            alerts.append(Alert("GRADED", key, sig["symbol"], f"{head}\n{body}", prev["verdict"]))
            if not v.take:
                book[key] = prev
                continue
        res = run(sig, px, plan)
        st = res["status"]
        new_state = {"PENDING": "WATCHING", "OPEN": "FILLED"}.get(st, st)
        if new_state == "WATCHING" and len(px[px.index > pd.Timestamp(sig["posted"].date())]) >= plan.fill_window:
            new_state = "EXPIRED"
        v = prev["verdict"]
        if new_state != prev.get("state"):
            msg = {
                "FILLED": f"🟢 FILLED {sig['symbol']} @ {_fmt(res.get('fill_px'))} on {res.get('fill_date')} — "
                          f"stop {_fmt(v['stop_px'])}, TP {_fmt(v['tp_px'])}"
                          + (" (chased: bought the open after a close above the zone)" if res.get("chased") else ""),
                "WIN": f"🎯 TP HIT {sig['symbol']} {res.get('exit_date')} (+{res.get('r')}R)",
                "LOSS": f"🛑 STOPPED {sig['symbol']} {res.get('exit_date')} ({res.get('r')}R)",
                "SCRATCH": f"➖ SCRATCH {sig['symbol']} {res.get('exit_date')}",
                "NOFILL_RAN": f"↗️ CANCEL {sig['symbol']}: target reached before the zone filled — don't chase",
                "NOFILL_INVALID": f"✖️ CANCEL {sig['symbol']}: stop level broke before entry",
                "EXPIRED": f"⌛ EXPIRED {sig['symbol']}: no fill within {plan.fill_window} sessions",
            }.get(new_state)
            if msg:
                kind = {"WIN": "TP_HIT", "LOSS": "STOPPED", "NOFILL_RAN": "CANCELLED",
                        "NOFILL_INVALID": "CANCELLED"}.get(new_state, new_state)
                alerts.append(Alert(kind, key, sig["symbol"], msg, res))
        # intraday "in zone now" nudge while still waiting
        q = quotes.get(sig["symbol"])
        if new_state == "WATCHING" and q is not None and v.get("entry_px"):
            long = sig["direction"] == "long"
            in_zone = (q <= v["entry_px"] and q > v["stop_px"]) if long else (q >= v["entry_px"] and q < v["stop_px"])
            if in_zone and prev.get("last_in_zone_alert") != str(datetime.now().date()):
                alerts.append(Alert("IN_ZONE", key, sig["symbol"],
                                    f"📍 {sig['symbol']} {_fmt(q)} is inside the entry zone — limit {_fmt(v['entry_px'])}, "
                                    f"stop {_fmt(v['stop_px'])}, TP {_fmt(v['tp_px'])}", {"quote": q}))
                prev["last_in_zone_alert"] = str(datetime.now().date())
        prev.update({"state": new_state, "result": res, "updated": datetime.now().isoformat(timespec="seconds")})
        book[key] = prev
    return book, alerts


def scoreboard(book: dict) -> dict:
    rows = [b for b in book.values() if b.get("verdict", {}).get("take")]
    closed = [b for b in rows if b["state"] in ("WIN", "LOSS", "SCRATCH")]
    wins = [b for b in closed if b["state"] == "WIN"]
    rs = [b["result"].get("r", 0) for b in closed]
    return {"taken": len(rows), "closed": len(closed), "wins": len(wins),
            "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
            "sum_R": round(sum(rs), 2), "open": sum(1 for b in rows if b["state"] == "FILLED"),
            "watching": sum(1 for b in rows if b["state"] == "WATCHING"),
            "skipped": sum(1 for b in book.values() if b["state"] == "SKIPPED")}
