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
    """GitHub serves ``github_csv`` (or fails when it is None); FMP answers on the legacy path only;
    history is served for anything asked."""
    def __init__(self, syms, fail_all=False, github_csv=None):
        self.syms, self.fail_all, self.urls, self.asked = syms, fail_all, [], []
        self.github_csv = github_csv
        outer = self

        class HTTP:
            async def get_text(self, url, provider, timeout=None, **kw):
                outer.urls.append(url)
                if outer.fail_all or outer.github_csv is None:
                    raise RuntimeError("github unreachable")
                return outer.github_csv

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


def history_csv(n=470, renames=True):
    """A small S&P-shaped history: n members, a rename, a stock that leaves and returns, a dotted class."""
    base = [f"T{i:03d}" for i in range(n - 4)]
    snaps = [("2015-01-02", base + ["FB", "AAL", "BRK.B", "OLD"]),
             ("2018-03-01", base + ["FB", "BRK.B", "OLD", "NEW1"]),             # AAL leaves, NEW1 joins
             ("2020-12-21", base + ["FB", "BRK.B", "NEW1", "TSLA"]),            # OLD leaves, TSLA joins
             ("2022-06-09", base + (["META"] if renames else ["FB"]) + ["BRK.B", "NEW1", "TSLA"]),
             ("2024-01-02", base + (["META"] if renames else ["FB"]) + ["BRK.B", "TSLA", "AAL"])]  # AAL back, NEW1 out
    snaps += [(f"2024-0{m}-01", snaps[-1][1]) for m in range(2, 9)]               # padding: >= 10 snapshots
    return "date,tickers\n" + "".join(f'{d},"{",".join(m)}"\n' for d, m in snaps)


def test_parse_history_membership_renames_and_reentries():
    from abg.stocks.index_sources import parse_history
    h = parse_history(history_csv())
    assert h.as_of == "2024-08-01" and len(h.current) == 470
    assert h.stints["META"] == [("2015-01-02", None)]               # FB's years folded into today's ticker
    assert "FB" not in h.stints and ("FB", "META") in h.renamed
    assert h.stints["AAL"] == [("2015-01-02", "2018-03-01"), ("2024-01-02", None)]   # left and came back
    assert h.stints["TSLA"] == [("2020-12-21", None)]               # not eligible before it joined
    assert h.stints["OLD"] == [("2015-01-02", "2020-12-21")]
    assert "BRK-B" in h.current and "BRK.B" not in h.stints        # dot -> dash, like the providers
    assert all(any(b is None for _, b in h.stints[t]) for t in h.current)


def test_parse_history_rejects_what_is_not_the_dataset():
    from abg.stocks.index_sources import parse_history
    with pytest.raises(ValueError, match="header"):
        parse_history("symbol,name\nAAPL,Apple\n")
    with pytest.raises(ValueError, match="snapshots"):
        parse_history('date,tickers\n2024-01-02,"A,B"\n')
    with pytest.raises(ValueError, match="members"):
        parse_history(history_csv(n=40))                            # 40 names is not an S&P 500 list


def test_an_unconfirmed_rename_is_left_alone():
    from abg.stocks.index_sources import parse_history
    h = parse_history(history_csv(renames=False))                  # FB never becomes META in this data
    assert "FB" in h.stints and "META" not in h.stints and not h.renamed


def test_first_start_loads_the_index_from_github_without_any_key(tmp_path):
    eng = IndexEngine([], github_csv=history_csv())
    d = _fresh_desk(tmp_path, eng, fmp=False)
    d.s.stocks_autofill = False
    out = asyncio.run(d.first_start())
    assert out["index"]["source"] == "github:fja05680/sp500" and out["index"]["symbols"] == 470
    assert out["index"]["membership_rows"] >= 470 and not d.u.is_seed()
    assert not any("financialmodelingprep" in u for u in eng.urls)  # FMP never needed
    # membership now drives eligibility: TSLA can't be bought before it joined
    st = d.store
    idx = pd.bdate_range("2019-01-02", periods=600)
    flat = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1e6}, index=idx)
    for t in ("SPY", "TSLA", "AAL"):
        st.upsert(t, flat)
    p = st.panel(["TSLA", "AAL"], benchmark="SPY", min_bars=250, warn=False)
    assert not p.eligible["TSLA"].loc[:"2020-12-18"].any() and p.eligible["TSLA"].loc["2020-12-21":].all()
    assert not p.eligible["AAL"].loc["2019-01-02":"2021-06-01"].any()   # between its two stints
    assert p.eligible["SPY"].all()                                     # ETFs are never members: always eligible
    # the weekly re-check doesn't refetch the next day
    eng.urls.clear()
    assert asyncio.run(d.load_index_if_seed()) is None and eng.urls == []
    d.store.close()


