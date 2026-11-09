"""Tonight's wiring (docs/21): the MNQ retire switch, the stock desk replacing the swing desk in the
monitor, the R&D desk's stock research job, the research study itself, loading the real index list on
first start, and the gauge-name alias. No network, no Discord, no provider keys.
"""
from __future__ import annotations

import asyncio
import json
import warnings
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from abg.config import Settings
from abg.stocks import research as R
from abg.stocks.universe import Universe

from test_stocks import DeskSettings, NoDataEngine, _desk, make_cfg, make_store

NY = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- the monitor
@pytest.fixture
def pstore(tmp_path):
    from abg.portfolio.store import PortfolioStore
    s = PortfolioStore(tmp_path / "p.sqlite3")
    yield s
    s.close()


def _settings(tmp_path, **kw):
    return Settings(allow_synthetic=True, cache_dir=tmp_path / "cache", data_dir=tmp_path / "data",
                    provider_order="synthetic", hedge_delay=0.0, ai_enabled=False, max_retries=0,
                    anthropic_api_key=None, notify_desktop=False, _env_file=None, **kw)


async def _monitor(settings, pstore):
    from abg.engine import AnalysisEngine
    from abg.live.monitor import Monitor
    from abg.live.notify import NotificationHub
    eng = AnalysisEngine(settings)
    await eng.__aenter__()
    hub = NotificationHub(settings, pstore, eng.http)
    return Monitor(eng, pstore, hub), eng, hub


async def test_retired_mnq_bot_never_starts_even_when_enabled(tmp_path, pstore):
    s = _settings(tmp_path, alpha_enabled=True)              # his .env had this on
    assert s.alpha_retired is True                           # retired by default
    mon, eng, hub = await _monitor(s, pstore)
    try:
        assert mon.alpha is None
        assert mon.status()["alpha"] == {"enabled": False, "retired": True}
        assert not any(e["where"] == "alpha init" for e in mon.errors)
    finally:
        await hub.aclose()
        await eng.__aexit__(None, None, None)


async def test_stock_desk_starts_and_the_retired_swing_desk_stays_off(tmp_path, pstore):
    s = _settings(tmp_path, stocks_enabled=True, swing_enabled=True, stocks_autofill=False)
    mon, eng, hub = await _monitor(s, pstore)
    try:
        assert mon.stocks is not None
        assert mon.swing is None                              # one set of stock signals, never two
        assert any(e["where"] == "swing init" and "retired" in e["error"] for e in mon.errors)
        st = mon.status()
        assert st["stocks"]["enabled"] is True and st["swing"]["retired"] is True
        assert "discord.com" not in json.dumps(st, default=str)
    finally:
        mon.stocks.store.close()
        await hub.aclose()
        await eng.__aexit__(None, None, None)


def test_system_tab_marks_retired_features(tmp_path):
    from abg.api import markets_routes as mr
    import inspect
    src = inspect.getsource(mr)
    assert '"MNQ bot" + (" (retired)"' in src and '"swing desk (retired)"' in src and '"stock desk"' in src


# --------------------------------------------------------------------------- the R&D desk
def _rd(tmp_path, stocks=None, **kw):
    from abg.agents.desk import RDDesk
    s = _settings(tmp_path, **kw)
    m = SimpleNamespace(s=s, engine=SimpleNamespace(http=None), stocks=stocks)
    return RDDesk(m)


def test_rd_runs_only_stock_research_once_mnq_is_retired(tmp_path):
    sat = datetime(2026, 11, 14, 9, 30, tzinfo=NY)            # Saturday
    rd = _rd(tmp_path, stocks=object())
    jobs = [j for j, _, _ in rd.due(sat)]
    assert jobs == ["stocks_research"]
    fri = datetime(2026, 11, 13, 17, 0, tzinfo=NY)           # the MNQ weekly digest slot
    assert rd.due(fri) == []
    assert rd.status()["mnq"] is False and rd.status()["stocks_desk"] is True


def test_rd_brings_mnq_jobs_back_only_when_unretired(tmp_path):
    sat = datetime(2026, 11, 14, 9, 30, tzinfo=NY)
    rd = _rd(tmp_path, stocks=object(), alpha_retired=False)
    jobs = [j for j, _, _ in rd.due(sat)]
    assert "research" in jobs and "stocks_research" in jobs


def test_rd_skips_stock_research_without_the_desk_or_when_off(tmp_path):
    sat = datetime(2026, 11, 14, 9, 30, tzinfo=NY)
    assert [j for j, _, _ in _rd(tmp_path, stocks=None).due(sat)] == []
    assert [j for j, _, _ in _rd(tmp_path, stocks=object(), stocks_research="off").due(sat)] == []


