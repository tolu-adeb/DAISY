"""Live vs backtest, every day (docs/14.12).

Every lab result assumes the backtest is what the live bot does.  This check takes a finished session and asks: did
the live bot take the trades the backtest takes on that day, and if not, why?

With the day's recording (``data/live/<day>/``: the morning's inputs and every bar the bot processed) it changes one
input at a time, so the cause is found rather than guessed:

    L   what happened live                       (the bot's trades table)
    R1  replay: live bars + live inputs           should equal L; if not -> the bot wasn't running, started late,
                                                  restarted, or a signal was replayed after a restart
    R2  R1 without the adaptive filter            differs from R1 -> the filter changed the day (the labs run without it)
    R3  R2 with the backtest's context            differs from R2 -> the morning context differs (trend read, follow/fade
                                                  mode, macro events, levels)
    B   the backtest on the lab's dataset          differs from R3 -> the price bars differ (live 1-min QQQ ticks vs the
                                                  dataset's 5-min bars)

Without a recording (days before it existed) only L vs B is compared, plus the morning context from the bot's brief.
Results: ``data/parity/<day>.json``.  Read-only for everything else: it never touches the bot, the labs or Discord.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import fields
from datetime import date, datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
PRICE_FIELDS = ("pdh", "pdl", "pdc", "onh", "onl", "open_ref", "sma_long", "atr_d")


# ---------------------------------------------------------------------- inputs
def live_day(data_dir: Path, day: str) -> dict:
    """The bot's own record of ``day``: first brief (inputs), trades, how each signal was delivered, first/last event."""
    p = Path(data_dir) / "alpha.sqlite3"
    out = {"brief": None, "trades": [], "signals": {}, "first_event": None, "last_event": None, "briefs": 0,
           "_event_times": []}
    if not p.exists():
        return out
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        for ts, ty, data, dl in c.execute("SELECT ts, type, data, delivered FROM events WHERE day=? ORDER BY id", (day,)):
            try:
                ev = json.loads(data)
            except ValueError:
                continue
            out["first_event"] = out["first_event"] or ts
            out["last_event"] = ts
            out["_event_times"].append(ts)
            if ty == "brief":
                out["briefs"] += 1
                if out["brief"] is None:
                    out["brief"] = {**ev, "at": ts}
            elif ty == "signal":
                t = ev.get("trade") or {}
                out["signals"][str(t.get("id"))] = {"delivered": dl, "at": ts, "opened": t.get("opened")}
        for (data,) in c.execute("SELECT data FROM trades WHERE day=? ORDER BY num", (day,)):
            try:
                out["trades"].append(json.loads(data))
            except ValueError:
                pass
    finally:
        c.close()
    return out


def ctx_from(d: dict):
    from abg.alpha.context import DayContext
    names = {f.name for f in fields(DayContext)}
    kw = {k: v for k, v in (d or {}).items() if k in names}
    kw["day"] = date.fromisoformat(str(kw["day"]))
    return DayContext(**kw)


def scaled_ctx(ctx, k: float):
    """A context moved onto another price scale (the dataset is QQQ x a fixed ratio; live levels are futures prices)."""
    from dataclasses import replace
    ch = {f: getattr(ctx, f) * k for f in PRICE_FIELDS if getattr(ctx, f) is not None}
    return replace(ctx, **ch)


# ---------------------------------------------------------------------- running one day
def _day_trades(params, ctx, warm, bars, learner=None) -> list[dict]:
    from abg.alpha.strategy import AlphaStrategy
    s = AlphaStrategy(params, learner)
    ev = s.start_day(ctx, warm)
    for b in bars:
        ev += s.on_bar(b)
    ev += s.end_day()
    return ev[-1]["trades"]


