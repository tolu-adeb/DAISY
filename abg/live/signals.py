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
* **Bands** for magnitudes: a 3 % move fires, a 6 % move fires again, but 3.1 %→3.4 % doesn't.

Severity: ``info`` (FYI), ``warning`` (actionable), ``critical`` (a stop was hit).
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from ..portfolio.store import AlertRule, PortfolioStore, Position
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

        out += self._rules(symbol, {"rsi": rsi, "score": sig.get("score")})
        return out

    # ------------------------------------------------------------------ from a live quote
    def from_quote(self, symbol: str, q: dict, levels: dict | None = None, position: Position | None = None) -> list[Signal]:
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

        out += self._rules(symbol, {"price": price, "change": chg})
        return out

    # ------------------------------------------------------------------ custom rules
    _METRIC = {"price_above": ("price", 1), "price_below": ("price", -1), "change_above": ("change", 1),
               "change_below": ("change", -1), "rsi_above": ("rsi", 1), "rsi_below": ("rsi", -1),
               "score_above": ("score", 1), "score_below": ("score", -1)}

    def _rules(self, symbol: str, values: dict) -> list[Signal]:
        out: list[Signal] = []
        for rule in self.store.rules(self.pf, symbol):
            if not rule.enabled:
                continue
            metric, direction = self._METRIC[rule.kind]
            v = values.get(metric)
            if v is None:
                continue
            hit = v > rule.value if direction > 0 else v < rule.value
            changed, old = self._transition(symbol, f"rule:{rule.id}", hit)
            if hit and (changed or old is None):
                self._add(out, _rule_signal(symbol, rule, v), cooldown=False)
                if rule.one_shot:
                    self.store.set_rule_enabled(self.pf, rule.id, False)
        return out

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
