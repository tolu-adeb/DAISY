"""Stock desk: store, universe, sweep, live signals, thesis and the daily pass.

Everything here runs on a small synthetic panel built in-process - no network, no provider keys, no
Discord. The point is that the whole path from bars to a posted pitch holds together, and that the
numbers in a pitch are the numbers the rule used.
"""
from __future__ import annotations

import asyncio
import json
import warnings

import numpy as np
import pandas as pd
import pytest

from abg.stocks import thesis as T
from abg.stocks.config import build_config, describe, enabled_books
from abg.stocks.live import LiveSignals
from abg.stocks.store import BarStore
from abg.stocks.sweep import Sweeper
from abg.stocks.universe import ETFS, SEED, Universe


# --------------------------------------------------------------------------- synthetic panel
def _series(n: int, seed: int, drift: float = 0.0004, vol: float = 0.011, base: float = 100.0):
    rng = np.random.default_rng(seed)
    r = drift + vol * rng.standard_normal(n)
    close = base * np.exp(np.cumsum(r))
    op = close * np.exp(0.002 * rng.standard_normal(n))
    hi = np.maximum(op, close) * np.exp(np.abs(0.005 * rng.standard_normal(n)))
    lo = np.minimum(op, close) * np.exp(-np.abs(0.005 * rng.standard_normal(n)))
    return op, hi, lo, close


def make_store(tmp_path, n_stocks: int = 24, days: int = 1400) -> tuple[BarStore, Universe]:
    dates = pd.bdate_range("2019-01-02", periods=days)
    st = BarStore(tmp_path / "stocks.sqlite3")
    etfs = ["SPY", "QQQ", "IWM", "EFA", "EEM", "XLK", "XLF", "XLE", "XLV", "XLP",
            "IEF", "TLT", "GLD", "SHY", "HYG", "LQD"]
    stocks = [f"T{i:02d}" for i in range(n_stocks)]
    for k, t in enumerate(etfs + stocks):
        op, hi, lo, close = _series(days, seed=100 + k, base=40 + 7 * k)
        df = pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close,
                           "volume": np.full(days, 8e6)}, index=dates)
        st.upsert(t, df, provider="synthetic")
    u = Universe()
    u.stocks, u.etfs, u.gauges = stocks, etfs, []
    return st, u


def make_cfg(u: Universe, settings=None):
    class S:
        stocks_config = None
        stocks_panel_start = None
        stocks_min_bars = 250
        stocks_books = ""
        def __getattr__(self, k):            # every other override is "not set"
            return None
    cfg = build_config(settings or S(), u)
    cfg["start"] = "2023-01-03"
    for b in ("trend_pullback", "mean_reversion", "xs_momentum"):
        cfg["books"][b]["min_adv_usd"] = 0
        cfg["books"][b]["min_price"] = 0
    return cfg


# --------------------------------------------------------------------------- store
def test_store_roundtrip_and_panel(tmp_path):
    st, u = make_store(tmp_path, n_stocks=6, days=400)
    s = st.stats()
    assert s["symbols"] == 22 and s["bars"] == 22 * 400
    last = pd.bdate_range("2019-01-02", periods=400)[-1].date().isoformat()
    assert st.last_day("SPY") == last and s["last_day"] == last

    df = st.frame("SPY")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 400 and df["close"].notna().all()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        p = st.panel(u.core(), benchmark="SPY", min_bars=250)
    assert len(p.dates) == 400
    assert "SPY" in p.close.columns and p.eligible["SPY"].all()
    st.close()


