"""Composite signal score, market regime and trade-setup ("play") classification.

Everything here is rule-based and fully explainable: each score comes with the
components / conditions that produced it, so a user (or a downstream risk model)
can audit *why* the terminal says what it says.

Educational tooling - not investment advice.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


def _v(row: pd.Series, k: str) -> float | None:
    x = row.get(k)
    return None if x is None or pd.isna(x) else float(x)


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


# --------------------------------------------------------------------------- composite score
def composite_signal(ind: pd.DataFrame) -> dict:
    r = ind.iloc[-1]
    prev = ind.iloc[-2] if len(ind) > 1 else r
    c = _v(r, "close")
    comps: dict[str, tuple[float, float, str]] = {}   # name -> (vote, weight, reason)

    s50, s200 = _v(r, "sma_50"), _v(r, "sma_200")
    if s50 and s200:
        v = (np.sign(c - s50) + np.sign(c - s200) + np.sign(s50 - s200)) / 3
        comps["trend"] = (v, 0.25, f"price {'>' if c > s50 else '<'} SMA50, {'>' if c > s200 else '<'} SMA200; "
                                   f"SMA50 {'>' if s50 > s200 else '<'} SMA200")
    elif s50:
        comps["trend"] = (float(np.sign(c - s50)), 0.15, f"price {'>' if c > s50 else '<'} SMA50")

    h, hp, atr_ = _v(r, "macd_hist"), _v(prev, "macd_hist"), _v(r, "atr_14")
    if h is not None and hp is not None and atr_:
        v = _clip(0.6 * math.tanh(h / (0.15 * atr_)) + 0.4 * np.sign(h - hp))
        comps["macd"] = (v, 0.15, f"histogram {h:+.3f} ({'rising' if h > hp else 'falling'})")

    rsi = _v(r, "rsi_14")
    if rsi is not None:
        if rsi >= 75:
            v, why = -0.4, "overbought (>75)"
        elif rsi >= 55:
            v, why = 0.6, "bullish momentum zone"
        elif rsi > 45:
            v, why = 0.0, "neutral"
        elif rsi > 25:
            v, why = -0.6, "bearish momentum zone"
        else:
            v, why = 0.4, "oversold (<25) - bounce potential"
        comps["rsi"] = (v, 0.12, f"RSI {rsi:.1f}: {why}")

    adx, pdi, mdi = _v(r, "adx"), _v(r, "plus_di"), _v(r, "minus_di")
    if adx is not None and pdi is not None and mdi is not None and pdi + mdi > 0:
        v = (pdi - mdi) / (pdi + mdi) * min(adx / 25, 1.0)
        comps["adx"] = (_clip(v * 1.5), 0.12, f"ADX {adx:.1f}, +DI {pdi:.1f} vs -DI {mdi:.1f}")

    pb = _v(r, "bb_pctb")
    if pb is not None:
        v = -0.5 if pb > 1.05 else 0.5 if pb < -0.05 else _clip((pb - 0.5) * 0.8)
        comps["bollinger"] = (v, 0.08, f"%B {pb:.2f}")

    obs = _v(r, "obv_slope_20")
    if obs is not None:
        comps["obv"] = (_clip(math.tanh(obs * 2)), 0.10, f"OBV 20-bar slope {obs:+.2f} (volume {'accumulation' if obs > 0 else 'distribution'})")

    mfi = _v(r, "mfi_14")
    if mfi is not None:
        comps["mfi"] = (_clip((mfi - 50) / 30) if 20 < mfi < 80 else (-0.4 if mfi >= 80 else 0.4), 0.07, f"MFI {mfi:.1f}")

    wr, k = _v(r, "williams_r"), _v(r, "stoch_k")
    if wr is not None and k is not None:
        v = 0.4 if wr < -80 and k < 20 else -0.4 if wr > -20 and k > 80 else _clip((k - 50) / 60)
        comps["oscillators"] = (v, 0.06, f"Williams %R {wr:.0f}, Stoch %K {k:.0f}")

    cci = _v(r, "cci_20")
    if cci is not None:
        comps["cci"] = (_clip(cci / 200) if abs(cci) < 200 else -0.3 * np.sign(cci), 0.05, f"CCI {cci:.0f}")

    tw = sum(w for _, w, _ in comps.values()) or 1.0
    score = 100 * sum(v * w for v, w, _ in comps.values()) / tw
    label = ("Strong Bullish" if score >= 50 else "Bullish" if score >= 20 else "Neutral" if score > -20
             else "Bearish" if score > -50 else "Strong Bearish")
    return {"score": round(score, 1), "label": label,
            "components": {k: {"vote": round(v, 3), "weight": w, "reason": why} for k, (v, w, why) in comps.items()}}


# --------------------------------------------------------------------------- regime
def market_regime(ind: pd.DataFrame) -> dict:
    r = ind.iloc[-1]
    c, s50, s200, adx = _v(r, "close"), _v(r, "sma_50"), _v(r, "sma_200"), _v(r, "adx")
    if s50 and s200 and c > s50 > s200:
        trend = "uptrend"
    elif s50 and s200 and c < s50 < s200:
        trend = "downtrend"
    else:
        trend = "sideways / transitioning"
    strength = None if adx is None else ("strong" if adx >= 30 else "moderate" if adx >= 20 else "weak")
    bw = ind["bb_bandwidth"].dropna().iloc[-252:]
    bw_pct = float((bw < bw.iloc[-1]).mean() * 100) if len(bw) > 20 else None
    vol = None if bw_pct is None else ("compressed" if bw_pct <= 20 else "expanded" if bw_pct >= 80 else "normal")
    return {"trend": trend, "trend_strength": strength, "adx": adx, "volatility_regime": vol,
            "bandwidth_percentile": bw_pct}


# --------------------------------------------------------------------------- plays
@dataclass
class Play:
    name: str
    direction: str                    # long | short | neutral
    confidence: float                 # 0..1 = share of conditions satisfied
    conditions: list[tuple[str, bool]]
    horizon: str
    thesis: str
    levels: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "direction": self.direction, "confidence": round(self.confidence, 2),
                "horizon": self.horizon, "thesis": self.thesis, "levels": self.levels,
                "conditions": [{"condition": c, "met": bool(m)} for c, m in self.conditions]}


def _levels(close: float, atr_: float | None, direction: str, swing: float | None = None) -> dict:
    if not atr_ or direction == "neutral":
        return {}
    if direction == "long":
        stop = min(close - 2 * atr_, swing - 0.25 * atr_) if swing and swing < close else close - 2 * atr_
        risk = close - stop
        t1, t2 = close + 1.5 * risk, close + 3 * risk
    else:
        stop = max(close + 2 * atr_, swing + 0.25 * atr_) if swing and swing > close else close + 2 * atr_
        risk = stop - close
        t1, t2 = close - 1.5 * risk, close - 3 * risk
    return {"entry": round(close, 2), "stop": round(stop, 2), "target_1": round(t1, 2), "target_2": round(t2, 2),
            "risk_per_share": round(risk, 2), "risk_pct": round(risk / close * 100, 2), "reward_risk_t1": 1.5,
            "reward_risk_t2": 3.0}


def classify_plays(ind: pd.DataFrame, min_confidence: float = 0.6) -> list[dict]:
    r = ind.iloc[-1]
    prev = ind.iloc[-2] if len(ind) > 1 else r
    g = lambda k: _v(r, k)  # noqa: E731
    c, atr_ = g("close"), g("atr_14")
    rsi, adx, pb, cci = g("rsi_14") or 50, g("adx") or 0, g("bb_pctb"), g("cci_20") or 0
    s20, s50, s200 = g("sma_20"), g("sma_50"), g("sma_200")
    h, hp = g("macd_hist") or 0, _v(prev, "macd_hist") or 0
    relv, wr = g("rel_volume") or 1, g("williams_r") or -50
    hi20p = _v(prev, "high_20")
    lo20p = _v(prev, "low_20")
    bw = ind["bb_bandwidth"].dropna().iloc[-126:]
    squeeze = len(bw) > 20 and bw.iloc[-1] <= bw.quantile(0.2)
    low10 = float(ind["low"].iloc[-10:].min())
    high10 = float(ind["high"].iloc[-10:].max())

    cands = [
        Play("Momentum Breakout", "long", 0, [
            ("Close above prior 20-bar high", bool(hi20p and c > hi20p)),
            ("Relative volume >= 1.5x", relv >= 1.5),
            ("RSI between 55 and 78", 55 <= rsi <= 78),
            ("ADX >= 20 (trend present)", adx >= 20),
            ("MACD histogram positive", h > 0),
        ], "days to weeks", "Price is breaking out of its recent range on above-average volume with momentum confirmation."),
        Play("Trend Pullback (buy the dip)", "long", 0, [
            ("Uptrend: close > SMA50 > SMA200", bool(s50 and s200 and c > s50 > s200)),
            ("RSI cooled to 38-55", 38 <= rsi <= 55),
            ("Price within 1 ATR of SMA20 or SMA50", bool(atr_ and ((s20 and abs(c - s20) <= atr_) or (s50 and abs(c - s50) <= atr_)))),
            ("MACD histogram turning up", h > hp),
        ], "1-4 weeks", "Established uptrend pulling back toward support; looking for trend resumption."),
        Play("Oversold Mean Reversion", "long", 0, [
            ("RSI <= 32", rsi <= 32),
            ("Close below lower Bollinger band (%B < 0.05)", pb is not None and pb < 0.05),
            ("Williams %R <= -80", wr <= -80),
            ("Long-term trend intact (close > SMA200)", bool(s200 and c > s200)),
        ], "days", "Short-term selling looks stretched inside a longer-term uptrend; snap-back candidate."),
        Play("Overextended / Fade", "short", 0, [
            ("RSI >= 75", rsi >= 75),
            ("Close above upper Bollinger band (%B > 1)", pb is not None and pb > 1.0),
            ("CCI >= 180", cci >= 180),
            ("Momentum fading (MACD histogram falling)", h < hp),
        ], "days", "Rally is statistically stretched; elevated odds of consolidation or pullback."),
        Play("Bearish Breakdown", "short", 0, [
            ("Close below prior 20-bar low", bool(lo20p and c < lo20p)),
            ("Downtrend: close < SMA50 < SMA200", bool(s50 and s200 and c < s50 < s200)),
            ("ADX >= 20", adx >= 20),
            ("MACD histogram negative", h < 0),
            ("Relative volume >= 1.3x", relv >= 1.3),
        ], "days to weeks", "Price is losing support in a downtrend with participation."),
        Play("Volatility Squeeze", "neutral", 0, [
            ("Bollinger bandwidth in lowest 20% of 6 months", bool(squeeze)),
            ("ADX < 20 (no trend yet)", adx < 20),
            ("RSI 40-60 (balanced)", 40 <= rsi <= 60),
        ], "watch for break", "Volatility has compressed; a directional expansion often follows. Wait for the break."),
    ]
    out = []
    for p in cands:
        met = sum(m for _, m in p.conditions)
        p.confidence = met / len(p.conditions)
        if p.confidence >= min_confidence and p.conditions[0][1]:   # first condition is the defining one
            swing = low10 if p.direction == "long" else high10 if p.direction == "short" else None
            p.levels = _levels(c, atr_, p.direction, swing)
            out.append(p)
    out.sort(key=lambda p: p.confidence, reverse=True)
    if not out:
        out.append(Play("No Clear Setup", "neutral", 1.0, [], "-",
                        "No rule-based setup currently qualifies; conditions are mixed."))
    return [p.to_dict() for p in out]
