"""Discord messages for the MNQ bot - the lifecycle a member follows, each with its reason.

🌅 brief → 🟡 trade idea (get ready) → 🟢/🔴 signal validated (enter) → ⚡ heads-up → 🔵 Target 1
(bank half, stop to break-even) → 🏃 runner updates → ✅ final / ❌ stopped / ⚪ break-even → 🔒 done for
the day → 🏁 day closed.  Every message says *why*, and every number is in MNQ points with the dollar
value per micro contract.
"""
from __future__ import annotations

from datetime import datetime

GREEN, RED, BLUE, GREY, YELLOW, ORANGE, TEAL = 0x2ECC71, 0xE74C3C, 0x3498DB, 0x95A5A6, 0xF1C40F, 0xF57C00, 0x13FE9E
SETUP_NAMES = {"orb": "Opening-range breakout + retest", "sweep": "Liquidity sweep + reclaim", "vwap": "VWAP trend pullback"}
LEVEL_NAMES = {"PDH": "yesterday's high", "PDL": "yesterday's low", "PDC": "yesterday's close", "ONH": "overnight high",
               "ONL": "overnight low", "ORH": "opening-range high", "ORL": "opening-range low", "VWAP": "VWAP"}


def f(x, nd=2) -> str:
    if x is None:
        return "n/a"
    s = f"{x:,.{nd}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def usd(pts, point_value=2.0, contracts=1.0) -> str:
    return f"${abs(pts) * point_value * contracts:,.0f}"


def _side(side) -> tuple[str, str, str]:
    long = side in (1, "LONG")
    return ("BUY" if long else "SELL"), ("LONG" if long else "SHORT"), ("🟢" if long else "🔴")


def _hhmm(ts) -> str:
    if isinstance(ts, str):
        return ts[11:16]
    return ts.strftime("%H:%M") if isinstance(ts, datetime) else ""


