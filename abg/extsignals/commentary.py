"""Entry grading and the analysis attached to every tracked-idea event.

Every message the tracker sends (ingest, entry timing, scale-in, entry changes, scale-outs, take
profits, stop warnings / moves / hits, exits, advisories, earnings warnings …) is built from the
same sections, so a reader always gets the decision *and* the reasoning:

    title      what happened, in one line (with the auto-classified signal type)
    summary    the decision in plain words
    why        the evidence for this decision right now
    context    market structure: trend vs 50/200-day, momentum, where price sits vs the plan in ATR
               terms, support / resistance vs the levels, volatility, volume, valuation, news, earnings
    thesis     the source's own case: setup, classification, reason, catalyst, flagged risks, scores,
               stated vs computed reward:risk
    plan       levels, sizing, scale-in / scale-out plan, what happens next
    risks      evidence against the trade
    watch      what would invalidate or change the view
"""
from __future__ import annotations

import math
import time
from datetime import date

from .lifecycle import Idea, open_tranches

GRADE_ORDER = ["D", "C", "B", "A"]
EMOJI = {"ingested": "📥", "approaching": "👀", "entry": "🟢", "scale_in": "➕", "entry_blocked": "⏸️",
         "entry_changed": "✏️", "entry_adjust": "🧭", "target_near": "🎯", "target_hit": "💰", "stop_moved": "🔒",
         "soft_stop": "🟠", "stop_near": "⚠️", "stop_hit": "🛑", "breakeven_stop": "⚪", "trailing_stop": "🟡",
         "time_exit": "⌛", "exit": "🔵", "trim": "✂️", "invalidated": "❌", "missed": "💨", "expired": "🕓",
         "cancelled": "🚫", "advisory": "⚠️", "source_update": "📣", "rejected": "⛔", "earnings_soon": "📅",
         "awaiting_confirmation": "⏳"}


def fmt(x, nd=2) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    return f"{x:,.{nd}f}"


def planned_entry(idea: Idea) -> float | None:
    """Average price if every planned tranche fills (falls back to the worst edge of the zone)."""
    if idea.entry_price is not None:
        return idea.entry_price
    lv = [(t["level"], t["frac"]) for t in idea.tranches if t.get("level") is not None and not t.get("cancelled")]
    if lv and abs(sum(f for _, f in lv) - 1) < 1e-6:
        return sum(p * f for p, f in lv)
    return idea.ref_entry


def humanize(name: str, reason: str, against: bool) -> str:
    """Indicator-speak -> plain English ("price > SMA50" -> "price above the 50-day")."""
    r = reason
    for a, b in (("SMA50 > SMA200", "50-day above the 200-day"), ("SMA50 < SMA200", "50-day below the 200-day"),
                 ("price > SMA50", "price above the 50-day"), ("price < SMA50", "price below the 50-day"),
                 ("> SMA200", "above the 200-day"), ("< SMA200", "below the 200-day"),
                 ("histogram", "momentum histogram"), ("OBV 20-bar slope", "on-balance volume trend"),
                 ("%B", "Bollinger %B")):
        r = r.replace(a, b)
    if r.lower().startswith(name.lower()):
        r = r[len(name):].lstrip(" :")
    return f"{'Against' if against else 'For'} — {name}: {r}"


def nice_date(iso: str | None) -> str:
    try:
        d = date.fromisoformat(iso)
        return d.strftime("%b %d").replace(" 0", " ") + ("" if d.year == date.today().year else f", {d.year}")
    except (TypeError, ValueError):
        return iso or "n/a"


def days_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (date.fromisoformat(iso) - date.today()).days
    except ValueError:
        return None


