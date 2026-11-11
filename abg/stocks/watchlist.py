"""The morning watchlist (docs/22.4): new names worth watching, every trading day, in one digest.

The format borrows from Zacks - a 1-5 rank, A-F style grades, a name of the day, a list that says what
moved in and out - and the content is DAISY's own: every number comes from the screen (abg/stocks/screen.py),
which reads the same indicators the rules read. Nothing here is a new strategy. A card says which book a
name is close to triggering, the exact condition still needed, and the dates that matter.

    build      the cards: today's pitches, names one or two steps from a trigger, names new to the
               momentum ranking, names the news or a theme shock put in play, and IPOs
    advance    the lifecycle - watching -> triggered (a pitch) -> in play -> exited, or dropped - and the
               list of what changed since yesterday
    embed      the Discord digest, within Discord's limits
    score      did the rank mean anything: how often each rank went on to trigger, and what it did next

Pure functions; the desk does the I/O.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import rank as RK
from .live import BOOK_LABEL, BOOK_SHORT

LIVE = ("watching", "triggered", "in_play")
DEFAULTS = {"size": 30, "post": 10, "per_book": 8, "watch_sessions": 10, "max_steps": 2, "ipo_days_ahead": 14,
            "ipo_days_back": 30}


def watch_cfg(cfg: dict | None) -> dict:
    out = dict(DEFAULTS)
    out.update((cfg or {}).get("watchlist") or {})
    return out


def _sessions_after(d: date, n: int) -> date:
    from .pitch_plan import next_trading_day
    for _ in range(max(1, n)):
        d = next_trading_day(d)
    return d


def _px(x) -> str:
    return "—" if x is None else f"${x:,.2f}"


# --------------------------------------------------------------------------- cards
def setup_card(r: dict, setup: dict, today: date, base: dict | None, wc: dict, earnings: str | None = None,
               grades: dict | None = None) -> dict:
    """A name that has not triggered: which book it is close to, what is still needed, and the dates."""
    from .pitch_plan import next_month_end_session
    book = setup["book"]
    hold = (base or {}).get("median_hold_days")
    earliest = _sessions_after(today - timedelta(days=1), 1) if book != "xs_momentum" else next_month_end_session(today)
    return {
        "symbol": r["symbol"], "name": r.get("name") or r["symbol"], "sector": r.get("sector"),
        "price": r.get("close"), "chg_1d": r.get("chg_1d"), "status": "watching", "source": "setup",
        "drank": RK.daisy_rank(None, steps_away=setup["steps"]), "grades": grades or RK.grades(r),
        "why": (f"{BOOK_LABEL.get(book, book)}: {'one step' if setup['steps'] == 1 else str(setup['steps']) + ' steps'} "
                f"from a trigger."),
        "strategy": {"book": book, "label": BOOK_LABEL.get(book, book), "steps": setup["steps"],
                     "trigger": setup["trigger"], "missing": setup["missing"], "passing": setup["passing"],
                     "level": setup.get("level"), "level_kind": setup.get("level_kind"),
                     "stop_note": setup.get("stop_note"), "entry": "the open after the close that triggers it"},
        "timeline": {"earliest": earliest.isoformat(),
                     "expected_hold": (f"about {max(1, round(hold))} sessions (median of the replayed trades)" if hold else
                                       "until the next month-end rebalance" if book == "xs_momentum" else None),
                     "review": next_month_end_session(today).isoformat() if book == "xs_momentum" else None,
                     "earnings": earnings, "leaves": _sessions_after(today, wc["watch_sessions"]).isoformat()},
        "links": [], "themes": [], "pitch_id": None,
    }


def pitch_card(p: dict, r: dict | None, today: date, wc: dict, status: str = "triggered") -> dict:
    """A name the rules triggered at the last close: the card carries the pitch's own plan."""
    rec, pl = p.get("rec") or {}, p.get("plan") or {}
    tgt, ent, risk = pl.get("targets") or {}, pl.get("entry") or {}, pl.get("risk") or {}
    conv = rec.get("conviction") or {}
    return {
        "symbol": p["symbol"], "name": p.get("name") or p["symbol"], "sector": p.get("sector") or (r or {}).get("sector"),
        "price": p.get("price"), "chg_1d": (r or {}).get("chg_1d"), "status": status, "source": "pitch",
        "drank": rec.get("daisy_rank") or RK.daisy_rank(rec.get("rating"), conv.get("bucket")),
        "grades": p.get("grades") or RK.grades(r or {}),
        "why": rec.get("one_liner") or p.get("what_happened"),
        "strategy": {"book": p["book"], "label": p.get("book_label") or BOOK_LABEL.get(p["book"], p["book"]), "steps": 0,
                     "trigger": "triggered at the last close", "missing": [], "passing": [k for k, v in (p.get("gates") or {}).items() if v],
                     "entry": ent.get("tested") or "the next open", "entry_ref": p.get("price"), "zone": ent.get("zone"),
                     "stop": p.get("stop"), "t1": tgt.get("t1"), "t2": tgt.get("t2"), "rr": risk.get("rr"),
                     "pct_of_fund": risk.get("pct_of_fund"), "conviction": conv.get("score"), "bucket": conv.get("bucket")},
        "timeline": {"earliest": ent.get("date"),
                     "expected_hold": (f"at most {pl['max_hold']} sessions" if pl.get("max_hold") else "until the next month-end rebalance"),
                     "review": tgt.get("review_date"), "earnings": (ent.get("earnings") or {}).get("date") if isinstance(ent.get("earnings"), dict) else None,
                     "leaves": None},
        "links": [{"title": n.get("title"), "url": n.get("url")} for n in (p.get("news") or [])[:2] if n.get("url")],
        "themes": [], "pitch_id": p["id"],
    }