class Renderer:
    def __init__(self, tag: str = "TEST", mention_role: str | None = None, point_value: float = 2.0,
                 contracts: float = 1.0, symbol: str = "MNQ"):
        self.tag, self.role, self.pv, self.contracts, self.symbol = tag, mention_role, point_value, contracts, symbol

    @property
    def footer(self) -> dict:
        t = f"{self.tag} · " if self.tag else ""
        return {"text": f"{t}{self.symbol} · paper signals · educational only, not financial advice"}

    def _t(self, title: str) -> str:
        return f"{title} · {self.tag}" if self.tag else title

    def _ping(self, text: str) -> str | None:
        return f"<@&{self.role}> · {text}" if self.role else None

    def render(self, ev: dict) -> tuple[str | None, dict] | None:
        fn = getattr(self, "_" + ev["type"], None)
        return fn(ev) if fn else None

    # ------------------------------------------------------------------ morning
    def _brief(self, ev):
        c, p = ev["ctx"], ev["params"]
        bias = c.get("bias_label", "Mixed")
        news = [e for e in c.get("events") or [] if e.get("time")]
        color = {"Bullish": 0x00C853, "Bearish": 0xD50000}.get(bias, ORANGE)
        title = ("⚠️ NEWS DAY — " if news else "") + f"🌅 Pre-Market Brief — {bias.upper()}"
        lv = {k: c.get(k.lower()) for k in ("PDH", "PDL", "PDC", "ONH", "ONL")}
        levels = " · ".join(f"**{k}** {f(v)}" for k, v in lv.items() if v)
        atr = c.get("atr_d")
        er = c.get("er5")
        regime = (f"Daily ATR ≈ {f(atr, 0)} pts. " if atr else "") + (
            "Last 5 days moved efficiently (trend-friendly)." if er and er >= 0.45 else
            "Last 5 days were choppy - expect fakeouts; the sweep setup suits this best." if er is not None and er < 0.25 else
            "Last 5 days were mixed.")
        plan = [f"• **Opening range** 09:30-{_add_min('09:30', p['or_minutes'])}: a strong 5-min close outside it arms a "
                "breakout-retest idea.",
                "• **Sweeps**: a quick run through " + ", ".join(k for k, v in lv.items() if v and k != "PDC")
                + " that snaps back = reversal setup."]
        if "vwap" in p.get("setups", []):
            plan.append("• **VWAP pullback** only if the open is clearly one-sided.")
        rules = (f"New entries {p['entry_start']}-{p['entry_end']} ET · max {p['max_trades']} trades"
                 + (" · stop after the first loss" if p.get("stop_after_loss") else "")
                 + f" · Target 1 = {f(p['t1_r'])}R banks half")
        fields = [{"name": "🎯 Levels", "value": levels or "n/a", "inline": False},
                  {"name": "🧭 Bias", "value": bias + (": " + "; ".join(c.get("bias_reasons") or []) if c.get("bias_reasons") else "")
                   + "\n_A lean only - setups in either direction can trigger._", "inline": False},
                  {"name": "🌡️ Regime", "value": regime, "inline": False},
                  {"name": "🗺️ The plan", "value": "\n".join(plan), "inline": False},
                  {"name": "📏 Rules", "value": rules, "inline": False}]
        if news:
            txt = "\n".join(f"• {e['time']} ET — {e['name']}" for e in news)
            fields.append({"name": "📰 Heads-up", "value": txt + f"\nNo entries {p['news_before_min']} min before to "
                           f"{p['news_after_min']} min after a release. Never trade the spike.", "inline": False})
        if c.get("fomc"):
            fields.append({"name": "🏦 FOMC day", "value": {"skip": "The bot sits today out.",
                                                            "half": "Half size on every signal today.",
                                                            "normal": "Normal size - be careful after 14:00."}[p.get("fomc_mode", "half")],
                           "inline": False})
        off = [f"{s['setup']} in {s['regime']}" for s in (ev.get("learner") or []) if not s.get("active")]
        if off:
            fields.append({"name": "🧠 Adaptive filter", "value": "Paused for now (recent results negative): " + ", ".join(off),
                           "inline": False})
        return None, {"title": self._t(title), "color": color, "fields": fields, "footer": self.footer}

    # ------------------------------------------------------------------ ideas
    def _idea(self, ev):
        verb, label, _ = _side(ev["side"])
        z0, z1 = ev["zone"]
        lines = []
        if ev.get("counter_bias"):
            lines.append("⚠️ **Counter-bias idea** — today's lean is the other way. Extra caution.\n")
        lines += [f"**Get ready — a {verb} may be coming.** ({SETUP_NAMES.get(ev['setup'], ev['setup'])})", "",
                  f"**Watch this price:** {f(ev['optimal'])}   (zone {f(min(z0, z1))} – {f(max(z0, z1))})",
                  f"**Risk if it triggers:** ~{f(ev['risk'], 0)} pts (≈ {usd(ev['risk'], self.pv)} per micro)", "",
                  "**Why:**"] + [f"• {r}" for r in ev.get("reasons") or []] + [
                  "", f"**Do NOT enter yet.** Wait for the {_side(ev['side'])[2]} **{verb}** signal to confirm. "
                  "No signal = the setup didn't hold, stand down."]
        return self._ping("TRADE IDEA — get ready, do not enter yet"), {
            "title": self._t(f"🟡 TRADE IDEA #{ev['id']} — possible {verb}"), "color": YELLOW,
            "description": "\n".join(lines), "footer": self.footer}

    def _idea_cancel(self, ev):
        verb, _, _ = _side(ev["side"])
        return None, {"title": self._t(f"⚪ TRADE IDEA #{ev['id']} CANCELLED"), "color": GREY,
                      "description": f"The possible {verb} at {f(ev['optimal'])} is off — **{ev.get('why') or 'no trigger'}**.\n"
                                     "Not a position; nothing to do.", "footer": self.footer}

    # ------------------------------------------------------------------ the signal
    def _signal(self, ev):
        t = ev["trade"]
        verb, label, dot = _side(t["side"])
        s = 1 if t["side"] > 0 else -1
        z0, z1 = sorted(t["zone"])
        risk = t["risk"]
        lines = [f"**Take the {verb} — enter now / within the zone.**"]
        if ev.get("direct"):
            lines.append("⚡ **Direct signal** — the setup confirmed without a heads-up.")
        if t.get("size", 1) < 1:
            lines.append("🏦 **Half size today** (FOMC).")
        lines += ["", f"**Entry:** {f(t['entry'])}  (zone {f(z0)} – {f(z1)})",
                  f"**Stop loss:** {f(t['stop'])}  (~{f(risk, 0)} pts · ≈ {usd(risk, self.pv)} per micro)", "",
                  f"**Target 1:** {f(t['t1'])}  · _bank half, stop to break-even_",
                  f"**Final target:** {f(t['final'])}  (" + (f"{LEVEL_NAMES[t['final_name']]}, " if t['final_name'] in LEVEL_NAMES else "")
                  + f"{abs(t['final'] - t['entry']) / risk:.1f}R)",
                  f"**Price now:** {f(ev.get('price'))}"]
        past = ev.get("past_optimal") or 0
        if past > 0.15 * risk:
            lines.append(f"\n⚠️ Price is {f(past, 0)} pts past the ideal entry. Smaller size, same stop - or wait for "
                         f"a pullback to {f(t['optimal'])}.")
        else:
            lines.append("\n✅ **Price is at the entry — take it now.**")
        lines += ["", f"**Why this trade** ({SETUP_NAMES.get(t['setup'], t['setup'])}, score {t['score']:.0f}/100, "
                  f"{t['regime']} conditions):"] + [f"• {r}" for r in t.get("reasons") or []]
        lines.append(f"\n_Invalid if price trades through {f(t['stop'])} — the stop is the plan, not a suggestion._")
        return self._ping(f"SIGNAL — take the {verb}"), {
            "title": self._t(f"{dot} SIGNAL VALIDATED #{t['id']} — {label}"), "color": GREEN if s > 0 else RED,
            "description": "\n".join(lines), "footer": self.footer}

    def _heads_up(self, ev):
        t = ev["trade"]
        c = ev.get("candle") or 0
        cushion = ev["mfe"] / c if c else None
        read = {"trend": "🟢 Trend read — the session is moving cleanly; consider holding for Target 1.",
                "range": "🔴 Chop read — choppy session; securing a partial here is reasonable.",
                "news": "🟡 News session — moves reverse fast; a partial is reasonable."}.get(ev.get("regime"), "")
        txt = (f"Price is **+{f(ev['mfe'], 0)} pts** in your favor but hasn't hit Target 1 ({f(t['t1'])}) yet.\n{read}\n\n"
               + (f"🌡️ 5-min candles are running ~**{f(c, 0)} pts** — your cushion is about **{cushion:.1f} candles**. "
                  + ("Moving the stop to break-even now could get wicked out by a normal pullback; halving the stop is safer."
                     if cushion < 2.5 else "Break-even is comfortable here if you want it.") if c else "")
               + "\n_Heads-up only — the plan is unchanged._")
        return None, {"title": self._t(f"⚡ Heads-up #{t['id']}"), "color": GREEN, "description": txt, "footer": self.footer}

    def _t1(self, ev):
        t = ev["trade"]
        mode = ev.get("be_mode", "t1")
        act = {"t1": f"**MOVE your stop to {f(ev['new_stop'])}** (break-even). The rest rides risk-free.",
               "t1_close": f"Keep the stop at {f(ev['old_stop'])} until a candle **closes** beyond Target 1 — then break-even "
                           "(avoids getting shaken out right before the move).",
               "lock": f"Move your stop to {f(ev['new_stop'])} (risk cut to a quarter; it ratchets up as the trade runs)."}[mode]
        return self._ping("TARGET 1 — bank half, move your stop"), {
            "title": self._t(f"🔵 TARGET 1 HIT #{t['id']}"), "color": BLUE,
            "description": f"Target 1 hit — **+{f(ev['pts'], 1)} pts**. {act}", "footer": self.footer}

    def _scaleout(self, ev):
        t = ev["trade"]
        return None, {"title": self._t(f"⚡ Scale-out #{t['id']}"), "color": BLUE,
                      "description": f"**Banked {int(ev['frac'] * 100)}% at +{f(ev['pts'], 1)} pts.** The rest is riding to the final "
                                     f"target {f(t['final'])}" + (f" ({LEVEL_NAMES[t['final_name']]})" if t['final_name'] in LEVEL_NAMES else "") + ".\n"
                                     "_Following: secure half, hold the rest._", "footer": self.footer}

    def _stop_moved(self, ev):
        t = ev["trade"]
        return None, {"title": self._t(f"🔒 Stop moved #{t['id']}"), "color": BLUE,
                      "description": f"New stop **{f(ev['new_stop'])}** — {ev.get('why', '')}", "footer": self.footer}

    def _runner(self, ev):
        t = ev["trade"]
        lock = ev.get("lock") or 0
        return None, {"title": self._t(f"🏃 Runner update #{t['id']}"), "color": BLUE,
                      "description": f"**+{f(ev['open_pts'], 0)} pts and running** (best +{f(ev['mfe'], 0)}) · final target "
                                     f"{f(max(ev['to_final'], 0), 0)} pts away · stop {f(ev['stop'])}"
                                     + (f" (locks +{f(lock, 0)})" if lock > 0 else " (break-even)"),
                      "footer": self.footer}

    def _exit_msg(self, ev, title, color, head, ping):
        t = ev["trade"]
        lines = [head, f"Entry {f(t['entry'])} → exit {f(t['exit'])} · blended result **{'+' if ev['pts'] >= 0 else ''}"
                       f"{f(ev['pts'], 2)} pts** ({ev['r']:+.2f}R, {'+' if ev['pts'] >= 0 else '-'}{usd(ev['pts'], self.pv)} per micro)."]
        if t.get("t1_hit"):
            lines.append(f"_Half was banked at Target 1 ({f(t['t1'])})._")
        if ev["type"] == "stopped":
            lines.append(f"Best it got was +{f(t['mfe'], 0)} pts. The setup failed — on to the next one (or done, per the rules).")
        return self._ping(ping), {"title": self._t(f"{title} #{t['id']}"), "color": color,
                                  "description": "\n".join(lines), "footer": self.footer}

    def _final(self, ev):
        return self._exit_msg(ev, "✅ FINAL TARGET HIT", GREEN, "Final target reached — **close it now** if you're still in.",
                              "FINAL TARGET hit — close it now")

    def _stopped(self, ev):
        return self._exit_msg(ev, "❌ STOPPED OUT", RED, "Your stop closed the trade.", "STOPPED OUT — the trade is closed")

    def _breakeven(self, ev):
        return self._exit_msg(ev, "⚪ BREAK-EVEN", GREY, "The runner came back to entry and closed.",
                              "BREAK-EVEN — the runner closed at entry")

    def _closed(self, ev):
        why = {"flat time": "Flat before the close.", "session end": "Session over.", "runner stop": "Trailing stop hit."}.get(
            ev.get("why"), ev.get("why", ""))
        return self._exit_msg(ev, "✅ TRADE CLOSED", GREEN if ev["pts"] > 0 else GREY, f"{why} **Close your position now** if you're still in.",
                              "TRADE CLOSED — close your position now")

    # ------------------------------------------------------------------ day
    def _done(self, ev):
        o = ev.get("open")
        txt = f"No more NEW signals today — **{ev.get('why')}**. Any open trade idea above is cancelled."
        if o:
            txt += f"\n\nTrade #{o['id']} is still running — you'll keep getting its management."
        return None, {"title": self._t("🔒 That's it for today"), "color": GREY, "description": txt, "footer": self.footer}

    def _session_closed(self, ev):
        return None, {"title": self._t("🕛 Session closed"), "color": GREY,
                      "description": "The bot has stopped for today. All positions are flat. Next session 9:30 AM ET.",
                      "footer": self.footer}

    def _day_summary(self, ev):
        lines = []
        for t in ev.get("trades") or []:
            icon = "🟢" if t["pts"] > 0.5 else "🔴" if t["pts"] < -0.5 else "🟡"
            lines.append(f"{icon} **{t['opened'][11:16]} ET · {'🟩 LONG' if t['side'] > 0 else '🟥 SHORT'} · "
                         f"{t['setup'].upper()}** @ {f(t['entry'])} → **{t['pts']:+.2f} pts** ({t['exit_reason']})")
        if not lines:
            lines = ["No qualifying setups today — the bot stayed flat."]
        usd_net = ev["net_pts"] * self.pv
        head = (f"**📊 {ev['n']} trade{'s' if ev['n'] != 1 else ''} · {ev['wins']}W/{ev['losses']}L/{ev['be']}BE · "
                f"{ev['net_pts']:+.2f} pts ({ev['net_r']:+.2f}R, {'+' if usd_net >= 0 else '-'}${abs(usd_net):,.0f} per micro before costs)**")
        color = GREEN if ev["net_pts"] > 0 else RED if ev["net_pts"] < 0 else GREY
        desc = head + "\n\n" + "\n".join(lines)
        if ev.get("done_reason"):
            desc += f"\n\n_Stopped for the day because: {ev['done_reason']}._"
        return None, {"title": self._t("🏁 Day closed"), "color": color, "description": desc, "footer": self.footer}


def _add_min(hhmm: str, m: int) -> str:
    h, mm = map(int, hhmm.split(":"))
    t = h * 60 + mm + m
    return f"{t // 60:02d}:{t % 60:02d}"