def test_store_drops_nan_close_and_rejects_bad_frames(tmp_path):
    st = BarStore(tmp_path / "s.sqlite3")
    idx = pd.bdate_range("2024-01-01", periods=4)
    df = pd.DataFrame({"open": [1, 2, 3, 4], "high": [1, 2, 3, 4], "low": [1, 2, 3, 4],
                       "close": [1.0, np.nan, 3.0, 4.0], "volume": [1, 1, 1, 1]}, index=idx)
    assert st.upsert("AAA", df) == 3                       # the NaN close row is dropped
    with pytest.raises(ValueError):
        st.upsert("BBB", df.reset_index(drop=True))        # no DatetimeIndex
    with pytest.raises(ValueError):
        st.upsert("CCC", df.drop(columns=["high"]))        # a required field is missing
    # volume alone may be missing (indexes have none)
    assert st.upsert("^VIX", df.drop(columns=["volume"]).dropna()) == 3
    st.close()


def test_panel_needs_the_benchmark(tmp_path):
    st, u = make_store(tmp_path, n_stocks=2, days=300)
    with pytest.raises(FileNotFoundError, match="benchmark"):
        st.panel(u.core(), benchmark="NOPE", min_bars=250, warn=False)
    st.close()


def test_membership_drives_eligibility(tmp_path):
    st, u = make_store(tmp_path, n_stocks=4, days=400)
    st.set_membership([("T00", "2019-01-02", "2019-06-30"), ("T01", "2019-01-02", None)])
    p = st.panel(u.core(), benchmark="SPY", min_bars=250, warn=False)
    assert not p.eligible["T00"].iloc[-1]        # membership ended
    assert p.eligible["T01"].iloc[-1]
    assert p.eligible["SPY"].iloc[-1]            # an ETF is never an index member: stays eligible
    st.close()


# --------------------------------------------------------------------------- universe
def test_universe_tiers_and_persistence(tmp_path):
    st = BarStore(tmp_path / "s.sqlite3")
    u = Universe()
    assert u.is_seed() and len(u.stocks) == len(set(u.stocks)) and len(u.stocks) > 100
    assert set(ETFS) <= set(u.core()) and set(SEED) <= set(u.core())
    assert "SPY" in u.non_stock() and "AAPL" not in u.non_stock()

    u.save_index(st, "sp500", ["aapl", "MSFT", "msft"] + [f"X{i}" for i in range(30)], source="test")
    again = Universe.load(st)
    assert not again.is_seed() and again.index_name == "sp500"
    assert again.stocks[:2] == ["AAPL", "MSFT"] and len(again.stocks) == 32   # upper-cased, deduped

    with pytest.raises(ValueError, match="refusing"):
        u.save_index(st, "tiny", ["AAPL"], source="test")

    again.save_extra(st, ["ZZZ", "SPY"])          # SPY is already core, so it is not duplicated
    assert Universe.load(st).extra == ["ZZZ"]
    st.close()


# --------------------------------------------------------------------------- sweep
class StubEngine:
    """Serves bars from a dict; counts calls so paging can be asserted."""
    def __init__(self, frames: dict, fail: set | None = None):
        self.frames, self.fail, self.calls = frames, fail or set(), []

    async def history(self, symbol, period="1y", interval="1d", use_cache=False, **kw):
        self.calls.append(symbol)
        if symbol in self.fail:
            raise RuntimeError("provider said no")

        class PH:
            def __init__(self, df):
                self.df, self.adjusted = df, True

        class F:
            def __init__(self, df):
                self.value = PH(df)
                self.provenance = type("P", (), {"provider": "stub"})()
        if symbol not in self.frames:
            raise KeyError(symbol)
        return F(self.frames[symbol])


class SweepSettings:
    stocks_sweep_page = 5
    stocks_sweep_concurrency = 2
    stocks_sweep_period = "6mo"
    stocks_sweep_pause = 0.0
    stocks_sweep_minutes = 30
    stocks_backfill_period = "10y"
    stocks_backfill_concurrency = 2
    stocks_min_bars = 250


