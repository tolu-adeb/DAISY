"""Entry grading and the "why" analysis attached to every tracked-idea event.

Every decision the tracker relays (entry, exit, stop move, block, advisory) is explained
with the live context: trend and regime, the composite signal's aligned and opposing
evidence, RSI / relative volume, where the levels sit in ATR terms, reward-to-risk, the
simulated odds of target-before-stop (Monte Carlo on the idea's own levels), the prediction
model's view, news sentiment and the baseline risk level.
"""
from __future__ import annotations

from .lifecycle import Idea

GRADE_ORDER = ["D", "C", "B", "A"]
EMOJI = {"ingested": "📥", "approaching": "👀", "entry": "🟢", "entry_blocked": "⏸️", "target_hit": "🎯",
         "stop_moved": "🔒", "stop_hit": "🛑", "breakeven_stop": "⚪", "trailing_stop": "🟡", "time_exit": "⌛",
         "exit": "🔵", "trim": "✂️", "invalidated": "❌", "missed": "💨", "expired": "🕓", "cancelled": "🚫",
         "advisory": "⚠️", "source_update": "📣", "rejected": "⛔"}


def fmt(x, nd=2) -> str:
    return "n/a" if x is None else f"{x:,.{nd}f}"


def facts(idea: Idea, report: dict | None, barrier: dict | None, price: float | None) -> dict:
    r = report or {}
    ind = r.get("indicators") or {}
    sig = r.get("signal") or {}
    reg = r.get("regime") or {}
    fc = r.get("forecast") or {}
    rec = fc.get("recommendation") or {}
    risk = next((x for x in r.get("risk") or [] if "error" not in x), {})
    sent = r.get("sentiment") or {}
    sgn = 1 if idea.long else -1
    comps = sorted((sig.get("components") or {}).items(), key=lambda kv: abs(kv[1]["vote"] * kv[1]["weight"]), reverse=True)
    names = {"trend": "Trend", "macd": "MACD", "rsi": "RSI", "adx": "ADX", "bollinger": "Bollinger", "obv": "Volume",
             "mfi": "Money flow", "oscillators": "Oscillators", "cci": "CCI"}
    aligned = [f"{names.get(k, k)}: {c['reason']}" for k, c in comps if c["vote"] * sgn > 0.25]
    opposed = [f"{names.get(k, k)}: {c['reason']}" for k, c in comps if c["vote"] * sgn < -0.25]
    atr = ind.get("atr_14")
    entry = idea.entry_price if idea.entry_price is not None else idea.ref_entry or price
    stop_atr = (abs(entry - idea.stop) / atr) if (atr and entry and idea.stop) else None
    levels = r.get("levels") or {}
    trend = reg.get("trend") or ""
    trend_aligned = ("uptrend" in trend and idea.long) or ("downtrend" in trend and not idea.long)
    counter = ("downtrend" in trend and idea.long) or ("uptrend" in trend and not idea.long)
    return {
        "price": price, "trend": trend, "trend_strength": reg.get("trend_strength"), "trend_aligned": trend_aligned,
        "counter_trend": counter, "signal_score": sig.get("score"), "signal_label": sig.get("label"),
        "aligned": aligned, "opposed": opposed, "rsi": ind.get("rsi_14"), "rel_volume": ind.get("rel_volume"),
        "atr": atr, "atr_pct": ind.get("atr_pct"), "stop_atr": stop_atr,
        "rr": [idea.rr(t, entry) for t in idea.targets] if entry else [],
        "p_t1_first": (barrier or {}).get("prob_target_first"), "p_stop_first": (barrier or {}).get("prob_stop_first"),
        "p_t2_first": (barrier or {}).get("prob_target2_before_stop"), "barrier_days": (barrier or {}).get("horizon_days"),
        "model_view": rec.get("action"), "model_conf": (fc.get("confidence") or {}).get("rating"),
        "p_up": rec.get("prob_up"), "risk_level": risk.get("level"), "risk_score": risk.get("score"),
        "sentiment": sent.get("score") if sent.get("articles", 0) >= 3 else None,
        "support": (levels.get("support") or [None])[0], "resistance": (levels.get("resistance") or [None])[0],
        "sma50": ind.get("sma_50"), "sma200": ind.get("sma_200"), "vwap": ind.get("vwap_20"),
    }


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
        elif sa > 4:
            s -= 0.5
            why.append(f"- stop {sa:.1f}x ATR away (wide)")
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
    g = "A" if s >= 3 else "B" if s >= 1.5 else "C" if s >= 0 else "D"
    return g, s, why


