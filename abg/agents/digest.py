"""R&D desk digests: daily / weekly / monthly findings for Discord and the dashboard's R&D tab (docs/17).

A digest answers four questions for its period, from data the terminal already keeps:

1. **How did the bot do?**  Paper trades from ``alpha.sqlite3`` - win rate, R, $ per micro, drawdown - against
   what the backtest says to expect, with the sampling error spelled out (a 5-trade week proves nothing).
2. **Was it running?**  Did a pre-market brief go out before the open each session; were signals held back as stale.
3. **What did R&D do?**  Strategy Lab trials, candidates, holdout looks, proposals waiting, agent research reports.
4. **Is the edge holding?**  (weekly / monthly) the current settings re-run on the most recent months of the
   lab dataset, next to their two-year numbers - the regime watch.

The findings are rule-based (deterministic); with an API key a short analyst note from Claude (Strands) is added on
top.  Nothing here trades or changes settings.
"""
from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
COMMISSION = 1.24
POINT = 2.0
COLORS = {"daily": 0x3B82F6, "weekly": 0x8B5CF6, "monthly": 0x10B981}


# --------------------------------------------------------------------------- periods
def period_window(period: str, ref: date) -> tuple[date, date, str]:
    if period == "daily":
        return ref, ref, ref.strftime("%a %b %d, %Y")
    if period == "weekly":
        start = ref - timedelta(days=ref.weekday())
        return start, start + timedelta(days=4), f"week of {start:%b %d, %Y}"
    if period == "monthly":
        start = ref.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return start, nxt - timedelta(days=1), start.strftime("%B %Y")
    raise ValueError("period must be daily, weekly or monthly")


def sessions_between(a: date, b: date, today: date | None = None) -> list[date]:
    from ..live.market_hours import is_trading_day
    today = today or datetime.now(NY).date()
    out, d = [], a
    while d <= min(b, today):
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- 1-2. the bot
def _maxdd(xs: list[float]) -> float:
    peak = cum = dd = 0.0
    for x in xs:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def bot_section(data_dir: Path, start: date, end: date, now: datetime | None = None) -> dict:
    from ..alpha.bot import AlphaStore
    now = now or datetime.now(NY)
    f = Path(data_dir) / "alpha.sqlite3"
    out: dict[str, Any] = {"trades": 0}
    if not f.exists():
        return {**out, "note": "no bot database yet (ABG_ALPHA_ENABLED=true and restart the monitor)"}
    try:
        st = AlphaStore(f)
        trades = [t for t in st.trades(5000) if start.isoformat() <= t["date"] <= end.isoformat()]
        sess = sessions_between(start, end, now.date())
        up, late_start, down, stale = [], [], [], 0
        for d in sess:
            evs = st.events(d.isoformat(), 500)
            if d == now.date() and now.time() < datetime.strptime("09:30", "%H:%M").time():
                continue
            briefs = [e for e in evs if e["type"] == "brief"]
            stale += sum(1 for e in evs if e.get("delivered") == "late - not posted")
            if not evs:
                down.append(d.isoformat())
            elif briefs and datetime.fromtimestamp(min(e["at"] for e in briefs), NY).time() <= datetime.strptime("09:31", "%H:%M").time():
                up.append(d.isoformat())
            else:
                late_start.append(d.isoformat())
    except Exception as e:                                   # a locked / synced DB must never kill a digest
        return {**out, "note": f"could not read the bot database: {type(e).__name__}"}
    out.update({"sessions": len(sess), "up_at_open": up, "late_start": late_start, "down": down, "held_back_stale": stale})
    if not trades:
        return out
    trades.sort(key=lambda t: (t["date"], t.get("opened", "")))
    pts = [float(t["pts"]) for t in trades]
    usd = [p * POINT - COMMISSION for p in pts]
    by_day: dict[str, float] = {}
    for t, u in zip(trades, usd):
        by_day[t["date"]] = by_day.get(t["date"], 0.0) + u
    wins = [p for p in pts if p > 0.5]
    best = max(trades, key=lambda t: t["pts"])
    worst = min(trades, key=lambda t: t["pts"])
    exits: dict[str, int] = {}
    for t in trades:
        exits[t.get("exit_reason", "?")] = exits.get(t.get("exit_reason", "?"), 0) + 1
    out.update({"trades": len(trades), "wins": len(wins), "win_rate": len(wins) / len(trades),
                "net_r": round(sum(float(t["r"]) for t in trades), 2), "net_pts": round(sum(pts), 2),
                "net_usd": round(sum(usd), 2), "max_dd_usd": round(_maxdd([by_day[d] for d in sorted(by_day)]), 2),
                "best": {"date": best["date"], "pts": best["pts"], "r": best["r"], "setup": best.get("setup")},
                "worst": {"date": worst["date"], "pts": worst["pts"], "r": worst["r"], "setup": worst.get("setup")},
                "exits": exits, "list": [{k: t.get(k) for k in ("date", "opened", "side_label", "setup", "entry", "exit",
                                                                 "exit_reason", "pts", "r")} for t in trades[-12:]]})
    return out


