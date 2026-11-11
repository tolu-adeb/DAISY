"""Analyst pitches (docs/23.1): a person's idea, written up in the same AIBG format as a rule pitch.

The idea, the direction and the levels are the analyst's. DAISY fills in every number from data, builds
the memo, the charts and the deck, and *checks* the levels - it does not approve or reject the idea. An
analyst pitch is always labelled as one, is never presented as a rule signal, never enters the day's top
picks, and is scored apart from rule pitches in the Scorecard.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

BOOK = "analyst"
DIRECTIONS = {"buy": "BUY", "sell": "SELL", "watch": "WATCH"}


def _num(x, name: str, required: bool = False) -> float | None:
    if x in (None, ""):
        if required:
            raise ValueError(f"{name} is needed")
        return None
    try:
        v = float(str(x).replace("$", "").replace(",", ""))
    except ValueError:
        raise ValueError(f"{name} must be a number")
    if v != v or v <= 0:
        raise ValueError(f"{name} must be above zero")
    return v


def clean(body: dict) -> dict:
    """What the analyst typed, validated. Raises ValueError with a sentence a person can act on."""
    sym = str(body.get("symbol") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9.^-]{1,12}", sym):
        raise ValueError("ticker is missing or not a valid symbol")
    direction = str(body.get("direction") or "buy").strip().lower()
    if direction not in DIRECTIONS:
        raise ValueError("direction must be buy, sell or watch")
    name = " ".join(str(body.get("analyst") or "").split())[:40]
    if not name:
        raise ValueError("the analyst's name is needed")
    thesis = " ".join(str(body.get("thesis") or "").split())[:1500]
    if len(thesis) < 20:
        raise ValueError("the thesis needs at least a sentence (20 characters)")
    hz = body.get("horizon_days")
    try:
        hz = int(hz) if hz not in (None, "") else 20
    except (TypeError, ValueError):
        raise ValueError("the time horizon must be a number of trading days")
    if not 1 <= hz <= 260:
        raise ValueError("the time horizon must be between 1 and 260 trading days")
    return {"symbol": sym, "direction": direction, "action": DIRECTIONS[direction], "analyst": name, "thesis": thesis,
            "entry": _num(body.get("entry"), "entry"), "stop": _num(body.get("stop"), "stop"),
            "t1": _num(body.get("t1"), "first target"), "t2": _num(body.get("t2"), "stretch target"), "horizon_days": hz}


def check_levels(v: dict, price: float | None, atr: float | None, min_rr: float, row: dict | None,
                 earnings: str | None, today: date) -> list[dict]:
    """Checks on the analyst's own levels. Each one is a fact with its numbers; none of them blocks the pitch."""
    out: list[dict] = []

    def add(ok, text):
        out.append({"ok": bool(ok), "text": text})
    entry = v.get("entry") or price
    stop, t1, t2 = v.get("stop"), v.get("t1"), v.get("t2")
    if v["direction"] == "buy":
        if entry and price:
            gap = entry / price - 1
            add(abs(gap) <= 0.05, f"Entry ${entry:,.2f} is {gap:+.1%} from the last close of ${price:,.2f}"
                                  + ("" if abs(gap) <= 0.05 else ": more than 5% away, so it may not fill soon"))
        if stop is None:
            add(False, "No stop given: the plan has no defined exit if the idea is wrong")
        elif entry:
            add(stop < entry, f"Stop ${stop:,.2f} is {'below' if stop < entry else 'not below'} the entry ${entry:,.2f}"
                              + (f" ({stop / entry - 1:+.1%})" if stop < entry else ": a buy needs its stop under the entry"))
            if atr and stop < entry:
                n = (entry - stop) / atr
                add(n >= 1.0, f"The stop is {n:.1f} ATR away (ATR ${atr:,.2f})" + ("" if n >= 1.0 else ": inside one day's typical move, so noise could hit it"))
        if t1 is None:
            add(False, "No target given: reward against risk cannot be worked out")
        elif entry:
            add(t1 > entry, f"First target ${t1:,.2f} is {'above' if t1 > entry else 'not above'} the entry")
            if stop and stop < entry < t1:
                rr = (t1 - entry) / (entry - stop)
                add(rr >= min_rr, f"Reward:risk to the first target is {rr:.2f}" + ("" if rr >= min_rr else f": under the {min_rr:g} minimum"))
        if t2 is not None and t1 is not None:
            add(t2 > t1, f"Stretch target ${t2:,.2f} is {'above' if t2 > t1 else 'not above'} the first target")
    if earnings:
        try:
            ed = date.fromisoformat(earnings)
            inside = today <= ed <= today + timedelta(days=int(v["horizon_days"] * 7 / 5) + 1)
            add(not inside, f"Earnings on {earnings} " + ("fall inside the holding window: the result can gap through any stop" if inside else "are outside the holding window"))
        except ValueError:
            pass
    r = row or {}
    if r:
        add(bool(r.get("eligible")), "Clears DAISY's price and liquidity floor" if r.get("eligible") else
            f"Outside the rule universe: {r.get('why_not') or 'not eligible'}")
        if r.get("too_new"):
            add(False, f"Only {int(r.get('sessions') or 0)} sessions of history: too new for the tested rules, so there is no rule read on it")
        if r.get("close") and r.get("sma200"):
            above = r["close"] > r["sma200"]
            add(above if v["direction"] == "buy" else True,
                f"Price is {r['close'] / r['sma200'] - 1:+.1%} {'above' if above else 'below'} its 200-day average")
    return out