# =========================================================================== facts
def facts(idea: Idea, report: dict | None, barrier: dict | None, price: float | None, regime: dict | None = None) -> dict:
    r = report or {}
    ind = r.get("indicators") or {}
    sig = r.get("signal") or {}
    reg = r.get("regime") or {}
    fc = r.get("forecast") or {}
    rec = fc.get("recommendation") or {}
    risk = next((x for x in r.get("risk") or [] if "error" not in x), {})
    sent = r.get("sentiment") or {}
    fund = r.get("fundamentals") or {}
    sgn = 1 if idea.long else -1
    comps = sorted((sig.get("components") or {}).items(), key=lambda kv: abs(kv[1]["vote"] * kv[1]["weight"]), reverse=True)
    names = {"trend": "Trend", "macd": "MACD", "rsi": "RSI", "adx": "ADX", "bollinger": "Bollinger", "obv": "Volume",
             "mfi": "Money flow", "oscillators": "Oscillators", "cci": "CCI"}
    aligned = [humanize(names.get(k, k), c["reason"], against=False) for k, c in comps if c["vote"] * sgn > 0.25]
    opposed = [humanize(names.get(k, k), c["reason"], against=True) for k, c in comps if c["vote"] * sgn < -0.25]
    atr = ind.get("atr_14")
    entry = planned_entry(idea) or price
    stop_atr = (abs(entry - idea.stop) / atr) if (atr and entry and idea.stop) else None
    levels = r.get("levels") or {}
    trend = reg.get("trend") or ""
    px = price or (r.get("quote") or {}).get("price")
    sup = sorted([x for x in levels.get("support") or [] if px and x < px], reverse=True)
    res = sorted([x for x in levels.get("resistance") or [] if px and x > px])
    earn = (idea.meta or {}).get("next_earnings")
    return {
        "price": px, "trend": trend, "trend_strength": reg.get("trend_strength"),
        "trend_aligned": ("uptrend" in trend and idea.long) or ("downtrend" in trend and not idea.long),
        "counter_trend": ("downtrend" in trend and idea.long) or ("uptrend" in trend and not idea.long),
        "signal_score": sig.get("score"), "signal_label": sig.get("label"),
        "components": {k: c["reason"] for k, c in comps}, "aligned": aligned, "opposed": opposed,
        "rsi": ind.get("rsi_14"), "rel_volume": ind.get("rel_volume"), "adx": ind.get("adx"),
        "atr": atr, "atr_pct": ind.get("atr_pct"), "stop_atr": stop_atr, "entry_ref": entry,
        "rr": [idea.rr(t, entry) for t in idea.targets] if entry else [],
        "p_t1_first": (barrier or {}).get("prob_target_first"), "p_stop_first": (barrier or {}).get("prob_stop_first"),
        "p_t2_first": (barrier or {}).get("prob_target2_before_stop"), "barrier_days": (barrier or {}).get("horizon_days"),
        "model_view": rec.get("action"), "model_conf": (fc.get("confidence") or {}).get("rating"),
        "p_up": rec.get("prob_up"), "risk_level": risk.get("level"), "risk_score": risk.get("score"),
        "sentiment": sent.get("score") if (sent.get("articles") or 0) >= 3 else None, "articles": sent.get("articles"),
        "supports": sup, "resistances": res, "support": sup[0] if sup else None, "resistance": res[0] if res else None,
        "sma50": ind.get("sma_50"), "sma200": ind.get("sma_200"), "vwap": ind.get("vwap_20"),
        "pe": fund.get("pe"), "forward_pe": fund.get("forward_pe"), "w52h": fund.get("week52_high"),
        "w52l": fund.get("week52_low"), "sector": fund.get("sector"),
        "earnings": earn, "earnings_days": days_until(earn), "has_report": bool(r), "regime": regime,
    }


# =========================================================================== grading
def grade(idea: Idea, f: dict) -> tuple[str, float, list[str]]:
    """A-D quality grade for taking the entry now, with the reasons."""
    s, why = 0.0, []
    sgn = 1 if idea.long else -1
    if f["trend_aligned"]:
        s += 1
        why.append(f"+ with the {f['trend']}")
    elif f["counter_trend"]:
        s -= 1
        why.append(f"- counter-trend ({f['trend']})")
    sc = f.get("signal_score")
    if sc is not None:
        if sc * sgn >= 20:
            s += 1
            why.append(f"+ composite signal agrees ({sc:+.0f})")
        elif sc * sgn <= -20:
            s -= 1
            why.append(f"- composite signal disagrees ({sc:+.0f})")
    p1 = f.get("p_t1_first")
    if p1 is not None:
        if p1 >= 0.45:
            s += 1
            why.append(f"+ {p1:.0%} simulated odds of TP1 before the stop")
        elif p1 < 0.30:
            s -= 1
            why.append(f"- only {p1:.0%} simulated odds of TP1 before the stop")
    rr1 = (f.get("rr") or [None])[0]
    if rr1 is not None:
        if rr1 >= 1.5:
            s += 1
            why.append(f"+ reward:risk to TP1 {rr1:.1f}")
        elif rr1 < 1.0:
            s -= 1
            why.append(f"- reward:risk to TP1 only {rr1:.1f}")
    rsi = f.get("rsi")
    if rsi is not None:
        if (idea.long and rsi > 75) or (not idea.long and rsi < 25):
            s -= 0.5
            why.append(f"- RSI stretched ({rsi:.0f})")
        elif (idea.long and rsi < 60) or (not idea.long and rsi > 40):
            s += 0.5
            why.append(f"+ RSI has room ({rsi:.0f})")
    sa = f.get("stop_atr")
    if sa is not None:
        if sa < 0.5:
            s -= 0.5
            why.append(f"- stop only {sa:.1f}x ATR away (noise can hit it)")
        elif sa > 6:
            s -= 0.5
            why.append(f"- stop {sa:.1f}x ATR away (wide: small size per $ risked)")
    if f.get("risk_level") in ("High", "Extreme"):
        s -= 1
        why.append(f"- baseline risk {f['risk_level']}")
    mv = f.get("model_view")
    if mv:
        good = {"Buy", "Strong Buy"} if idea.long else {"Sell", "Reduce"}
        bad = {"Sell", "Reduce"} if idea.long else {"Buy", "Strong Buy"}
        if mv in good:
            s += 0.5
            why.append(f"+ prediction model: {mv}")
        elif mv in bad:
            s -= 0.5
            why.append(f"- prediction model: {mv}")
    se = f.get("sentiment")
    if se is not None and abs(se) > 0.2:
        s += 0.5 if se * sgn > 0 else -0.5
        why.append(f"{'+' if se * sgn > 0 else '-'} news sentiment {se:+.2f}")
    rg = f.get("regime") or {}
    lab = rg.get("label")
    if lab and idea.meta.get("instrument", {}).get("asset_class", "stock") in ("stock", "etf", "future"):
        adj = {"risk-off": -1.0, "mixed": -0.25, "risk-on": 0.25}.get(lab, 0.0) * sgn
        if adj:
            s += adj
            why.append(f"{'+' if adj > 0 else '-'} market is {lab} ({rg.get('summary', '').split(': ', 1)[-1][:90]})")
    res = f.get("resistances") or []
    e, t1 = f.get("entry_ref"), (idea.targets or [None])[-1]
    if idea.long and e and t1 and any(e * 1.01 < x < t1 * 0.99 for x in res):
        s -= 0.25
        why.append(f"- resistance at {fmt(next(x for x in res if e * 1.01 < x < t1 * 0.99))} before the target")
    g = "A" if s >= 3 else "B" if s >= 1.5 else "C" if s >= 0 else "D"
    return g, s, why


