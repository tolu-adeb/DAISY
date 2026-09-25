"""End-to-end: engine, risk layer and REST API (synthetic data, fully offline)."""
import json

import numpy as np
import pandas as pd
import pytest

from abg.engine import AnalysisEngine
from abg.errors import AllProvidersFailed, NoDataError
from abg.risk import (FEATURE_NAMES, FeatureVector, RiskAssessment, register_risk_model, registered_models,
                      unregister_risk_model)
from abg.risk.baseline import BaselineRiskModel, position_size
from abg.risk.features import build_feature_frame
from abg.risk.interface import RiskContext, run_models
from abg.risk.portfolio import portfolio_risk
from conftest import FakeProvider


@pytest.fixture
async def engine(settings):
    e = AnalysisEngine(settings)
    yield e
    await e.aclose()


async def test_analyze_full_report_is_strict_json(engine):
    r = await engine.analyze("AAPL", include_series=True)
    json.dumps(r, allow_nan=False)                       # no NaN/inf leaks
    for k in ("signal", "regime", "plays", "indicators", "statistics", "options", "features", "risk", "ai_insight",
              "provenance", "timings_ms", "series"):
        assert k in r
    assert r["data_quality"]["synthetic"] is True
    assert r["risk"][0]["model"] == "baseline" and 0 <= r["risk"][0]["score"] <= 100
    assert r["options"]["model_generated"] is True
    assert r["ai_insight"]["engine"] == "rule-based"
    assert len(r["series"]["time"]) == len(r["series"]["close"]) == r["data_quality"]["bars_in_view"]
    assert set(r["features"]["values"]) == set(FEATURE_NAMES)


async def test_partial_failure_is_isolated(settings, tmp_path):
    from abg.cache import TieredCache
    from abg.models import Capability
    good = FakeProvider(settings, "good")
    news = FakeProvider(settings, "badnews")
    news.capabilities = frozenset({Capability.NEWS})

    async def boom(*a, **k):
        raise RuntimeError("vendor changed their JSON")
    news.get_news = boom
    settings.provider_order = "good,badnews"
    e = AnalysisEngine(settings, providers=[good, news], cache=TieredCache(tmp_path / "x"))
    try:
        r = await e.analyze("MSFT", ai=False)
        assert r["signal"]["score"] is not None
        assert any(w.startswith("news:") for w in r["warnings"])
    finally:
        await e.aclose()


async def test_history_failure_raises(settings, tmp_path):
    from abg.cache import TieredCache
    bad = FakeProvider(settings, "bad", fail=NoDataError("unknown ticker", provider="bad"))
    settings.provider_order = "bad"
    e = AnalysisEngine(settings, providers=[bad], cache=TieredCache(tmp_path / "y"))
    try:
        with pytest.raises(AllProvidersFailed):
            await e.analyze("ZZZZ")
    finally:
        await e.aclose()


async def test_analyze_csv_and_compare(engine):
    csv = "Date,Open,High,Low,Close,Volume\n" + "\n".join(
        f"{d.date()},{100 + i},{101 + i},{99 + i},{100.5 + i},{1e6}" for i, d in enumerate(pd.bdate_range("2023-01-02", periods=300)))
    r = await engine.analyze_csv(csv, "MYCSV")
    assert r["symbol"] == "MYCSV" and r["data_quality"]["csv_format"] == "generic"
    c = await engine.compare(["AAPL", "MSFT", "NVDA"], "1y")
    assert len(c["rows"]) == 3 and c["portfolio"]["available"]
    assert sum(c["portfolio"]["risk_contribution_pct"].values()) == pytest.approx(100, abs=0.1)