def apply(pack: dict, v: dict, checks: list[dict], pcfg: dict, risk: dict, entry_date: str) -> dict:
    """Turn a rule-shaped pack into an analyst pitch: the analyst's thesis and levels, clearly labelled."""
    price = pack.get("price")
    entry = v.get("entry") or price
    stop, t1, t2 = v.get("stop"), v.get("t1"), v.get("t2")
    name = pack.get("name") or pack["symbol"]
    pack.update({"book": BOOK, "book_label": "Analyst pitch", "kind": "analyst", "stop": stop, "rank": None, "of": None,
                 "edge": "an idea from a member of the fund, written up and checked by DAISY",
                 "what_happened": f"{v['analyst']} pitched {pack['symbol']}: {v['thesis']}",
                 "exits": [x for x in (f"the stop at ${stop:,.2f} is touched" if stop else None,
                                       f"the first target ${t1:,.2f} is reached" if t1 else None,
                                       f"{v['horizon_days']} trading days pass without either" if v.get("horizon_days") else None) if x],
                 "max_hold": v["horizon_days"],
                 "analyst": {"name": v["analyst"], "thesis": v["thesis"], "direction": v["direction"], "entry": v.get("entry"),
                             "horizon_days": v["horizon_days"], "checks": checks,
                             "passed": sum(1 for c in checks if c["ok"]), "of": len(checks)}})
    rec, pl = pack["rec"], pack["plan"]
    verb = {"buy": "Buy", "sell": "Sell", "watch": "Watch"}[v["direction"]]
    one = f"{verb} {pack['symbol']}" + (f" near ${entry:,.2f}" if entry and v["direction"] == "buy" else "") + f": {v['analyst']}'s idea."
    if stop and t1 and entry and v["direction"] == "buy":
        one += f" Risk ${entry - stop:,.2f}/share to a ${t1:,.2f} first target over about {v['horizon_days']} trading days."
    rec.update({"one_liner": one, "core_thesis": f"{name}: {v['thesis']}", "open_pitch": None,
                "method": {"book": BOOK, "label": "Analyst pitch", "edge": pack["edge"],
                           "style": f"an analyst's idea, held up to {v['horizon_days']} trading days"}})
    pl["targets"] = {**(pl.get("targets") or {}), "book": BOOK, "entry_ref": entry, "stop": stop, "t1": t1, "t2": t2,
                     "t1_kind": "analyst target" if t1 else None, "t2_kind": "analyst target" if t2 else None,
                     "t1_basis": f"{v['analyst']}'s first target" if t1 else None,
                     "t2_basis": f"{v['analyst']}'s stretch target" if t2 else None, "review_date": None,
                     "notes": ["These levels are the analyst's. DAISY checked them; it did not choose them."]}
    pl["entry"] = {**(pl.get("entry") or {}), "date": entry_date, "reference": entry,
                   "tested": f"the analyst's entry near ${entry:,.2f}" if entry else "at the analyst's discretion", "refinements": []}
    pl["risk"] = risk
    pl["max_hold"] = v["horizon_days"]
    pl["wrong_if"] = pack["exits"] + [c["text"] for c in checks if not c["ok"]][:3]
    pl["questions"] = [f"What does {v['analyst']} see that the price has not already paid for?",
                       "Which of the checks DAISY flagged matters most, and what is the answer to it?",
                       "What would make us close this early: a number, a date or a headline?"]
    return pack


def banner(pack: dict) -> str:
    a = pack.get("analyst") or {}
    L = [f"> **Analyst pitch.** This is {a.get('name')}'s idea, not a rule signal. DAISY filled in the numbers from data and "
         f"checked the levels; it did not choose them. {a.get('passed')} of {a.get('of')} checks passed.", "",
         "### DAISY's checks on the analyst's levels", ""]
    L += [f"- {'✓' if c['ok'] else '⚑'} {c['text']}" for c in a.get("checks") or []]
    return "\n".join(L + [""])


def render(pack: dict, md: str) -> str:
    """The standard memo with the analyst banner and checks placed under its header."""
    lines = md.split("\n")
    lines = [ln for ln in lines if "signal from the close, to be acted on at the next open" not in ln]
    cut = next((i for i, ln in enumerate(lines) if ln.startswith("**TL;DR**")), 3)
    return "\n".join(lines[:cut] + [banner(pack)] + lines[cut:])