# --------------------------------------------------------------------------- 3-4. the lab
def _lab(bars_csv=None, trend_csv=None, data_dir=".", holdout_months: int = 6):
    from .lab import Lab
    return Lab.from_paths(bars_csv, trend_csv, data_dir, holdout_months)


def fresh_holdout_eta(lab) -> str | None:
    """Earliest last-session date at which a new 6-month holdout overlaps the used one by < 50%."""
    if lab.peeks_used() < lab.gate.peek_budget or not lab.ds.holdout:
        return None
    h0, h1 = lab.ds.holdout[0], lab.ds.holdout[-1]
    span = (h1 - h0).days
    return (h1 + timedelta(days=span // 2 + 1)).isoformat()


def lab_section(lab, t0: float, t1: float) -> dict:
    trials = [t for t in lab.ledger.trials(5000) if t0 <= (t.get("ts") or 0) < t1]
    peeks_total = lab.peeks_used()
    reports = sorted((Path(lab.ledger.path).parent / "reports").glob("run-*.md"))
    new_reports = [r for r in reports if t0 <= r.stat().st_mtime < t1]
    pend = lab.ledger.proposals("pending")
    by: dict[str, int] = {}
    for t in trials:
        by[t["verdict"]] = by.get(t["verdict"], 0) + 1
    rep_txt = None
    if new_reports:
        txt = new_reports[-1].read_text(encoding="utf-8", errors="replace")
        rep_txt = next((ln.strip() for ln in txt.splitlines() if ln.strip().startswith("**Result")), txt.strip()[:300])
    return {"trials_in_period": len(trials), "verdicts": by,
            "candidates": [{"id": t["id"], "name": t["name"], "changes": t["changes"],
                            "win_rate": (t.get("summary") or {}).get("win_rate"), "ratio": (t.get("summary") or {}).get("ratio")}
                           for t in trials if t["verdict"] in ("candidate", "confirmed")],
            "trials_total": lab.ledger.count_trials(), "holdout_looks": peeks_total, "look_budget": lab.gate.peek_budget,
            "fresh_holdout_from": fresh_holdout_eta(lab), "proposals_pending": [{"id": p["id"], "name": p["name"]} for p in pend],
            "reports_in_period": [r.name for r in new_reports], "latest_report_result": rep_txt,
            "data_last_session": str(lab.ds.sessions[-1])}


def regime_watch(lab, months: int = 3) -> dict | None:
    """The current settings on the last ``months`` of the lab dataset vs all of it (cached backtests)."""
    from .lab.runner import metrics
    try:
        run = lab.runner.run_many([lab.base])[0]
    except Exception:
        return None
    sess = [str(d) for d in lab.ds.sessions]
    cut = (lab.ds.sessions[-1] - timedelta(days=int(months * 30.5))).isoformat()
    recent = [d for d in sess if d > cut]
    r, a = metrics(run["trades"], recent), metrics(run["trades"], sess)
    keep = ("trades", "win_rate", "net_usd", "max_dd_usd", "ratio", "avg_r")
    return {"months": months, "recent": {k: r[k] for k in keep}, "all": {k: a[k] for k in keep},
            "from": recent[0] if recent else None, "to": sess[-1]}


# --------------------------------------------------------------------------- findings
def _se(p: float, n: int) -> float:
    return math.sqrt(max(p * (1 - p), 1e-9) / max(n, 1))


def findings(d: dict) -> list[str]:
    out: list[str] = []
    b, exp, lab, rw = d.get("bot") or {}, d.get("expected") or {}, d.get("lab") or {}, d.get("regime")
    n = b.get("trades", 0)
    if n:
        wr, ewr = b["win_rate"], exp.get("win_rate")
        if ewr:
            z = (wr - ewr) / _se(ewr, n)
            verdict = ("in line with" if abs(z) < 1 else "a little off" if abs(z) < 2 else "well off")
            out.append(f"Win rate {wr:.0%} on {n} trades vs {ewr:.0%} expected from the backtest - {verdict} "
                       f"(±{_se(ewr, n):.0%} is normal noise at this sample size).")
        if exp.get("avg_r") is not None:
            out.append(f"Net {b['net_r']:+.2f}R ({b['net_usd']:+,.0f} $/micro) vs about {exp['avg_r'] * n:+.2f}R expected for {n} trades.")
    elif b.get("sessions"):
        out.append("No paper trades this period" + (" - the bot wasn't running at most opens." if len(b.get("up_at_open", [])) < b["sessions"] / 2 else "."))
    miss = len(b.get("late_start", [])) + len(b.get("down", []))
    if b.get("sessions") and miss:
        out.append(f"Running at the open on {len(b.get('up_at_open', []))} of {b['sessions']} sessions "
                   f"(late: {', '.join(b.get('late_start', [])) or 'none'}; off: {', '.join(b.get('down', [])) or 'none'}) - "
                   "missed opens are missed trades; check the autostart task or the server.")
    if b.get("held_back_stale"):
        out.append(f"{b['held_back_stale']} trade message(s) were held back as stale (the bars arrived late) - check the price feed.")
    if lab:
        if lab.get("trials_in_period"):
            v = lab.get("verdicts", {})
            out.append(f"R&D tested {lab['trials_in_period']} hypotheses ({v.get('candidate', 0)} new candidates, "
                       f"{v.get('rejected', 0)} rejected, {v.get('failed_holdout', 0)} failed the holdout, {v.get('confirmed', 0)} confirmed).")
        if lab.get("proposals_pending"):
            out.append("Waiting for you: " + ", ".join(f"proposal #{p['id']} {p['name']}" for p in lab["proposals_pending"])
                       + " - `abg lab proposals`, then approve or reject.")
        if lab.get("fresh_holdout_from"):
            out.append(f"Holdout looks used ({lab['holdout_looks']}/{lab['look_budget']}): nothing can be confirmed until the "
                       f"dataset reaches {lab['fresh_holdout_from']} (data currently ends {lab['data_last_session']}).")
        if lab.get("latest_report_result"):
            out.append("Agent research: " + lab["latest_report_result"].replace("**", ""))
    if rw and rw.get("recent", {}).get("trades"):
        r, a = rw["recent"], rw["all"]
        trend = ("holding" if r["win_rate"] >= a["win_rate"] - 0.03 and r["avg_r"] >= 0.5 * a["avg_r"] else "weakening")
        mo = f"{rw['months']} month{'s' if rw['months'] != 1 else ''}"
        out.append(f"Regime watch (last {mo} of data, to {rw['to']}): win rate {r['win_rate']:.0%} vs {a['win_rate']:.0%} "
                   f"over all data, avg {r['avg_r']:+.2f}R vs {a['avg_r']:+.2f}R, net/DD {r['ratio']:.2f} - edge {trend}.")
    return out


def analyst_note(settings, digest: dict) -> str | None:
    """Optional 3-4 sentence note from Claude via Strands (needs ABG_ANTHROPIC_API_KEY)."""
    if not getattr(settings, "anthropic_api_key", None):
        return None
    try:
        from strands import Agent

        from .graph import build_model
        model, _ = build_model(settings)
        agent = Agent(model=model, callback_handler=None, system_prompt=(
            "You are the R&D analyst for a paper-traded MNQ futures signal bot. Write 3-4 plain sentences for the owner: "
            "what matters in this period's numbers and what to do next. Never present backtests as promises, never advise "
            "live trading, and say when a sample is too small to conclude anything."))
        slim = {k: digest.get(k) for k in ("period", "label", "bot", "expected", "lab", "regime", "findings")}
        if slim.get("bot"):
            slim["bot"] = {k: v for k, v in slim["bot"].items() if k != "list"}
        return str(agent(json.dumps(slim, default=str)[:12000])).strip()[:900]
    except Exception:
        return None


# --------------------------------------------------------------------------- build / render / post / store
def build_digest(settings, period: str, ref: date | None = None, lab=None, with_note: bool = True,
                 now: datetime | None = None) -> dict:
    now = now or datetime.now(NY)
    ref = ref or now.date()
    start, end, label = period_window(period, ref)
    d: dict[str, Any] = {"period": period, "label": label, "start": start.isoformat(), "end": end.isoformat(),
                         "built": now.isoformat(timespec="minutes")}
    data_dir = Path(settings.data_dir).expanduser()
    d["bot"] = bot_section(data_dir, start, end, now)
    try:
        lab = lab or _lab(None, None, data_dir, settings.lab_holdout_months)
    except Exception as e:
        lab, d["lab_error"] = None, f"{type(e).__name__}: {e}"[:200]
    if lab is not None:
        from .lab.runner import metrics
        try:
            run = lab.runner.run_many([lab.base])[0]
            m = metrics(run["trades"], [str(x) for x in lab.ds.sessions])
            d["expected"] = {"win_rate": m["win_rate"], "avg_r": m["avg_r"],
                             "trades_per_session": round(m["trades"] / max(1, len(lab.ds.sessions)), 3)}
        except Exception:
            d["expected"] = {}
        t0 = datetime.combine(start, datetime.min.time(), NY).timestamp()
        t1 = datetime.combine(end + timedelta(days=1), datetime.min.time(), NY).timestamp()
        d["lab"] = lab_section(lab, t0, t1)
        if period in ("weekly", "monthly"):
            d["regime"] = regime_watch(lab, 3 if period == "monthly" else 1)
    d["findings"] = findings(d)
    if with_note:
        d["note"] = analyst_note(settings, d)
    return d


def render_discord(d: dict) -> dict:
    title = {"daily": "🧪 R&D daily", "weekly": "🧪 R&D weekly", "monthly": "🧪 R&D monthly"}[d["period"]] + f" — {d['label']}"
    b = d.get("bot") or {}
    fields = []
    if b.get("trades"):
        fields.append({"name": "📈 Bot (paper)", "inline": False, "value":
                       f"{b['trades']} trades · win {b['win_rate']:.0%} · {b['net_r']:+.2f}R · {b['net_usd']:+,.0f} $/micro · "
                       f"max DD {b['max_dd_usd']:,.0f} $\nbest {b['best']['pts']:+.1f} pts ({b['best']['date']}) · worst "
                       f"{b['worst']['pts']:+.1f} pts ({b['worst']['date']})"})
    elif b.get("note"):
        fields.append({"name": "📈 Bot (paper)", "value": b["note"], "inline": False})
    if b.get("sessions"):
        fields.append({"name": "⏱️ Uptime", "inline": True,
                       "value": f"up at the open {len(b.get('up_at_open', []))}/{b['sessions']} sessions"})
    lab = d.get("lab") or {}
    if lab:
        fields.append({"name": "🔬 Strategy Lab", "inline": True,
                       "value": f"{lab.get('trials_in_period', 0)} new trials · {lab.get('trials_total', 0)} total · holdout looks "
                                f"{lab.get('holdout_looks', 0)}/{lab.get('look_budget', 0)}"})
    rw = d.get("regime")
    if rw and rw.get("recent", {}).get("trades"):
        r = rw["recent"]
        fields.append({"name": f"🌡️ Regime watch (last {rw['months']} mo of data)".replace("1 mo of", "month of"), "inline": False,
                       "value": f"win {r['win_rate']:.0%} · avg {r['avg_r']:+.2f}R · net/DD {r['ratio']:.2f} "
                                f"(all data: win {rw['all']['win_rate']:.0%}, avg {rw['all']['avg_r']:+.2f}R)"})
    desc = "\n".join(f"• {x}" for x in d.get("findings") or ["Nothing notable."])
    if d.get("note"):
        desc += f"\n\n**Analyst note:** {d['note']}"
    return {"username": "ABG R&D", "allowed_mentions": {"parse": []},
            "embeds": [{"title": title[:250], "description": desc[:3900], "color": COLORS[d["period"]], "fields": fields[:10],
                        "footer": {"text": "ABG Strategy Lab · paper results and backtests · not financial advice"}}]}


def store_dir(settings) -> Path:
    p = Path(settings.data_dir).expanduser() / "rd"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_digest(settings, d: dict, delivered: str) -> Path:
    d = {**d, "delivered": delivered, "saved": time.time()}
    f = store_dir(settings) / f"{d['period']}-{d['start']}.json"
    f.write_text(json.dumps(d, default=str, indent=1), encoding="utf-8")
    return f


def recent_digests(settings, limit: int = 12) -> list[dict]:
    files = sorted(store_dir(settings).glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return out


async def post_digest(settings, http, d: dict) -> str:
    url = getattr(settings, "rd_webhook_url", None)
    if not url:
        return "no webhook (set ABG_RD_WEBHOOK_URL in .env)"
    try:
        await http.request("POST", url + ("&" if "?" in url else "?") + "wait=true", provider="discord", json=render_discord(d))
        return "posted"
    except Exception as e:
        return f"failed: {type(e).__name__}"[:120]