def grade_ok(g: str | None, minimum: str) -> bool:
    if minimum in ("", "none", None) or g is None:
        return True
    return GRADE_ORDER.index(g) >= GRADE_ORDER.index(minimum.upper())


def levels_line(idea: Idea) -> str:
    if idea.entry_type == "market":
        e = "market"
    elif idea.entry_low == idea.entry_high:
        e = {"breakout_above": "break above ", "breakdown_below": "break below ", "limit_below": "at/below ",
             "limit_above": "at/above "}.get(idea.entry_type, "") + fmt(idea.entry_low)
    else:
        e = f"{fmt(idea.entry_low)}–{fmt(idea.entry_high)}"
    tg = " / ".join(fmt(t) for t in idea.targets) or "n/a"
    return f"Entry {e} · Stop {fmt(idea.stop)}{' (close)' if idea.stop_basis == 'close' else ''} · Targets {tg}"


def explain(kind: str, idea: Idea, ev: dict, f: dict, extra: dict | None = None) -> dict:
    """Build {title, summary, why[], plan[], risks[], fields{}} for one event."""
    extra = extra or {}
    sym, d = idea.symbol, idea.direction.upper()
    px = ev.get("price")
    why: list[str] = []
    plan: list[str] = []
    risks: list[str] = []
    context_line = (f"{f['trend'] or 'n/a'} ({f['trend_strength'] or 'n/a'}) · signal {f['signal_label'] or 'n/a'} "
                    f"({fmt(f['signal_score'], 0)}) · RSI {fmt(f['rsi'], 0)} · rel. vol {fmt(f['rel_volume'], 1)}x")
    odds = (f"simulated odds TP1 before stop {f['p_t1_first']:.0%} (stop first {f['p_stop_first'] or 0:.0%}, "
            f"{f['barrier_days']}d horizon)") if f.get("p_t1_first") is not None else None
    model = f"prediction model: {f['model_view']} ({(f['model_conf'] or '').lower()} confidence, P(up) {f['p_up']:.0%})" \
        if f.get("model_view") and f.get("p_up") is not None else None

    if kind == "ingested":
        dist = idea.distance_to_entry_pct(px) if px else None
        title = f"{EMOJI[kind]} Tracking {sym} {d} idea #{idea.id}"
        summary = (f"Interpreted: {levels_line(idea)}. Price {fmt(px)}"
                   + (f" ({dist:+.1f}% to the entry trigger)" if dist else " (inside the entry zone)" if dist == 0 else "") + ".")
        why = [context_line] + ([odds] if odds else []) + ([model] if model else [])
        if f.get("rr"):
            why.append("reward:risk " + " / ".join(f"TP{i + 1} {r:.1f}R" for i, r in enumerate(f["rr"]) if r is not None))
        if idea.grade:
            why.append(f"entry quality right now: grade {idea.grade}")
        plan = [f"alert when price is within {extra.get('approach_pct', 1.5):g}% of the trigger, then grade and paper-enter",
                f"cancel if not triggered by {extra.get('expires', 'expiry')}"]
        risks = list(idea.warnings)
    elif kind == "approaching":
        title = f"{EMOJI[kind]} {sym} approaching the entry ({ev.get('distance_pct', 0):+.1f}%)"
        summary = f"Price {fmt(px)} vs trigger — {levels_line(idea)}."
        why = [context_line] + ([odds] if odds else []) + [f"entry grade if triggered now: {idea.grade or 'n/a'}"]
        why += [r for r in idea.grade_reasons[:4]]
    elif kind == "entry":
        rr = f.get("rr") or []
        title = f"{EMOJI[kind]} ENTRY {sym} {d} @ {fmt(px)} (paper) · grade {idea.grade}"
        summary = (f"Price reached the trigger ({levels_line(idea)}). Size {idea.shares:g} units risking "
                   f"{fmt(idea.risk_per_share * idea.shares if idea.risk_per_share else None)} "
                   f"({extra.get('risk_pct', 1):g}% of the paper account).")
        why = [context_line] + f["aligned"][:3] + ([odds] if odds else []) + ([model] if model else [])
        why += [x for x in idea.grade_reasons if x.startswith("+")][:3]
        plan = [f"stop {fmt(idea.stop)} ({fmt(f['stop_atr'], 1)}x ATR)" + (" on a daily close" if idea.stop_basis == "close" else ""),
                *(f"TP{i + 1} {fmt(t)} ({rr[i]:.1f}R)" if i < len(rr) and rr[i] is not None else f"TP{i + 1} {fmt(t)}"
                  for i, t in enumerate(idea.targets)),
                "take an equal slice at each target; stop to breakeven after TP1, then trail to the prior target"]
        risks = f["opposed"][:3] + [x for x in idea.grade_reasons if x.startswith("-")][:3]
    elif kind == "entry_blocked":
        title = f"{EMOJI[kind]} {sym} reached the entry but NOT entering (grade {idea.grade})"
        summary = f"Price {fmt(px)} triggered {levels_line(idea)}, but the setup quality is too low right now."
        why = [x for x in idea.grade_reasons if x.startswith("-")] or idea.grade_reasons
        plan = [f"re-grade every {extra.get('regrade_min', 15):g} min while the idea is pending; enter if it improves",
                "still invalidated if the stop trades first, or expires if never taken"]
    elif kind == "target_hit":
        i = ev.get("target_index", 0)
        title = f"{EMOJI[kind]} TP{i + 1} hit {sym} @ {fmt(px)} ({ev.get('r', 0):+.2f}R on this slice)"
        summary = (f"Closed {ev.get('fraction', 0):.0%} of the position; {idea.remaining:.0%} left. "
                   f"Realized so far {idea.realized_r:+.2f}R ({idea.realized_pct:+.2f}%).")
        why = [context_line] + f["aligned"][:2]
        nxt = [t for j, t in enumerate(idea.targets) if j not in idea.targets_hit]
        plan = (["position fully closed at the final target"] if idea.status == "closed" else
                ([f"next target {fmt(nxt[0])}"] if nxt else []) + [f"stop now {fmt(idea.stop)}"])
        risks = f["opposed"][:3]
    elif kind == "stop_moved":
        title = f"{EMOJI[kind]} {sym} stop moved {fmt(ev.get('old'))} → {fmt(ev.get('new'))}"
        locked = None
        if idea.entry_price is not None and idea.risk_per_share and ev.get("new") is not None:
            mv = (ev["new"] - idea.entry_price) if idea.long else (idea.entry_price - ev["new"])
            locked = mv / idea.risk_per_share
        if idea.entry_price is None:
            ref = idea.ref_entry
            risk = abs(ref - ev["new"]) / ref * 100 if (ref and ev.get("new")) else None
            summary = (f"{ev.get('reason', '').capitalize()}. Not entered yet: the planned stop is now {fmt(ev.get('new'))}"
                       f" ({fmt(risk, 1)}% {'below' if idea.long else 'above'} the entry edge); position size re-planned to {idea.shares:g} units.")
        else:
            summary = (f"{ev.get('reason', '').capitalize()}. Worst case for the remaining {idea.remaining:.0%} is now "
                       f"{fmt(locked)}R" + (" — the trade can no longer lose money." if locked is not None and locked >= -1e-9 else "."))
    elif kind in ("stop_hit", "breakeven_stop", "trailing_stop", "time_exit", "exit", "trim"):
        label = {"stop_hit": "STOPPED OUT", "breakeven_stop": "Breakeven stop", "trailing_stop": "Trailing stop",
                 "time_exit": "Time exit", "exit": "EXIT", "trim": "Trimmed"}[kind]
        title = f"{EMOJI[kind]} {label} {sym} @ {fmt(px)} · trade {idea.total_r():+.2f}R"
        summary = (f"{'Closed' if idea.status == 'closed' else 'Reduced'} "
                   f"{ev.get('fraction', 0):.0%}: {ev.get('pct', 0):+.2f}% on the slice. "
                   f"Trade total {idea.realized_r:+.2f}R / {idea.realized_pct:+.2f}% / {idea.realized_pnl:+,.2f} (paper). "
                   f"Best {idea.mfe_pct:+.1f}%, worst {idea.mae_pct:+.1f}% while open.")
        why = [context_line] + (f["opposed"][:3] if kind == "stop_hit" else f["aligned"][:2])
        if ev.get("reason"):
            why.insert(0, f"reason: {ev['reason']}")
        if kind == "stop_hit" and f.get("stop_atr") is not None and f["stop_atr"] < 0.8:
            risks.append(f"the stop was only {f['stop_atr']:.1f}x ATR wide, inside normal daily noise")
    elif kind in ("invalidated", "missed", "expired", "cancelled", "rejected"):
        title = f"{EMOJI[kind]} {sym} idea #{idea.id} {kind}"
        summary = f"{(ev.get('reason') or idea.close_reason or '').capitalize()}. Price {fmt(px)}. {levels_line(idea)}."
        why = [context_line]
    elif kind == "advisory":
        title = f"{EMOJI[kind]} {sym} {d}: conditions deteriorating"
        summary = f"Open {idea.total_r(px):+.2f}R at {fmt(px)}. Not an automatic exit — consider tightening the stop."
        why = ev.get("reasons") or []
        if ev.get("suggested_stop"):
            plan = [f"suggested stop {fmt(ev['suggested_stop'])} (1.5x ATR from price)"]
    elif kind == "source_update":
        title = f"{EMOJI[kind]} Source update on {sym} #{idea.id}: {ev.get('action')}"
        summary = f"\"{(ev.get('text') or '')[:180]}\" → {ev.get('applied', 'noted')}."
        why = [context_line] + ([model] if model else [])
    else:
        title, summary = f"{sym} {kind}", ""
    fields = {"Price": fmt(px), "Status": idea.status, "Grade": idea.grade or "n/a",
              "Stop": fmt(idea.stop), "Targets": " / ".join(fmt(t) for t in idea.targets) or "n/a"}
    if idea.entry_price is not None:
        fields["Entry"] = fmt(idea.entry_price)
        fields["Trade R"] = f"{idea.total_r(px):+.2f}R"
    if f.get("p_t1_first") is not None:
        fields["P(TP1 first)"] = f"{f['p_t1_first']:.0%}"
    return {"title": title, "summary": summary, "why": [w for w in why if w], "plan": plan, "risks": [r for r in risks if r],
            "fields": fields}


def to_text(x: dict, footer: str = "") -> str:
    lines = [x["summary"]]
    if x.get("why"):
        lines.append("**Why:**\n" + "\n".join(f"• {w}" for w in x["why"][:8]))
    if x.get("plan"):
        lines.append("**Plan:**\n" + "\n".join(f"• {p}" for p in x["plan"][:6]))
    if x.get("risks"):
        lines.append("**Risks:**\n" + "\n".join(f"• {r}" for r in x["risks"][:5]))
    if footer:
        lines.append(footer)
    return "\n".join(lines)
