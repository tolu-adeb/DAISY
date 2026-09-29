"""Live signal detection.

Design rules
------------
* **Edge-triggered, not level-triggered.**  A signal fires when something *changes*
  (RSI enters overbought, MACD flips, a setup appears, price crosses a level) - not on
  every sweep while the condition stays true.  The last seen value of every tracked
  condition is persisted in ``signal_state`` so restarting the monitor doesn't re-fire.
* **Nothing fires on the first observation** of a transition-type condition (we don't
  know what it changed *from*).  Threshold alerts (stop hit, big move, custom rules) do
  fire on first observation, because the condition itself is the news.
* **Cooldown** (``ABG_SIGNAL_COOLDOWN``, default 4 h) per (symbol, kind, key) stops
  flapping conditions from spamming.
* **Cross alerts** (``price_cross``, ``price_cross_up``, ``price_cross_down``) remember which side of
  the level price is on and fire only on an actual crossing - never just because price is already
  past it.  Streamed ticks carry the high/low of each batch, so a quick poke through the level that
  reverses before the next sweep still counts.  A repeating cross alert re-arms only after price has
  moved ``ABG_ALERT_CROSS_REARM_PCT`` past the level, so chop around the line sends one alert, not twenty.
* **Bands** for magnitudes: a 3 % move fires, a 6 % move fires again, but 3.1 %→3.4 % doesn't.

Severity: ``info`` (FYI), ``warning`` (actionable), ``critical`` (a stop was hit).
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from ..portfolio.store import CROSS_KINDS, AlertRule, PortfolioStore, Position
from .market_hours import NY

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
RISK_RANK = {"Low": 0, "Moderate": 1, "Elevated": 2, "High": 3, "Extreme": 4}
PORTFOLIO = "PORTFOLIO"


@dataclass
class Signal:
    symbol: str
    kind: str
    key: str
    severity: str
    direction: str            # bullish | bearish | neutral
    title: str
    message: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    portfolio: str = "main"
    id: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time"] = datetime.fromtimestamp(self.ts).astimezone(NY).isoformat(timespec="seconds")
        return d


def _band(value: float, step: float) -> int:
    """Signed band index: 0 inside (-step, step), 1 for [step, 2*step), -1 for (-2*step, -step] ..."""
    if value is None or step <= 0 or not math.isfinite(value):
        return 0
    return int(math.copysign(math.floor(abs(value) / step), value))


def _fmt(x: Any, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:,.{nd}f}"


class SignalEngine:
    def __init__(self, store: PortfolioStore, settings, portfolio: str = "main"):
        self.store = store
        self.s = settings
        self.pf = store.ensure(portfolio)

    # ------------------------------------------------------------------ primitives
    def _transition(self, symbol: str, key: str, new: Any) -> tuple[bool, Any]:
        old = self.store.get_state(self.pf, symbol, key)
        if old != new:
            self.store.set_state(self.pf, symbol, key, new)
        return (old is not None and old != new), old

    def _cooled(self, symbol: str, kind: str, key: str) -> bool:
        last = self.store.last_fired(self.pf, symbol, kind, key)
        return last is None or (time.time() - last) >= self.s.signal_cooldown

    def _add(self, out: list[Signal], sig: Signal, cooldown: bool = True) -> None:
        sig.portfolio = self.pf
        if not cooldown or self._cooled(sig.symbol, sig.kind, sig.key):
            out.append(sig)

    # ------------------------------------------------------------------ from a full analysis report
    def from_report(self, symbol: str, r: dict) -> list[Signal]:
        out: list[Signal] = []
        price = (r.get("quote") or {}).get("price")
        sig = r.get("signal") or {}
        ind = r.get("indicators") or {}
        base = {"price": price, "score": sig.get("score")}

        label = sig.get("label")
        if label:
            changed, old = self._transition(symbol, "label", label)
            if changed:
                bull = "Bullish" in label
                bear = "Bearish" in label
                self._add(out, Signal(symbol, "signal_change", label, "warning" if "Strong" in label else "info",
                                      "bullish" if bull else "bearish" if bear else "neutral",
                                      f"{symbol} signal {old} -> {label}",
                                      f"Composite score now {_fmt(sig.get('score'), 0)}/100 at ${_fmt(price)}.",
                                      {**base, "from": old, "to": label}))

        plays = [p for p in (r.get("plays") or [])
                 if p.get("name") != "No Clear Setup" and p.get("confidence", 0) >= self.s.setup_min_confidence]
        names = sorted(p["name"] for p in plays)
        changed, old = self._transition(symbol, "setups", names)
        if changed:
            for p in plays:
                if p["name"] in (old or []):
                    continue
                lv = p.get("levels") or {}
                lvtxt = (f" Entry {_fmt(lv.get('entry'))}, stop {_fmt(lv.get('stop'))}, "
                         f"targets {_fmt(lv.get('target_1'))} / {_fmt(lv.get('target_2'))}.") if lv else ""
                self._add(out, Signal(symbol, "setup", p["name"], "warning" if p["direction"] != "neutral" else "info",
                                      "bullish" if p["direction"] == "long" else "bearish" if p["direction"] == "short" else "neutral",
                                      f"{symbol}: {p['name']} ({p['confidence']:.0%})",
                                      f"{p.get('thesis', '')}{lvtxt}", {**base, "setup": p}))

        rsi = ind.get("rsi_14")
        if rsi is not None:
            zone = "overbought" if rsi >= 70 else "oversold" if rsi <= 30 else "neutral"
            changed, old = self._transition(symbol, "rsi_zone", zone)
            if changed and zone != "neutral":
                self._add(out, Signal(symbol, "rsi", zone, "info", "bearish" if zone == "overbought" else "bullish",
                                      f"{symbol} RSI {zone} ({rsi:.0f})",
                                      f"RSI(14) moved from {old} into {zone} territory at ${_fmt(price)}.", {**base, "rsi": rsi}))

        hist = ind.get("macd_hist")
        if hist is not None:
            sign = "positive" if hist > 0 else "negative"
            changed, _ = self._transition(symbol, "macd_sign", sign)
            if changed:
                self._add(out, Signal(symbol, "macd_cross", sign, "info", "bullish" if hist > 0 else "bearish",
                                      f"{symbol} MACD {'bullish' if hist > 0 else 'bearish'} crossover",
                                      f"MACD crossed {'above' if hist > 0 else 'below'} its signal line "
                                      f"(histogram {hist:+.3f}).", {**base, "macd_hist": hist}))

        s50, s200 = ind.get("sma_50"), ind.get("sma_200")
        if s50 and s200:
            side = "golden" if s50 > s200 else "death"
            changed, _ = self._transition(symbol, "ma_cross", side)
            if changed:
                self._add(out, Signal(symbol, "ma_cross", side, "warning", "bullish" if side == "golden" else "bearish",
                                      f"{symbol} {side} cross (SMA50 {'>' if side == 'golden' else '<'} SMA200)",
                                      f"50-day SMA {_fmt(s50)} crossed {'above' if side == 'golden' else 'below'} "
                                      f"200-day SMA {_fmt(s200)}.", {**base, "sma_50": s50, "sma_200": s200}))
        if s200 and price:
            side = "above" if price > s200 else "below"
            changed, _ = self._transition(symbol, "sma200_side", side)
            if changed:
                self._add(out, Signal(symbol, "trend_break", side, "info", "bullish" if side == "above" else "bearish",
                                      f"{symbol} moved {side} its 200-day average",
                                      f"Price ${_fmt(price)} vs SMA200 {_fmt(s200)}.", {**base, "sma_200": s200}))

        risk = next((x for x in r.get("risk") or [] if "error" not in x), None)
        if risk and risk.get("level") in RISK_RANK:
            changed, old = self._transition(symbol, "risk_level", risk["level"])
            if changed and old in RISK_RANK:
                up = RISK_RANK[risk["level"]] > RISK_RANK[old]
                self._add(out, Signal(symbol, "risk_change", risk["level"],
                                      "warning" if up and RISK_RANK[risk["level"]] >= 2 else "info",
                                      "bearish" if up else "bullish",
                                      f"{symbol} risk {old} -> {risk['level']}",
                                      f"Baseline risk score {_fmt(risk.get('score'), 0)}/100; top driver: "
                                      f"{(risk.get('drivers') or [{}])[0].get('factor', 'n/a')}.",
                                      {**base, "risk_score": risk.get("score")}))

        sent = (r.get("sentiment") or {}).get("score")
        if sent is not None and (r.get("sentiment") or {}).get("articles", 0) >= 3:
            bucket = "positive" if sent > 0.3 else "negative" if sent < -0.3 else "neutral"
            changed, _ = self._transition(symbol, "sentiment", bucket)
            if changed and bucket != "neutral":
                self._add(out, Signal(symbol, "sentiment", bucket, "info",
                                      "bullish" if bucket == "positive" else "bearish",
                                      f"{symbol} news sentiment turned {bucket} ({sent:+.2f})",
                                      "Headlines: " + " | ".join(n.get("title", "")[:90] for n in (r.get("news") or [])[:2]),
                                      {**base, "sentiment": sent}))

        fc = r.get("forecast") or {}
        rec = (fc.get("recommendation") or {}).get("action")
        if rec:
            changed, old = self._transition(symbol, "recommendation", rec)
            if changed:
                order = ["Sell", "Reduce", "Hold", "Buy", "Strong Buy"]
                up = order.index(rec) > order.index(old) if old in order else True
                th = fc.get("thesis") or {}
                conf = (fc.get("confidence") or {}).get("rating", "")
                self._add(out, Signal(symbol, "recommendation", rec,
                                      "warning" if rec in ("Strong Buy", "Sell") or conf == "High" else "info",
                                      "bullish" if up else "bearish",
                                      f"{symbol} model view {old} -> {rec} ({conf.lower()} confidence)",
                                      th.get("headline", ""), {**base, "prob_up": (fc.get("recommendation") or {}).get("prob_up")}))

        out += self._rules(symbol, {"rsi": rsi, "score": sig.get("score")})
        return out

    # ------------------------------------------------------------------ from a live quote
    def from_quote(self, symbol: str, q: dict, levels: dict | None = None, position: Position | None = None,
                   price_rules: bool = True) -> list[Signal]:
        out: list[Signal] = []
        price, chg = q.get("price"), q.get("change_pct")
        if not price:
            return out
        today = datetime.now(NY).date().isoformat()

        band = _band(chg, self.s.price_move_pct)
        old = self.store.get_state(self.pf, symbol, f"move:{today}") or 0
        if band != old:
            self.store.set_state(self.pf, symbol, f"move:{today}", band)
        if band != 0 and (abs(band) > abs(old) or (band > 0) != (old > 0)):
            self._add(out, Signal(symbol, "big_move", f"{today}:{band}", "warning", "bullish" if band > 0 else "bearish",
                                  f"{symbol} {'up' if chg > 0 else 'down'} {abs(chg):.1f}% today",
                                  f"Price ${_fmt(price)} (prev close ${_fmt(q.get('prev_close'))}).",
                                  {"price": price, "change_pct": chg}), cooldown=False)

        if levels:
            res = min((x for x in levels.get("resistance") or [] if x), default=None)
            sup = max((x for x in levels.get("support") or [] if x), default=None)
            zone = "above_resistance" if res and price > res else "below_support" if sup and price < sup else "inside"
            changed, old = self._transition(symbol, "level_zone", zone)
            if changed and zone != "inside":
                lvl = res if zone == "above_resistance" else sup
                self._add(out, Signal(symbol, "level_break", f"{zone}:{lvl:.2f}",
                                      "info" if zone == "above_resistance" else "warning",
                                      "bullish" if zone == "above_resistance" else "bearish",
                                      f"{symbol} broke {'above resistance' if zone == 'above_resistance' else 'below support'} "
                                      f"{_fmt(lvl)}", f"Price ${_fmt(price)}.", {"price": price, "level": lvl}))

        if position is not None and position.shares > 0:
            if position.stop_loss:
                hit = price <= position.stop_loss
                changed, old = self._transition(symbol, "stop_hit", hit)
                if hit and (changed or old is None):
                    self._add(out, Signal(symbol, "stop_hit", f"{position.stop_loss}", "critical", "bearish",
                                          f"STOP HIT: {symbol} ${_fmt(price)} <= stop ${_fmt(position.stop_loss)}",
                                          f"{position.shares:g} shares, avg cost ${_fmt(position.avg_cost)}; "
                                          f"P&L {(price / position.avg_cost - 1) * 100:+.1f}%.",
                                          {"price": price, "stop": position.stop_loss}), cooldown=False)
            if position.take_profit:
                hit = price >= position.take_profit
                changed, old = self._transition(symbol, "target_hit", hit)
                if hit and (changed or old is None):
                    self._add(out, Signal(symbol, "target_hit", f"{position.take_profit}", "warning", "bullish",
                                          f"TARGET HIT: {symbol} ${_fmt(price)} >= target ${_fmt(position.take_profit)}",
                                          f"{position.shares:g} shares, avg cost ${_fmt(position.avg_cost)}; "
                                          f"P&L {(price / position.avg_cost - 1) * 100:+.1f}%.",
                                          {"price": price, "target": position.take_profit}), cooldown=False)
            if position.avg_cost:
                pnl = (price / position.avg_cost - 1) * 100
                lb = max(0, -_band(pnl, self.s.position_loss_pct))
                old = self.store.get_state(self.pf, symbol, "loss_band") or 0
                if lb != old:
                    self.store.set_state(self.pf, symbol, "loss_band", lb)
                if lb > old:
                    self._add(out, Signal(symbol, "position_loss", str(lb), "warning", "bearish",
                                          f"{symbol} position down {abs(pnl):.1f}% from cost",
                                          f"Price ${_fmt(price)} vs avg cost ${_fmt(position.avg_cost)} "
                                          f"({position.shares:g} shares, {position.shares * (price - position.avg_cost):+,.2f} USD).",
                                          {"price": price, "pnl_pct": pnl}), cooldown=False)

        out += self._rules(symbol, {"price": price if price_rules else None, "change": chg})
        return out

    # ------------------------------------------------------------------ custom rules
    _METRIC = {"price_above": ("price", 1), "price_below": ("price", -1), "change_above": ("change", 1),
               "change_below": ("change", -1), "rsi_above": ("rsi", 1), "rsi_below": ("rsi", -1),
               "score_above": ("score", 1), "score_below": ("score", -1),
               "price_cross": ("price", 0), "price_cross_up": ("price", 1), "price_cross_down": ("price", -1)}

    def price_rules(self, symbol: str, price: float, high: float | None = None, low: float | None = None) -> list[Signal]:
        """Only the price rules - cheap enough to run on every streamed tick batch."""
        if not price:
            return []
        return self._rules(symbol, {"price": price, "high": high, "low": low}, price_only=True)

    def _rules(self, symbol: str, values: dict, price_only: bool = False) -> list[Signal]:
        out: list[Signal] = []
        for rule in self.store.rules(self.pf, symbol):
            if not rule.enabled:
                continue
            metric, direction = self._METRIC[rule.kind]
            if price_only and metric != "price":
                continue
            v = values.get(metric)
            if v is None:
                continue
            if rule.kind in CROSS_KINDS:
                sig = self._cross(symbol, rule, v, values.get("high"), values.get("low"), values.get("change"))
                if sig is None:
                    continue
                self._add(out, sig, cooldown=False)
            else:
                probe = v
                if metric == "price":                      # a tick batch's wick counts
                    probe = max(v, values.get("high") or v) if direction > 0 else min(v, values.get("low") or v)
                hit = probe > rule.value if direction > 0 else probe < rule.value
                changed, old = self._transition(symbol, f"rule:{rule.id}", hit)
                if not (hit and (changed or old is None)):
                    continue
                self._add(out, _rule_signal(symbol, rule, v), cooldown=False)
            if rule.one_shot:
                self.store.set_rule_enabled(self.pf, rule.id, False)
        return out

    def _cross(self, symbol: str, rule: AlertRule, price: float, high: float | None, low: float | None,
               change: float | None) -> Signal | None:
        """Edge-triggered level cross with re-arm hysteresis.  State: {"side": above|below, "armed": bool}."""
        x = rule.value
        key = f"rule:{rule.id}"
        st = self.store.get_state(self.pf, symbol, key)
        side_now = "above" if price >= x else "below"
        if not isinstance(st, dict) or st.get("side") not in ("above", "below"):
            self.store.set_state(self.pf, symbol, key, {"side": side_now, "armed": True})   # first look: just remember
            return None
        side, armed = st["side"], bool(st.get("armed", True))
        hi = max(price, high) if high is not None else price
        lo = min(price, low) if low is not None else price
        crossed = None
        if side == "below" and hi >= x:
            crossed = "up"
        elif side == "above" and lo < x:
            crossed = "down"
        buf = x * max(self.s.alert_cross_rearm_pct, 0.0) / 100.0
        if crossed is None:
            if not armed and (price >= x + buf if side == "above" else price <= x - buf):
                self.store.set_state(self.pf, symbol, key, {"side": side, "armed": True})
            return None
        wanted = rule.kind == "price_cross" or rule.kind.endswith("_" + crossed)
        fire = armed and wanted
        # after any cross, the new side must move clear of the level before the next one counts
        new_side = "above" if crossed == "up" else "below"
        clear = price >= x + buf if new_side == "above" else price <= x - buf
        if side_now != new_side:          # poked through and came straight back inside one batch
            new_side, clear = side_now, False
        self.store.set_state(self.pf, symbol, key, {"side": new_side, "armed": clear, "last_cross": crossed,
                                                     "last_cross_at": time.time()})
        return _cross_signal(symbol, rule, crossed, price, high if crossed == "up" else low, change) if fire else None

    # ------------------------------------------------------------------ portfolio level
    def from_snapshot(self, snap: dict) -> list[Signal]:
        out: list[Signal] = []
        t = snap.get("totals") or {}
        today = datetime.now(NY).date().isoformat()
        if t.get("day_pct") is not None and t.get("positions"):
            band = _band(t["day_pct"], self.s.portfolio_move_pct)
            old = self.store.get_state(self.pf, PORTFOLIO, f"move:{today}") or 0
            if band != old:
                self.store.set_state(self.pf, PORTFOLIO, f"move:{today}", band)
            if band != 0 and (abs(band) > abs(old) or (band > 0) != (old > 0)):
                self._add(out, Signal(PORTFOLIO, "portfolio_move", f"{today}:{band}", "warning",
                                      "bullish" if band > 0 else "bearish",
                                      f"Portfolio {'up' if band > 0 else 'down'} {abs(t['day_pct']):.1f}% today",
                                      f"Day P&L {t['day_pnl']:+,.2f} USD on {t['market_value']:,.2f} USD.",
                                      {"day_pct": t["day_pct"], "day_pnl": t["day_pnl"]}), cooldown=False)
        for h in snap.get("holdings") or []:
            w = h.get("weight_pct")
            if w is None or t.get("positions", 0) < 3:
                continue
            over = w > self.s.concentration_pct
            changed, old = self._transition(h["symbol"], "concentrated", over)
            if over and (changed or old is None):
                self._add(out, Signal(h["symbol"], "concentration", "over", "info", "neutral",
                                      f"{h['symbol']} is {w:.0f}% of the portfolio",
                                      f"Above the {self.s.concentration_pct:.0f}% concentration threshold; consider position sizing.",
                                      {"weight_pct": w}))
        return out


def _rule_signal(symbol: str, rule: AlertRule, v: float) -> Signal:
    label = {"price": "price", "change": "day change %", "rsi": "RSI", "score": "signal score"}[
        SignalEngine._METRIC[rule.kind][0]]
    up = rule.kind.endswith("_above")
    return Signal(symbol, "custom_rule", str(rule.id), "warning", "bullish" if up else "bearish",
                  f"{symbol} {label} {'above' if up else 'below'} {rule.value:g} (now {v:,.2f})",
                  (rule.note or f"Custom alert #{rule.id}") + (" - rule disabled after firing (one-shot)." if rule.one_shot else ""),
                  {"rule_id": rule.id, "value": v, "threshold": rule.value})


def _cross_signal(symbol: str, rule: AlertRule, crossed: str, price: float, extreme: float | None,
                  change: float | None) -> Signal:
    up = crossed == "up"
    x = rule.value
    back = (up and price < x) or (not up and price >= x)
    detail = [f"Price {price:,.2f}" + (f" ({change:+.2f}% today)" if change is not None else "") + "."]
    if back and extreme is not None:
        detail.append(f"It touched {extreme:,.2f} and is back {'below' if up else 'above'} the level - a wick, not a close.")
    else:
        detail.append(f"{abs(price / x - 1) * 100:.2f}% {'above' if up else 'below'} the level.")
    if rule.note:
        detail.append(rule.note)
    tail = ("Rule disabled after firing (one-shot)." if rule.one_shot else
            "Repeating: re-arms once price moves clear of the level again.")
    return Signal(symbol, "price_cross", f"{rule.id}:{crossed}", "warning", "bullish" if up else "bearish",
                  f"{symbol} crossed {'above' if up else 'below'} {x:,.2f} (now {price:,.2f})",
                  " ".join(detail + [tail]),
                  {"rule_id": rule.id, "value": price, "threshold": x, "direction": crossed, "wick": back,
                   "extreme": extreme})
