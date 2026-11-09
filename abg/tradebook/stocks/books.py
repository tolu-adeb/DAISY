"""The four stock books. Each book reads the day's close and returns orders for the next open.

Book A  dual_momentum   monthly ETF rotation with an absolute-momentum (market) switch
Book B  xs_momentum     monthly cross-sectional 12-1 momentum, risk-adjusted, inverse-vol weights
Book C  trend_pullback  daily swing: buy pullbacks inside established uptrends, ATR stops
Book D  mean_reversion  daily swing: buy short-term oversold stocks above their 200-day average
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from abg.tradebook.common import indicators as ind
from abg.tradebook.common.fastframe import cache_attrs


# ----------------------------------------------------------------------------- shared data
class PanelIndicators:
    """Indicators computed once for the whole panel (dates x tickers)."""

    def __init__(self, panel):
        c, h, l, v = panel.close, panel.high, panel.low, panel.volume
        self.close = c
        self.ret = c.pct_change()
        self.ema50 = ind.ema(c, 50)
        self.ema200 = ind.ema(c, 200)
        self.sma5 = ind.sma(c, 5)
        self.sma50 = ind.sma(c, 50)
        self.sma200 = ind.sma(c, 200)
        self.mom63 = ind.momentum(c, 63)
        self.rsi14 = ind.rsi(c, 14)
        self.rsi2 = ind.rsi(c, 2)
        pc = c.shift(1)
        tr = np.maximum(np.maximum(h - l, (h - pc).abs()), (l - pc).abs())
        self.atr14 = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        _, _, self.macd_hist = ind.macd(c)
        self.bb_low, self.bb_mid, self.bb_up = ind.bollinger(c, 20, 2.0)
        self.adv20 = (c * v).rolling(20, min_periods=20).mean()
        self.vol60 = self.ret.rolling(60, min_periods=60).std() * np.sqrt(252)
        self.vol252 = self.ret.rolling(252, min_periods=200).std() * np.sqrt(252)
        self.mom126 = ind.momentum(c, 126)
        self.mom252 = ind.momentum(c, 252)
        self.mom_12_1 = ind.momentum(c, 231, 21)      # t-252 .. t-21
        self.min_rsi14_5 = self.rsi14.rolling(5, min_periods=1).min()
        self.prev_high = h.shift(1)
        self.macd_hist_prev = self.macd_hist.shift(1)
        # the books read these one scalar at a time; cache column lookups (see fastframe)
        cache_attrs(self)


@dataclass
class Order:
    book: str
    ticker: str
    kind: str                 # 'target' (set shares), 'buy' (open new swing position), 'sell' (close)
    shares: float
    reason: str = ""
    stop_atr: float | None = None   # for 'buy': initial stop = fill - stop_atr * ATR(signal day)
    atr: float | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Position:
    shares: float
    entry_price: float
    entry_date: pd.Timestamp
    stop: float | None = None
    init_stop: float | None = None
    highest_close: float = 0.0
    atr_at_entry: float = 0.0
    days_held: int = 0
    meta: dict = field(default_factory=dict)


class Book:
    name = "book"
    family = "trend"
    swing = False

    def __init__(self, params: dict):
        self.p = params

    def regime_mult(self, regime: str) -> float:
        return float(self.p.get("regimes", {}).get(regime, 0.0)) if regime != "warmup" else 0.0

    def on_close(self, ctx) -> list[Order]:
        raise NotImplementedError


def _inv_vol_weights(vols: pd.Series, cap: float | None) -> pd.Series:
    w = 1.0 / vols.replace(0, np.nan)
    w = w.dropna()
    if w.empty:
        return w
    w = w / w.sum()
    if cap:
        for _ in range(20):        # iterative capping with redistribution
            over = w > cap
            if not over.any():
                break
            excess = (w[over] - cap).sum()
            w[over] = cap
            under = ~over
            if w[under].sum() <= 0:
                break
            w[under] += excess * w[under] / w[under].sum()
    return w


# ----------------------------------------------------------------------------- Book A
class DualMomentum(Book):
    """Monthly. Score = mean(126-day, 252-day return). Risk-on (benchmark above its 200-day SMA):
    hold the top_k universe ETFs with positive score and price above their 200-day SMA, inverse-vol
    weighted; any unfilled slots go to the best defensive asset. Risk-off: 100% best defensive asset
    with positive 126-day return, else cash."""
    name, family = "dual_momentum", "trend"

    def on_close(self, ctx) -> list[Order]:
        if not ctx.month_end:
            return []
        p, I, i = self.p, ctx.ind, ctx.i
        uni = [t for t in p["universe"] if t in I.close.columns]
        dfn = [t for t in p.get("defensive", []) if t in I.close.columns]
        ma = I.sma200
        bench = ctx.benchmark
        risk_on = I.close[bench].iat[i] > ma[bench].iat[i]
        targets: dict[str, float] = {}

        def best_defensive():
            cands = [(I.mom126[t].iat[i], t) for t in dfn if pd.notna(I.mom126[t].iat[i]) and I.mom126[t].iat[i] > 0]
            return max(cands)[1] if cands else None

        if risk_on:
            sc = {}
            for t in uni:
                m1, m2 = I.mom126[t].iat[i], I.mom252[t].iat[i]
                if pd.isna(m1) or pd.isna(m2) or pd.isna(ma[t].iat[i]):
                    continue
                s = 0.5 * (m1 + m2)
                if s > 0 and I.close[t].iat[i] > ma[t].iat[i]:
                    sc[t] = s
            top = sorted(sc, key=lambda t: (-sc[t], t))[: p.get("top_k", 3)]
            if top:
                w = _inv_vol_weights(pd.Series({t: I.vol60[t].iat[i] for t in top}), None)
                share = len(top) / p.get("top_k", 3)
                for t, x in w.items():
                    targets[t] = x * share
            rest = 1.0 - sum(targets.values())
            if rest > 1e-9:
                d = best_defensive()
                if d:
                    targets[d] = targets.get(d, 0.0) + rest
        else:
            d = best_defensive()
            if d:
                targets[d] = 1.0
        return ctx.target_orders(self.name, targets, p.get("min_change", 0.02), reason="monthly_rebalance")


# ----------------------------------------------------------------------------- Book B
class XSMomentum(Book):
    """Monthly. Universe filters, then score = 12-1 momentum / 252-day volatility. Hold the top_n,
    inverse-vol weighted, capped at max_weight; total scaled by the regime multiplier."""
    name, family = "xs_momentum", "trend"

    def on_close(self, ctx) -> list[Order]:
        if not ctx.month_end:
            return []
        p, I, i = self.p, ctx.ind, ctx.i
        mult = self.regime_mult(ctx.regime)
        excluded = set(p.get("exclude", []))
        scores = {}
        for t in ctx.universe(p):
            if t in excluded:
                continue
            m, v = I.mom_12_1[t].iat[i], I.vol252[t].iat[i]
            if pd.isna(m) or pd.isna(v) or v <= 0:
                continue
            scores[t] = m / v
        targets = {}
        if mult > 0 and scores:
            top = sorted(scores, key=lambda t: (-scores[t], t))[: p.get("top_n", 20)]
            top = [t for t in top if scores[t] > p.get("min_score", 0.0)]
            w = _inv_vol_weights(pd.Series({t: I.vol60[t].iat[i] for t in top}), p.get("max_weight", 0.10))
            targets = {t: x * mult for t, x in w.items()}
        return ctx.target_orders(self.name, targets, p.get("min_change", 0.01), reason="monthly_rebalance")


# ----------------------------------------------------------------------------- swing base
class SwingBook(Book):
    swing = True

    def size(self, ctx, t: str, stop_dist: float) -> float:
        p, I, i = self.p, ctx.ind, ctx.i
        price = I.close[t].iat[i]
        budget = ctx.budget * self.regime_mult(ctx.regime)
        if budget <= 0 or stop_dist <= 0 or pd.isna(price):
            return 0.0
        by_risk = p.get("risk_per_trade", 0.005) * budget / stop_dist
        by_cap = p.get("max_weight", 0.10) * budget / price
        return float(np.floor(min(by_risk, by_cap)))

    def entries(self, ctx, candidates: list[tuple[float, str]], stop_atr: float) -> list[Order]:
        held = set(ctx.positions)
        slots = self.p.get("max_positions", 10) - len(held)
        out = []
        for _, t in sorted(candidates):
            if slots <= 0:
                break
            if t in held:
                continue
            atr = ctx.ind.atr14[t].iat[ctx.i]
            sh = self.size(ctx, t, stop_atr * atr)
            if sh >= 1:
                out.append(Order(self.name, t, "buy", sh, "entry", stop_atr=stop_atr, atr=float(atr)))
                slots -= 1
        return out


# ----------------------------------------------------------------------------- Book C
class TrendPullback(SwingBook):
    """Daily swing in uptrends.
    Entry (signal at close, fill next open), all required:
      regime multiplier > 0; liquidity filters;
      close > EMA200 and EMA50 > EMA200                        (established uptrend)
      min RSI14 over the last pullback_days <= rsi_pullback    (a pullback happened)
      close > yesterday's high and MACD histogram rising        (pullback is ending)
      optional: ML probability >= ml.threshold; sentiment >= sentiment_floor
    Rank by 126-day momentum. Stop = fill - stop_atr*ATR14; trailing stop = highest close - trail_atr*ATR14.
    Exits (at close, fill next open): RSI14 > rsi_exit; close < EMA50; regime risk_off; held >= max_hold
    days; sentiment < floor."""
    name, family = "trend_pullback", "trend"

    def on_close(self, ctx) -> list[Order]:
        p, I, i = self.p, ctx.ind, ctx.i
        out = []
        for t, pos in ctx.positions.items():
            c = I.close[t].iat[i]
            reason = None
            if I.rsi14[t].iat[i] > p.get("rsi_exit", 70):
                reason = "rsi_exit"
            elif c < I.ema50[t].iat[i]:
                reason = "trend_break"
            elif ctx.regime in ("risk_off", "crisis"):
                reason = "regime"
            elif pos.days_held >= p.get("max_hold", 30):
                reason = "time"
            elif ctx.sentiment(t) < p.get("sentiment_floor", -0.70):
                reason = "sentiment"
            if reason:
                out.append(Order(self.name, t, "sell", pos.shares, reason))
            else:
                pos.stop = max(pos.stop, pos.highest_close - p.get("trail_atr", 3.0) * I.atr14[t].iat[i])
        if self.regime_mult(ctx.regime) <= 0:
            return out
        cands = []
        for t in ctx.universe(p):
            if t in ctx.positions:
                continue
            c = I.close[t].iat[i]
            e50, e200 = I.ema50[t].iat[i], I.ema200[t].iat[i]
            if not (c > e200 and e50 > e200):
                continue
            if not (I.min_rsi14_5[t].iat[i] <= p.get("rsi_pullback", 40)):
                continue
            if not (c > I.prev_high[t].iat[i] and I.macd_hist[t].iat[i] > I.macd_hist_prev[t].iat[i]):
                continue
            if ctx.sentiment(t) < p.get("sentiment_floor", -0.70):
                continue
            if ctx.ml is not None:
                prob = ctx.ml.prob(t, i)
                mlp = p.get("ml", {})
                if prob is None:
                    if mlp.get("fallback", "rules") != "rules":   # no trained model yet for this year
                        continue
                elif prob < mlp.get("threshold", 0.55):
                    continue
            m = I.mom126[t].iat[i]
            cands.append((-(m if pd.notna(m) else -9), t))
        return out + self.entries(ctx, cands, p.get("stop_atr", 2.0))


# ----------------------------------------------------------------------------- Book D
class MeanReversion(SwingBook):
    """Daily swing, buy short-term weakness in long-term uptrends.
    Entry: regime multiplier > 0; liquidity filters; close > SMA200; RSI(rsi_period) < rsi_entry;
           optional close < lower Bollinger(20, 2) if require_bb.
    Rank by lowest RSI. Stop = fill - stop_atr*ATR14 (catastrophe stop, no trailing).
    Exits (at close): close > SMA5; RSI > rsi_exit; held >= max_hold days; regime risk_off."""
    name, family = "mean_reversion", "reversion"

    def on_close(self, ctx) -> list[Order]:
        p, I, i = self.p, ctx.ind, ctx.i
        r = I.rsi2 if p.get("rsi_period", 2) == 2 else I.rsi14
        out = []
        for t, pos in ctx.positions.items():
            c = I.close[t].iat[i]
            reason = None
            if c > I.sma5[t].iat[i]:
                reason = "sma5_exit"
            elif r[t].iat[i] > p.get("rsi_exit", 70):
                reason = "rsi_exit"
            elif pos.days_held >= p.get("max_hold", 7):
                reason = "time"
            elif ctx.regime in ("risk_off", "crisis"):
                reason = "regime"
            if reason:
                out.append(Order(self.name, t, "sell", pos.shares, reason))
        if self.regime_mult(ctx.regime) <= 0:
            return out
        cands = []
        for t in ctx.universe(p):
            if t in ctx.positions:
                continue
            c = I.close[t].iat[i]
            if not (c > I.sma200[t].iat[i]):
                continue
            rv = r[t].iat[i]
            if not (rv < p.get("rsi_entry", 10)):
                continue
            if p.get("require_bb", False) and not (c < I.bb_low[t].iat[i]):
                continue
            if ctx.sentiment(t) < p.get("sentiment_floor", -0.70):
                continue
            cands.append((rv, t))
        return out + self.entries(ctx, cands, p.get("stop_atr", 2.5))


# ----------------------------------------------------------------------------- Book E
class CrisisAlpha(Book):
    """Active only in crisis mode (engine gives it budget only then). Rebalanced every
    `rebalance_days` trading days and on entering/leaving crisis mode.
      1. Defensive trend: hold defensive assets with positive 63-day return and price > SMA50,
         inverse-vol weighted, sharing `defensive_weight` of the budget; unused share stays in cash.
      2. Index short (optional): short `short_weight` of the budget in the benchmark (or an inverse ETF
         held long) while the benchmark is below its 50-day SMA.
      3. Capitulation rebound (optional): when the benchmark's RSI(2) < rebound_rsi and VIX (if present) is
         in its top 5% of the last year, and the next close is above the prior high, go long
         `rebound_weight` of the budget in the benchmark for at most `rebound_days` days.
    """
    name, family = "crisis_alpha", "defensive"

    def __init__(self, params: dict):
        super().__init__(params)
        self.rebound_left = 0
        self.armed = False
        self.last_rebalance = -10**9
        self.was_active = False

    def on_close(self, ctx) -> list[Order]:
        p, I, i, b = self.p, ctx.ind, ctx.i, ctx.benchmark
        active = ctx.crisis
        changed = active != self.was_active
        self.was_active = active
        if not active:
            self.rebound_left, self.armed = 0, False
            return ctx.target_orders(self.name, {}, 0.0, reason="crisis_off") if changed else []
        # capitulation tracking (daily)
        vix_ok = True
        if "VIX" in I.close.columns:
            vix = I.close["VIX"]
            vix_ok = bool(vix.iat[i] >= vix.iloc[max(0, i - 251):i + 1].quantile(0.95))
        if p.get("rebound_weight", 0) > 0:
            if self.rebound_left > 0:
                self.rebound_left -= 1
            elif self.armed and I.close[b].iat[i] > I.prev_high[b].iat[i]:
                self.rebound_left, self.armed = p.get("rebound_days", 10), False
                changed = True
            if I.rsi2[b].iat[i] < p.get("rebound_rsi", 5) and vix_ok:
                self.armed = True
        if not changed and i - self.last_rebalance < p.get("rebalance_days", 5):
            return []
        self.last_rebalance = i
        targets: dict[str, float] = {}
        dfn = [t for t in p.get("defensive", []) if t in I.close.columns]
        good = [t for t in dfn if I.close[t].iat[i] > I.sma50[t].iat[i] and I.mom63[t].iat[i] > 0]
        if good:
            w = _inv_vol_weights(pd.Series({t: I.vol60[t].iat[i] for t in good}), None)
            share = p.get("defensive_weight", 0.7) * len(good) / max(len(dfn), 1)
            for t, x in w.items():
                targets[t] = x * share
        sma50_b = I.sma50[b].iat[i]
        if p.get("short_weight", 0) > 0 and I.close[b].iat[i] < sma50_b and self.rebound_left == 0:
            inv = p.get("inverse_etf")
            if inv and inv in I.close.columns:
                targets[inv] = targets.get(inv, 0.0) + p["short_weight"]
            else:
                targets[b] = targets.get(b, 0.0) - p["short_weight"]
        if self.rebound_left > 0:
            targets[b] = targets.get(b, 0.0) + p.get("rebound_weight", 0.25)
        return ctx.target_orders(self.name, targets, p.get("min_change", 0.02), reason="crisis_rebalance")


BOOKS = {b.name: b for b in (DualMomentum, XSMomentum, TrendPullback, MeanReversion, CrisisAlpha)}
