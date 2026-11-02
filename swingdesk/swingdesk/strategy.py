"""Deterministic grading of a swing signal: take / skip, sizing, and the exact trade plan.

Everything that touches money lives here, in plain code, so the LLM agents can explain
and research but never decide size or override a gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

import pandas as pd
import yaml

from .backtest import atr
from .engine import Plan

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config/strategy.yaml"


def load_config(path: Path | str = CONFIG_PATH) -> dict:
    return yaml.safe_load(open(path))


@dataclass
class Verdict:
    take: bool
    tier: str                         # A (gate passes on both) | B (one) | SKIP
    reasons: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    entry_px: float | None = None     # limit price
    stop_px: float | None = None
    tp_px: float | None = None
    posted_target: float | None = None
    r_per_share: float | None = None
    risk_pct: float | None = None     # % of equity to risk on this trade
    shares_per_10k: int | None = None # position size for every $10k of equity
    metrics: dict = field(default_factory=dict)

    def as_dict(self):
        return asdict(self)


def market_metrics(px: pd.DataFrame, spy: pd.DataFrame, asof: pd.Timestamp | None = None,
                   sma_n: int = 20) -> dict:
    if asof is not None:
        px, spy = px[px.index <= asof], spy[spy.index <= asof]
    last = float(px["close"].iloc[-1])
    a = float(atr(px).iloc[-1])
    spy_sma = float(spy["close"].rolling(sma_n).mean().iloc[-1])
    return {
        "asof": str(px.index[-1].date()),
        "last": last,
        "atr": a,
        "atr_pct": round(100 * a / last, 2),
        "spy_close": float(spy["close"].iloc[-1]),
        "spy_sma": round(spy_sma, 2),
        "spy_below_sma": bool(spy["close"].iloc[-1] < spy_sma),
        "dollar_vol_m": round(float((px["close"] * px["volume"] * 1000).tail(20).mean()) / 1e6, 1),
    }


def grade(sig: dict, px: pd.DataFrame, spy: pd.DataFrame, cfg: dict | None = None,
          asof: pd.Timestamp | None = None, flags: list | None = None) -> Verdict:
    """`sig` needs direction, entry_type, entry_lo/hi, target_lo/hi, stop, stop_basis."""
    cfg = cfg or load_config()
    g, hb, ex, rk = cfg["gate"], cfg["hard_blocks"], cfg["exit"], cfg["risk"]
    m = market_metrics(px, spy, asof, g["spy_below_sma"])
    flags = list(flags or [])
    reasons = []

    if m["last"] < hb["min_price"]:
        return Verdict(False, "SKIP", [f"price {m['last']:.2f} < ${hb['min_price']} floor"], flags, metrics=m)
    if m["dollar_vol_m"] < hb["min_dollar_volume_m"]:
        return Verdict(False, "SKIP", [f"avg dollar volume ${m['dollar_vol_m']}M < ${hb['min_dollar_volume_m']}M"],
                       flags, metrics=m)

    low_vol = m["atr_pct"] <= g["max_atr_pct"]
    pullback_mkt = m["spy_below_sma"]
    reasons.append(f"ATR {m['atr_pct']}% {'<=' if low_vol else '>'} {g['max_atr_pct']}%")
    reasons.append(f"SPY {'below' if pullback_mkt else 'above'} its {g['spy_below_sma']}-day SMA "
                   f"({m['spy_close']:.2f} vs {m['spy_sma']:.2f})")
    passed = (low_vol or pullback_mkt) if g["require"] == "any" else (low_vol and pullback_mkt)
    tier = "A" if (low_vol and pullback_mkt) else ("B" if passed else "SKIP")
    if not passed:
        reasons.append("gate failed: high-volatility name while the market is extended "
                       "(historically 1 win in 14 as posted)")

    long = sig["direction"] == "long"
    if sig["entry_type"] == "zone":
        entry = float(sig["entry_hi"] if long else sig["entry_lo"])
    elif sig["entry_type"] == "close_above":
        entry = float(sig["entry_lo"])
    else:
        entry = m["last"]
    stop = float(sig["stop"])
    r = (entry - stop) if long else (stop - entry)
    if r <= 0:
        return Verdict(False, "SKIP", reasons + ["stop is on the wrong side of entry"], flags, metrics=m)
    posted_t = float(sig["target_lo"] if long else sig["target_hi"])
    tp = entry + (1 if long else -1) * ex["take_profit_r"] * r
    if (long and posted_t < tp) or (not long and posted_t > tp):
        tp = posted_t
        reasons.append("posted target is closer than 0.75R; using it")

    if m["last"] < 10:
        flags.append("price_below_10")
    risk_pct = rk["risk_per_trade_pct"]
    if any(f in rk["size_down_flags"] for f in flags):
        risk_pct *= 0.5
        reasons.append(f"size halved for {', '.join(f for f in flags if f in rk['size_down_flags'])}")
    shares = int((10_000 * risk_pct / 100) // r)
    return Verdict(passed, tier, reasons, flags, round(entry, 4), round(stop, 4), round(tp, 4), posted_t,
                   round(r, 4), round(risk_pct, 3), shares, m)


def plan_from_config(cfg: dict | None = None) -> Plan:
    cfg = cfg or load_config()
    return Plan(name=f"v{cfg['version']}", entry=cfg["entry"]["style"], tp1_r=cfg["exit"]["take_profit_r"],
                tp1_frac=1.0, stop=cfg["exit"]["stop"], time_stop=cfg["exit"]["time_stop_bars"],
                fill_window=cfg["entry"]["fill_window_bars"],
                chase_atr=float(cfg["entry"].get("chase_atr") or 0.0))      # spin-off PATCH 7 (absent = off)