def test_sweep_pages_through_the_universe_and_wraps(tmp_path):
    st = BarStore(tmp_path / "s.sqlite3")
    idx = pd.bdate_range("2024-01-01", periods=10)
    frames = {t: pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                              index=idx) for t in [f"S{i}" for i in range(12)]}
    u = Universe()
    u.stocks, u.etfs, u.gauges = list(frames), [], []
    eng = StubEngine(frames)
    sw = Sweeper(SweepSettings(), eng, st, u)

    r1 = asyncio.run(sw.pass_once())
    assert r1.asked == 5 and r1.ok == 5 and r1.cursor == 5 and not r1.wrapped
    r2 = asyncio.run(sw.pass_once())
    assert r2.cursor == 10
    r3 = asyncio.run(sw.pass_once())              # 10, 11, then round to 0, 1, 2
    assert r3.wrapped and r3.cursor == 3
    assert len(set(eng.calls)) == 12               # a full cycle touched everything exactly once
    st.close()


def test_sweep_parks_a_symbol_that_keeps_failing(tmp_path):
    st = BarStore(tmp_path / "s.sqlite3")
    idx = pd.bdate_range("2024-01-01", periods=5)
    good = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    eng = StubEngine({"OK": good}, fail={"BAD"})
    u = Universe()
    u.stocks, u.etfs, u.gauges = ["OK", "BAD"], [], []
    sw = Sweeper(SweepSettings(), eng, st, u)
    for _ in range(7):
        asyncio.run(sw.pass_once())
    cov = st.coverage()
    assert cov["BAD"].delisted and cov["BAD"].errors >= 6
    assert cov["OK"].error is None and not cov["OK"].delisted
    before = len(eng.calls)
    asyncio.run(sw.pass_once())
    assert "BAD" not in eng.calls[before:]         # parked symbols stop costing rate limit
    st.close()


def test_sweep_prefers_the_adjusted_close(tmp_path):
    st = BarStore(tmp_path / "s.sqlite3")
    idx = pd.bdate_range("2024-01-01", periods=6)
    df = pd.DataFrame({"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 1.0,
                       "adj_close": 5.0}, index=idx)
    u = Universe()
    u.stocks, u.etfs, u.gauges = ["AAA"], [], []
    sw = Sweeper(SweepSettings(), StubEngine({"AAA": df}), st, u)
    asyncio.run(sw.pass_once())
    got = st.frame("AAA")
    assert got["close"].iloc[0] == pytest.approx(5.0)       # adjusted close wins
    assert got["high"].iloc[0] == pytest.approx(5.5)        # and OHL are scaled by the same ratio
    st.close()


# --------------------------------------------------------------------------- live signals
def test_live_signals_produce_ranked_candidates(tmp_path):
    st, u = make_store(tmp_path)
    cfg = make_cfg(u)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ls = LiveSignals(cfg, st, u).build(warn=False)
        ls.run()
    cands, state = ls.candidates(), ls.state()

    assert state.date == pd.bdate_range("2019-01-02", periods=1400)[-1].date().isoformat()
    assert state.regime in ("bull_trend", "bull_chop", "risk_off", "crisis", "warmup")
    assert set(state.weights) <= set(enabled_books(cfg))
    assert abs(sum(state.weights.values()) - 1.0) < 1e-6

    for c in cands:
        assert c.book in enabled_books(cfg)
        assert c.action in ("BUY", "SELL", "REBALANCE")
        assert c.symbol in st.symbols()
        if c.is_entry:
            assert 1 <= c.rank <= c.of
    # ranks inside a book follow that book's own score, best first
    for book in {c.book for c in cands if c.is_entry}:
        e = sorted([c for c in cands if c.book == book and c.is_entry], key=lambda c: c.rank)
        scores = [c.score for c in e if c.score is not None]
        assert scores == sorted(scores, reverse=True)
    st.close()


