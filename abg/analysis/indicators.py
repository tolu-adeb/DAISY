"""Vectorised technical indicators (pure pandas/numpy - no TA-Lib dependency).

Conventions
-----------
* Inputs are the sanitised OHLCV frame from ``PriceHistory.df``.
* Every function returns a Series/DataFrame aligned to the input index, NaN during
  the warm-up window.  Nothing is forward-filled or look-ahead biased.
* Wilder's smoothing (RSI, ATR, ADX) is implemented as an EWM with alpha = 1/n,
  which matches TradingView / TA-Lib after the warm-up period.

Computing all indicators for 10 years of daily bars takes a few milliseconds.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = wilder(d.clip(lower=0), n)
    loss = wilder((-d).clip(lower=0), n)
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out = out.where(loss != 0, 100.0).where(~((gain == 0) & (loss == 0)), 50.0)
    return out.where(gain.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    up, lo = mid + k * sd, mid - k * sd
    width = (up - lo)
    return pd.DataFrame({"bb_mid": mid, "bb_upper": up, "bb_lower": lo,
                         "bb_pctb": (close - lo) / width.replace(0, np.nan),
                         "bb_bandwidth": width / mid})


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(df), n)


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = wilder(true_range(df), n)
    pdi = 100 * wilder(plus_dm, n) / tr.replace(0, np.nan)
    mdi = 100 * wilder(minus_dm, n) / tr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return pd.DataFrame({"plus_di": pdi, "minus_di": mdi, "adx": wilder(dx, n)})


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0.0)
    return (direction * df["volume"]).cumsum()


def williams_r(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hh = df["high"].rolling(n, min_periods=n).max()
    ll = df["low"].rolling(n, min_periods=n).min()
    return -100 * (hh - df["close"]) / (hh - ll).replace(0, np.nan)


def cci(df: pd.DataFrame, n: int = 20) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = sma(tp, n)
    vals = tp.to_numpy()
    md = np.full(len(vals), np.nan)
    if len(vals) >= n:                          # vectorised mean absolute deviation
        w = sliding_window_view(vals, n)
        md[n - 1:] = np.abs(w - w.mean(axis=1, keepdims=True)).mean(axis=1)
    return (tp - ma) / (0.015 * pd.Series(md, index=df.index).replace(0, np.nan))


def stochastic(df: pd.DataFrame, n: int = 14, d: int = 3) -> pd.DataFrame:
    hh = df["high"].rolling(n, min_periods=n).max()
    ll = df["low"].rolling(n, min_periods=n).min()
    k = 100 * (df["close"] - ll) / (hh - ll).replace(0, np.nan)
    return pd.DataFrame({"stoch_k": k, "stoch_d": sma(k, d)})


def mfi(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    flow = tp * df["volume"]
    up = flow.where(tp.diff() > 0, 0.0).rolling(n, min_periods=n).sum()
    dn = flow.where(tp.diff() < 0, 0.0).rolling(n, min_periods=n).sum()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def vwap(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Rolling n-bar VWAP.  (A true session VWAP needs intraday bars; for daily data a
    rolling volume-weighted typical price is the meaningful equivalent.)"""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = (tp * df["volume"]).rolling(n, min_periods=n).sum()
    v = df["volume"].rolling(n, min_periods=n).sum()
    return pv / v.replace(0, np.nan)


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP for intraday bars (resets each calendar day)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    day = df.index.normalize()
    pv = (tp * df["volume"]).groupby(day).cumsum()
    v = df["volume"].groupby(day).cumsum()
    return pv / v.replace(0, np.nan)


def compute_all(df: pd.DataFrame, intraday: bool = False) -> pd.DataFrame:
    """Return a copy of ``df`` with every indicator column appended."""
    c = df["close"]
    out = df.copy()
    for n in (10, 20, 50, 100, 200):
        out[f"sma_{n}"] = sma(c, n)
    for n in (9, 12, 21, 26, 50):
        out[f"ema_{n}"] = ema(c, n)
    out["rsi_14"] = rsi(c, 14)
    out = out.join(macd(c)).join(bollinger(c)).join(adx(df)).join(stochastic(df))
    out["atr_14"] = atr(df, 14)
    out["atr_pct"] = out["atr_14"] / c * 100
    out["obv"] = obv(df)
    out["obv_slope_20"] = out["obv"].diff(20) / (df["volume"].rolling(20).mean() * 20).replace(0, np.nan)
    out["williams_r"] = williams_r(df)
    out["cci_20"] = cci(df)
    out["mfi_14"] = mfi(df)
    out["vwap_20"] = session_vwap(df) if intraday else vwap(df, 20)
    out["vol_avg_20"] = df["volume"].rolling(20, min_periods=5).mean()
    out["rel_volume"] = df["volume"] / out["vol_avg_20"].replace(0, np.nan)
    out["high_20"] = df["high"].rolling(20, min_periods=20).max()
    out["low_20"] = df["low"].rolling(20, min_periods=20).min()
    out["high_252"] = df["high"].rolling(252, min_periods=20).max()
    out["low_252"] = df["low"].rolling(252, min_periods=20).min()
    out["roc_20"] = c.pct_change(20, fill_method=None) * 100
    out["ret_1d"] = c.pct_change(fill_method=None)
    return out


INDICATOR_LABELS = {
    "rsi_14": "RSI (14)", "macd": "MACD", "macd_signal": "MACD signal", "macd_hist": "MACD histogram",
    "bb_pctb": "Bollinger %B", "bb_bandwidth": "Bollinger bandwidth", "adx": "ADX (14)", "plus_di": "+DI",
    "minus_di": "-DI", "atr_14": "ATR (14)", "atr_pct": "ATR % of price", "obv": "OBV", "williams_r": "Williams %R",
    "cci_20": "CCI (20)", "mfi_14": "MFI (14)", "stoch_k": "Stochastic %K", "stoch_d": "Stochastic %D",
    "vwap_20": "VWAP (20)", "sma_20": "SMA 20", "sma_50": "SMA 50", "sma_200": "SMA 200", "ema_21": "EMA 21",
    "rel_volume": "Relative volume", "roc_20": "ROC 20 (%)",
}


def latest_snapshot(ind: pd.DataFrame) -> dict[str, float | None]:
    row = ind.iloc[-1]
    return {k: (None if pd.isna(row.get(k)) else float(row.get(k))) for k in INDICATOR_LABELS}