def backtest_day(ds_bars, trend_daily, params, day: date) -> dict:
    """The backtest's trades and context for one session (exactly what run_backtest does for that day)."""
    from abg.alpha.backtest import _warmup
    from abg.alpha.context import contexts_from_intraday
    from abg.alpha.data import bar_minutes, iter_bars, split_sessions
    ctxs = contexts_from_intraday(ds_bars, None, None, params, trend_daily)
    if day not in ctxs:
        return {"available": False}
    ctx, rth = ctxs[day]
    minutes = bar_minutes(ds_bars)
    warm = _warmup(split_sessions(ds_bars), day, minutes)
    bars = list(iter_bars(rth, minutes))
    return {"available": True, "ctx": ctx, "warm": warm, "bars": bars, "trades": _day_trades(params, ctx, warm, bars)}


# ---------------------------------------------------------------------- comparing
def _slim(t: dict, k: float = 1.0) -> dict:
    return {"setup": t.get("setup"), "side": t.get("side"), "opened": str(t.get("opened"))[11:16],
            "entry": round(float(t["entry"]) * k, 2) if t.get("entry") is not None else None,
            "exit": t.get("exit_reason"), "r": round(float(t.get("r") or 0), 3)}


def _mins(hm: str) -> int:
    try:
        h, m = hm.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return -9999


def compare(a: list[dict], b: list[dict], tol_min: int = 15) -> dict:
    """Pair trades by setup + side + entry time (within ``tol_min``).  same = nothing unpaired and every pair ends alike."""
    a, b = [dict(x) for x in a], [dict(x) for x in b]
    pairs, used = [], set()
    for x in a:
        best = None
        for j, y in enumerate(b):
            if j in used or y["setup"] != x["setup"] or y["side"] != x["side"]:
                continue
            dm = abs(_mins(x["opened"]) - _mins(y["opened"]))
            if dm <= tol_min and (best is None or dm < best[0]):
                best = (dm, j)
        if best is not None:
            used.add(best[1])
            y = b[best[1]]
            pairs.append({"a": x, "b": y, "same_exit": x["exit"] == y["exit"], "r_diff": round(x["r"] - y["r"], 3)})
    only_a = [x for x in a if not any(p["a"] is x for p in pairs)]
    only_b = [y for j, y in enumerate(b) if j not in used]
    same = not only_a and not only_b and all(p["same_exit"] and abs(p["r_diff"]) <= 0.15 for p in pairs)
    return {"same": same, "pairs": pairs, "only_first": only_a, "only_second": only_b,
            "r_first": round(sum(x["r"] for x in a), 2), "r_second": round(sum(x["r"] for x in b), 2)}


def ctx_diff(live: dict, bt: dict, k: float) -> list[str]:
    """Plain-language differences in the morning context that can change a trade (bt levels moved to the live scale)."""
    out = []
    tl, tb = live.get("trend_long"), bt.get("trend_long")
    if tl != tb:
        word = lambda x: {1: "UP", -1: "DOWN"}.get(x, "none")  # noqa: E731
        out.append(f"daily trend read: live {word(tl)}, backtest {word(tb)} ({live.get('trend_len') or 50}-day average)")
    if (live.get("orx_mode") or "off") != (bt.get("orx_mode") or "off"):
        out.append(f"follow/fade mode: live {live.get('orx_mode')}, backtest {bt.get('orx_mode')}")
    le = [e.get("name") for e in live.get("events") or []]
    be = [e.get("name") for e in bt.get("events") or []]
    if le != be:
        out.append(f"macro events: live {', '.join(le) or 'none'}; backtest {', '.join(be) or 'none'} "
                   "(the backtest has no calendar, so it ignores the news windows)")
    if (live.get("onh") is not None) != (bt.get("onh") is not None):
        out.append("overnight high/low: " + ("live has them; the backtest's dataset is regular hours only"
                                             if live.get("onh") is not None else "the backtest has them; live didn't"))
    for f in ("pdh", "pdl", "pdc"):
        x, y = live.get(f), bt.get(f)
        if x is not None and y is not None and abs(x - y * k) > 15:
            out.append(f"yesterday's {f[2:].upper() if f != 'pdc' else 'close'}: live {x:,.2f}, backtest {y * k:,.2f} "
                       f"({x - y * k:+.1f} pts on the live scale)")
    if (live.get("bias_label") or "") != (bt.get("bias_label") or ""):
        out.append(f"day bias: live {live.get('bias_label')}, backtest {bt.get('bias_label')}")
    return out