def test_pullback_gates_match_the_indicator_values(tmp_path):
    """The gate list is what a pitch shows, so it has to agree with the frames the book read."""
    st, u = make_store(tmp_path)
    cfg = make_cfg(u)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ls = LiveSignals(cfg, st, u).build(warn=False)
        ls.run()
    for c in ls.candidates():
        if c.book != "trend_pullback" or not c.is_entry:
            continue
        f = c.facts
        assert f["close"] > f["ema200"]
        assert f["ema50"] > f["ema200"]
        assert f["min_rsi14_5d"] <= cfg["books"]["trend_pullback"]["rsi_pullback"]
        assert f["close"] > f["prev_high"]
        assert f["macd_hist"] > f["macd_hist_prev"]
        assert all(c.gates.values()), f"a gate is false on a taken trade: {c.gates}"
    st.close()


def test_etfs_never_enter_a_single_stock_book(tmp_path):
    st, u = make_store(tmp_path)
    cfg = make_cfg(u)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ls = LiveSignals(cfg, st, u).build(warn=False)
        ls.run()
    etfs = set(u.etfs)
    for c in ls.candidates():
        if c.book in ("xs_momentum", "trend_pullback", "mean_reversion"):
            assert c.symbol not in etfs, f"{c.symbol} is an ETF but {c.book} picked it"
    st.close()


# --------------------------------------------------------------------------- thesis
def test_thesis_answers_the_four_questions(tmp_path):
    st, u = make_store(tmp_path)
    cfg = make_cfg(u)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ls = LiveSignals(cfg, st, u).build(warn=False)
        ls.run()
    cands, state = ls.candidates(), ls.state()
    # a single-stock entry: Book A trades ETFs, which get no valuation
    entry = next(c for c in cands if c.is_entry and c.book != "dual_momentum")
    fund = {"name": "Test Co", "sector": "Technology", "eps": 5.0, "forward_pe": 18.0, "beta": 1.1}
    peers = [{"symbol": "P1", "pe": 20.0, "forward_pe": 19.0},
             {"pe": 24.0, "forward_pe": 22.0}]          # no symbol: must not crash the comps
    pack = T.build_pack(entry, state, cfg, fund=fund, peers=peers)
    md = T.render_md(pack)

    for heading in ("## The thesis", "## Market context", "## The trade",
                    "## What would make this wrong"):
        assert heading in md
    assert entry.symbol in md
    assert "Research, not advice." in md
    assert "Paper only" in md
    assert pack["valuation"] and pack["valuation"]["fair_value"]
    assert "Valuation cross-check" in md
    assert pack["exits"] and pack["risks"]

    emb = T.embed(pack)
    assert emb["title"].startswith(entry.action)
    assert len(emb["description"]) <= 4000
    for f in emb["fields"]:
        assert 0 < len(f["value"]) <= 1024
        assert len(f["name"]) <= 256
    assert len(emb["footer"]["text"]) <= 2048

    dig = T.digest_embed([T.build_pack(c, state, cfg) for c in cands], state, cfg)
    assert dig["title"] and len(dig["fields"]) <= 5
    for f in dig["fields"]:
        assert 0 < len(f["value"]) <= 1024
    st.close()


def test_thesis_survives_missing_fundamentals(tmp_path):
    st, u = make_store(tmp_path)
    cfg = make_cfg(u)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ls = LiveSignals(cfg, st, u).build(warn=False)
        ls.run()
    cands, state = ls.candidates(), ls.state()
    for c in cands:
        pack = T.build_pack(c, state, cfg, fund=None)
        assert pack["valuation"] is None
        md = T.render_md(pack)
        assert "Valuation cross-check" not in md
        assert c.symbol in md
        T.embed(pack)                 # must not raise
    st.close()


def test_no_secret_reaches_a_pack(tmp_path):
    st, u = make_store(tmp_path)
    cfg = make_cfg(u)
    cfg["__secret_probe__"] = "https://discord.com/api/webhooks/123/abcdef"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ls = LiveSignals(cfg, st, u).build(warn=False)
        ls.run()
    cands, state = ls.candidates(), ls.state()
    blob = json.dumps([T.build_pack(c, state, cfg) for c in cands], default=str)
    assert "discord.com/api/webhooks" not in blob
    assert "abcdef" not in blob
    st.close()


