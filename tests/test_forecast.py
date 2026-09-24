"""Prediction engine: statistical sanity, calibration, barriers, recommendation rules and integration."""
import copy
import json
import math

import numpy as np
import pandas as pd
import pytest

from abg.analysis.forecast import ForecastConfig, calibrate, ewma_var, finalize, simulate, vol_term_structure
from abg.utils import jsonable


def gbm_series(n=1500, vol=0.25, mu=0.08, seed=7, start=100.0):
    rng = np.random.default_rng(seed)
    r = (mu - 0.5 * vol**2) / 252 + vol / math.sqrt(252) * rng.standard_normal(n)
    idx = pd.bdate_range("2020-01-01", periods=n + 1)
    return pd.Series(start * np.exp(np.concatenate([[0], np.cumsum(r)])), index=idx)


def test_reproducible_and_json_safe():
    s = gbm_series()
    a = simulate(s, symbol="X", seed=1)
    b = simulate(s, symbol="X", seed=1)
    c = simulate(s, symbol="X", seed=2)
    assert a["horizons"] == b["horizons"] and a["horizons"] != c["horizons"]
    json.dumps(jsonable(a), allow_nan=False)
    d1 = simulate(s, symbol="X")                       # default seed derived from symbol + last bar
    d2 = simulate(s, symbol="X")
    assert d1["horizons"][2]["return_pct"] == d2["horizons"][2]["return_pct"]


def test_percentiles_monotonic_and_fan_shape():
    fc = simulate(gbm_series(), symbol="X", cfg=ForecastConfig(fan_days=60))
    for h in fc["horizons"]:
        q = [h["return_pct"][f"p{p}"] for p in (5, 10, 25, 50, 75, 90, 95)]
        assert q == sorted(q)
        assert 0 <= h["prob_up"] <= 1 and h["var_95_pct"] > 0 and h["cvar_95_pct"] >= h["var_95_pct"]
    assert len(fc["fan"]["time"]) == 60 and all(len(fc["fan"][k]) == 60 for k in ("p5", "p50", "p95"))
    widths = [fc["fan"]["p95"][i] - fc["fan"]["p5"][i] for i in (0, 29, 59)]
    assert widths[0] < widths[1] < widths[2]                              # uncertainty grows with time
    assert sum(s["probability"] for s in fc["scenarios"]) == pytest.approx(1)


def test_mean_matches_drift_and_vol_matches_model():
    s = gbm_series(n=2000, vol=0.20)
    cfg = ForecastConfig(paths=20000, equity_premium=0.05, signal_tilt=0.0)
    fc = simulate(s, symbol="X", rf=0.03, beta=1.0, cfg=cfg, seed=3)
    one_year = fc["horizons"][-1]
    expected = (math.exp(fc["drift"]["total_annual"]) - 1) * 100             # E[S_T]/S0 - 1 under the model
    assert one_year["expected_return_pct"] == pytest.approx(expected, abs=1.5)
    r = np.diff(np.log(s.to_numpy()))
    ev = ewma_var(r, 0.94)
    vp = vol_term_structure(ev[-1], np.var(r[-756:], ddof=1), 21, 22)
    model_sd = math.sqrt(vp.sum())                                          # log-return sd over 21 days
    one_month = fc["horizons"][1]
    sim_sd = one_month["std_pct"] / 100
    assert sim_sd == pytest.approx(model_sd, rel=0.12)


def test_signal_tilt_moves_distribution():
    s = gbm_series()
    up = simulate(s, symbol="X", signal_score=80, seed=5)
    dn = simulate(s, symbol="X", signal_score=-80, seed=5)
    assert up["drift"]["signal_tilt"] > 0 > dn["drift"]["signal_tilt"]
    assert up["horizons"][-1]["prob_up"] > dn["horizons"][-1]["prob_up"]


def test_barrier_probabilities():
    s = gbm_series(vol=0.25)
    p0 = float(s.iloc[-1])
    near_target = simulate(s, symbol="X", plays=[{"name": "L", "direction": "long", "levels": {"stop": p0 * 0.70, "target_1": p0 * 1.02}}])
    b = near_target["barriers"][0]
    assert b["prob_target_first"] > 0.8 and b["prob_stop_first"] < 0.05
    assert b["prob_target_first"] + b["prob_stop_first"] + b["prob_neither"] == pytest.approx(1)
    short = simulate(s, symbol="X", plays=[{"name": "S", "direction": "short", "levels": {"stop": p0 * 1.02, "target_1": p0 * 0.70}}])
    assert short["barriers"][0]["prob_stop_first"] > 0.8
    none = simulate(s, symbol="X", plays=[{"name": "Squeeze", "direction": "neutral", "levels": {}}])
    assert none["barriers"] == []


def test_calibration_on_constant_vol_process():
    s = gbm_series(n=3000, vol=0.30, seed=11)
    r = np.diff(np.log(s.to_numpy()))
    cal = calibrate(r, ewma_var(r, 0.94), ForecastConfig(calibration_origins=100))
    assert cal["available"] and 0.78 <= cal["coverage_90"] <= 0.99 and 0.35 <= cal["coverage_50"] <= 0.65