def bars_diff(live_bars, bt_bars, k: float) -> dict:
    """Live 1-min bars resampled to 5 minutes vs the dataset's 5-min bars (dataset moved to the live scale)."""
    five: dict[str, list] = {}
    for b in live_bars:
        key = b.ts.replace(minute=b.ts.minute - b.ts.minute % 5, second=0, microsecond=0).strftime("%H:%M")
        five.setdefault(key, []).append(b)
    bt = {b.ts.strftime("%H:%M"): b for b in bt_bars}
    diffs = []
    for key, y in bt.items():
        xs = five.get(key)
        if xs:
            diffs.append(abs(xs[-1].close - y.close * k))
    expected = 390 if bt_bars else 0
    have = len(live_bars) if live_bars and live_bars[0].minutes == 1 else len(live_bars) * 5
    return {"compared": len(diffs), "max_close_diff": round(max(diffs), 2) if diffs else None,
            "median_close_diff": round(statistics.median(diffs), 2) if diffs else None,
            "missing_minutes": max(0, expected - have), "first_bar": live_bars[0].ts.strftime("%H:%M") if live_bars else None}


# ---------------------------------------------------------------------- the check
def check(data_dir: Path, day: str, ds_bars, trend_daily, uptime=None) -> dict:
    from abg.alpha.daylog import DayLog, bar_from
    from abg.alpha.learn import AdaptiveBook
    from abg.alpha.strategy import AlphaParams, load_saved_params
    data_dir = Path(data_dir)
    d = date.fromisoformat(day)
    live = live_day(data_dir, day)
    pack, rows = DayLog(data_dir / "live").load(day)
    brief = live.get("brief") or {}
    pdict = (pack or {}).get("params") or brief.get("params")
    if pdict:
        params = AlphaParams.from_dict(pdict)
    else:
        try:
            params = load_saved_params(json.loads((data_dir / "alpha_params.json").read_text()))
        except (OSError, ValueError):
            params = AlphaParams()
    out = {"day": day, "checked_at": datetime.now(NY).isoformat(timespec="minutes"), "recorded": bool(pack and rows),
           "steps": [], "causes": [], "notes": []}
    missing = sorted(set(AlphaParams().to_dict()) - set(pdict or {})) if pdict else []
    if missing:
        out["older_strategy"] = missing
    bt = backtest_day(ds_bars, trend_daily, params, d)
    if not bt["available"]:
        out.update(verdict="no backtest", why=f"the lab's dataset has no {day} session yet")
        return out
    # the price scale between the live feed and the dataset
    k = 1.0
    live_bars = [bar_from(r) for r in rows] if rows else []
    if live_bars and bt["bars"]:
        from datetime import timedelta
        m = {(b.ts + timedelta(minutes=b.minutes - 1)).strftime("%H:%M"): b.close for b in bt["bars"]}   # the last minute of each bar
        rr = [b.close / m[b.ts.strftime("%H:%M")] for b in live_bars if b.minutes == 1 and b.ts.strftime("%H:%M") in m]
        k = statistics.median(rr) if rr else k
    elif brief.get("ctx", {}).get("pdc") and bt["ctx"].pdc:
        k = brief["ctx"]["pdc"] / bt["ctx"].pdc
    out["scale"] = round(k, 5)
    L = [_slim(t) for t in live["trades"]]
    B = [_slim(t, k) for t in bt["trades"]]
    out["live"] = L
    out["backtest"] = B
    # uptime and delivery
    first = live.get("first_event")
    if live["brief"] is None:
        out["causes"].append("the bot didn't run this day (no morning brief in its log)"
                             + (" - the backtest traded" if bt["trades"] else ""))
    elif first and datetime.fromtimestamp(first, NY).time() > dtime(9, 31):
        out["causes"].append(f"the bot started at {datetime.fromtimestamp(first, NY):%H:%M} ET, after the 9:30 open")
    for t in live["trades"]:
        sg = live["signals"].get(str(t.get("id"))) or {}
        if sg.get("delivered", "").startswith("late"):
            seen = datetime.fromtimestamp(sg["at"], NY).strftime("%H:%M")
            out["causes"].append(f"live trade #{t.get('id')} ({str(t.get('opened'))[11:16]}) was replayed after a restart "
                                 f"(seen at {seen} ET) and never posted")
    if uptime is not None:
        cov = uptime.coverage(d)
        out["uptime"] = cov
        for t in B:
            ts = datetime.combine(d, dtime(*map(int, t["opened"].split(":"))), NY).timestamp()
            if cov["pct"] and not uptime.running_at(ts):
                out["causes"].append(f"the bot wasn't running at {t['opened']} ET, when the backtest's {t['setup']} trade triggered")
    if live["briefs"] > 1:
        out["notes"].append(f"{live['briefs']} morning briefs this day (restarts)")
    lc = (pack or {}).get("ctx") or brief.get("ctx") or {}
    btc = bt["ctx"].to_dict()
    cdiff = ctx_diff(lc, btc, k) if lc else []
    out["ctx_diff"] = cdiff
    LB = compare(L, B)
    out["steps"].append({"name": "live vs backtest", **LB})
    if pack and live_bars:
        warm = [bar_from(b) for b in pack.get("warm") or []]
        lctx = ctx_from(pack["ctx"])
        learner = AdaptiveBook.from_dict(pack["learner"]) if pack.get("learner") else None
        R1 = [_slim(t) for t in _day_trades(params, lctx, warm, live_bars, learner)]
        R2 = [_slim(t) for t in _day_trades(params, lctx, warm, live_bars, None)]
        R3 = [_slim(t) for t in _day_trades(params, scaled_ctx(bt["ctx"], k), warm, live_bars, None)]
        s1, s2, s3, s4 = compare(L, R1), compare(R1, R2), compare(R2, R3), compare(R3, B)
        out["steps"] += [{"name": "live vs replay of what it saw", **s1}, {"name": "adaptive filter on vs off", **s2},
                         {"name": "live context vs the backtest's", **s3}, {"name": "live bars vs the dataset's", **s4}]
        if not s1["same"] and not out["causes"]:
            out["causes"].append("the replay of the recorded bars doesn't match what the bot did - it may have missed bars "
                                 "or restarted mid-trade")
        if not s2["same"]:
            out["causes"].append("the bot's adaptive filter changed the day (the labs and the backtest run without it)")
        if not s3["same"]:
            out["causes"].append("the morning context differs: " + ("; ".join(cdiff) if cdiff else "see the context fields"))
        if not s4["same"]:
            bd = bars_diff(live_bars, bt["bars"], k)
            out["bars"] = bd
            out["causes"].append(f"the price bars differ (live 1-min bars from QQQ vs the dataset's 5-min bars): 5-min closes "
                                 f"differ by up to {bd['max_close_diff']} pts" + (f", {bd['missing_minutes']} minutes missing"
                                                                                  if bd["missing_minutes"] else ""))
    else:
        out["notes"].append("no recording of this day's bars (recording started with this update), so the live bars "
                            "can't be replayed")
        if lc and not LB["same"]:
            # the dataset's bars with the live morning context: does the context alone explain the difference?
            BL = [_slim(t, k) for t in _day_trades(params, scaled_ctx(ctx_from(lc), 1 / k), bt["warm"], bt["bars"])]
            s = compare(BL, B)
            out["steps"].append({"name": "backtest with the live context vs the backtest", **s})
            if not s["same"]:
                out["causes"].append("the morning context differs: " + ("; ".join(cdiff) if cdiff else "see the context fields"))
            else:
                out["notes"].append("with the live morning context the backtest still trades the same way, so the "
                                    "context doesn't explain it" + (f" ({'; '.join(cdiff)})" if cdiff else ""))
                gap = _silence(live, d, B)
                out["causes"].append(gap or "the live bars or the bot's uptime that morning (not recorded before this update)")
    if missing and not LB["same"]:
        out["causes"].insert(0, "the bot ran an older version of the strategy that day (settings it didn't have: "
                                + ", ".join(missing[:6]) + ("..." if len(missing) > 6 else "") + "), so the backtest of "
                                "today's code isn't a fair comparison")
    out["verdict"] = ("match" if LB["same"] else "no live data" if live["brief"] is None
                      else "older strategy" if missing else "differs")
    if out["verdict"] == "differs" and not out["causes"]:
        out["causes"].append("not found from the recorded inputs")
    return out


