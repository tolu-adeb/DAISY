"""The stock-pitching desk (docs/22.1): ratings against our own open pitches, sizes as a % of the fund,
fit against the fund's real holdings, the pitch lifecycle and the track record. No network, no keys."""
from __future__ import annotations

import asyncio
import json
import warnings

import pandas as pd
import pytest

from abg.stocks import pitch_plan as P
from abg.stocks import pitchbook as PB
from abg.stocks import rank as RK
from abg.stocks import thesis as T
from abg.stocks.deskdb import DeskDB

from test_pitch_plan import cand
from test_stocks import _desk

PC = P.pitch_cfg({})


def bars(rows, start="2026-11-09"):
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1e6)


ROW = {"id": "2026-11-06-trend_pullback-TST", "symbol": "TST", "book": "trend_pullback", "as_of": "2026-11-06",
       "plan": {"stop": 96.0, "t1": 104.8, "t2": 108.4, "max_hold": 5}}


# --------------------------------------------------------------------------- ratings
@pytest.mark.parametrize("action,shares,same,other,want", [
    ("BUY", 10, False, False, "BUY"), ("BUY", 10, False, True, "ADD"), ("BUY", 10, True, False, "HOLD"),
    ("SELL", -10, True, False, "SELL"), ("SELL", -10, True, True, "TRIM"),
    ("REBALANCE", 5, False, False, "BUY"), ("REBALANCE", 5, True, False, "HOLD"),
    ("REBALANCE", 0, True, False, "SELL"), ("REBALANCE", None, False, False, "HOLD"),
])
def test_rating_is_decided_against_our_open_pitches(action, shares, same, other, want):
    assert P.rating(action, shares, open_same=same, open_other=other) == want


def test_daisy_rank_mapping_is_the_documented_one():
    assert [RK.daisy_rank("BUY", b) for b in ("High", "Medium", "Low")] == [1, 2, 3]
    assert RK.daisy_rank("HOLD") == 3 and RK.daisy_rank("TRIM") == 4 and RK.daisy_rank("SELL") == 5
    assert RK.daisy_rank(None, steps_away=1) == 3 and RK.daisy_rank(None, steps_away=3) == 4


# --------------------------------------------------------------------------- size and fit
def test_size_is_percent_only_without_a_fund_size_and_matches_the_share_count_with_one():
    c = cand()                                                    # entry 100, stop 96
    pct = P.risk_and_size(c, 100.0, 96.0, 106.0, None, "High", PC)
    assert pct["shares"] is None and pct["notional"] is None and pct["risk_dollars"] is None and pct["equity"] is None
    assert pct["pct_of_fund"] == pytest.approx(min(0.005 / 0.04, PC["max_position_pct"]), abs=1e-4)
    assert pct["risk_pct_of_fund"] == pytest.approx(pct["pct_of_fund"] * 0.04, abs=1e-5)
    usd = P.risk_and_size(c, 100.0, 96.0, 106.0, 1_000_000, "High", PC)
    assert usd["shares"] > 0 and usd["pct_of_fund"] == pytest.approx(pct["pct_of_fund"], abs=1e-3)
    mom = P.risk_and_size(cand("xs_momentum", stop=None), 100.0, None, None, None, "Medium", PC, rule_weight=0.04)
    assert mom["pct_of_fund"] == 0.04 and mom["bound"] == "book weight" and mom["shares"] is None


class _Store:
    def __init__(self, frames):
        self.f = frames

    def frame(self, t):
        return self.f.get(t, pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))


def test_fund_fit_uses_the_real_holdings_or_says_they_are_missing():
    st = _Store({})
    none = P.fund_fit("NVDA", "Technology", st, None, {}, 0.05, PC)
    assert none["holdings_provided"] is False and none["already_held"] is None and none["sector_before"] is None
    fit = P.fund_fit("NVDA", "Technology", st, {"MSFT": 0.10, "XOM": 0.05, "NVDA": 0.02},
                     {"MSFT": "Technology", "XOM": "Energy", "NVDA": "Technology"}, 0.05, PC)
    assert fit["holdings_provided"] and fit["already_held"] is True
    assert fit["sector_before"] == pytest.approx(0.12) and fit["sector_after"] == pytest.approx(0.17)
    assert "paper" not in json.dumps(fit).lower()