def grade_ok(g: str | None, minimum: str) -> bool:
    if minimum in ("", "none", None) or g is None:
        return True
    return GRADE_ORDER.index(g) >= GRADE_ORDER.index(minimum.upper())


# =========================================================================== building blocks
def levels_line(idea: Idea) -> str:
    if idea.entry_type == "market":
        e = "market"
    elif idea.entry_low == idea.entry_high:
        e = {"breakout_above": "break above ", "breakdown_below": "break below ", "limit_below": "at/below ",
             "limit_above": "at/above "}.get(idea.entry_type, "") + fmt(idea.entry_low)
    else:
        e = f"{fmt(idea.entry_low)}–{fmt(idea.entry_high)}"
        if idea.flags.get("approach") == "from_below":
            e += " (on a reclaim from below)"
    tg = " / ".join(fmt(t) for t in idea.targets) or "n/a"
    stop = fmt(idea.stop) + (" (close)" if idea.stop_basis == "close" else "")
    if idea.soft_stop is not None:
        stop = f"{fmt(idea.soft_stop)}–{stop} zone"
    return f"Entry {e} · Stop {stop} · Targets {tg}"


def cls_label(idea: Idea) -> str:
    return ((idea.meta or {}).get("class") or {}).get("label") or ""


def _pattern(idea: Idea) -> str:
    return ((idea.meta or {}).get("class") or {}).get("pattern") or ""


def context_lines(idea: Idea, f: dict, price: float | None) -> list[str]:
    out = []
    px = price or f.get("price")
    if f.get("trend"):
        ma = []
        for n, v in (("50-day", f.get("sma50")), ("200-day", f.get("sma200"))):
            if v and px:
                ma.append(f"{'above' if px > v else 'below'} the {n} ({fmt(v)})")
        out.append(f"Structure: {f['trend']}" + (f" ({f['trend_strength']})" if f.get("trend_strength") else "")
                   + (f"; price {', '.join(ma)}" if ma else ""))
    mom = []
    if f.get("rsi") is not None:
        r = f["rsi"]
        mom.append(f"RSI {r:.0f} ({'overbought' if r > 70 else 'oversold' if r < 30 else 'strong' if r > 55 else 'weak' if r < 45 else 'neutral'})")
    if f["components"].get("macd"):
        mom.append("MACD " + f["components"]["macd"])
    if f.get("adx") is not None:
        mom.append(f"ADX {f['adx']:.0f} ({'trending' if f['adx'] >= 25 else 'no strong trend'})")
    if mom:
        out.append("Momentum: " + "; ".join(mom))
    atr = f.get("atr")
    if px and idea.status == "pending" and idea.entry_low is not None:
        d = idea.distance_to_entry_pct(px)
        if d:
            away = abs(px - (idea.entry_high if px > idea.entry_high else idea.entry_low))
            out.append(f"Location: price {fmt(px)} is {abs(d):.1f}%"
                       + (f" ({away / atr:.1f} ATR)" if atr else "")
                       + f" {'above' if px > idea.entry_high else 'below'} the zone")
        else:
            out.append(f"Location: price {fmt(px)} is inside the entry zone")
    elif px and idea.entry_price is not None:
        out.append(f"Location: average entry {fmt(idea.entry_price)}, price {fmt(px)} "
                   f"({(px / idea.entry_price - 1) * 100 * (1 if idea.long else -1):+.1f}%, {idea.open_r(px):+.2f}R open)")
    lv = []
    s0, r0 = f.get("support"), f.get("resistance")
    if s0:
        tag = ""
        if idea.entry_low is not None and idea.entry_low * 0.995 <= s0 <= idea.entry_high * 1.005:
            tag = " — inside the buy zone (confluence)"
        elif idea.stop is not None and idea.long and s0 < idea.stop:
            tag = " — below the stop"
        lv.append(f"support {fmt(s0)}{tag}")
    if r0:
        tag = ""
        t_last = (idea.targets or [None])[-1]
        if idea.long and t_last and r0 < t_last * 0.99:
            tag = " — before the target (possible stall)"
        lv.append(f"resistance {fmt(r0)}{tag}")
    if lv:
        out.append("Levels: " + "; ".join(lv))
    if atr and f.get("atr_pct"):
        v = f"Volatility: ATR {fmt(atr)} ({f['atr_pct']:.1f}%/day)"
        if f.get("stop_atr"):
            v += f"; stop is {f['stop_atr']:.1f} ATR from the entry"
            v += " (room to breathe)" if f["stop_atr"] >= 2 else " (tight)" if f["stop_atr"] < 1 else ""
        out.append(v)
    if f.get("rel_volume"):
        rv = f["rel_volume"]
        out.append(f"Volume: {rv:.1f}× average ({'heavy' if rv > 1.5 else 'light' if rv < 0.7 else 'normal'})")
    val = []
    if f.get("pe"):
        val.append(f"P/E {f['pe']:.0f}")
    if f.get("forward_pe"):
        val.append(f"forward P/E {f['forward_pe']:.0f}")
    if f.get("w52h") and f.get("w52l") and px and f["w52h"] > f["w52l"]:
        pos = (px - f["w52l"]) / (f["w52h"] - f["w52l"]) * 100
        val.append(f"{pos:.0f}% of the 52-week range ({fmt(f['w52l'])}–{fmt(f['w52h'])})")
    if val:
        out.append("Valuation: " + ", ".join(val))
    if f.get("sentiment") is not None:
        se = f["sentiment"]
        out.append(f"News: tone {se:+.2f} over {f.get('articles')} articles "
                   f"({'positive' if se > 0.15 else 'negative' if se < -0.15 else 'mixed'})")
    if (f.get("regime") or {}).get("summary"):
        out.append("Market: " + f["regime"]["summary"] + ("; " + "; ".join(f["regime"]["notes"][:2])
                                                          if f["regime"].get("notes") else ""))
    ed = f.get("earnings_days")
    if ed is not None and ed >= 0:
        out.append(f"Earnings: {nice_date(f['earnings'])} (in {ed} day{'s' if ed != 1 else ''})"
                   + (" — inside a typical swing hold, expect a gap risk" if ed <= 30 else ""))
    return out