def _silence(live: dict, d: date, bt_trades: list[dict]) -> str | None:
    """'The bot logged nothing between 09:25 and 12:28 ET' when a backtest trade falls inside such a silence."""
    p_first, p_last = live.get("first_event"), live.get("last_event")
    if not p_first or not bt_trades:
        return None
    ts = sorted(x for x in (live.get("_event_times") or []) if x)
    if not ts:
        return None
    for a, b in zip(ts, ts[1:]):
        if b - a < 1800:
            continue
        for t in bt_trades:
            tt = datetime.combine(d, dtime(*map(int, t["opened"].split(":"))), NY).timestamp()
            if a < tt < b:
                return (f"the bot logged nothing between {datetime.fromtimestamp(a, NY):%H:%M} and "
                        f"{datetime.fromtimestamp(b, NY):%H:%M} ET, which covers the backtest's {t['opened']} trade - it "
                        "probably wasn't receiving bars (or wasn't running)")
    return None


# ---------------------------------------------------------------------- storage + the daily job
def home(data_dir: Path) -> Path:
    p = Path(data_dir) / "parity"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save(data_dir: Path, res: dict) -> None:
    p = home(data_dir) / f"{res['day']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, default=str), encoding="utf-8")
    tmp.replace(p)


def recent(data_dir: Path, n: int = 15) -> list[dict]:
    out = []
    for p in sorted(home(data_dir).glob("*.json"), reverse=True)[:n]:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return out