def test_insufficient_history():
    fc = simulate(gbm_series(n=50), symbol="X")
    assert fc["available"] is False and "bars" in fc["reason"]


# ---------------------------------------------------------------- recommendation rules
def _report_with(fc, score=0.0, risk="Moderate"):
    return {"symbol": "X", "forecast": fc, "signal": {"score": score, "label": "x", "components": {}},
            "risk": [{"model": "baseline", "level": risk, "drivers": []}], "regime": {"trend": "uptrend"},
            "levels": {"support": [], "resistance": []}, "plays": [], "sentiment": {}}


def _fake_fc(exp_ret, sd, pup, rating="High"):
    base = simulate(gbm_series(), symbol="X", seed=1)
    fc = copy.deepcopy(base)
    for h in fc["horizons"]:
        if h["days"] == fc["primary_horizon"]:
            h.update(expected_return_pct=exp_ret, std_pct=sd, prob_up=pup)
    fc["confidence"]["rating"] = rating
    return fc


@pytest.mark.parametrize("exp_ret,pup,score,rating,risk,expected", [
    (12.0, 0.70, 60, "High", "Moderate", "Strong Buy"),
    (12.0, 0.70, 60, "High", "High", "Buy"),            # capped by risk level
    (12.0, 0.70, 60, "Medium", "Moderate", "Buy"),      # Strong Buy needs high confidence
    (6.0, 0.60, 30, "Medium", "Moderate", "Buy"),
    (0.5, 0.50, 60, "High", "Moderate", "Hold"),        # technicals alone can't make it a Buy
    (-8.0, 0.38, -60, "High", "Moderate", "Sell"),
    (-8.0, 0.38, -60, "Medium", "Moderate", "Reduce"),
    (-2.0, 0.46, -30, "Low", "Moderate", "Hold"),       # low confidence pulls one step toward Hold
    (-8.0, 0.38, -60, "Low", "Moderate", "Reduce"),     # ...only one step
])
def test_recommendation_rules(exp_ret, pup, score, rating, risk, expected):
    fc = _fake_fc(exp_ret, 10.0, pup, rating)
    out = finalize(_report_with(fc, score, risk), rf=0.04)
    assert out["recommendation"]["action"] == expected, out["recommendation"]
    th = out["thesis"]
    assert th["headline"].startswith(expected) and th["invalidation"] and th["disclaimer"]


# ---------------------------------------------------------------- integration
async def test_engine_report_contains_prediction(settings):
    from abg.engine import AnalysisEngine
    async with AnalysisEngine(settings) as eng:
        r = await eng.analyze("AAPL", options=False, forecast_paths=1000)
        fc = r["forecast"]
        assert fc["available"] and fc["recommendation"]["action"] in ("Strong Buy", "Buy", "Hold", "Reduce", "Sell")
        assert fc["confidence"]["rating"] in ("High", "Medium", "Low") and fc["thesis"]["headline"]
        json.dumps(r, allow_nan=False)
        wk = await eng.analyze("AAPL", interval="1wk", options=False)
        assert wk["forecast"]["available"] is False
        off = await eng.analyze("AAPL", options=False, forecast=False)
        assert off["forecast"]["available"] is False


def test_recommendation_change_signal(settings, tmp_path):
    from abg.live import SignalEngine
    from abg.portfolio import PortfolioStore
    st = PortfolioStore(tmp_path / "p.sqlite3")
    se = SignalEngine(st, settings)
    rep = lambda a: {"quote": {"price": 10}, "signal": {}, "indicators": {}, "plays": [], "risk": [],  # noqa: E731
                     "forecast": {"recommendation": {"action": a, "prob_up": 0.6}, "confidence": {"rating": "High"},
                                  "thesis": {"headline": f"{a} headline"}}}
    assert se.from_report("X", rep("Hold")) == []
    sigs = se.from_report("X", rep("Buy"))
    assert [s.kind for s in sigs] == ["recommendation"] and sigs[0].direction == "bullish" and "Hold -> Buy" in sigs[0].title
    st.close()


def test_predict_api(monkeypatch, tmp_path):
    for k, v in {"ABG_ALLOW_SYNTHETIC": "true", "ABG_PROVIDER_ORDER": "synthetic", "ABG_CACHE_DIR": str(tmp_path / "c"),
                 "ABG_DATA_DIR": str(tmp_path / "d"), "ABG_AI_ENABLED": "false", "ABG_MONITOR_ON_SERVE": "false"}.items():
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient
    from abg.api.server import app
    with TestClient(app) as c:
        d = c.get("/api/predict/KO?horizon=126&paths=1000").json()
        assert d["forecast"]["primary_horizon"] == 126 and d["forecast"]["paths"] == 1000
        assert c.get("/api/predict/KO?horizon=2").status_code == 422