def thesis_lines(idea: Idea, f: dict) -> list[str]:
    m = idea.meta or {}
    c = m.get("class") or {}
    out = []
    if c:
        out.append(f"Type: {c.get('label')} · {c.get('horizon')} · entry style: {c.get('entry_style')}")
    if m.get("setup"):
        out.append(f"Setup (source): {m['setup']}")
    if m.get("reason"):
        out.append(f"Source reason: {m['reason'][:260]}")
    elif m.get("summary"):
        out.append(f"Source summary: {m['summary'][:260]}")
    if m.get("catalyst"):
        out.append(f"Catalyst: {m['catalyst'][:200]}")
    sc = [f"{n} {m[k]:g}/10" for k, n in (("fund_score", "fundamentals"), ("fin_target", "financials-to-target"),
                                           ("confidence", "confidence")) if m.get(k) is not None]
    if sc:
        out.append("Source scores: " + " · ".join(sc))
    rs = m.get("rr_stated")
    rr = (f.get("rr") or [None])[-1]
    if rs and rr:
        note = "" if abs(rs - rr) < 0.15 else (" (their figure assumes the best fill and the tight stop)" if rs > rr
                                                else " (ours is better: planned average fill)")
        out.append(f"Reward:risk: source {rs:.2f}R vs {rr:.2f}R from the planned average entry to the hard stop{note}")
    return out


def source_risks(idea: Idea) -> list[str]:
    m = idea.meta or {}
    out = []
    if m.get("risk_note"):
        out.append(f"Source-flagged: {m['risk_note'][:240]}")
    out += [f"Source: {s}" for s in (m.get("source_risks") or [])[:2]]
    return out


def plan_lines(idea: Idea, f: dict, extra: dict) -> list[str]:
    out = []
    rr = f.get("rr") or []
    added = set(idea.flags.get("added_targets") or [])
    tr = [t for t in idea.tranches if t.get("level") is not None]
    if idea.status == "pending" and len(idea.tranches) > 1 and tr:
        out.append("Scale in: " + " + ".join(f"{t['frac']:.0%} at {fmt(t['level'])}" for t in idea.tranches)
                   + f" (avg {fmt(planned_entry(idea))})")
    elif idea.status == "active" and open_tranches(idea):
        out.append("Resting add: " + ", ".join(f"{t['frac']:.0%} at {fmt(t['level'])}" for t in open_tranches(idea))
                   + " (cancelled once the first target is hit)")
    inst = idea.meta.get("instrument") or {}
    st = f"Stop {fmt(idea.stop)}"
    if f.get("stop_atr"):
        st += f" ({f['stop_atr']:.1f} ATR)"
    if inst.get("unit") == "contract" and idea.stop is not None and planned_entry(idea):
        pts = abs(planned_entry(idea) - idea.stop)
        st += (f"; {pts:g} pts = {pts / inst['tick']:.0f} ticks = ${pts * inst['multiplier']:,.0f} per "
               f"{inst.get('contract')} contract")
    if idea.soft_stop is not None:
        st += f"; warning at {fmt(idea.soft_stop)} (top of the source's stop range)"
    if idea.stop_basis == "close":
        st += "; judged on the daily close"
    out.append(st)
    n = len(idea.targets)
    for i, t in enumerate(idea.targets):
        if i in idea.targets_hit:
            continue
        lab = "scale-out" if t in added else ("final target" if i == n - 1 else f"TP{i + 1}")
        r = f" ({rr[i]:.1f}R)" if i < len(rr) and rr[i] is not None else ""
        why = f" — {idea.meta.get('scale_out_basis')}" if t in added and idea.meta.get("scale_out_basis") else ""
        out.append(f"{lab.capitalize()} {fmt(t)}{r}{why}")
    left = n - len(idea.targets_hit)
    if left > 1:
        out.append("Exits: equal slices at each level; stop → breakeven after the first, then trails to the prior level")
    elif n == 1 and idea.meta.get("scale_out_note") and idea.status == "pending":
        out.append("Exit: " + idea.meta["scale_out_note"])
    mult = idea.flags.get("multiplier", 1.0)
    if idea.shares:
        risk = (idea.risk_per_share or 0) * idea.shares * mult
        unit = inst.get("unit", "unit")
        what = (f"{idea.shares:g} {inst.get('contract', '')} contract{'s' if idea.shares != 1 else ''}"
                if unit == "contract" else f"{idea.shares:g} {unit}{'s' if idea.shares != 1 else ''}")
        out.append(f"Size (paper): {what} ≈ ${fmt(risk)} at risk ({extra.get('risk_pct', 1):g}% of the account)")
    elif inst.get("note"):
        out.append(f"Size: {inst['note']}")
    return out