# --------------------------------------------------------------------------- config
def test_config_wires_the_universe_and_overrides(tmp_path):
    st, u = make_store(tmp_path, n_stocks=4, days=300)

    class S:
        stocks_config = None
        stocks_panel_start = "2020-01-01"
        stocks_min_bars = 300
        stocks_books = "c,d"
        stocks_vol_target = 0.09
        stocks_dd_kill = 0.15
        def __getattr__(self, k):
            return None

    cfg = build_config(S(), u)
    assert enabled_books(cfg) == ["trend_pullback", "mean_reversion"]
    assert cfg["allocation"]["vol_target"] == 0.09
    assert cfg["risk"]["dd_kill"] == 0.15
    assert cfg["panel_start"] == "2020-01-01" and cfg["min_bars"] == 300
    # Book A rotates only ETFs the desk actually follows, and never a single stock
    assert set(cfg["books"]["dual_momentum"]["universe"]) <= set(u.etfs)
    assert not set(cfg["books"]["dual_momentum"]["universe"]) & set(u.stocks)
    assert "SPY" in cfg["non_stock_tickers"]
    d = describe(cfg)
    assert d["books"] == ["trend_pullback", "mean_reversion"] and d["vol_target"] == 0.09
    st.close()


def test_unknown_book_name_is_ignored_not_fatal(tmp_path):
    st, u = make_store(tmp_path, n_stocks=2, days=300)

    class S:
        stocks_config = None
        stocks_books = "c,not_a_book"
        stocks_panel_start = None
        stocks_min_bars = 250
        def __getattr__(self, k):
            return None

    cfg = build_config(S(), u)
    assert enabled_books(cfg) == ["trend_pullback"]
    st.close()


# --------------------------------------------------------------------------- the desk
class DeskSettings:
    """Enough of Settings for the desk, with no network and no webhook."""
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.stocks_enabled = True
        self.stocks_tag = "PAPER"
        self.stocks_config = None
        self.stocks_panel_start = None
        self.stocks_min_bars = 250
        self.stocks_books = ""
        self.stocks_daily_time = "16:20"
        self.stocks_post_max = 5
        self.stocks_autofill = False
        self.stocks_webhook_url = None
        self.pitch_webhook_url = None
        self.stocks_sweep_page = 5
        self.stocks_sweep_minutes = 30
        self.stocks_sweep_concurrency = 2
        self.stocks_sweep_period = "6mo"
        self.stocks_sweep_pause = 0.0
        self.stocks_min_bars = 250

    def __getattr__(self, k):
        return None


class NoDataEngine:
    """Fundamentals and news always fail, which is the common case on free tiers."""
    class _HTTP:
        async def request(self, *a, **kw):
            raise AssertionError("the desk must not post when no webhook is configured")
    http = _HTTP()

    async def fundamentals(self, symbol):
        raise RuntimeError("no fundamentals provider")

    async def news(self, symbol, limit=4):
        raise RuntimeError("no news provider")

    async def history(self, *a, **kw):
        raise RuntimeError("no history provider")


def _desk(tmp_path, n_stocks=24, days=1400):
    from abg.stocks.desk import StockDesk
    st, u = make_store(tmp_path, n_stocks=n_stocks, days=days)
    st.close()
    s = DeskSettings(tmp_path)
    d = StockDesk(s, NoDataEngine())
    d.u.stocks, d.u.etfs, d.u.gauges = u.stocks, u.etfs, []
    d.cfg = make_cfg(d.u)
    return d