async def test_feature_frame_has_no_lookahead(engine):
    f = await engine.feature_frame("AAPL", "2y", labels=True)
    assert f.attrs["schema_version"]
    assert f["y_fwd_ret_5d"].iloc[-5:].isna().all()      # future unknown at the end
    # features at t must not change when future bars are removed
    h = (await engine.history("AAPL", "2y")).value
    from abg.analysis.indicators import compute_all
    full = build_feature_frame(compute_all(h.df))
    cut = build_feature_frame(compute_all(h.df.iloc[:-30]))
    common = cut.index[-1]
    assert np.allclose(full.loc[common].to_numpy(float), cut.loc[common].to_numpy(float), equal_nan=True)


# ---------------------------------------------------------------- risk layer
def _fv(**vals):
    base = {n: None for n in FEATURE_NAMES}
    base.update(vals)
    return FeatureVector("X", base)


def _ctx():
    r = pd.Series(np.random.default_rng(0).normal(0, 0.02, 300))
    return RiskContext("X", pd.DataFrame({"close": 100 * (1 + r).cumprod()}), r)


def test_baseline_monotonic_in_volatility():
    m = BaselineRiskModel()
    low = m.assess(_fv(vol_60d=0.12, cvar_95_1d=0.012), _ctx())
    high = m.assess(_fv(vol_60d=0.70, cvar_95_1d=0.07), _ctx())
    assert high.score > low.score and high.level != low.level
    assert low.metrics["var_95_1d_pct"] > 0 and low.metrics["cvar_95_1d_pct"] >= low.metrics["var_95_1d_pct"]


def test_position_size():
    ps = position_size(50_000, 1.0, entry=100, stop=95)
    assert ps["shares"] == 100 and ps["risk_amount"] == 500


async def test_plugin_models_isolated_and_schema_checked():
    class Good:
        name, version, schema_major = "good", "0.1", 1

        def assess(self, f, ctx):
            return RiskAssessment(self.name, self.version, 70.0, "High")

    class Crashy:
        name, version, schema_major = "crashy", "0.1", 1

        async def assess(self, f, ctx):
            raise ValueError("model weights missing")

    class Old:
        name, version, schema_major = "old", "0.1", 0

        def assess(self, f, ctx):
            return RiskAssessment(self.name, self.version, 1.0, "Low")

    for m in (Good, Crashy, Old):
        register_risk_model(m)
    try:
        res = await run_models(_fv(vol_60d=0.3), _ctx(), [BaselineRiskModel(), *[x for x in registered_models() if x.name in ("good", "crashy", "old")]])
        by = {r["model"]: r for r in res}
        assert res[0]["model"] == "baseline"
        assert by["good"]["score"] == 70 and "error" in by["crashy"] and "schema mismatch" in by["old"]["error"]
    finally:
        for n in ("good", "crashy", "old"):
            unregister_risk_model(n)


def test_register_rejects_non_models():
    with pytest.raises(TypeError):
        register_risk_model(object())


def test_portfolio_risk_contributions():
    rng = np.random.default_rng(3)
    closes = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, [0.01, 0.02, 0.03], (300, 3)), axis=0)),
                          columns=["A", "B", "C"])
    p = portfolio_risk(closes)
    assert sum(p["risk_contribution_pct"].values()) == pytest.approx(100, abs=0.1)
    assert p["risk_contribution_pct"]["C"] > p["risk_contribution_pct"]["A"]
    assert p["diversification_ratio"] >= 1


# ---------------------------------------------------------------- sentiment
def test_sentiment_lexicon():
    from abg.analysis.sentiment import LexiconSentiment, analyze_news
    from abg.models import NewsItem
    m = LexiconSentiment()
    assert m.score_one("Apple beats estimates and raises guidance") > 0.5
    assert m.score_one("Company cuts guidance amid SEC investigation") < -0.5
    assert m.score_one("Shares did not fall despite concerns") > m.score_one("Shares fall on concerns")
    agg = analyze_news([NewsItem("Stock surges on record revenue", "t"), NewsItem("Analyst downgrades shares", "t")])
    assert agg["articles"] == 2 and -1 <= agg["score"] <= 1