def test_first_start_falls_back_to_fmp_then_fills_missing_history(tmp_path):
    syms = [f"N{i:02d}" for i in range(30)]
    eng = IndexEngine(syms)                                            # GitHub unreachable
    d = _fresh_desk(tmp_path, eng)
    out = asyncio.run(d.first_start())
    assert out["index"]["symbols"] == 30 and not d.u.is_seed()
    assert out["index"]["source"] == "fmp" and out["index"]["fallback_from"]
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


def test_first_start_with_no_source_reachable_stays_on_the_seed(tmp_path):
    eng = IndexEngine(["A"] * 30)                                      # GitHub down, no FMP key
    d = _fresh_desk(tmp_path, eng, fmp=False)
    d.s.stocks_autofill = False
    out = asyncio.run(d.first_start())
    assert "error" in out["index"] and "github" in out["index"]["error"] and d.u.is_seed()
    assert not any("financialmodelingprep" in u for u in eng.urls)
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


# --------------------------------------------------------------------------- the whole market (docs/21.4)
class StubPolygon:
    """Grouped daily for any session (or None for `unpublished` ones), and a common-stock list."""
    def __init__(self, tickers, unpublished=(), fail_on=None):
        self.tickers, self.unpublished, self.fail_on = tickers, set(unpublished), fail_on
        self.calls, self.lists = [], 0

    async def grouped(self, day):
        self.calls.append(day)
        if self.fail_on is not None and day == self.fail_on:
            raise RuntimeError("polygon down (url with apiKey=SECRET would be here)")
        if day in self.unpublished:
            return None
        k = (day - day.replace(month=1, day=1)).days
        rows = []
        for i, t in enumerate(self.tickers):
            px = 20.0 + i + 0.01 * k
            vol = 5e6 if not t.startswith("THIN") else 1e3            # THIN* never clears $20M a day
            rows.append({"T": t, "o": px, "h": px * 1.01, "l": px * 0.99, "c": px, "v": vol})
        return rows

    async def common_stocks(self, types="CS"):
        self.lists += 1
        assert "ADRC" in types                                         # ADRs are stocks for this desk
        names = [t for t in self.tickers if not t.startswith("ETF")]
        return [{"ticker": t} for t in names] + [{"ticker": f"PAD{i}"} for i in range(1000)]

    polygon_key = "SECRET"


def _market_desk(tmp_path, tickers, sessions=70, **kw):
    from abg.stocks.desk import StockDesk
    s = DeskSettings(tmp_path)
    s.polygon_api_key, s.stocks_market, s.stocks_market_sessions = "SECRET", True, sessions
    s.stocks_market_max, s.swing_wide_polygon_per_min = 0, 1000
    d = StockDesk(s, NoDataEngine())
    d.market.src = StubPolygon(tickers, **kw)
    return d


def test_market_tier_is_liquid_common_stocks_outside_the_core(tmp_path):
    tickers = ["AAPL", "MSFT", "BIGCO", "MIDCO", "ADR1", "THIN1", "ETFX", "BRK.B"]
    d = _market_desk(tmp_path, tickers, sessions=65)
    out = asyncio.run(d.market_refresh())
    assert out["reloaded"] and out["sync"]["fetched"] >= 60
    tier = set(d.u.market)
    assert {"BIGCO", "MIDCO", "ADR1"} <= tier
    assert "AAPL" not in tier and "MSFT" not in tier                  # already in the core: per-symbol history wins
    assert "THIN1" not in tier                                        # never clears the books' $20M floor
    assert "ETFX" not in tier                                         # not a common stock
    assert "BRK-B" not in tier                                        # core name, after the dot -> dash fix
    assert d.store.frame("BIGCO").shape[0] >= 60                      # bars are where the books read them
    assert set(d.u.daily()) >= tier | set(d.u.core())
    assert "SECRET" not in json.dumps(d.status(), default=str)
    d.store.close()