def news_card(sym: str, r: dict | None, why: str, links: list[dict], today: date, wc: dict, source: str = "news",
              themes: list[str] | None = None) -> dict:
    r = r or {}
    eligible = bool(r.get("eligible"))
    return {
        "symbol": sym, "name": r.get("name") or sym, "sector": r.get("sector"), "price": r.get("close"),
        "chg_1d": r.get("chg_1d"), "status": "watching", "source": source, "drank": 4,
        "grades": RK.grades(r), "why": why,
        "strategy": {"book": None, "label": "No rule setup yet", "steps": None,
                     "trigger": ("in the research queue: the rules have not set up on it" if eligible else
                                 f"researched, not pitched: {r.get('why_not') or 'not in the screen yet'}"),
                     "missing": [], "passing": [], "entry": None},
        "timeline": {"earliest": None, "expected_hold": None, "review": None, "earnings": None,
                     "leaves": _sessions_after(today, max(3, wc["watch_sessions"] // 2)).isoformat()},
        "links": links[:3], "themes": themes or [], "pitch_id": None,
    }


def ipo_card(x: dict, r: dict | None, today: date, wc: dict) -> dict:
    """An upcoming or recent listing. Too new for the tested rules: watched and researched, not pitched."""
    upcoming = x["date"] > today.isoformat()
    sym = x.get("symbol") or x["name"][:12].upper()
    since = None
    if r and r.get("close") and x.get("ipo_close"):
        since = r["close"] / x["ipo_close"] - 1
    return {
        "symbol": sym, "name": x["name"], "sector": (r or {}).get("sector"), "price": (r or {}).get("close"),
        "chg_1d": (r or {}).get("chg_1d"), "status": "watching", "source": "ipo", "drank": 4,
        "grades": RK.grades(r or {}),
        "why": (f"IPO expected {x['date']}" + (f" on {x['exchange']}" if x.get("exchange") else "")
                + (f", priced {x['price']}" if x.get("price") else "") + "." if upcoming else
                f"Listed {x['date']}" + (f"; {since:+.1%} since its first close" if since is not None else "") + "."),
        "strategy": {"book": None, "label": "IPO & new listings", "steps": None,
                     "trigger": "too new for the tested rules (they need about a year of history): watched and researched, not rule-pitched",
                     "missing": [], "passing": [], "entry": None},
        "timeline": {"earliest": None, "expected_hold": None, "review": None, "earnings": None, "listing": x["date"],
                     "leaves": (date.fromisoformat(x["date"]) + timedelta(days=wc["ipo_days_back"])).isoformat()},
        "links": [], "themes": [], "pitch_id": None, "ipo": {k: x.get(k) for k in ("date", "exchange", "price", "shares", "value", "status")},
    }


def build(screen: dict[str, dict], setups_fn, packs: list[dict], today: date, cfg: dict, base_by_book: dict | None = None,
          prev_top: set | None = None, in_play: dict | None = None, ipos: list[dict] | None = None,
          earnings: dict | None = None, shocks: list[dict] | None = None, themes: dict | None = None) -> list[dict]:
    """Every card for today, best first. ``screen``: symbol -> screen row. ``setups_fn(row)`` -> the row's
    setups. ``in_play``: ``{symbol: {"why", "links"}}`` from the news scan. One card per name: a pitch
    beats a setup, a setup beats a news mention."""
    wc = watch_cfg(cfg)
    cards: dict[str, dict] = {}
    for p in packs or []:
        if (p.get("rec") or {}).get("rating") in ("BUY", "ADD") and p["book"] != "dual_momentum":
            cards.setdefault(p["symbol"], pitch_card(p, screen.get(p["symbol"]), today, wc))
    per_book: dict[str, int] = {}
    cand = []
    for sym, r in screen.items():
        if sym in cards or not r.get("eligible"):
            continue
        ss = [s for s in setups_fn(r) if s["steps"] <= wc["max_steps"]]
        if ss:
            cand.append((ss[0]["steps"], -(r.get("mom_pct") or 0), sym, r, ss[0]))
    for _, _, sym, r, s in sorted(cand, key=lambda x: x[:3]):
        if per_book.get(s["book"], 0) >= wc["per_book"]:
            continue
        per_book[s["book"]] = per_book.get(s["book"], 0) + 1
        c = setup_card({**r, "symbol": sym}, s, today, (base_by_book or {}).get(s["book"]), wc, (earnings or {}).get(sym))
        if s["book"] == "xs_momentum" and prev_top is not None and sym not in prev_top:
            c["why"] = "New to the 12-1 momentum ranking. " + c["why"]
            c["source"] = "momentum_new"
        cards[sym] = c
    for sym, v in (in_play or {}).items():
        if sym in cards:
            cards[sym]["links"] = (v.get("links") or [])[:3] + cards[sym]["links"]
            cards[sym]["why"] += " In the news: " + v["why"]
        else:
            cards[sym] = news_card(sym, screen.get(sym), v["why"], v.get("links") or [], today, wc, v.get("source", "news"))
    for sh in shocks or []:
        for n in (sh.get("names") or [])[:6]:
            sym = n["symbol"]
            why = f"Theme shock ({sh['label']}): {sym} {n['chg']:+.1%} on the day."
            link = [{"title": sh["source"]["title"], "url": sh["source"]["url"]}] if (sh.get("source") or {}).get("url") else []
            if sym in cards:
                if sh["theme"] not in cards[sym]["themes"]:
                    cards[sym]["themes"].append(sh["theme"])
                    cards[sym]["why"] += " " + why
            else:
                cards[sym] = news_card(sym, screen.get(sym), why, link, today, wc, "theme", [sh["theme"]])
    for x in ipos or []:
        sym = x.get("symbol") or x["name"][:12].upper()
        if sym not in cards:
            cards[sym] = ipo_card(x, screen.get(sym), today, wc)
    for sym, c in cards.items():
        c["rank_words"] = RK.RANK_WORDS.get(c["drank"])
        if themes:
            from .news import themes_of
            c["themes"] = sorted(set(c["themes"]) | set(themes_of(sym, themes)))
    order = {"pitch": 0, "setup": 1, "momentum_new": 1, "theme": 2, "news": 3, "ipo": 4}
    out = sorted(cards.values(), key=lambda c: (c["drank"], order.get(c["source"], 5),
                                                -((c["strategy"].get("conviction") or 0)), c["symbol"]))
    return out[:wc["size"]]


# --------------------------------------------------------------------------- lifecycle
def advance(prev: dict[str, dict], cards: list[dict], pitch_status: dict[str, str], today: date) -> tuple[list[dict], dict]:
    """Yesterday's list plus today's cards -> today's rows and what changed.

    ``prev``: symbol -> stored row (``status``, ``added``, ``drank``, ``card``). ``pitch_status``:
    symbol -> the status of our latest pitch on it (new / active / closed).
    """
    t = today.isoformat()
    ch = {"added": [], "removed": [], "up": [], "down": [], "triggered": [], "exited": []}
    rows = []
    seen = set()
    for c in cards:
        sym = c["symbol"]
        seen.add(sym)
        p = prev.get(sym)
        ps = pitch_status.get(sym)
        status = "in_play" if ps == "active" else "triggered" if (ps == "new" or c["source"] == "pitch") else "watching"
        was_live = bool(p) and p["status"] in LIVE
        added = p["added"] if was_live else t
        if not was_live:
            ch["added"].append({"symbol": sym, "drank": c["drank"]})
        else:
            if p.get("drank") and c["drank"] < p["drank"]:
                ch["up"].append({"symbol": sym, "from": p["drank"], "to": c["drank"]})
            elif p.get("drank") and c["drank"] > p["drank"]:
                ch["down"].append({"symbol": sym, "from": p["drank"], "to": c["drank"]})
        if status == "triggered" and (not was_live or p["status"] == "watching"):
            ch["triggered"].append({"symbol": sym})
        if was_live and (p.get("card") or {}).get("timeline", {}).get("leaves") and c["source"] != "pitch":
            c["timeline"]["leaves"] = p["card"]["timeline"]["leaves"]        # the clock does not restart
        rows.append({"symbol": sym, "status": status, "added": added, "updated": t, "drank": c["drank"],
                     "card": {**c, "status": status}, "changed": (not was_live) or p["status"] != status or p.get("drank") != c["drank"]})
    for sym, p in prev.items():
        if sym in seen or p["status"] not in LIVE:
            continue
        ps = pitch_status.get(sym)
        card = dict(p.get("card") or {})
        leaves = (card.get("timeline") or {}).get("leaves")
        if ps == "active":
            status = "in_play"
        elif ps == "new":
            status = "triggered"
        elif ps == "closed" and p["status"] in ("triggered", "in_play"):
            status = "exited"
            ch["exited"].append({"symbol": sym})
        elif p["status"] in ("triggered", "in_play"):
            status = "exited"
            ch["exited"].append({"symbol": sym})
        elif leaves and t >= leaves:
            status = "dropped"
            ch["removed"].append({"symbol": sym, "why": f"nothing happened by {leaves}"})
        else:
            status = "watching"
            card["stale"] = True                                   # still on the clock, not in today's scan
        drank = 5 if status in ("exited", "dropped") else p.get("drank")
        rows.append({"symbol": sym, "status": status, "added": p["added"], "updated": t, "drank": drank,
                     "card": {**card, "status": status, "drank": drank}, "changed": status != p["status"]})
    return rows, ch


def changed_line(ch: dict) -> str:
    """One line for the top of the morning post."""
    bits = []
    if ch.get("added"):
        bits.append("added " + ", ".join(x["symbol"] for x in ch["added"][:10]) + (" …" if len(ch["added"]) > 10 else ""))
    if ch.get("triggered"):
        bits.append("triggered overnight: " + ", ".join(x["symbol"] for x in ch["triggered"][:8]))
    if ch.get("up"):
        bits.append("rank up: " + ", ".join(f"{x['symbol']} {x['from']}→{x['to']}" for x in ch["up"][:6]))
    if ch.get("down"):
        bits.append("rank down: " + ", ".join(f"{x['symbol']} {x['from']}→{x['to']}" for x in ch["down"][:6]))
    if ch.get("exited"):
        bits.append("exited: " + ", ".join(x["symbol"] for x in ch["exited"][:8]))
    if ch.get("removed"):
        bits.append("removed: " + ", ".join(x["symbol"] for x in ch["removed"][:8]))
    return "; ".join(bits) if bits else "no changes since the last list"


# --------------------------------------------------------------------------- the morning post
def _short(d: str | None) -> str | None:
    try:
        x = date.fromisoformat(d)
        return f"{x:%a} {x.month}/{x.day}"
    except (TypeError, ValueError):
        return None


def card_lines(c: dict) -> str:
    """Two or three lines: rank and grades, the plan, the timeline."""
    st, tl = c["strategy"], c["timeline"]
    head = f"**{c['symbol']}**" + (f" {_px(c.get('price'))}" if c.get("price") else "")
    if c.get("chg_1d") is not None:
        head += f" ({c['chg_1d']:+.1%})"
    head += f" · {RK.grade_line(c.get('grades'))}"
    if st.get("steps") == 0:
        lv = " · ".join(x for x in (f"stop {_px(st['stop'])}" if st.get("stop") else None,
                                    f"T1 {_px(st['t1'])}" if st.get("t1") else None,
                                    f"T2 {_px(st['t2'])}" if st.get("t2") else None,
                                    f"{st['pct_of_fund']:.1%} of fund" if st.get("pct_of_fund") else None) if x)
        plan = f"{BOOK_SHORT.get(st['book'], st['label'])}: buy at the open ~{_px(st.get('entry_ref'))}" + (f" · {lv}" if lv else "")
    elif st.get("book"):
        plan = f"{BOOK_SHORT.get(st['book'], st['label'])}: needs {st['trigger']}"
    else:
        plan = c["why"]
    when = " · ".join(x for x in (
        f"earliest trigger {_short(tl.get('earliest'))}" if st.get("steps") and _short(tl.get("earliest")) else
        f"enter {_short(tl.get('earliest'))}" if st.get("steps") == 0 and _short(tl.get("earliest")) else None,
        f"hold {tl['expected_hold']}" if tl.get("expected_hold") else None,
        f"review {_short(tl.get('review'))}" if _short(tl.get("review")) else None,
        f"earnings {_short(tl.get('earnings'))}" if _short(tl.get("earnings")) else None,
        f"listing {_short(tl.get('listing'))}" if _short(tl.get("listing")) else None,
        f"off the list {_short(tl.get('leaves'))}" if _short(tl.get("leaves")) else None) if x)
    return "\n".join(x for x in (head, plan[:260], when) if x)


def name_of_the_day(rows: list[dict]) -> dict | None:
    live = [r["card"] for r in rows if r["status"] in LIVE and r["card"].get("strategy", {}).get("book")]
    if not live:
        return None
    return sorted(live, key=lambda c: (c["drank"], -(c["strategy"].get("conviction") or 0),
                                       c["strategy"].get("steps") or 0, c["symbol"]))[0]


def name_writeup(c: dict) -> str:
    """The Name of the Day, written from the card's own numbers (no LLM, so nothing to validate)."""
    st, tl = c["strategy"], c["timeline"]
    s = [f"{c['name']} ({c['symbol']}) is DAISY Rank {c['drank']} ({RK.RANK_WORDS.get(c['drank'], '')})."]
    if st.get("steps") == 0:
        s.append(f"The {st['label'].lower()} rule triggered at the last close" + (f" with {st['bucket'].lower()} conviction ({st['conviction']})" if st.get("bucket") else "") + ".")
        if st.get("stop") and st.get("t1"):
            s.append(f"The plan: buy at the open near {_px(st.get('entry_ref'))}, stop {_px(st['stop'])}, first target {_px(st['t1'])}"
                     + (f" (R:R {st['rr']:.1f})" if st.get("rr") else "") + ".")
    else:
        s.append(f"It is {'one step' if st.get('steps') == 1 else str(st.get('steps')) + ' steps'} from the {st['label'].lower()} rule: it still needs {st['trigger']}.")
        if st.get("stop_note"):
            s.append(f"If it triggers: entry at the next open, {st['stop_note']}.")
    g = c.get("grades") or {}
    known = [f"{RK.GRADE_LABEL[k]} {g[k]}" for k in RK.GRADE_KEYS if g.get(k) and g[k] != RK.NA]
    if known:
        s.append("Grades: " + ", ".join(known) + ".")
    if tl.get("earnings"):
        s.append(f"Earnings are due {tl['earnings']}.")
    return " ".join(s)


def embed(rows: list[dict], ch: dict, now: datetime, mood: str | None = None, shocks: list[dict] | None = None,
          screen_day: str | None = None, coverage: dict | None = None, tag: str = "", post: int = 10) -> dict:
    """The morning digest: what changed, then up to ``post`` names grouped by rank, then the name of the
    day. Kept inside Discord's limits (6,000 characters, 25 fields, 1,024 per field)."""
    from .news import shock_sentence
    from .thesis import _fit_embed
    live = [r["card"] for r in rows if r["status"] in LIVE and not r["card"].get("stale")]
    live.sort(key=lambda c: (c["drank"], c["strategy"].get("steps") if c["strategy"].get("steps") is not None else 9, c["symbol"]))
    shown = live[:post]
    title = f"Morning watchlist — {now:%a %b} {now.day}"
    desc = []
    if mood:
        desc.append(mood)
    desc.append(f"**What changed:** {changed_line(ch)}.")
    for sh in (shocks or [])[:2]:
        src = sh.get("source") or {}
        desc.append("**Theme shock:** " + shock_sentence(sh) + (f" [source]({src['url']})" if src.get("url") else ""))
    fields = []
    for k in (1, 2, 3, 4, 5):
        grp = [c for c in shown if c["drank"] == k]
        if not grp:
            continue
        val, used = [], 0
        for c in grp:
            block = card_lines(c)
            if used + len(block) + 2 > 1000:
                val.append(f"… and {len(grp) - len(val)} more on the dashboard")
                break
            val.append(block)
            used += len(block) + 2
        fields.append({"name": f"Rank {k} — {RK.RANK_WORDS[k]}", "value": "\n\n".join(val)[:1024], "inline": False})
    nod = name_of_the_day(rows)
    if nod:
        fields.append({"name": f"Name of the day — {nod['symbol']}", "value": name_writeup(nod)[:1000], "inline": False})
    if not shown:
        desc.append("Nothing is close to a trigger this morning. That is a reading, not a fault.")
    cov = coverage or {}
    foot = "Stock desk · " + (f"screen of {cov['covered']:,} names ({cov['eligible']:,} eligible) · " if cov.get("covered") else "") \
        + (f"as of the {screen_day} close · " if screen_day else "") + "grades M/T/V/Q/N · research, not advice"
    return _fit_embed({"title": (f"[{tag}] " if tag else "") + title, "description": "\n".join(desc)[:3500],
                       "color": 0x446266, "fields": fields[:8], "footer": {"text": foot[:300]}})


# --------------------------------------------------------------------------- did the rank mean anything
def score(adds: list[dict], pitch_days: dict[str, list[str]], closes_fn, bench_fn, horizon: int = 10) -> dict:
    """``adds``: every time a name joined the list (``symbol``, ``day``, ``drank``, ``price``).
    ``pitch_days``: symbol -> the days we pitched it. ``closes_fn(symbol)`` -> a close series.
    Per rank group: how many, how often a pitch followed within 15 days, and the forward return from the
    day it was added (``horizon`` sessions, or to date) against SPY over the same days."""
    import pandas as pd
    groups = {"rank_1_2": (1, 2), "rank_3": (3,), "rank_4_5": (4, 5)}
    spy = bench_fn()
    out = {}
    for name, ks in groups.items():
        rs = [a for a in adds if a.get("drank") in ks]
        trig, rets, exc = 0, [], []
        for a in rs:
            d0 = date.fromisoformat(a["day"])
            if any(d0.isoformat() <= d <= (d0 + timedelta(days=15)).isoformat() for d in pitch_days.get(a["symbol"], [])):
                trig += 1
            s = closes_fn(a["symbol"])
            if s is None or len(s) == 0:
                continue
            after = s[s.index >= pd.Timestamp(a["day"])]
            if len(after) < 2:
                continue
            end = after.index[min(horizon, len(after) - 1)]
            r = float(after.loc[end]) / float(after.iloc[0]) - 1
            rets.append(r)
            if spy is not None and len(spy):
                b = spy[(spy.index >= after.index[0]) & (spy.index <= end)]
                if len(b) >= 2:
                    exc.append(r - (float(b.iloc[-1]) / float(b.iloc[0]) - 1))
        out[name] = {"n": len(rs), "triggered": trig, "trigger_rate": round(trig / len(rs), 4) if rs else None,
                     "marked": len(rets), "avg_ret": round(sum(rets) / len(rets), 5) if rets else None,
                     "avg_excess": round(sum(exc) / len(exc), 5) if exc else None}
    out["horizon_sessions"] = horizon
    return out