# --------------------------------------------------------------------------- lifecycle
def test_a_pitch_is_new_then_active_then_closed_by_the_earliest_event():
    assert PB.resolve(ROW, bars([])[:0])["status"] == "new"
    px = bars([(100, 101, 99, 100)])
    r = PB.resolve(ROW, px, P.mark_plan(ROW["plan"], ROW["as_of"], px))
    assert r["status"] == "active" and r["entry_open"] == 100 and r["entry_date"] == "2026-11-09"

    px = bars([(100, 101, 99, 100), (100, 100.5, 95.5, 96.5)])               # day 2 touches the stop
    r = PB.resolve(ROW, px, P.mark_plan(ROW["plan"], ROW["as_of"], px))
    assert (r["status"], r["reason"], r["closed_at"], r["close_price"]) == ("closed", "stop", "2026-11-10", 96.0)

    px = bars([(100, 105, 99.5, 104.9), (105, 109, 104, 108.6)])             # T1 then T2
    r = PB.resolve(ROW, px, P.mark_plan(ROW["plan"], ROW["as_of"], px))
    assert (r["reason"], r["close_price"]) == ("target", 108.4)

    px = bars([(100, 101, 99, 100.5)] * 5)                                   # five quiet sessions
    r = PB.resolve(ROW, px, P.mark_plan(ROW["plan"], ROW["as_of"], px))
    assert (r["reason"], r["closed_at"], r["close_price"]) == ("time", "2026-11-13", 100.5)


def test_a_rule_exit_closes_the_pitch_at_the_next_open():
    px = bars([(100, 101, 99, 100), (100, 101, 99, 100.5), (101.5, 102, 100, 101)])
    row = {**ROW, "exit_signal": {"as_of": "2026-11-10", "price": 100.5, "reason": "trend_break"}}
    r = PB.resolve(row, px, P.mark_plan(row["plan"], row["as_of"], px))
    assert (r["status"], r["reason"], r["closed_at"], r["close_price"]) == ("closed", "rule exit", "2026-11-11", 101.5)
    r2 = PB.resolve(row, px.iloc[:2], P.mark_plan(row["plan"], row["as_of"], px.iloc[:2]))
    assert r2["close_price"] == 100.5 and "tested exit is the next open" in r2["close_basis"]


def test_track_measures_from_the_tested_entry_against_spy_and_the_sector():
    px = bars([(100, 103, 99.5, 102), (102, 105, 101, 104)])
    spy = bars([(500, 502, 499, 501), (501, 506, 500, 505)])
    xlk = bars([(200, 201, 199, 200), (200, 203, 199, 202)])
    res = PB.resolve(ROW, px, P.mark_plan(ROW["plan"], ROW["as_of"], px))
    t = PB.track(ROW, res, px, spy, xlk, "XLK")
    assert t["ret"] == pytest.approx(0.04) and t["spy"] == pytest.approx(0.01) and t["sector"] == pytest.approx(0.01)
    assert t["excess_spy"] == pytest.approx(0.03) and t["r"] == pytest.approx(1.0) and t["open"] is True
    assert PB.sector_etf("Information Technology") == "XLK" and PB.sector_etf(None) is None


def test_pitch_book_keeps_history_and_the_track_record_cuts(tmp_path):
    db = DeskDB(tmp_path / "desk.sqlite3")
    pb = PB.PitchBook(db)
    assert pb.add({**ROW, "rating": "BUY", "drank": 1, "sector": "Technology"})
    assert not pb.add({**ROW})                                                 # one pitch per session/book/name
    row = pb.get(ROW["id"])
    pb.set_status(row, "active", "entered")
    assert pb.exit_signal("TST", "trend_pullback", "2026-11-10", 99.0, "trend_break")["exit_signal"]["price"] == 99.0
    row = pb.get(ROW["id"])
    row.update({"track": {"ret": 0.02, "excess_spy": 0.01, "excess_sector": -0.01, "r": 0.5},
                "closed_at": "2026-11-11", "close_reason": "rule exit"})
    pb.set_status(row, "closed", "rule exit")
    assert [h["status"] for h in pb.history(ROW["id"])] == ["new", "active", "active", "closed"]
    assert pb.counts("2026-11-11") == {"new": 0, "active": 0, "closed": 1, "closed_today": 1}
    tr = PB.track_record(pb.rows())
    assert tr["all"]["closed"] == 1 and tr["all"]["beat_spy"] == 1.0 and tr["all"]["closed_by"] == {"rule exit": 1}
    assert set(tr["by_rank"]) == {"1"} and set(tr["by_sector"]) == {"Technology"} and set(tr["by_book"]) == {"trend_pullback"}
    db.close()


