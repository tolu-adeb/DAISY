"""Market regime engine (stocks).

Computed once per day on the benchmark (e.g. SPY) from data up to that close:
  risk_off   : close < SMA(sma_long)  OR  realized-vol percentile >= vol_high_pct
  bull_trend : not risk_off AND efficiency ratio(er_window) >= er_trend AND SMA(ret_sma) of daily returns > 0
  bull_chop  : everything else
"""
from __future__ import annotations

import pandas as pd

from abg.tradebook.common import indicators as ind

RISK_OFF, BULL_TREND, BULL_CHOP = "risk_off", "bull_trend", "bull_chop"


def regime_series(close: pd.Series, p: dict) -> pd.DataFrame:
    sma_long = ind.sma(close, p.get("sma_long", 200))
    ret_mean = close.pct_change().rolling(p.get("ret_sma", 20)).mean()
    vol = ind.realized_vol(close, p.get("vol_window", 20))
    vol_pct = ind.pct_rank(vol, p.get("vol_lookback", 252))
    er = ind.efficiency_ratio(close, p.get("er_window", 20))
    risk_off = (close < sma_long) | (vol_pct >= p.get("vol_high_pct", 0.80))
    trend = (~risk_off) & (er >= p.get("er_trend", 0.30)) & (ret_mean > 0)
    label = pd.Series(BULL_CHOP, index=close.index)
    label[trend] = BULL_TREND
    label[risk_off] = RISK_OFF
    label[sma_long.isna() | vol_pct.isna()] = "warmup"
    return pd.DataFrame({"regime": label, "sma_long": sma_long, "vol_pct": vol_pct, "er": er, "ret_mean": ret_mean})
