"""Technical indicators used by the rule books.

All functions take pandas Series/DataFrames indexed by time and return values
that use only data up to and including each row (no look-ahead).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    au = up.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = au / ad.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(ad != 0.0, 100.0)
    return out.where(au.notna())


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    pc = close.shift(1)
    return pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's ATR."""
    return true_range(high, low, close).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    m = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return m - k * sd, m, m + k * sd


def efficiency_ratio(close: pd.Series, n: int) -> pd.Series:
    """|net move over n| / sum of |moves|; 1 = clean trend, 0 = pure chop."""
    net = (close - close.shift(n)).abs()
    path = close.diff().abs().rolling(n, min_periods=n).sum()
    return net / path.replace(0.0, np.nan)


def realized_vol(close: pd.Series, n: int = 20, annualize: int = 252) -> pd.Series:
    r = np.log(close).diff()
    return r.rolling(n, min_periods=n).std(ddof=1) * np.sqrt(annualize)


def pct_rank(s: pd.Series, n: int) -> pd.Series:
    """Percentile rank (0..1) of the last value within a trailing window of n values."""
    def _rank(x: np.ndarray) -> float:
        last = x[-1]
        return float((x <= last).sum() - 1) / (len(x) - 1) if len(x) > 1 else np.nan
    return s.rolling(n, min_periods=n).apply(_rank, raw=True)


def momentum(close: pd.Series, lookback: int, skip: int = 0) -> pd.Series:
    """Return from t-lookback-skip to t-skip (e.g. 252/21 = classic 12-1)."""
    return close.shift(skip) / close.shift(skip + lookback) - 1.0