def test_pitches_json_is_carried_into_the_pitch_book_once(tmp_path):
    old = [{"id": "2026-10-01-trend_pullback-AAA", "as_of": "2026-10-01", "symbol": "AAA", "book": "trend_pullback",
            "action": "BUY", "price": 50.0, "ret": 0.03, "spy_ret": 0.01, "excess": 0.02, "plan": None},
           {"as_of": "2026-10-02", "symbol": "BBB", "book": "mean_reversion", "action": "BUY", "price": 20.0,
            "plan": {"rating": "ADD", "stop": 19.0}}]
    (tmp_path / "stocks").mkdir()
    (tmp_path / "stocks" / "pitches.json").write_text(json.dumps(old), encoding="utf-8")
    d = _desk(tmp_path)
    rows = d.pitches()
    assert [(r["symbol"], r["excess"] if "excess" in r else None) for r in rows] == [("AAA", 0.02), ("BBB", None)]
    assert rows[1]["id"] == "2026-10-02-mean_reversion-BBB" and rows[1]["rating"] == "ADD"
    assert d.book.history(rows[0]["id"])[0]["note"] == "carried over from pitches.json"
    assert d.db.kv_get("pitches_json_migrated") == {"rows_in_file": 2, "carried_over": 2}
    assert (tmp_path / "stocks" / "pitches.json").exists()                     # the old file is left in place
    d._migrate_pitches()
    assert len(d.pitches()) == 2                                               # and it only happens once
    d.store.close()