def test_daily_pass_writes_ledger_memos_and_pitches(tmp_path):
    d = _desk(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = asyncio.run(d.daily(post=True))     # no webhook -> "dashboard", never a request

    assert out["posted"] == "dashboard"
    assert out["candidates"] == out["opens"] + out["closes"]
    assert out["regime"] in ("bull_trend", "bull_chop", "risk_off", "crisis", "warmup")

    ledger = d.ledger_tail(500)
    assert len(ledger) == out["candidates"]       # every candidate recorded, taken or not
    row = ledger[0]
    for k in ("as_of", "symbol", "book", "action", "regime", "stress", "gates_passed", "gates_total"):
        assert k in row
    assert any(k.startswith("f_") for k in row)   # the indicator values ride along for a ranker

    today = d.today()
    assert today["state"] and len(today["candidates"]) == out["candidates"]

    pitches = d.pitches()
    assert len(pitches) == out["opens"]
    for p in pitches:
        assert p["price"] and p["symbol"] and p["book"]

    opens = [c for c in today["candidates"] if c["action"] in ("BUY", "REBALANCE")]
    for c in opens:
        if c["book"] == "dual_momentum":
            continue
        md = d.memo(c["id"])
        assert md and "## The thesis" in md and c["symbol"] in md
    d.store.close()


def test_daily_pass_is_idempotent_for_one_session(tmp_path):
    d = _desk(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        a = asyncio.run(d.daily(post=False))
        b = asyncio.run(d.daily(post=False))
    assert a["as_of"] == b["as_of"]
    # the pitch record keys on (session, book, symbol), so a re-run does not duplicate it
    assert len(d.pitches()) == a["opens"]
    d.store.close()


def test_scorecard_marks_pitches_only_after_a_session_has_passed(tmp_path):
    d = _desk(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        asyncio.run(d.daily(post=False))
    n = len(d.pitches())
    sc = d.scorecard()
    # signals come from the last close: nothing has happened to them yet, so nothing is "marked"
    assert sc["all"]["n"] == n and sc["all"]["marked"] == 0 and sc["all"]["beat_spy"] is None

    # one more session: every pitched name and the benchmark get a bar a percent higher / flat
    last = d.store.frame("SPY").index[-1]
    nxt = last + pd.offsets.BDay(1)
    for sym in {p["symbol"] for p in d.pitches()} | {"SPY"}:
        f = d.store.frame(sym)
        c = float(f["close"].iloc[-1]) * (1.0 if sym == "SPY" else 1.01)
        d.store.upsert(sym, pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1e6}, index=[nxt]))
    sc = d.scorecard()
    assert sc["all"]["marked"] == n
    for r in sc["pitches"]:
        assert r["ret"] == pytest.approx(0.01, abs=1e-4) and r["spy_ret"] == pytest.approx(0.0, abs=1e-9)
        assert r["excess"] == pytest.approx(r["ret"] - r["spy_ret"], abs=1e-6)
    assert sc["all"]["beat_spy"] == 1.0
    assert set(sc["by_book"]) <= set(enabled_books(d.cfg))
    d.store.close()


def test_status_reports_no_secrets(tmp_path):
    d = _desk(tmp_path, n_stocks=4, days=300)
    d.s.stocks_webhook_url = "https://discord.com/api/webhooks/999/supersecret"
    blob = json.dumps(d.status(), default=str)
    assert d.status()["webhook"] is True          # only that one exists
    assert "supersecret" not in blob and "discord.com" not in blob
    d.store.close()


def test_schedule_fires_once_per_weekday_after_the_close(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    d = _desk(tmp_path, n_stocks=4, days=300)
    assert not d._daily_due(datetime(2026, 11, 9, 15, 0, tzinfo=ny))    # Monday, before the close
    assert d._daily_due(datetime(2026, 11, 9, 16, 25, tzinfo=ny))       # Monday, after it
    assert not d._daily_due(datetime(2026, 11, 7, 18, 0, tzinfo=ny))    # Saturday
    d._mark_daily_run(datetime(2026, 11, 9, 16, 25, tzinfo=ny))
    d._save("state.json", {**d._load("state.json", {}), "last_daily_day": "2026-11-09"})
    assert not d._daily_due(datetime(2026, 11, 9, 16, 40, tzinfo=ny))   # already ran today
    assert d._daily_due(datetime(2026, 11, 10, 16, 25, tzinfo=ny))      # next weekday
    d.store.close()