def watch_lines(idea: Idea, f: dict) -> list[str]:
    out = []
    if idea.stop is not None:
        out.append(f"Invalidation: {'a daily close' if idea.stop_basis == 'close' else 'any trade'} "
                   f"{'below' if idea.long else 'above'} {fmt(idea.stop)}")
    if idea.status == "pending" and idea.expires_at:
        out.append(f"Entry window closes {time.strftime('%b %d', time.localtime(idea.expires_at))} if the zone isn't reached")
    if f.get("counter_trend"):
        out.append(f"Improves if price reclaims the 50-day ({fmt(f.get('sma50'))})" if idea.long and f.get("sma50")
                   else "Improves if the trend turns in the trade's favour")
    ed = f.get("earnings_days")
    if ed is not None and 0 <= ed <= 21:
        out.append(f"Earnings on {nice_date(f['earnings'])}: decide before then whether to hold through the report")
    return out


def odds_line(f: dict, idea: Idea) -> str | None:
    if f.get("p_t1_first") is None:
        return None
    nxt = next((t for j, t in enumerate(idea.targets) if j not in idea.targets_hit), None)
    p1, ps = f["p_t1_first"], f.get("p_stop_first") or 0
    return (f"Simulation (1,500 paths, next {f['barrier_days']} trading days): {p1:.0%} reach {fmt(nxt)} first, "
            f"{ps:.0%} hit the stop first, {max(0.0, 1 - p1 - ps):.0%} still between the two")


def why_lines(f: dict, idea: Idea, extra_first: list[str] | None = None) -> list[str]:
    out = [x for x in (extra_first or []) if x]
    txt = " ".join(out).lower()
    ol = odds_line(f, idea)
    if ol:
        out.append(ol)
    if f.get("model_view") and f.get("p_up") is not None and "prediction model" not in txt:
        out.append(f"Prediction model: {f['model_view']} ({(f.get('model_conf') or '').lower()} confidence, "
                   f"P(up) {f['p_up']:.0%})")
    if f.get("signal_label") and "composite signal" not in txt:
        out.append(f"Composite signal: {f['signal_label']} ({fmt(f['signal_score'], 0)})")
    out += [a for a in f["aligned"][:2] if a.split(":")[0].lower() not in txt]
    return out