# --------------------------------------------------------------------------- the desk shows no paper book
def test_nothing_the_desk_shows_carries_the_simulated_book(tmp_path):
    d = _desk(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = asyncio.run(d.daily(post=False))
    today = d.today()
    st = today["state"]
    assert all(st[k] is None for k in ("equity", "drawdown", "gross", "budgets", "positions", "bench_vs"))
    assert today["pitch_book"]["new"] == out["opens"] > 0
    for c in today["candidates"]:
        assert all(c[k] is None for k in ("shares", "notional", "weight", "risk_pct", "r_dollars"))
        assert c["context"]["equity"] is None and c["context"]["drawdown"] is None
        risk = (c.get("plan") or {}).get("risk") or {}
        assert risk.get("shares") is None and risk.get("equity") is None      # no fund size set: percent only
    packs = [json.loads(p.read_text()) for p in (tmp_path / "stocks" / "memos").glob("*.json")]
    from abg.stocks.live import LiveSignals
    ls = LiveSignals(d.cfg, d.store, d.u).build(warn=False)
    ls.run()
    digest = T.digest_embed(packs, ls.state(), d.cfg, book=d.book.counts())
    text = json.dumps(digest).lower()
    assert "pitch book" in text and "equity" not in text and "paper" not in text and "position(s) open" not in text
    for p in (tmp_path / "stocks" / "memos").glob("*.md"):
        md = p.read_text(encoding="utf-8").lower()
        assert "paper" not in md
        assert "of the fund" in md or not md.startswith(("# buy", "# add"))
    assert d.status()["pitch_book"]["new"] == out["opens"]
    d.store.close()


def test_the_next_session_holds_an_open_pitch_instead_of_pitching_it_again(tmp_path):
    d = _desk(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        asyncio.run(d.daily(post=False))
        first = d.pitches()
        assert first
        r = next(x for x in first if not (x.get("plan") or {}).get("stop"))    # no stop: stays open
        r["as_of"] = d.store.frame("SPY").index[-3].date().isoformat()         # the same idea, pitched earlier
        d.book.save(r)
        asyncio.run(d.daily(post=False))
    mine = [c for c in d.today()["candidates"] if c["symbol"] == r["symbol"] and c["book"] == r["book"]]
    assert mine and mine[0]["rec"]["rating"] == "HOLD" and mine[0]["rec"]["open_pitch"] == r["id"]
    assert mine[0]["rec"]["daisy_rank"] == 3
    d.store.close()


# --------------------------------------------------------------------------- the fund's recorded portfolio (docs/22.12)
def test_recorded_portfolio_is_validated_marked_and_logged(tmp_path):
    from abg.stocks import portfolio as PF
    db = DeskDB(tmp_path / "desk.sqlite3")
    pf = PF.Portfolio(db)
    a = pf.add({"symbol": " nvda ", "shares": "10", "cost": "100", "opened": "2026-10-01", "note": "  first   buy "})
    b = pf.add({"symbol": "MSFT", "shares": 5, "cost": 400, "opened": "2026-10-15", "pitch_id": "2026-10-14-trend_pullback-MSFT"})
    assert a["symbol"] == "NVDA" and a["note"] == "first buy" and a["status"] == "open"
    for bad in ({"symbol": "bad sym", "shares": 1, "cost": 1}, {"symbol": "X", "shares": 0, "cost": 1}, {"symbol": "X", "shares": 1, "cost": "abc"},
                {"symbol": "X", "shares": 1, "cost": 1, "opened": "yesterday"}, {"symbol": "X", "shares": 1, "cost": 1, "pitch_id": "a b"}):
        with pytest.raises(ValueError):
            pf.add(bad)
    pf.set_cash(1000)
    last = {"NVDA": (120.0, "2026-11-10"), "MSFT": (380.0, "2026-11-10")}
    v = PF.value(pf.rows(), lambda s: last.get(s), lambda day: 0.05, pf.cash())
    t = v["totals"]
    assert t["invested"] == 3100.0 and t["total"] == 4100.0 and t["gain"] == 100.0 and t["gain_pct"] == pytest.approx(100 / 3000, abs=1e-5)
    nv = next(r for r in v["open"] if r["symbol"] == "NVDA")
    assert nv["gain_pct"] == pytest.approx(0.2) and nv["excess"] == pytest.approx(0.15) and nv["weight"] == pytest.approx(1200 / 4100, abs=1e-5)
    assert PF.weights(v) == {"MSFT": pytest.approx(1900 / 4100, abs=1e-5), "NVDA": pytest.approx(1200 / 4100, abs=1e-5)}
    pf.update(a["id"], {"shares": 12})
    assert pf.get(a["id"])["shares"] == 12
    pf.close(b["id"], 390, "2026-11-05")
    v = PF.value(pf.rows(), lambda s: last.get(s), None, pf.cash())
    assert [r["symbol"] for r in v["open"]] == ["NVDA"] and v["closed"][0]["gain"] == -50.0 and v["totals"]["realized"] == -50.0
    unpriced = PF.value([{**pf.get(a["id"])}], lambda s: None)
    assert unpriced["open"][0]["value"] is None and unpriced["totals"]["gain"] is None and unpriced["totals"]["unmarked"] == 1
    pf.reopen(b["id"])
    assert pf.get(b["id"])["status"] == "open" and pf.delete(b["id"]) and pf.get(b["id"]) is None
    with pytest.raises(KeyError):
        pf.close(999, 1)
    notes = " | ".join(h["note"] for h in pf.log())
    assert "logged 10 NVDA at $100.00" in notes and "sold at $390.00" in notes and "removed the MSFT entry" in notes and "cash set" in notes
    db.close()


def test_portfolio_feeds_fund_fit_and_only_an_admin_can_change_it(tmp_path, monkeypatch):
    d = _desk(tmp_path)
    assert d.fund_holdings() is None
    sym = d.u.stocks[0]
    d.portfolio.add({"symbol": sym, "shares": 10, "cost": 50, "opened": "2024-01-02"})
    d.portfolio.set_cash(0)
    view = d.portfolio_view()
    assert view["open"][0]["last"] and view["open"][0]["spy_pct"] is not None and view["totals"]["positions"] == 1
    assert d.fund_holdings() == {sym: 1.0}                                      # the recorded portfolio is what fit compares against
    for k, v in (("ABG_ALLOW_SYNTHETIC", "true"), ("ABG_PROVIDER_ORDER", "synthetic"), ("ABG_CACHE_DIR", str(tmp_path / "c")),
                 ("ABG_AI_ENABLED", "false"), ("ABG_DATA_DIR", str(tmp_path / "api")), ("ABG_MONITOR_ON_SERVE", "false")):
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient
    from abg.api.server import app
    with TestClient(app) as c:
        app.state.stock_desk = d
        try:
            got = c.get("/api/stocks/portfolio").json()
            assert got["totals"]["positions"] == 1 and got["log"]
            hid = c.post("/api/stocks/portfolio", json={"symbol": "SPY", "shares": 2, "cost": 400, "opened": "2024-02-01"}).json()["row"]["id"]
            assert c.post("/api/stocks/portfolio", json={"symbol": "SPY", "shares": -1, "cost": 400}).status_code == 400
            assert c.post(f"/api/stocks/portfolio/{hid}", json={"action": "close", "exit_price": 420}).json()["row"]["status"] == "closed"
            assert c.post("/api/stocks/portfolio/9999", json={"action": "delete"}).status_code == 404
            assert c.post("/api/stocks/portfolio", json={"cash": 2500}).json() == {"cash": 2500.0}
            assert c.get("/api/stocks/portfolio").json()["totals"]["cash"] == 2500.0
            assert "#/portfolio" in c.get("/").text
        finally:
            app.state.stock_desk = None
    from abg.ops import auth
    from types import SimpleNamespace

    class Req:
        def __init__(self, method, path, token):
            self.method, self.headers, self.cookies, self.url = method, {"authorization": f"Bearer {token}"}, {}, SimpleNamespace(path=path)
    mw = auth.middleware(lambda r: SimpleNamespace(admin_token="adm", view_token="view"))

    async def ok(_):
        return "passed"
    assert asyncio.run(mw(Req("GET", "/api/stocks/portfolio", "view"), ok)) == "passed"        # viewers can read it
    assert asyncio.run(mw(Req("POST", "/api/stocks/portfolio", "view"), ok)).status_code == 403   # and cannot change it
    assert asyncio.run(mw(Req("POST", "/api/stocks/portfolio/1", "view"), ok)).status_code == 403
    assert asyncio.run(mw(Req("POST", "/api/stocks/portfolio", "adm"), ok)) == "passed"
    d.close()