def test_rd_stock_research_posts_only_to_the_rd_channel(tmp_path):
    seen = {}

    class Desk:
        async def research(self, post_url=None):
            seen["url"] = post_url
            return {"at": "x", "verdict": {"overall": "fails"}, "delivered": "posted" if post_url else "dashboard"}

    rd = _rd(tmp_path, stocks=Desk(), rd_webhook_url="https://example.invalid/rd")
    out = asyncio.run(rd.stocks_research(post=True))
    assert seen["url"] == "https://example.invalid/rd" and out["verdict"] == "fails"
    asyncio.run(rd.stocks_research(post=False))
    assert seen["url"] is None                                # a manual run never posts
    assert asyncio.run(_rd(tmp_path, stocks=None).stocks_research())["skipped"]


# --------------------------------------------------------------------------- research
def test_parse_grid():
    g = R.parse_grid("books.trend_pullback.rsi_pullback=35|40; books.xs_momentum.top_n=10|20")
    assert g == {"books.trend_pullback.rsi_pullback": [35, 40], "books.xs_momentum.top_n": [10, 20]}
    assert R.parse_grid("") is None and R.parse_grid(None) is None
    for bad in ("nokey", "a b=1", "a=", "a=1|2|3|4|5;b=1|2|3|4|5"):
        with pytest.raises(ValueError):
            R.parse_grid(bad)


def test_verdict_reads_each_pass_bar():
    wf = {"oos": {"sharpe": 0.9, "max_drawdown": -0.12}, "benchmark": {"sharpe": 0.5, "max_drawdown": -0.30},
          "positive_years": 0.75}
    abl = [{"variant": "costs_x2", "swing_ci90_low": 0.02, "swing_trades": 300}]
    v = R.verdict(wf, abl)
    assert v["overall"] == "passes" and not v["failed"]
    v = R.verdict({**wf, "oos": {"sharpe": 0.4, "max_drawdown": -0.12}}, abl)
    assert v["overall"] == "fails" and any("Sharpe" in f for f in v["failed"])
    assert R.verdict({}, [])["overall"] == "not enough data"


def test_report_shows_r_multiples_as_r_not_percent():
    rep = {"at": "2026-11-09T01:00:00-05:00", "data": {}, "runs": 1, "seconds": 1.0,
           "verdict": R.verdict({}, [{"variant": "costs_x2", "swing_ci90_low": -0.076, "swing_trades": 10}])}
    md = R.render_md(rep)
    assert "-0.076R" in md and "-7.6%" not in md


def test_clean_makes_reports_json_safe():
    import datetime as dt
    import numpy as np
    x = R._clean({"d": dt.date(2020, 3, 9), "t": pd.Timestamp("2020-03-09"), "nat": pd.NaT,
                  "nan": float("nan"), "b": np.bool_(True), "i": np.int64(3)})
    assert json.loads(json.dumps(x)) == {"d": "2020-03-09", "t": "2020-03-09T00:00:00", "nat": None,
                                         "nan": None, "b": True, "i": 3}


def test_desk_research_saves_and_serves_the_latest_study(tmp_path):
    d = _desk(tmp_path, n_stocks=12, days=1000)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        rep = asyncio.run(d.research(post_url=None, years=2, do_wf=False, do_ew=True))
    assert rep["delivered"] == "dashboard"
    assert {a["variant"] for a in rep["ablations"]} >= {"baseline", "fixed_weights", "no_crisis", "costs_x2"}
    assert rep["verdict"]["overall"] in ("passes", "fails", "incomplete", "not enough data")
    json.dumps(rep)                                           # the API returns this as-is
    last = d.last_research()
    assert last and last["dir"] and last["at"] == rep["at"]
    md = d.research_md()
    assert md and "## Verdict" in md and "## Ablations" in md
    assert d.status()["research"]["last"]["verdict"] == rep["verdict"]["overall"]
    assert d.research_busy is None
    d.store.close()