def summary(data_dir: Path, n: int = 20) -> dict:
    rows = recent(data_dir, n)
    judged = [r for r in rows if r.get("verdict") in ("match", "differs")]
    lr = sum(sum(t["r"] for t in r.get("live") or []) for r in judged)
    br = sum(sum(t["r"] for t in r.get("backtest") or []) for r in judged)
    return {"days": len(rows), "judged": len(judged), "matched": sum(1 for r in judged if r["verdict"] == "match"),
            "live_r": round(lr, 2), "backtest_r": round(br, 2), "rows": rows}


def _first_logged_day(data_dir: Path) -> str | None:
    p = Path(data_dir) / "alpha.sqlite3"
    if not p.exists():
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        r = c.execute("SELECT MIN(day) FROM events").fetchone()
    finally:
        c.close()
    return r[0] if r else None


def run_recent(data_dir: Path, bars_csv: Path, trend_csv: Path | None, sessions: int = 5, uptime=None) -> list[dict]:
    """Check the last ``sessions`` sessions of the dataset that the bot has a log for (re-checks a day only while it
    is among the most recent, so a later fix to the dataset is picked up)."""
    from abg.agents.lab.dataset import load_dataset
    ds = load_dataset(bars_csv, trend_csv, 0)
    out = []
    first_day = _first_logged_day(data_dir)
    for d in ds.sessions[-sessions:]:
        if first_day is None or str(d) < first_day:          # before the bot's log starts: nothing to compare
            continue
        res = check(data_dir, str(d), ds.bars, ds.trend_daily, uptime)
        save(data_dir, res)
        out.append(res)
    return out