# =========================================================================== per-event analysis
def explain(kind: str, idea: Idea, ev: dict, f: dict, extra: dict | None = None) -> dict:
    """Build {title, summary, why, context, thesis, plan, risks, watch, fields} for one event."""
    extra = extra or {}
    sym, d = idea.symbol, idea.direction.upper()
    px = ev.get("price") or f.get("price")
    pat = _pattern(idea)
    tag = f" · {pat}" if pat else ""
    grade_s = f" · grade {idea.grade}" if idea.grade else ""
    why: list[str] = []
    plan = plan_lines(idea, f, extra)
    risks = f["opposed"][:3] + [x[2:] for x in idea.grade_reasons if x.startswith("-")][:2]
    watch = watch_lines(idea, f)
    context = context_lines(idea, f, px)
    thesis = thesis_lines(idea, f) if kind in ("ingested", "entry", "entry_blocked", "approaching", "rejected",
                                               "entry_adjust", "advisory") else thesis_lines(idea, f)[:1]
    summary = ""

    if kind == "ingested":
        dist = idea.distance_to_entry_pct(px) if px else None
        title = f"{EMOJI[kind]} Tracking {sym} {d} #{idea.id}{tag}{grade_s}"
        where = ("" if dist is None else " — inside the zone now" if dist == 0 else f" — {abs(dist):.1f}% "
                 f"{'above' if dist < 0 else 'below'} the trigger")
        summary = (f"Interpreted: {levels_line(idea)}. Price {fmt(px)}{where}. "
                   f"{_entry_intent(idea, f, extra)}")
        why = why_lines(f, idea, [f"Entry quality if triggered now: grade {idea.grade}"
                                  + (f" ({'; '.join(x[2:] for x in idea.grade_reasons[:3])})" if idea.grade_reasons else "")
                                  if idea.grade else ""])
        risks = source_risks(idea) + risks + list(idea.warnings)
    elif kind == "approaching":
        title = f"{EMOJI[kind]} {sym} nearing the entry ({ev.get('distance_pct', 0):+.1f}%){tag}{grade_s}"
        summary = f"Price {fmt(px)} is close to the trigger. {_entry_intent(idea, f, extra)}"
        why = why_lines(f, idea, [f"Would grade {idea.grade} right now"] + idea.grade_reasons[:3])
    elif kind == "entry":
        fills = ev.get("fills") or []
        full = (ev.get("fraction") or 1) >= 0.999
        title = (f"{EMOJI[kind]} ENTRY {sym} {d} @ {fmt(px)}" + ("" if full else f" ({ev.get('fraction', 0):.0%} size)")
                 + f"{tag}{grade_s}")
        how = " + ".join(f"{x['frac']:.0%} @ {fmt(x['price'])}" for x in fills) if fills else fmt(px)
        pend = ev.get("pending_tranches") or []
        summary = (f"Price reached the entry ({levels_line(idea)}). Paper position opened: {how}"
                   + (f"; the rest is resting at {', '.join(fmt(p) for p in pend)}." if pend else ".")
                   + (f" {idea.flags['full_size_reason']}" if idea.flags.get("full_size_reason") and full else ""))
        if idea.flags.get("confirm_note"):
            why_first = [idea.flags["confirm_note"]]
        else:
            why_first = []
        why = why_first + why_lines(f, idea, [f"Entry grade {idea.grade}: " + "; ".join(x[2:] for x in idea.grade_reasons
                                                                           if x.startswith("+"))[:240]]
                        if idea.grade else [])
        if len(fills) == 1 and pend:
            why.insert(0, f"Scaling in: first half at the zone edge, second at {fmt(pend[0])} in case the dip extends")
        elif idea.flags.get("approach") == "from_below":
            why.insert(0, "Price reclaimed the zone from below: buyers defended the level")
    elif kind == "scale_in":
        title = f"{EMOJI[kind]} ADD {sym} @ {fmt(px)} — now {ev.get('fraction', 0):.0%} size, avg {fmt(ev.get('avg'))}"
        summary = ("The second tranche filled at the zone midpoint, so the average entry improved to "
                   f"{fmt(idea.entry_price)} and risk per unit fell to {fmt(idea.risk_per_share)}.")
        why = [f"Price kept pulling back inside the zone without reaching the stop ({fmt(idea.stop)})"] + f["aligned"][:2]
    elif kind == "entry_blocked":
        reason = ev.get("reason")
        title = f"{EMOJI[kind]} {sym} hit the entry but NOT entering{grade_s}"
        summary = (f"Price {fmt(px)} triggered {levels_line(idea)}, but "
                   + (reason if reason else "the setup quality is too low right now") + ".")
        why = [x[2:] for x in idea.grade_reasons if x.startswith("-")] or idea.grade_reasons
        plan = [f"Re-grading every {extra.get('regrade_min', 15):g} min while pending; enters automatically if it improves",
                "Still invalidated if the stop trades first, or expires if never taken"] + plan[:2]
    elif kind == "awaiting_confirmation":
        title = f"{EMOJI[kind]} {sym} is in the zone: waiting for a reversal before entering{tag}"
        summary = f"Price {fmt(px)} reached {levels_line(idea)}. {(ev.get('reason') or '').capitalize()}."
        why = ["Touching the zone isn't enough: waiting for buyers (or sellers, for a short) to show up on the "
               "shorter timeframe avoids buying into a level that is breaking"] + f["aligned"][:2]
        plan = ["Enters automatically on a reversal bar (close beyond the prior bar, or a 9-EMA reclaim)",
                "Enters anyway after the maximum wait; still invalidated if the stop trades first"] + plan[:3]
    elif kind == "entry_changed":
        title = f"{EMOJI[kind]} {sym} entry changed → {fmt(idea.entry_low)}–{fmt(idea.entry_high)}"
        summary = f"{(ev.get('reason') or 'Updated').capitalize()}. Old zone {fmt((ev.get('old') or [None])[0])}–" \
                  f"{fmt((ev.get('old') or [None, None])[1])}. New plan: {levels_line(idea)}."
        why = why_lines(f, idea, [f"Re-graded: {idea.grade}" if idea.grade else ""])
    elif kind == "entry_adjust":
        title = f"{EMOJI[kind]} {sym} entry suggestion: {ev.get('headline', 'adjust the entry')}"
        summary = ev.get("summary", "")
        why = ev.get("reasons") or []
        plan = ev.get("plan") or plan
    elif kind == "target_near":
        title = f"{EMOJI[kind]} {sym} {ev.get('distance_pct', 0):.1f}% from {_tname(idea, ev.get('target_index', 0))} {fmt(ev.get('target'))}"
        summary = (f"Price {fmt(px)} is approaching the next exit. Open {idea.open_r(px) if px else 0:+.2f}R. "
                   "Get ready to take profit; a rejection here would be the time to tighten the stop.")
        why = [f"Momentum into the level: MACD {humanize('MACD', f['components'].get('macd', 'n/a'), False).split(': ', 1)[-1]}"] \
            + f["aligned"][:1]
    elif kind == "target_hit":
        i = ev.get("target_index", 0)
        name = _tname(idea, i)
        title = f"{EMOJI[kind]} {name.upper()} hit {sym} @ {fmt(px)} ({ev.get('r', 0):+.2f}R on this slice)"
        can = ev.get("cancelled_tranches") or []
        summary = (f"Took profit on {ev.get('fraction', 0):.0%} of the planned size; {idea.remaining:.0%} still open. "
                   f"Realized so far {idea.realized_r:+.2f}R ({idea.realized_pct:+.2f}%)."
                   + (f" Unfilled add at {', '.join(fmt(c) for c in can)} cancelled." if can else ""))
        why = [f"Level reached: {name} {fmt(ev.get('target'))}"] + f["aligned"][:2]
        if idea.status == "closed":
            plan = ["Position fully closed at the final target"]
    elif kind == "stop_moved":
        locked = None
        if idea.entry_price is not None and idea.risk_per_share and ev.get("new") is not None:
            mv = (ev["new"] - idea.entry_price) if idea.long else (idea.entry_price - ev["new"])
            locked = mv / idea.risk_per_share
        title = f"{EMOJI[kind]} {sym} stop moved {fmt(ev.get('old'))} → {fmt(ev.get('new'))}"
        if idea.entry_price is None:
            ref = idea.ref_entry
            risk = abs(ref - ev["new"]) / ref * 100 if (ref and ev.get("new")) else None
            summary = (f"{(ev.get('reason') or '').capitalize()}. Not entered yet: the planned stop is now {fmt(ev.get('new'))}"
                       f" ({fmt(risk, 1)}% {'below' if idea.long else 'above'} the entry edge); size re-planned to "
                       f"{idea.shares:g} units.")
        else:
            summary = (f"{(ev.get('reason') or '').capitalize()}. Worst case for the remaining {idea.remaining:.0%} is now "
                       f"{fmt(locked)}R" + (" — this trade can no longer lose money." if locked is not None and locked >= -1e-9 else "."))
        why = [ev.get("reason") or "risk management"]
    elif kind == "soft_stop":
        title = f"{EMOJI[kind]} {sym} inside the stop zone ({fmt(ev.get('soft'))}–{fmt(ev.get('hard'))})"
        summary = (f"Price {fmt(px)} traded into the source's stop range. The hard stop {fmt(ev.get('hard'))} is still "
                   f"live; this is the last warning before it. Open {idea.open_r(px) if px else 0:+.2f}R.")
        why = f["opposed"][:3] or ["Price is testing the lower edge of the plan"]
    elif kind == "stop_near":
        title = f"{EMOJI[kind]} {sym} {ev.get('distance_pct', 0):.1f}% above the stop {fmt(ev.get('stop'))}"
        if not idea.long:
            title = f"{EMOJI[kind]} {sym} {ev.get('distance_pct', 0):.1f}% below the stop {fmt(ev.get('stop'))}"
        summary = (f"Price {fmt(px)} is close to the stop. Open {idea.open_r(px) if px else 0:+.2f}R. "
                   "Nothing to do if you follow the plan — the stop handles it; this is a heads-up.")
        why = f["opposed"][:3] or ["Price is moving against the trade"]
    elif kind in ("stop_hit", "breakeven_stop", "trailing_stop", "time_exit", "exit", "trim"):
        label = {"stop_hit": "STOPPED OUT", "breakeven_stop": "Breakeven stop", "trailing_stop": "Trailing stop",
                 "time_exit": "Time exit", "exit": "EXIT", "trim": "Trimmed"}[kind]
        title = f"{EMOJI[kind]} {label} {sym} @ {fmt(px)} · trade {idea.total_r():+.2f}R"
        summary = (f"{'Closed' if idea.status == 'closed' else 'Reduced'} {ev.get('fraction', 0):.0%}: "
                   f"{ev.get('pct', 0):+.2f}% on the slice. Trade total {idea.realized_r:+.2f}R / "
                   f"{idea.realized_pct:+.2f}% / {idea.realized_pnl:+,.2f} (paper). "
                   f"Best {idea.mfe_pct:+.1f}%, worst {idea.mae_pct:+.1f}% while open.")
        why = ([f"Reason: {ev['reason']}"] if ev.get("reason") else []) + \
              (f["opposed"][:3] if kind == "stop_hit" else f["aligned"][:2])
        if kind == "stop_hit" and f.get("stop_atr") is not None and f["stop_atr"] < 0.8:
            risks = [f"the stop was only {f['stop_atr']:.1f}× ATR wide, inside normal daily noise"] + risks
        plan = ["Position closed"] if idea.status == "closed" else plan
        watch = []
    elif kind in ("invalidated", "missed", "expired", "cancelled", "rejected"):
        title = f"{EMOJI[kind]} {sym} idea #{idea.id} {kind}{tag}"
        summary = f"{(ev.get('reason') or idea.close_reason or '').capitalize()}. Price {fmt(px)}. {levels_line(idea)}."
        why = [{"invalidated": "The stop level traded before the entry, so the setup failed without a position",
                "missed": "Price ran to the target without offering the entry; chasing would give poor reward:risk",
                "expired": "The entry window passed without the zone being reached",
                "cancelled": "Cancelled by the source or the user",
                "rejected": "The plan failed validation against live data"}[kind]]
        plan, watch = [], []
    elif kind == "advisory":
        title = f"{EMOJI[kind]} {sym} {d}: conditions deteriorating (open {idea.total_r(px):+.2f}R)"
        summary = "Not an automatic exit, but the evidence has turned against the trade; consider tightening the stop."
        why = ev.get("reasons") or []
        if ev.get("suggested_stop"):
            plan = [f"Suggested stop {fmt(ev['suggested_stop'])} (1.5× ATR from price)"] + plan
    elif kind == "earnings_soon":
        ed = f.get("earnings_days")
        title = f"{EMOJI[kind]} {sym} earnings {nice_date(f.get('earnings'))} (in {ed} days) · {idea.status}"
        if idea.status == "active":
            summary = (f"Open {idea.open_r(px) if px else 0:+.2f}R into the report. Earnings can gap the stock through the stop. "
                       "Options: hold through with the plan, trim to reduce gap risk, or move the stop to lock gains.")
        else:
            summary = (f"The entry is not triggered yet; new entries are paused from {ev.get('blackout_days', 2)} day(s) "
                       "before the report and resume after it.")
        why = [f"Report date from the source post: {nice_date(f.get('earnings'))}"] + f["aligned"][:1]
    elif kind == "source_update":
        title = f"{EMOJI[kind]} Update on {sym} #{idea.id}: {ev.get('action')}"
        summary = f"\"{(ev.get('text') or '')[:180]}\" → {ev.get('applied', 'noted')}."
        why = why_lines(f, idea)[:2]
    else:
        title, summary = f"{sym} {kind}", ""
    if kind in UPDATE_KINDS:                     # follow-ups: compact, no stale entry-time reasons
        context = [c for c in context if c.split(":")[0] in ("Structure", "Momentum", "Location", "Levels", "Earnings")]
        thesis = []
        plan = [x for x in plan if not x.startswith("Size")]
        risks = f["opposed"][:3] if kind in ("soft_stop", "stop_near", "advisory", "earnings_soon") else []
        if kind in ("soft_stop", "stop_near"):
            why = [w for w in why if w not in risks] or ["Price is moving against the trade"]
        watch = watch[:2]
    elif kind in FINAL_KINDS:
        context = [c for c in context if c.split(":")[0] in ("Structure", "Momentum", "Levels")]
        thesis = []
        risks = risks if kind == "stop_hit" else []
        if idea.entry_price is not None and kind not in ("invalidated", "missed", "expired", "cancelled", "rejected"):
            watch = [f"Review: best {idea.mfe_pct:+.1f}% / worst {idea.mae_pct:+.1f}% while open; "
                     f"{len(idea.targets_hit)}/{len(idea.targets)} exits reached"]
    fields = {"Price": fmt(px), "Status": idea.status, "Grade": idea.grade or "n/a",
              "Stop": fmt(idea.stop), "Targets": " / ".join(fmt(t) for t in idea.targets) or "n/a"}
    if idea.entry_price is not None:
        fields["Avg entry"] = fmt(idea.entry_price)
        fields["Trade R"] = f"{idea.total_r(px):+.2f}R"
    if f.get("p_t1_first") is not None:
        fields["P(target first)"] = f"{f['p_t1_first']:.0%}"
    if cls_label(idea):
        fields["Type"] = cls_label(idea)
    clean = lambda xs: [x for x in xs if x]  # noqa: E731
    return {"title": title, "summary": summary, "why": clean(why), "context": clean(context), "thesis": clean(thesis),
            "plan": clean(plan), "risks": clean(risks), "watch": clean(watch), "fields": fields}


