"""Versioned feature schema - the contract between the terminal and any risk model.

Why a schema?
  A future ML risk model will be trained on a fixed, ordered set of inputs.  If the
  terminal silently renamed or re-scaled a feature, the model would degrade with no
  error.  So every feature is declared once here with a name, group, unit and
  description, and the schema carries a semantic version:

    * PATCH - documentation only
    * MINOR - features *added* (old models keep working; they ignore new columns)
    * MAJOR - features renamed/removed/re-scaled (models must declare compatibility)

Two builders share the same definitions:
  ``build_features``       -> one ``FeatureVector`` for "now"   (inference)
  ``build_feature_frame``  -> a per-bar DataFrame + optional forward labels (training)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from ..utils import utcnow

FEATURE_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    group: str
    unit: str
    description: str
    timeseries: bool = True        # can be computed historically from OHLCV (+benchmark) alone


FEATURES: list[FeatureSpec] = [
    # returns
    FeatureSpec("ret_1d", "returns", "decimal", "1-bar simple return"),
    FeatureSpec("ret_5d", "returns", "decimal", "5-bar simple return"),
    FeatureSpec("ret_21d", "returns", "decimal", "21-bar (~1 month) simple return"),
    FeatureSpec("ret_63d", "returns", "decimal", "63-bar (~1 quarter) simple return"),
    FeatureSpec("ret_252d", "returns", "decimal", "252-bar (~1 year) simple return"),
    # volatility
    FeatureSpec("vol_20d", "volatility", "annualised decimal", "20-bar close-to-close realised vol"),
    FeatureSpec("vol_60d", "volatility", "annualised decimal", "60-bar realised vol"),
    FeatureSpec("vol_252d", "volatility", "annualised decimal", "252-bar realised vol"),
    FeatureSpec("vol_ratio_20_252", "volatility", "ratio", "vol_20d / vol_252d (volatility regime shift)"),
    FeatureSpec("parkinson_vol_20d", "volatility", "annualised decimal", "20-bar high-low (Parkinson) vol"),
    FeatureSpec("downside_vol_60d", "volatility", "annualised decimal", "60-bar semi-deviation of negative returns"),
    FeatureSpec("atr_pct", "volatility", "percent", "ATR(14) as % of price"),
    # tails
    FeatureSpec("var_95_1d", "tail", "decimal (positive = loss)", "Historical 1-bar 95% VaR over 252 bars"),
    FeatureSpec("cvar_95_1d", "tail", "decimal (positive = loss)", "Historical 1-bar 95% expected shortfall"),
    FeatureSpec("skew_252d", "tail", "unitless", "Skewness of 252-bar log returns"),
    FeatureSpec("kurt_252d", "tail", "unitless", "Excess kurtosis of 252-bar log returns"),
    FeatureSpec("gap_freq_3pct", "tail", "fraction", "Share of last 252 bars that gapped >3% at the open"),
    # drawdown
    FeatureSpec("max_dd_252d", "drawdown", "decimal (positive)", "Max peak-to-trough drawdown within 252 bars"),
    FeatureSpec("current_dd", "drawdown", "decimal (positive)", "Current drawdown from the 252-bar high"),
    # trend / momentum
    FeatureSpec("dist_sma50", "trend", "decimal", "close / SMA50 - 1"),
    FeatureSpec("dist_sma200", "trend", "decimal", "close / SMA200 - 1"),
    FeatureSpec("adx", "trend", "0-100", "ADX(14) trend strength"),
    FeatureSpec("rsi", "trend", "0-100", "RSI(14)"),
    FeatureSpec("macd_hist_atr", "trend", "ATR units", "MACD histogram divided by ATR"),
    FeatureSpec("bb_pctb", "trend", "unitless", "Bollinger %B"),
    FeatureSpec("bb_bw_pctile", "trend", "0-1", "Percentile of Bollinger bandwidth within 252 bars"),
    # liquidity
    FeatureSpec("rel_volume", "liquidity", "ratio", "Volume / 20-bar average volume"),
    FeatureSpec("log_dollar_volume_20d", "liquidity", "log10 USD", "log10 of 20-bar average dollar volume"),
    FeatureSpec("amihud_illiq_20d", "liquidity", "|ret| per $1B", "Amihud illiquidity (mean |r| / dollar volume * 1e9)"),
    # market
    FeatureSpec("beta_252d", "market", "unitless", "Beta vs benchmark (252 bars)"),
    FeatureSpec("corr_252d", "market", "-1..1", "Correlation vs benchmark (252 bars)"),
    # non-price (snapshot-only)
    FeatureSpec("news_sentiment", "sentiment", "-1..1", "Recency-weighted news sentiment", False),
    FeatureSpec("news_count_24h", "sentiment", "count", "Articles in the last 24h", False),
    FeatureSpec("atm_iv", "options", "annualised decimal", "At-the-money implied vol (nearest >=5d expiry)", False),
    FeatureSpec("iv_rv_spread", "options", "decimal", "atm_iv - vol_20d (volatility risk premium)", False),
    FeatureSpec("skew_25d", "options", "decimal", "25-delta put IV minus 25-delta call IV", False),
    FeatureSpec("put_call_oi", "options", "ratio", "Put/call open-interest ratio", False),
    FeatureSpec("log_market_cap", "fundamentals", "log10 USD", "log10 market capitalisation", False),
    FeatureSpec("pe", "fundamentals", "ratio", "Trailing P/E", False),
    FeatureSpec("signal_score", "signal", "-100..100", "Composite technical signal score", False),
]
FEATURE_NAMES = [f.name for f in FEATURES]
TIMESERIES_FEATURES = [f.name for f in FEATURES if f.timeseries]


@dataclass
class FeatureVector:
    symbol: str
    values: dict[str, float | None]
    as_of: datetime | None = None
    schema_version: str = FEATURE_SCHEMA_VERSION
    created_at: datetime = field(default_factory=utcnow)

    def to_array(self, names: list[str] | None = None) -> np.ndarray:
        names = names or FEATURE_NAMES
        return np.array([np.nan if self.values.get(n) is None else float(self.values[n]) for n in names], dtype=float)

    def get(self, name: str, default=None):
        v = self.values.get(name)
        return default if v is None else v

    def coverage(self) -> float:
        return sum(v is not None for v in self.values.values()) / len(FEATURE_NAMES)

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "as_of": self.as_of, "schema_version": self.schema_version,
                "coverage": round(self.coverage(), 3), "values": self.values}


def schema() -> dict:
    return {"version": FEATURE_SCHEMA_VERSION,
            "features": [f.__dict__ for f in FEATURES]}


# --------------------------------------------------------------------------- time-series builder
def _rolling_max_drawdown(close: pd.Series, window: int, min_periods: int) -> pd.Series:
    """Largest peak-to-trough loss whose peak AND trough both lie inside the trailing window."""
    x = close.to_numpy(dtype=float)
    out = np.full(len(x), np.nan)
    for i in range(min_periods - 1, len(x)):
        w = x[max(0, i - window + 1): i + 1]
        out[i] = float(np.max(1 - w / np.maximum.accumulate(w)))
    return pd.Series(out, index=close.index)


def _rolling_var_cvar(r: pd.Series, window: int, min_periods: int, alpha: float) -> tuple[pd.Series, pd.Series]:
    """Exact rolling historical VaR / CVaR (expected shortfall), reported as positive losses."""
    x = r.to_numpy(dtype=float)
    var = np.full(len(x), np.nan)
    cvar = np.full(len(x), np.nan)
    for i in range(len(x)):
        w = x[max(0, i - window + 1): i + 1]
        w = w[np.isfinite(w)]
        if len(w) < min_periods:
            continue
        k = max(1, int(np.ceil(alpha * len(w))))
        worst = np.partition(w, k - 1)[:k]
        var[i] = -worst.max()
        cvar[i] = -worst.mean()
    return pd.Series(var, index=r.index), pd.Series(cvar, index=r.index)


def build_feature_frame(ind: pd.DataFrame, bench_close: pd.Series | None = None, ppy: float = 252) -> pd.DataFrame:
    """Per-bar price-derived features (no look-ahead: every value uses data up to that bar)."""
    c, h, l, o, v = (ind[k] for k in ("close", "high", "low", "open", "volume"))
    lr = np.log(c / c.shift(1))
    sr = c.pct_change(fill_method=None)
    f = pd.DataFrame(index=ind.index)
    for n in (1, 5, 21, 63, 252):
        f[f"ret_{n}d"] = c.pct_change(n, fill_method=None)
    for n in (20, 60, 252):
        f[f"vol_{n}d"] = lr.rolling(n, min_periods=max(10, n // 2)).std() * np.sqrt(ppy)
    f["vol_ratio_20_252"] = f["vol_20d"] / f["vol_252d"]
    pk = (np.log(h / l) ** 2) / (4 * np.log(2))
    f["parkinson_vol_20d"] = np.sqrt(pk.rolling(20, min_periods=10).mean() * ppy)
    f["downside_vol_60d"] = np.sqrt((sr.clip(upper=0) ** 2).rolling(60, min_periods=30).mean() * ppy)
    f["atr_pct"] = ind["atr_pct"] if "atr_pct" in ind else np.nan
    f["var_95_1d"], f["cvar_95_1d"] = _rolling_var_cvar(sr, 252, 60, 0.05)
    f["skew_252d"] = lr.rolling(252, min_periods=60).skew()
    f["kurt_252d"] = lr.rolling(252, min_periods=60).kurt()
    gap = (o / c.shift(1) - 1).abs()
    f["gap_freq_3pct"] = (gap > 0.03).astype(float).where(gap.notna()).rolling(252, min_periods=60).mean()
    roll_max = c.rolling(252, min_periods=20).max()
    f["current_dd"] = 1 - c / roll_max
    f["max_dd_252d"] = _rolling_max_drawdown(c, 252, 20)
    f["dist_sma50"] = c / ind["sma_50"] - 1
    f["dist_sma200"] = c / ind["sma_200"] - 1
    f["adx"] = ind["adx"]
    f["rsi"] = ind["rsi_14"]
    f["macd_hist_atr"] = ind["macd_hist"] / ind["atr_14"].replace(0, np.nan)
    f["bb_pctb"] = ind["bb_pctb"]
    f["bb_bw_pctile"] = ind["bb_bandwidth"].rolling(252, min_periods=60).rank(pct=True)
    f["rel_volume"] = ind["rel_volume"]
    dv = (c * v).rolling(20, min_periods=5).mean()
    f["log_dollar_volume_20d"] = np.log10(dv.where(dv > 0))
    f["amihud_illiq_20d"] = (sr.abs() / (c * v).replace(0, np.nan)).rolling(20, min_periods=5).mean() * 1e9
    if bench_close is not None and len(bench_close) > 30:
        br = np.log(bench_close / bench_close.shift(1)).reindex(lr.index)
        cov = lr.rolling(252, min_periods=60).cov(br)
        f["beta_252d"] = cov / br.rolling(252, min_periods=60).var()
        f["corr_252d"] = lr.rolling(252, min_periods=60).corr(br)
    else:
        f["beta_252d"] = np.nan
        f["corr_252d"] = np.nan
    return f[TIMESERIES_FEATURES].replace([np.inf, -np.inf], np.nan)


def add_forward_labels(frame: pd.DataFrame, close: pd.Series, horizons: tuple[int, ...] = (5, 21)) -> pd.DataFrame:
    """Append *future* targets for supervised training.  Prefixed ``y_`` - never use these as inputs."""
    out = frame.copy()
    lr = np.log(close / close.shift(1))
    for hz in horizons:
        out[f"y_fwd_ret_{hz}d"] = close.shift(-hz) / close - 1
        out[f"y_fwd_vol_{hz}d"] = lr[::-1].rolling(hz, min_periods=hz).std()[::-1].shift(-1) * np.sqrt(252)
        fwd_min = close[::-1].rolling(hz, min_periods=hz).min()[::-1].shift(-1)
        out[f"y_fwd_max_loss_{hz}d"] = 1 - fwd_min / close
    return out


# --------------------------------------------------------------------------- snapshot builder
def build_features(symbol: str, ind: pd.DataFrame, bench_close: pd.Series | None = None, *,
                   sentiment: dict | None = None, options: dict | None = None, fundamentals: dict | None = None,
                   signal: dict | None = None, ppy: float = 252, frame: pd.DataFrame | None = None) -> FeatureVector:
    frame = frame if frame is not None else build_feature_frame(ind, bench_close, ppy)
    last = frame.iloc[-1]
    vals: dict[str, float | None] = {n: (None if pd.isna(last.get(n)) else float(last.get(n))) for n in TIMESERIES_FEATURES}
    sentiment, options, fundamentals, signal = sentiment or {}, options or {}, fundamentals or {}, signal or {}
    vals["news_sentiment"] = sentiment.get("score")
    vals["news_count_24h"] = float(sentiment["last_24h"]) if sentiment.get("last_24h") is not None else None
    osum = (options or {}).get("summary") or {}
    real_opts = options.get("available") and not options.get("model_generated")
    vals["atm_iv"] = osum.get("atm_iv") if real_opts else None
    vals["iv_rv_spread"] = (vals["atm_iv"] - vals["vol_20d"]) if (vals["atm_iv"] is not None and vals["vol_20d"] is not None) else None
    vals["skew_25d"] = osum.get("skew_25d") if real_opts else None
    vals["put_call_oi"] = osum.get("put_call_oi_ratio") if real_opts else None
    mc = fundamentals.get("market_cap")
    vals["log_market_cap"] = float(np.log10(mc)) if mc and mc > 0 else None
    vals["pe"] = fundamentals.get("pe")
    vals["signal_score"] = signal.get("score")
    vals = {n: vals.get(n) for n in FEATURE_NAMES}     # enforce schema order / completeness
    return FeatureVector(symbol=symbol, values=vals, as_of=ind.index[-1].to_pydatetime())