def test_market_sync_fetches_newest_first_and_resumes(tmp_path):
    d = _market_desk(tmp_path, ["BIGCO", "MIDCO"], sessions=65)
    rep = asyncio.run(d.market.sync(max_calls=5))
    calls = list(d.market.src.calls)                                 # a copy: the stub keeps appending
    assert len(calls) == 5 and calls == sorted(calls, reverse=True)  # recent sessions first
    rep2 = asyncio.run(d.market.sync())
    assert rep2.have == rep2.wanted and set(d.market.src.calls[5:]).isdisjoint(calls)   # carries on, no refetch
    assert asyncio.run(d.market.sync()).fetched == []                 # nothing left to do
    d.store.close()


def test_market_refresh_skips_the_reload_when_nothing_new(tmp_path):
    d = _market_desk(tmp_path, ["BIGCO", "MIDCO"], sessions=65)
    assert asyncio.run(d.market_refresh())["reloaded"]
    again = asyncio.run(d.market_refresh())
    assert again["reloaded"] is False and again["market_tier"] == len(d.u.market)
    assert asyncio.run(d.market_refresh(force=True))["reloaded"]
    d.store.close()


def test_market_sync_failure_never_leaks_the_key(tmp_path):
    from abg.swing.wide import last_session
    d = _market_desk(tmp_path, ["BIGCO"], sessions=65, fail_on=last_session())
    rep = asyncio.run(d.market.sync())
    assert rep.error and "SECRET" not in rep.error and "apiKey" not in rep.error
    d.store.close()


def test_daily_pass_waits_for_polygons_bar_until_the_cutoff(tmp_path):
    from abg.swing.wide import last_session
    d = _market_desk(tmp_path, ["BIGCO", "MIDCO"], sessions=65)
    asyncio.run(d.market_refresh())
    assert d.u.market
    day = last_session()
    at = lambda h, m: datetime(day.year, day.month, day.day, h, m, tzinfo=NY)   # noqa: E731
    assert d._market_ready(at(16, 25))                                # the bar is already stored
    d.market.days.day_path(day).unlink()                              # pretend Polygon hasn't published it
    assert not d._market_ready(at(16, 25)) and not d._market_ready(at(21, 0))
    assert d._market_ready(at(21, 30))                                # give up waiting and run
    d.s.stocks_market = False
    assert d._market_ready(at(16, 25))                                # no market tier: never waits
    d.store.close()


def test_market_tier_off_without_a_polygon_key(tmp_path):
    from abg.stocks.desk import StockDesk
    d = StockDesk(DeskSettings(tmp_path), NoDataEngine())
    assert not d.market.enabled
    assert "skipped" in asyncio.run(d.market_refresh())
    assert d.u.daily() == d.u.core()
    d.store.close()


def test_market_tier_names_reach_the_books(tmp_path):
    d = _desk(tmp_path, n_stocks=24, days=1400)
    d.u.market = d.u.stocks[12:]                       # half the names now come from the market tier
    d.u.stocks = d.u.stocks[:12]
    d.s.stocks_market_replay_years = 2
    d.s.stocks_replay_years = 5
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ls = d._signals()
    cols = set(ls.panel.close.columns)
    assert set(d.u.market) <= cols and set(d.u.stocks) <= cols
    # thousands of names: the panel stops at the replay window plus warm-up, not the full history
    assert ls.panel.dates[0] > pd.Timestamp(d.store.stats()["first_day"])
    assert ls.panel.dates[0] >= ls.panel.dates[-1] - pd.DateOffset(years=2) - pd.Timedelta(days=430)
    cands = ls.candidates()
    single = [c for c in cands if c.book in ("xs_momentum", "trend_pullback", "mean_reversion")]
    assert single and {c.symbol for c in single} & set(d.u.market)   # market names can be picked
    d.store.close()