UPDATE_KINDS = {"scale_in", "target_near", "target_hit", "stop_moved", "soft_stop", "stop_near", "earnings_soon",
                "source_update", "approaching"}
FINAL_KINDS = {"stop_hit", "breakeven_stop", "trailing_stop", "time_exit", "exit", "trim", "invalidated", "missed",
               "expired", "cancelled"}


def _tname(idea: Idea, i: int) -> str:
    t = idea.targets[i] if i < len(idea.targets) else None
    if t in set(idea.flags.get("added_targets") or []):
        return "scale-out"
    return "final target" if i == len(idea.targets) - 1 else f"TP{i + 1}"


def _entry_intent(idea: Idea, f: dict, extra: dict) -> str:
    c = (idea.meta or {}).get("class") or {}
    style = c.get("entry_style", "")
    if idea.entry_type == "market":
        return "Market entry: taking it now if the grade allows."
    if idea.flags.get("approach") == "from_below":
        return (f"Price is below the zone, so this waits for a reclaim: entry when price trades back up to "
                f"{fmt(idea.entry_low)} (confirmation that buyers defend it).")
    if len(idea.tranches) > 1:
        a, b = idea.tranches[0], idea.tranches[1]
        return (f"Plan: {style.lower() or 'limit at the zone'} — {a['frac']:.0%} at {fmt(a['level'])} (zone edge), "
                f"{b['frac']:.0%} at {fmt(b['level'])} (zone midpoint); grade-A setups take full size at the edge.")
    return f"Plan: {style.lower() or 'enter at the trigger'}."


# =========================================================================== text rendering
SECTIONS = [("why", "Why", 6), ("context", "Market context", 7), ("thesis", "Source thesis", 6), ("plan", "Plan", 7),
            ("risks", "Risks", 5), ("watch", "Watch", 3)]


def to_text(x: dict, footer: str = "") -> str:
    lines = [x["summary"]] + ([f"**Read:** {x['narrative']}"] if x.get("narrative") else [])
    for key, name, n in SECTIONS:
        if x.get(key):
            lines.append(f"**{name}:**\n" + "\n".join(f"• {w}" for w in x[key][:n]))
    if footer:
        lines.append(footer)
    return "\n".join(lines)
