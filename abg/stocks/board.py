"""The day's top picks on Discord (docs/21.7): after the close, at 9:00, 12:00 and 15:00 ET.

    pick_board(packs)        the top N buys by conviction (single-stock books), plus reserves to swap in
    live_status(pick, q)     where a pick stands against its plan right now, from a live quote
    check_in(board, quotes)  statuses for the picks, and a reserve swapped in for any that dropped out
    board_embed(...)         one Discord embed for a post

The signals are computed from the close and the tested entry is the next open, so no new pick is made
during the session. The 12:00 and 15:00 posts are check-ins on the morning's picks: still a buy, missed,
at T1 or stopped. A pick that drops out is replaced by the next-best reserve that is still in play, and
that is the only time a new name (and a new deck) appears midday. Pure functions; no I/O.
"""
from __future__ import annotations

from datetime import date, datetime

SINGLE_STOCK_BOOKS = ("trend_pullback", "mean_reversion", "xs_momentum")
ACTIVE = ("in_zone", "above_zone", "below_zone")
STATE_WORDS = {
    "in_zone": "✅ in the entry zone — still a buy at the plan",
    "above_zone": "⏫ above the entry zone — don't chase; the tested entry was the open",
    "below_zone": "⏬ below the entry zone, still above the stop",
    "t1": "🎯 reached T1 today",
    "stopped": "⛔ touched the stop today — out",
    "missed": "⏭️ opened above the don't-chase line (untested refinement: skip)",
    "gap_stop": "⏭️ opened at or below the stop — skip",
    "no_quote": "no live quote right now",
}


def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def summarize(p: dict, chase_atr: float = 1.0) -> dict:
    """The few fields a board post needs from a full pack (kept in board.json)."""
    rec, pl = p.get("rec") or {}, p.get("plan") or {}
    ent, tgt, risk = pl.get("entry") or {}, pl.get("targets") or {}, pl.get("risk") or {}
    conv = rec.get("conviction") or {}
    price, atr = _f(p.get("price")), _f(p.get("atr"))
    return {"id": p["id"], "symbol": p["symbol"], "name": p.get("name") or p["symbol"], "book": p["book"],
            "book_label": p.get("book_label") or p["book"], "rating": rec.get("rating") or p.get("action"),
            "score": conv.get("score"), "bucket": conv.get("bucket"), "price": price,
            "stop": _f(p.get("stop")), "t1": _f(tgt.get("t1")), "t2": _f(tgt.get("t2")),
            "review_date": tgt.get("review_date"), "zone": ent.get("zone"), "entry_date": ent.get("date"),
            "chase_above": round(price + chase_atr * atr, 2) if price and atr else None,
            "pct_of_fund": risk.get("pct_of_fund"), "shares": risk.get("shares"), "rr": risk.get("rr"),
            "one_liner": rec.get("one_liner"), "disagree": bool(pl.get("horizons_disagree")),
            "prob_weighted": (pl.get("investment") or {}).get("prob_weighted")}


def pick_board(packs: list[dict], size: int = 5, reserves: int = 10, chase_atr: float = 1.0) -> dict:
    """Top ``size`` BUY/ADD pitches from the single-stock books by conviction (rank breaks ties), and the
    next ``reserves`` as substitutes."""
    buys = [p for p in packs if (p.get("rec") or {}).get("rating") in ("BUY", "ADD")
            and p.get("book") in SINGLE_STOCK_BOOKS and p.get("price")]
    buys.sort(key=lambda p: (-(((p.get("rec") or {}).get("conviction") or {}).get("score") or 0),
                             p.get("rank") or 99, p["symbol"]))
    rows = [summarize(p, chase_atr) for p in buys]
    as_of = packs[0]["as_of"] if packs else None
    entry = next((r["entry_date"] for r in rows if r.get("entry_date")), None)
    return {"as_of": as_of, "entry_date": entry, "picks": rows[:size], "reserves": rows[size:size + reserves],
            "dropped": [], "decks_sent": [], "posts": {}}


def live_status(pick: dict, q: dict | None) -> dict:
    """Where the pick stands against its plan. ``q``: price, open, day_high, day_low (any may be missing)."""
    if not q or not _f(q.get("price")):
        return {"state": "no_quote"}
    px, op = _f(q.get("price")), _f(q.get("open"))
    hi, lo = _f(q.get("day_high")) or px, _f(q.get("day_low")) or px
    stop, t1, zone, chase = pick.get("stop"), pick.get("t1"), pick.get("zone"), pick.get("chase_above")
    out = {"price": px, "open": op, "from_open": round(px / op - 1, 4) if op else None,
           "from_ref": round(px / pick["price"] - 1, 4) if pick.get("price") else None}
    if op and stop and op <= stop:
        state = "gap_stop"
    elif op and chase and op > chase:
        state = "missed"
    elif stop and lo <= stop:
        state = "stopped"
    elif t1 and hi >= t1:
        state = "t1"
    elif zone and px > zone[1]:
        state = "above_zone"
    elif zone and px < zone[0]:
        state = "below_zone"
    else:
        state = "in_zone"
    if op and stop and op > stop:
        out["r_from_open"] = round((px - op) / (op - stop), 2)
    out["state"] = state
    return out