def test_walk_forward_stitches_out_of_sample_years(tmp_path):
    st, u = make_store(tmp_path, n_stocks=10, days=1900)      # ~7.5 years
    cfg = make_cfg(u)
    cfg["start"] = "2019-12-31"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        panel = st.panel(u.core(), benchmark="SPY", min_bars=250, warn=False)
        lab = R.Lab(cfg, panel)
        wf = R.walk_forward(lab, {"books.trend_pullback.rsi_pullback": [35, 40]},
                            first_test_year=2024, registry=tmp_path / "trials.csv", min_train_years=3)
    assert not wf.get("skipped"), wf
    assert panel.dates[-1].year == 2026 and panel.dates[-1].month < 12      # 2026 is partial...
    assert wf["test_years"] == [2024, 2025]                                 # ...so it is not a test year
    assert wf["trials_logged"] == 2 * 2 + 2                   # 2 combos x 2 training windows + 2 test runs
    assert wf["oos"] and wf["per_year"][0]["params"]["books.trend_pullback.rsi_pullback"] in (35, 40)
    st.close()


# --------------------------------------------------------------------------- first start
class IndexEngine(NoDataEngine):
    """FMP answers on the legacy path only; history is served for anything asked."""
    def __init__(self, syms, fail_all=False):
        self.syms, self.fail_all, self.urls, self.asked = syms, fail_all, [], []
        outer = self

        class HTTP:
            async def get_json(self, url, provider, params=None, **kw):
                outer.urls.append(url)
                assert "apikey" in (params or {})
                if outer.fail_all:
                    raise RuntimeError("network down")
                if "/stable/" in url:
                    return {"Error Message": "Restricted Endpoint: not on your plan"}
                return [{"symbol": s, "sector": "Technology" if i % 2 else "Energy"} for i, s in enumerate(outer.syms)]

            async def request(self, *a, **kw):
                raise AssertionError("must not post")
        self.http = HTTP()

    async def history(self, symbol, period="1y", interval="1d", use_cache=False, **kw):
        self.asked.append(symbol)
        idx = pd.bdate_range("2020-01-01", periods=300)
        df = pd.DataFrame({"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1e6}, index=idx)
        return SimpleNamespace(value=SimpleNamespace(df=df, adjusted=True),
                               provenance=SimpleNamespace(provider="stub"))


def _fresh_desk(tmp_path, engine, fmp=True):
    from abg.stocks.desk import StockDesk
    s = DeskSettings(tmp_path)
    s.fmp_api_key = "test-key" if fmp else None
    s.stocks_autofill = True
    s.stocks_backfill_concurrency = 4
    d = StockDesk(s, engine)
    assert d.u.is_seed()
    return d


def test_first_start_loads_the_index_then_fills_missing_history(tmp_path):
    syms = [f"N{i:02d}" for i in range(30)]
    eng = IndexEngine(syms)
    d = _fresh_desk(tmp_path, eng)
    out = asyncio.run(d.first_start())
    assert out["index"]["symbols"] == 30 and not d.u.is_seed()
    assert any("/stable/" in u for u in eng.urls) and any("/api/v3/" in u for u in eng.urls)   # fell back
    assert d.store.sectors()["N01"] == "Technology"
    assert set(syms) <= set(eng.asked)                         # the new names were backfilled
    assert out["missing"] == len(d.u.core())
    # a second start has nothing left to do and makes no calls
    eng.asked.clear()
    again = asyncio.run(d.first_start())
    assert again["index"] is None and again["missing"] == 0 and eng.asked == []
    d.store.close()


def test_first_start_survives_fmp_failing(tmp_path):
    eng = IndexEngine([], fail_all=True)
    d = _fresh_desk(tmp_path, eng)
    d.s.stocks_autofill = False
    out = asyncio.run(d.first_start())
    assert "error" in out["index"] and d.u.is_seed()
    assert any(e["where"] == "index list" for e in d.errors)
    assert "test-key" not in json.dumps(list(d.errors))         # the key never lands in an error message
    d.store.close()


def test_first_start_without_an_fmp_key_stays_on_the_seed(tmp_path):
    eng = IndexEngine(["A"] * 30)
    d = _fresh_desk(tmp_path, eng, fmp=False)
    d.s.stocks_autofill = False
    assert asyncio.run(d.first_start())["index"] is None and eng.urls == []
    d.store.close()


# --------------------------------------------------------------------------- gauge alias
def test_panel_finds_vix_stored_without_its_caret(tmp_path):
    st, u = make_store(tmp_path, n_stocks=2, days=300)
    vix = st.frame("SPY").copy()
    st.upsert("VIX", vix)                                     # how the sweep stores ^VIX
    u.gauges = ["^VIX"]
    assert Universe.stored("^VIX") == "VIX"
    p = st.panel(u.core(), benchmark="SPY", min_bars=250, warn=False)
    assert "VIX" in p.close.columns                           # the VIX stress flags can see it
    st.close()