# ---------------------------------------------------------------- API
@pytest.fixture
def api_client(monkeypatch, tmp_path):
    monkeypatch.setenv("ABG_ALLOW_SYNTHETIC", "true")
    monkeypatch.setenv("ABG_PROVIDER_ORDER", "synthetic")
    monkeypatch.setenv("ABG_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("ABG_AI_ENABLED", "false")
    monkeypatch.setenv("ABG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABG_MONITOR_ON_SERVE", "false")
    from fastapi.testclient import TestClient
    from abg.api.server import app
    with TestClient(app) as c:
        yield c


def test_api_endpoints(api_client):
    c = api_client
    assert c.get("/api/health").json()["status"] == "ok"
    r = c.get("/api/analyze/AAPL?ai=false")
    assert r.status_code == 200 and r.json()["symbol"] == "AAPL"
    assert c.get("/api/quote/AAPL").json()["quote"]["price"] > 0
    assert len(c.get("/api/history/AAPL?period=3mo").json()["bars"]) > 40
    assert c.get("/api/options/AAPL").json()["options"]["available"]
    assert c.get("/api/risk/AAPL").json()["risk"][0]["model"] == "baseline"
    assert c.get("/api/compare?symbols=AAPL,MSFT").json()["portfolio"]["available"]
    f = c.get("/api/features/AAPL?tail=10").json()
    assert len(f["rows"]) == 10 and f["columns"][0] == "symbol"
    assert c.get("/api/schema").json()["version"]
    assert c.get("/").status_code == 200 and "Intelligence Terminal" in c.get("/").text
    csv = "Date,Open,High,Low,Close,Volume\n" + "\n".join(
        f"{d.date()},{10 + i % 7},{11 + i % 7},{9 + i % 7},{10.5 + i % 7},1000" for i, d in enumerate(pd.bdate_range("2023-01-02", periods=120)))
    assert c.post("/api/analyze-csv", json={"symbol": "UP", "csv": csv}).status_code == 200


def test_api_errors_are_json(api_client):
    r = api_client.get("/api/analyze/$$$")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_symbol"
    r = api_client.get("/api/analyze/AAPL?period=banana")
    assert r.status_code == 400
    r = api_client.get("/api/analyze/AAPL?source=polygon")        # not configured
    assert r.status_code == 400 and "not configured" in r.json()["error"]["message"]


# ---------------------------------------------------------------- AI insight (mocked Anthropic API)
async def test_claude_insight_success_cache_and_fallback(settings, tmp_path):
    import httpx
    from abg.analysis.insights import claude_insight
    from abg.cache import TieredCache
    from abg.http import HttpClient
    calls = {"n": 0}
    reply = {"summary": "Uptrend intact.", "bull_case": ["a"], "bear_case": ["b"], "key_levels": [], "risks_to_watch": [],
             "stance": "neutral", "confidence": "low"}

    def handler(req):
        calls["n"] += 1
        assert req.headers["x-api-key"] == "sk-test" and req.headers["anthropic-version"]
        body = json.loads(req.content)
        assert body["model"] == settings.anthropic_model and "digest" in body["messages"][0]["content"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "```json\n" + json.dumps(reply) + "\n```"}]})
    s = settings.model_copy(update={"ai_enabled": True, "anthropic_api_key": "sk-test"})
    http, cache = HttpClient(transport=httpx.MockTransport(handler)), TieredCache(tmp_path / "ai")
    report = {"symbol": "X", "signal": {"score": 10, "label": "Neutral"}, "regime": {}, "plays": []}
    out = await claude_insight(report, s, http, cache)
    assert out["summary"] == "Uptrend intact." and out["engine"].startswith("claude:")
    await claude_insight(report, s, http, cache)
    assert calls["n"] == 1                                  # second call served from cache

    bad = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(529, text="overloaded")))
    out = await claude_insight({**report, "symbol": "Y"}, s, bad, None)
    assert out["engine"] == "rule-based" and "unavailable" in out["note"]