def check_in(board: dict, quotes: dict[str, dict]) -> tuple[dict, list[str]]:
    """Mark every pick; replace the ones that dropped out with reserves still in play (in the zone first).
    Returns the updated board and the ids swapped in (they need a deck)."""
    size = len(board.get("picks") or [])
    keep, dropped = [], []
    for p in board.get("picks") or []:
        st = live_status(p, quotes.get(p["symbol"]))
        p = {**p, "live": st}
        (keep if st["state"] in ACTIVE + ("no_quote",) else dropped).append(p)
    swapped: list[str] = []
    spare = []
    for r in board.get("reserves") or []:
        st = live_status(r, quotes.get(r["symbol"]))
        spare.append({**r, "live": st})
    spare.sort(key=lambda r: (0 if r["live"]["state"] == "in_zone" else 1 if r["live"]["state"] in ACTIVE else 2))
    for r in spare:
        if len(keep) >= size:
            break
        if r["live"]["state"] in ACTIVE:
            keep.append({**r, "swapped_in": True})
            swapped.append(r["id"])
    used = set(swapped)
    new = {**board, "picks": keep,
           "reserves": [r for r in board.get("reserves") or [] if r["id"] not in used],
           "dropped": (board.get("dropped") or []) + dropped}
    return new, swapped


# ------------------------------------------------------------------------------------------ embeds
def _px(x) -> str:
    return "—" if x is None else f"${x:,.2f}"


def _pct(x) -> str:
    return "—" if x is None else f"{x:+.1%}"


def _plan_line(p: dict) -> str:
    bits = []
    z = p.get("zone")
    bits.append(f"Entry at the open ~{_px(p.get('price'))}" + (f" (zone {_px(z[0])}–{_px(z[1])})" if z else ""))
    lv = [f"stop {_px(p['stop'])}" if p.get("stop") else None, f"T1 {_px(p['t1'])}" if p.get("t1") else None,
          f"T2 {_px(p['t2'])}" if p.get("t2") else None,
          f"review {p['review_date']}" if p.get("review_date") else None]
    lv = [x for x in lv if x]
    if lv:
        bits.append(" · ".join(lv))
    if p.get("pct_of_fund") is not None:
        bits.append(f"size {p['pct_of_fund']:.1%} of the fund" + (f" · R:R {p['rr']:.2f}" if p.get("rr") else ""))
    return "\n".join(bits)


def _live_line(p: dict) -> str:
    st = p.get("live") or {}
    if st.get("state") == "no_quote" or not st:
        return STATE_WORDS["no_quote"]
    move = f"{_px(st.get('price'))}"
    if st.get("from_open") is not None:
        move += f" ({_pct(st['from_open'])} from the {_px(st.get('open'))} open"
        move += f", {st['r_from_open']:+.2f}R" if st.get("r_from_open") is not None else ""
        move += ")"
    return f"Now {move}\n{STATE_WORDS.get(st['state'], st['state'])}"


def board_embed(board: dict, kind: str, now: datetime, tag: str = "") -> dict:
    """``kind``: close | open | check. Field per pick, within Discord's limits."""
    title = {"close": f"Top {len(board['picks'])} for {board.get('entry_date') or 'the next session'} — buy at the open",
             "open": f"Today's top {len(board['picks'])} — buy at the 9:30 open",
             "check": f"{now.strftime('%I:%M %p').lstrip('0')} check-in — today's top picks"}[kind]
    if tag:
        title = f"[{tag}] {title}"
    fields = []
    for i, p in enumerate(board["picks"], 1):
        head = (f"{i}. {p['rating']} {p['symbol']} — {p['book_label']}"
                + (f" · {p['bucket']} ({p['score']})" if p.get("bucket") else "")
                + (" · new" if p.get("swapped_in") else ""))
        val = _plan_line(p)
        if kind == "check":
            val += "\n" + _live_line(p)
        elif p.get("disagree"):
            val += "\n⚠️ 12-month valuation disagrees: sized as a trade"
        fields.append({"name": head[:256], "value": val[:1024], "inline": False})
    desc = ("Signals from the close; the tested entry is the open. Decks attached for new picks. "
            "Paper only · research, not advice.")
    gone = [d for d in board.get("dropped") or [] if d.get("live")]
    if kind == "check" and gone:
        desc = "Out since this morning: " + ", ".join(
            f"{d['symbol']} ({(d['live'] or {}).get('state', '').replace('_', ' ')})" for d in gone[-5:]) + "\n" + desc
    if not fields:
        desc = "No buys in play from the single-stock books right now.\n" + desc
    return {"title": title[:256], "description": desc[:4000], "fields": fields[:25],
            "color": 0x1A7F4B if fields else 0x5A6A7A,
            "footer": {"text": f"Stock desk · as of {board.get('as_of')} close"}}


def due_slot(now: datetime, slots: list[str], posted: list[str], trading_day: bool, grace_min: int = 45) -> str | None:
    """The slot (``"HH:MM"``) to post now, if any: a trading day, at or after the slot and within the grace,
    not posted yet today."""
    if not trading_day:
        return None
    mins = now.hour * 60 + now.minute
    for s in slots:
        try:
            hh, mm = (int(x) for x in s.strip().split(":"))
        except ValueError:
            continue
        start = hh * 60 + mm
        if start <= mins < start + grace_min and s.strip() not in posted:
            return s.strip()
    return None


def is_fresh(board: dict | None, today: date) -> bool:
    """A board is for today's session when its entry date is today."""
    return bool(board and board.get("entry_date") == today.isoformat() and board.get("picks") is not None)
