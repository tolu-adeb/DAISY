import numpy as np
import pandas as pd
import pytest

from abg.analysis import indicators as ta
from abg.analysis import signals, stats


def rsi_reference(close: pd.Series, n: int = 14) -> pd.Series:
    """Textbook Wilder RSI with an explicit loop (seeded with the first-n EWM convention)."""
    d = close.diff().to_numpy()
    g, l = np.clip(d, 0, None), np.clip(-d, 0, None)
    ag = al = None
    out = np.full(len(close), np.nan)
    a = 1 / n
    for i in range(1, len(close)):
        ag = g[i] if ag is None else a * g[i] + (1 - a) * ag
        al = l[i] if al is None else a * l[i] + (1 - a) * al
        if i >= n:
            out[i] = 100 - 100 / (1 + ag / al) if al else 100
    return pd.Series(out, index=close.index)


def test_rsi_matches_reference(ohlcv):
    ours = ta.rsi(ohlcv["close"])
    ref = rsi_reference(ohlcv["close"])
    tail = slice(100, None)          # after warm-up both converge
    assert np.allclose(ours.iloc[tail], ref.iloc[tail], atol=1e-8)
    assert ours.between(0, 100).all() or ours.dropna().between(0, 100).all()


def test_rsi_flat_series_is_neutral():
    s = pd.Series([10.0] * 40)
    assert ta.rsi(s).dropna().eq(50).all()


def test_sma_ema_macd_relationships(ohlcv):
    c = ohlcv["close"]
    assert np.isclose(ta.sma(c, 20).iloc[-1], c.iloc[-20:].mean())
    m = ta.macd(c)
    assert np.allclose(m["macd"], ta.ema(c, 12) - ta.ema(c, 26), equal_nan=True)
    assert np.allclose(m["macd_hist"], m["macd"] - m["macd_signal"], equal_nan=True)


def test_bollinger(ohlcv):
    b = ta.bollinger(ohlcv["close"])
    last = ohlcv["close"].iloc[-20:]
    assert np.isclose(b["bb_upper"].iloc[-1], last.mean() + 2 * last.std(ddof=0))
    assert (b["bb_upper"].dropna() >= b["bb_lower"].dropna()).all()


def test_cci_matches_rolling_apply(ohlcv):
    tp = (ohlcv["high"] + ohlcv["low"] + ohlcv["close"]) / 3
    md = tp.rolling(20).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    ref = (tp - tp.rolling(20).mean()) / (0.015 * md)
    assert np.allclose(ta.cci(ohlcv), ref, equal_nan=True)


def test_bounded_oscillators(ohlcv):
    assert ta.williams_r(ohlcv).dropna().between(-100, 0).all()
    assert ta.stochastic(ohlcv)["stoch_k"].dropna().between(0, 100).all()
    assert ta.mfi(ohlcv).dropna().between(0, 100).all()
    a = ta.adx(ohlcv)
    assert a["adx"].dropna().between(0, 100).all()


def test_obv_direction():
    df = pd.DataFrame({"open": [1, 2, 3, 2], "high": [1, 2, 3, 2], "low": [1, 2, 3, 2], "close": [1, 2, 3, 2],
                       "volume": [10, 10, 10, 10]}, dtype=float)
    assert ta.obv(df).tolist() == [0, 10, 20, 10]


def test_compute_all_and_signals(ohlcv):
    ind = ta.compute_all(ohlcv)
    for col in ("rsi_14", "macd", "bb_pctb", "adx", "atr_14", "obv", "williams_r", "cci_20", "vwap_20", "mfi_14"):
        assert col in ind and ind[col].notna().iloc[-1]
    sig = signals.composite_signal(ind)
    assert -100 <= sig["score"] <= 100 and sig["label"]
    plays = signals.classify_plays(ind)
    assert plays and all(0 <= p["confidence"] <= 1 for p in plays)
    for p in plays:
        lv = p["levels"]
        if p["direction"] == "long" and lv:
            assert lv["stop"] < lv["entry"] < lv["target_1"] < lv["target_2"]
        if p["direction"] == "short" and lv:
            assert lv["stop"] > lv["entry"] > lv["target_1"] > lv["target_2"]
    assert signals.market_regime(ind)["trend"]


def test_short_history_does_not_crash():
    df = pd.DataFrame({"open": [1.0, 1.1, 1.2], "high": [1.1, 1.2, 1.3], "low": [0.9, 1.0, 1.1],
                       "close": [1.0, 1.1, 1.2], "volume": [5.0, 6.0, 7.0]}, index=pd.bdate_range("2024-01-01", periods=3))
    ind = ta.compute_all(df)
    signals.composite_signal(ind)
    signals.classify_plays(ind)
    stats.summary(df["close"])


def test_stats_summary(ohlcv):
    s = stats.summary(ohlcv["close"])
    assert s["max_drawdown_pct"] <= 0
    assert s["ann_volatility_pct"] > 0
    lv = stats.support_resistance(ohlcv)
    px = ohlcv["close"].iloc[-1]
    assert all(x > px for x in lv["resistance"]) and all(x < px for x in lv["support"])


@pytest.mark.parametrize("period", ["1mo", "6mo", "1y", "ytd", "max", "45d"])
def test_period_parse(period):
    from abg.utils import period_to_start
    assert period_to_start(period)
