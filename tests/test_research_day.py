"""The research day (docs/22.2-22.6): whole-market screen and eligibility, news and sentiment, theme
shocks, the morning watchlist, live alerts, Explore, notes and the weekly recap. Fake providers only: no
network, no keys, no Discord, no sleeps."""
from __future__ import annotations

import asyncio
import json
import warnings
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from abg.stocks import alerts as AL
from abg.stocks import news as N
from abg.stocks import pitch_plan as P
from abg.stocks import rank as RK
from abg.stocks import screen as SC
from abg.stocks import watchlist as WL
from abg.stocks.deskdb import DeskDB

from test_stocks import _desk, make_cfg
from test_stocks_rd import _market_desk

NY = ZoneInfo("America/New_York")
CFG = {"books": {"trend_pullback": {"rsi_pullback": 40, "stop_atr": 2.0, "max_hold": 30},
                 "mean_reversion": {"rsi_entry": 10, "rsi_period": 2, "max_hold": 7},
                 "xs_momentum": {"top_n": 2, "min_score": 0.0}}}
TODAY = date(2026, 11, 10)                                   # a Tuesday
NOW = datetime(2026, 11, 10, 10, 30, tzinfo=NY)


# --------------------------------------------------------------------------- the screen
def _panel(n=320, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-02", periods=n)
    cols = {"UP": 0.0015, "FLAT": 0.0, "DOWN": -0.001, "PENNY": 0.0005, "THIN": 0.001}
    close = pd.DataFrame({k: 50 * np.exp(np.cumsum(rng.normal(mu, 0.012, n))) for k, mu in cols.items()}, index=idx)
    close["PENNY"] = close["PENNY"] / 25
    close["IPO"] = np.nan
    close.loc[idx[-30:], "IPO"] = np.linspace(40, 55, 30)
    vol = pd.DataFrame(2e6, index=idx, columns=close.columns)
    vol["THIN"] = 1e3
    return SimpleNamespace(open=close * 0.999, high=close * 1.01, low=close * 0.99, close=close, volume=vol)


def test_screen_covers_every_name_and_says_why_one_is_not_eligible():
    df = SC.build(_panel(), CFG, names={"UP": "Up Corp"}, non_stock=set())
    assert set(df.index) == {"UP", "FLAT", "DOWN", "PENNY", "THIN", "IPO"}      # coverage: all of them
    assert bool(df.loc["UP", "eligible"]) and df.loc["UP", "why_not"] is None
    assert not df.loc["PENNY", "eligible"] and "price under $5" in df.loc["PENNY", "why_not"]
    assert not df.loc["THIN", "eligible"] and "trades under $20M" in df.loc["THIN", "why_not"]
    assert not df.loc["IPO", "eligible"] and bool(df.loc["IPO", "too_new"]) and df.loc["IPO", "sessions"] == 30
    assert "30 sessions" in df.loc["IPO", "why_not"]
    assert df.loc["UP", "mom_pct"] > df.loc["DOWN", "mom_pct"] and np.isnan(df.loc["PENNY", "mom_pct"])
    b = SC.breadth(df)
    assert b["n"] == int(df["eligible"].sum()) and 0 <= b["above_200"] <= 1
    rows = SC.rows(df)
    json.dumps(rows)                                                            # plain floats, no NaN objects
    assert all(isinstance(d["eligible"], bool) for _, _, d in rows)


def test_eligibility_floor_is_a_setting():
    cfg = {**CFG, "pitch": {"eligibility": {"min_price": 1.0, "min_adv_usd": 1e6}}}
    df = SC.build(_panel(), cfg, non_stock=set())
    assert bool(df.loc["PENNY", "eligible"])


def test_setups_name_the_exact_condition_still_needed():
    r = {"eligible": True, "too_new": False, "sessions": 400, "close": 100.0, "high": 101.0, "ema50": 98.0, "ema200": 90.0,
         "sma200": 91.0, "rsi14": 43.0, "min_rsi14_4": 44.0, "macd_hist": 0.2, "macd_hist_prev": 0.1, "atr14": 2.0,
         "rsi2": 60.0, "mom_rank": 9.0, "mom_score": 1.0}
    ss = SC.setups(r, CFG)
    tp = next(s for s in ss if s["book"] == "trend_pullback")
    assert tp["steps"] == 2 and "RSI-14 at or below 40 (now 43)" in tp["trigger"] and "$101.00" in tp["trigger"]
    assert tp["level"] == 101.0 and tp["level_kind"] == "above" and "$4.00" in tp["stop_note"]
    one = SC.setups({**r, "min_rsi14_4": 38.0}, CFG)[0]
    assert one["book"] == "trend_pullback" and one["steps"] == 1
    assert SC.setups({**r, "eligible": False}, CFG) == []                       # never a setup on an ineligible name
    assert not any(s["book"] == "trend_pullback" for s in SC.setups({**r, "too_new": True}, CFG))
    mom = SC.setups({**r, "ema200": 200.0, "sma200": 200.0, "mom_rank": 2.0}, CFG)
    assert [s["book"] for s in mom] == ["xs_momentum"] and mom[0]["steps"] == 1


def test_rsi_trigger_price_is_where_the_rule_would_fire():
    from abg.tradebook.common import indicators as ind
    s = pd.Series(np.linspace(100, 120, 60) + np.sin(np.arange(60)), index=pd.bdate_range("2026-01-02", periods=60))
    lvl = SC.rsi_trigger_price(s, 2, 10)
    assert lvl is not None and lvl < float(s.iloc[-1])
    nxt = s.index[-1] + pd.Timedelta(days=1)
    assert ind.rsi(pd.concat([s, pd.Series([lvl - 0.05], index=[nxt])]), 2).iloc[-1] < 10
    assert ind.rsi(pd.concat([s, pd.Series([lvl + 0.5], index=[nxt])]), 2).iloc[-1] >= 10


def test_gate_report_says_rule_by_rule_why_a_name_is_not_pitched():
    rep = SC.gate_report({"eligible": True, "close": 100, "ema200": 110, "ema50": 105, "min_rsi14_5": 35, "prev_high": 99,
                          "macd_hist": 1, "macd_hist_prev": 0, "sma200": 110, "rsi2": 50, "mom_score": -1, "mom_rank": 40}, CFG)
    tp = rep[0]
    assert tp["book"] == "trend_pullback" and not tp["passes"]
    assert [c["pass"] for c in tp["checks"]] == [True, False, False, True, True, True]


def test_the_rule_universe_has_no_cap_by_default(tmp_path):
    from abg.config import Settings
    assert Settings.model_fields["stocks_market_max"].default == 0
    tickers = [f"BIG{i:02d}" for i in range(40)] + ["THIN1"]
    d = _market_desk(tmp_path, tickers, sessions=65)
    out = asyncio.run(d.market_refresh())
    assert out["market_tier"] == 40                                             # all 40 liquid names, none cut
    cache = json.loads(d.market._common_cache().read_text())
    assert "names" in cache and "first_seen" in cache
    d.close()


# --------------------------------------------------------------------------- news and sentiment
def test_headline_scoring_relevance_and_dedup(tmp_path):
    assert N.score_text("Nvidia beats estimates, raises guidance on record demand") > 0.5
    assert N.score_text("Shares plunge after lawsuit and guidance cut") < -0.5
    assert N.score_text("Company reports no growth") < 0 and N.score_text("Annual meeting scheduled") == 0.0
    assert N.relevance("NVDA jumps on earnings", "", "NVDA") == 1.0
    assert N.relevance("Chip makers rally", "Nvidia led the gains", "NVDA", "Nvidia Corp") == 0.6
    assert N.relevance("Markets wrap", "", "NVDA", "Nvidia Corp") == 0.3
    db = DeskDB(tmp_path / "d.sqlite3")
    a = N.article("NVDA", "NVDA beats estimates", "", "https://x.test/1", "2026-11-10T08:00:00-05:00", "Wire")
    b = N.article("NVDA", "NVDA beats estimates", "", "https://x.test/1", "2026-11-10T09:00:00-05:00", "Wire")
    assert a["id"] == b["id"] and N.store(db, [a, b]) == 1 and N.store(db, [a]) == 0
    got = N.recent(db, "NVDA")
    assert len(got) == 1 and got[0]["url"] == "https://x.test/1" and got[0]["at"].startswith("2026-11-10")
    db.close()


def test_name_sentiment_level_change_and_volume():
    rows = [{"day": (TODAY - timedelta(days=k)).isoformat(), "score": 0.4, "n": 2} for k in range(4, 20)]
    rows += [{"day": TODAY.isoformat(), "score": -0.6, "n": 12}]
    s = N.name_sentiment(rows, TODAY)
    assert s["level"] == -0.6 and s["baseline"] == 0.4 and s["change"] == -1.0
    assert s["n_today"] == 12 and s["volume_ratio"] == pytest.approx(12 / (32 / 20), abs=0.01)
    empty = N.name_sentiment([], TODAY)
    assert empty["level"] is None and empty["change"] is None and empty["volume_ratio"] is None


def test_market_sentiment_is_a_count_of_named_signals():
    on = N.market_sentiment({"above_200": 0.7, "above_50": 0.65, "new_highs": 80, "new_lows": 10}, vix=14, vix3m=17, sectors_up=0.8)
    assert on["label"] == "risk-on" and len(on["why"]) >= 4 and "risk-on" in N.mood_sentence(on)
    off = N.market_sentiment({"above_200": 0.3, "above_50": 0.25, "new_highs": 5, "new_lows": 90}, vix=31, vix3m=27, sectors_up=0.1)
    assert off["label"] == "risk-off" and off["score"] < -0.5
    assert N.market_sentiment(None)["label"] == "not in data" and "not in data" in N.mood_sentence(None)


THEMES = {"ai_compute": {"label": "AI compute", "symbols": ["NVDA", "AVGO", "AMD", "MU"], "keywords": ["nvidia"]},
          "ai_models": {"label": "AI models", "symbols": ["MSFT", "GOOGL", "AMZN"], "keywords": ["anthropic", "anthropic ipo"]},
          "banks": {"label": "Banks", "symbols": ["JPM", "BAC", "WFC"], "keywords": []}}


def test_a_theme_shock_names_the_listed_stocks_an_event_touches():
    day = "2026-11-10"
    moves = {"NVDA": {"chg": -0.085}, "AVGO": {"chg": -0.04}, "AMD": {"chg": -0.05}, "MU": {"chg": -0.035},
             "MSFT": {"chg": 0.004}, "GOOGL": {"chg": 0.012}, "AMZN": {"chg": 0.02}, "JPM": {"chg": 0.002}, "BAC": {"chg": -0.001}}
    arts = [N.article("NVDA", "Nvidia plunges after export controls widen", "", "https://x.test/nv", f"{day}T07:00:00-05:00"),
            N.article(None, "Anthropic files for IPO at record valuation", "", "https://x.test/a1", f"{day}T06:00:00-05:00"),
            N.article(None, "What the Anthropic IPO means for cloud partners", "", "https://x.test/a2", f"{day}T06:30:00-05:00")]
    shocks = N.theme_shocks(THEMES, moves, arts, None, day)
    by = {s["theme"]: s for s in shocks}
    assert set(by) == {"ai_compute", "ai_models"}                               # the banks did nothing
    chip = by["ai_compute"]
    assert chip["kind"] == "move" and chip["direction"] == "down" and chip["lead"] == {"symbol": "NVDA", "chg": -0.085}
    assert [n["symbol"] for n in chip["names"]][0] == "NVDA" and chip["source"]["url"] == "https://x.test/nv"
    ipo = by["ai_models"]                                                       # a private company, no ticker
    assert ipo["kind"] == "news" and ipo["n_articles"] == 2 and {n["symbol"] for n in ipo["names"]} == {"MSFT", "GOOGL", "AMZN"}
    assert ipo["source"]["url"].startswith("https://x.test/a") and "headlines today" in N.shock_sentence(ipo)
    assert N.theme_shocks(THEMES, {k: {"chg": 0.001} for k in moves}, [], None, day) == []


def test_correlated_names_join_a_theme_apart_from_the_hand_list():
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2026-06-01", periods=80)
    base = rng.normal(0, 0.02, 80)
    px = lambda r: 100 * np.exp(np.cumsum(r))                                   # noqa: E731
    closes = pd.DataFrame({"NVDA": px(base + rng.normal(0, 0.003, 80)), "AVGO": px(base + rng.normal(0, 0.003, 80)),
                           "AMD": px(base + rng.normal(0, 0.003, 80)), "TWIN": px(base + rng.normal(0, 0.004, 80)),
                           "OTHER": px(rng.normal(0, 0.02, 80))}, index=idx)
    th = N.extend_by_correlation({"t": {"label": "T", "symbols": ["NVDA", "AVGO", "AMD"], "keywords": []}}, closes,
                                 {"TWIN", "OTHER"}, 0.75, 8)
    assert th["t"]["correlated"] == ["TWIN"] and N.themes_of("TWIN", th) == ["t"]


def test_risk_flags_carry_their_source_and_conviction_takes_two_news_terms():
    arts = [N.article("TST", "TST faces class action lawsuit over disclosures", "", "https://x.test/law", "2026-11-10T08:00:00-05:00"),
            {**N.article("TST", "TST filed: material event (8-K)", "", "https://sec.test/8k", "2026-11-09T00:00:00-05:00", kind="filing")},
            N.article("TST", "Markets wrap", "TST mentioned", None, "2026-11-10T09:00:00-05:00")]
    flags = N.risk_flags("TST", arts, {"change": -0.5, "n": 6, "as_of": "2026-11-10"})
    kinds = [f["kind"] for f in flags]
    assert kinds == ["headline", "filing", "sentiment"] and flags[0]["url"] == "https://x.test/law"
    assert "lawsuit / investigation" in flags[0]["text"]
    pc = P.pitch_cfg({})
    kw = dict(gates={"a": True}, ml_prob=None, ml_threshold=None, upside=None, regime="bull_chop", flags_on=0,
              earnings_in_hold=False, has_fundamentals=True, horizons_disagree=False, small_sample=False, pcfg=pc)
    base = P.conviction(**kw)["score"]
    bad = P.conviction(**kw, news={"level": -0.5, "n": 5, "market": "risk-off"})
    assert bad["score"] == base - 8 - 5 and any("news sentiment negative" in b["input"] for b in bad["breakdown"])
    assert P.conviction(**kw, news={"level": -0.5, "n": 1, "market": None})["score"] == base   # too few articles: no term
    assert P.conviction(**kw, news={"level": 0.4, "n": 4, "market": "risk-on"})["score"] == base + 5 + 3


# --------------------------------------------------------------------------- the watchlist
def _row(sym, **kw):
    r = {"symbol": sym, "name": f"{sym} Inc", "eligible": True, "too_new": False, "close": 100.0, "chg_1d": 0.01,
         "mom_pct": 0.9, "trend_pct": 0.7, "setups": []}
    r.update(kw)
    return r


SETUP1 = {"book": "trend_pullback", "steps": 1, "missing": ["a close above $101.00 (today's high)"], "passing": ["uptrend"],
          "trigger": "a close above $101.00 (today's high)", "level": 101.0, "level_kind": "above", "stop_note": "stop 2 ATR", "max_hold": 30}
PACK = {"id": "2026-11-09-trend_pullback-BUY1", "symbol": "BUY1", "name": "Buy One", "book": "trend_pullback",
        "book_label": "Trend pullback", "price": 50.0, "stop": 48.0, "gates": {"uptrend": True},
        "rec": {"rating": "BUY", "daisy_rank": 1, "one_liner": "Buy BUY1 at the open.", "conviction": {"score": 74, "bucket": "High"}},
        "plan": {"entry": {"date": "2026-11-10", "tested": "at the next open", "zone": [49.5, 50.5]},
                 "targets": {"t1": 53.0, "t2": 55.0}, "risk": {"rr": 1.5, "pct_of_fund": 0.06}, "max_hold": 30}}


def _build(extra=None, **kw):
    screen = {"NEAR": _row("NEAR", setups=[SETUP1]), "FAR": _row("FAR", setups=[{**SETUP1, "steps": 2}], mom_pct=0.3),
              "BUY1": _row("BUY1"), "NOPE": _row("NOPE", eligible=False, setups=[SETUP1]), **(extra or {})}
    return WL.build(screen, lambda r: r["setups"], [PACK], TODAY, {}, {"trend_pullback": {"median_hold_days": 8}}, **kw)


def test_watchlist_cards_carry_rank_grades_strategy_and_timeline():
    cards = _build(in_play={"NEAR": {"why": "NEAR wins a contract", "links": [{"title": "t", "url": "https://x.test/n"}]},
                            "HOT": {"why": "HOT surges on news", "links": []}},
                   ipos=[{"date": "2026-11-13", "symbol": "NEWCO", "name": "NewCo Inc", "exchange": "NASDAQ", "price": "20-22"}])
    by = {c["symbol"]: c for c in cards}
    assert [c["symbol"] for c in cards][:3] == ["BUY1", "NEAR", "FAR"] and "NOPE" not in by     # ineligible: never listed
    buy = by["BUY1"]
    assert buy["drank"] == 1 and buy["status"] == "triggered" and buy["strategy"]["stop"] == 48.0 and buy["pitch_id"] == PACK["id"]
    near = by["NEAR"]
    assert near["drank"] == 3 and near["strategy"]["trigger"].startswith("a close above $101.00")
    assert near["timeline"]["earliest"] == "2026-11-10" and "8 sessions" in near["timeline"]["expected_hold"]
    assert near["timeline"]["leaves"] > TODAY.isoformat() and near["links"][0]["url"] == "https://x.test/n"
    assert near["grades"]["momentum"] == "A" and near["grades"]["value"] == RK.NA                # never guessed
    assert by["FAR"]["drank"] == 4 and by["HOT"]["source"] == "news" and by["HOT"]["drank"] == 4
    ipo = by["NEWCO"]
    assert ipo["source"] == "ipo" and "too new for the tested rules" in ipo["strategy"]["trigger"]
    assert ipo["timeline"]["listing"] == "2026-11-13"


def test_lifecycle_and_what_changed():
    cards = _build()
    rows, ch = WL.advance({}, cards, {"BUY1": "new"}, TODAY)
    st = {r["symbol"]: r["status"] for r in rows}
    assert st == {"BUY1": "triggered", "NEAR": "watching", "FAR": "watching"}
    assert {a["symbol"] for a in ch["added"]} == {"BUY1", "NEAR", "FAR"} and ch["triggered"] == [{"symbol": "BUY1"}]
    prev = {r["symbol"]: r for r in rows}
    # next day: NEAR triggered and is pitched; FAR improved to one step; BUY1's pitch is active
    pack2 = {**PACK, "id": "2026-11-10-trend_pullback-NEAR", "symbol": "NEAR", "rec": {**PACK["rec"], "daisy_rank": 2,
                                                                                         "conviction": {"score": 60, "bucket": "Medium"}}}
    screen = {"NEAR": _row("NEAR"), "FAR": _row("FAR", setups=[SETUP1]), "BUY1": _row("BUY1")}
    cards2 = WL.build(screen, lambda r: r["setups"], [pack2], TODAY + timedelta(days=1), {})
    rows2, ch2 = WL.advance(prev, cards2, {"BUY1": "active", "NEAR": "new"}, TODAY + timedelta(days=1))
    st2 = {r["symbol"]: r["status"] for r in rows2}
    assert st2 == {"NEAR": "triggered", "FAR": "watching", "BUY1": "in_play"}
    assert ch2["triggered"] == [{"symbol": "NEAR"}] and ch2["up"] == [{"symbol": "NEAR", "from": 3, "to": 2},
                                                                      {"symbol": "FAR", "from": 4, "to": 3}]
    line = WL.changed_line(ch2)
    assert "triggered overnight: NEAR" in line and "FAR 4→3" in line
    # later: BUY1's pitch closed -> exited; FAR never triggered and its clock ran out -> dropped
    prev2 = {r["symbol"]: r for r in rows2}
    late = date.fromisoformat(prev2["FAR"]["card"]["timeline"]["leaves"])
    rows3, ch3 = WL.advance(prev2, [], {"BUY1": "closed", "NEAR": "active"}, late)
    st3 = {r["symbol"]: r["status"] for r in rows3}
    assert st3 == {"NEAR": "in_play", "FAR": "dropped", "BUY1": "exited"}
    assert ch3["exited"] == [{"symbol": "BUY1"}] and ch3["removed"][0]["symbol"] == "FAR"
    assert WL.changed_line({}) == "no changes since the last list"


def test_morning_post_fits_discord_and_leads_with_what_changed():
    extra = {f"N{i:02d}": _row(f"N{i:02d}", setups=[SETUP1]) for i in range(30)}
    cards = WL.build({**extra, "BUY1": _row("BUY1")}, lambda r: r["setups"], [PACK], TODAY, {"watchlist": {"per_book": 30}})
    rows, ch = WL.advance({}, cards, {"BUY1": "new"}, TODAY)
    shock = {"label": "AI compute", "direction": "down", "kind": "move", "median": -0.04, "names": [{"symbol": "NVDA", "chg": -0.08}],
             "lead": {"symbol": "NVDA", "chg": -0.08}, "source": {"url": "https://x.test/nv", "title": "t"}, "n_articles": 1}
    e = WL.embed(rows, ch, NOW, "Market mood is neutral: no signal is at an extreme.", [shock], "2026-11-09",
                 {"covered": 5500, "eligible": 2900}, "RESEARCH", post=10)
    total = len(e["title"]) + len(e["description"]) + sum(len(f["name"]) + len(f["value"]) for f in e["fields"]) + len(e["footer"]["text"])
    assert total <= 6000 and len(e["fields"]) <= 25 and all(len(f["value"]) <= 1024 for f in e["fields"])
    assert e["title"].startswith("[RESEARCH] Morning watchlist — Tue Nov 10")
    assert "**What changed:** added" in e["description"] and "**Theme shock:** AI compute sold off" in e["description"]
    assert e["fields"][0]["name"] == "Rank 1 — Strong buy" and "**BUY1**" in e["fields"][0]["value"]
    assert e["fields"][-1]["name"] == "Name of the day — BUY1" and "DAISY Rank 1" in e["fields"][-1]["value"]
    shown = sum(f["value"].count("**N") for f in e["fields"][:-1])
    assert shown <= 9                                                           # ten names at most, BUY1 included
    assert "5,500 names (2,900 eligible)" in e["footer"]["text"] and "research, not advice" in e["footer"]["text"]


def test_watchlist_score_compares_ranks_with_spy():
    idx = pd.bdate_range("2026-11-02", periods=15)
    px = {"A": pd.Series(np.linspace(100, 110, 15), index=idx), "B": pd.Series(np.linspace(100, 95, 15), index=idx)}
    spy = pd.Series(np.linspace(500, 505, 15), index=idx)
    adds = [{"symbol": "A", "day": "2026-11-02", "drank": 3, "price": 100}, {"symbol": "B", "day": "2026-11-02", "drank": 4, "price": 100}]
    sc = WL.score(adds, {"A": ["2026-11-05"]}, lambda s: px[s], lambda: spy, horizon=10)
    assert sc["rank_3"]["trigger_rate"] == 1.0 and sc["rank_3"]["avg_excess"] > 0
    assert sc["rank_4_5"]["trigger_rate"] == 0.0 and sc["rank_4_5"]["avg_ret"] < 0 and sc["rank_1_2"]["n"] == 0


# --------------------------------------------------------------------------- live alerts
PICK = {"symbol": "TST", "role": "pick", "id": "p1", "ref": 100.0, "atr": 2.0, "stop": 96.0, "t1": 104.0, "t2": 108.0,
        "zone": [99.0, 101.0], "chase_above": 102.0, "book": "trend_pullback", "adv_shares": 1_000_000}
AC = AL.alert_cfg({})


def q(price, **kw):
    return {"price": price, "open": kw.pop("open", 100.0), "day_high": kw.pop("hi", price), "day_low": kw.pop("lo", price),
            "prev_close": 100.0, **kw}


def test_events_fire_on_a_change_of_state_and_only_once():
    ev, st = AL.detect(PICK, q(100.2), None, NOW, AC)
    assert ev == [] and st["zone"] == "in"                                      # the first look sets the state
    ev, st = AL.detect(PICK, q(102.5, hi=102.5), st, NOW, AC)
    assert [e["kind"] for e in ev] == ["zone_leave"] and "Don't chase" in ev[0]["plan"]
    ev2, st = AL.detect(PICK, q(102.6, hi=102.6), st, NOW, AC)
    assert ev2 == []                                                            # same state: nothing new
    ev, st = AL.detect(PICK, q(100.5, hi=102.6), st, NOW, AC)
    assert [e["kind"] for e in ev] == ["zone_enter"] and "$99.00–$101.00" in ev[0]["text"]
    ev, st = AL.detect(PICK, q(96.8, hi=102.6, lo=96.8), st, NOW, AC)
    assert {e["kind"] for e in ev} == {"zone_leave", "near_stop"}
    ev, st = AL.detect(PICK, q(95.9, hi=102.6, lo=95.5), st, NOW, AC)
    assert [e["kind"] for e in ev] == ["stop"] and ev[0]["plan"].startswith("The stop is the exit")
    assert AL.detect(PICK, q(95.0, hi=102.6, lo=95.0), st, NOW, AC)[0] == []    # the stop is said once


def test_targets_gaps_volume_and_forming():
    ev, st = AL.detect(PICK, q(104.5, hi=104.6), {"zone": "above", "gap_seen": True}, NOW, AC)
    assert [e["kind"] for e in ev] == ["t1"] and "$108.00" in ev[0]["plan"]
    ev, st = AL.detect(PICK, q(108.2, hi=108.3), st, NOW, AC)
    assert [e["kind"] for e in ev] == ["t2"]
    gap, _ = AL.detect(PICK, q(103.5, open=103.0, hi=103.6), None, NOW, AC)
    assert gap[0]["kind"] == "gap" and gap[0]["gap_atr"] == 1.5 and "untested refinement says skip" in gap[0]["plan"]
    vol, _ = AL.detect(PICK, q(100.3, volume=900_000), {"zone": "in", "gap_seen": True}, NOW, AC)     # 10:30 = 15% of the day
    assert vol[0]["kind"] == "rel_volume" and vol[0]["rel_volume"] > 2
    watch = {"symbol": "W", "role": "watch", "ref": 100.0, "atr": 2.0, "book": "trend_pullback", "label": "Trend pullback",
             "level": 101.0, "level_kind": "above", "steps": 1}
    ev, st = AL.detect(watch, q(101.4, hi=101.5), {"gap_seen": True}, NOW, AC)
    assert [e["kind"] for e in ev] == ["forming"] and ev[0]["untested"] is True
    assert "Forming, untested; it confirms only at the close. Not a buy." == ev[0]["plan"]
    assert AL.detect(watch, q(101.6), st, NOW, AC)[0] == []
    assert AL.detect({**watch, "steps": 2}, q(101.4), {"gap_seen": True}, NOW, AC)[0] == []    # more than one step away
    assert AL.session_fraction(NOW) == pytest.approx(60 / 390) and AL.in_session(NOW)
    assert not AL.in_session(NOW.replace(hour=8)) and not AL.in_session(datetime(2026, 11, 14, 11, 0, tzinfo=NY))


def test_gate_dedups_cools_down_and_caps(tmp_path):
    db = DeskDB(tmp_path / "d.sqlite3")
    g = AL.Gate(db, AC)
    e = lambda sym, kind: {"kind": kind, "symbol": sym, "role": "pick", "text": "t", "plan": "p", "at": NOW.isoformat()}   # noqa: E731
    out, held = g.admit([e("A", "zone_leave"), e("A", "zone_enter")], NOW)
    assert [x["kind"] for x in out] == ["zone_leave"] and held == 1             # one alert per name inside the cooldown
    assert g.admit([e("A", "zone_leave")], NOW + timedelta(minutes=5)) == ([], 0)          # same window: already sent
    out, held = g.admit([e("A", "stop")], NOW + timedelta(minutes=6))
    assert [x["kind"] for x in out] == ["stop"]                                 # a stop is never held back
    assert g.admit([e("A", "stop")], NOW + timedelta(minutes=7)) == ([], 0)     # and never repeated
    out, held = g.admit([e("A", "zone_enter")], NOW + timedelta(minutes=45))
    assert len(out) == 1                                                        # the cooldown ended
    storm = [e(f"S{i}", "near_stop") for i in range(30)]
    out, held = g.admit(storm, NOW + timedelta(minutes=50))
    assert len(out) == AC["max_per_tick"] and held == 30 - AC["max_per_tick"]   # a reconnect storm cannot flood Discord
    off = AL.Gate(db, AL.alert_cfg({"alerts": {"kinds": {"gap": False}}}))
    assert off.admit([e("Z", "gap")], NOW) == ([], 0)                           # a kind can be switched off
    assert len(g.recent("2026-01-01")) >= 3
    db.close()


def test_alert_embed_bundles_and_links():
    evs = [{"kind": "t1", "symbol": "A", "id": "pid-A", "text": "A reached T1.", "plan": "T1 is met."},
           {"kind": "news", "symbol": "B", "id": None, "text": "B: headline.", "plan": "Context.", "url": "https://x.test/b"}]
    e = AL.embed(evs, NOW, held=2, base_url="https://dash.test/", tag="RESEARCH", degraded="quotes missing for 3 names")
    assert e["title"] == "[RESEARCH] 10:30 AM · 2 changes on the watched names"
    assert "https://dash.test/#/pitch/pid-A" in e["fields"][0]["value"] and "[source](https://x.test/b)" in e["fields"][1]["value"]
    assert "2 more change(s) held back" in e["description"] and "quotes missing" in e["description"]
    one = AL.embed(evs[:1], NOW)
    assert one["title"] == "10:30 AM · A reached T1" and one["color"] == 0x1A7F4B
    fs = AL.forming_stats([{"kind": "forming", "symbol": "A", "at": "2026-11-10T10:00:00"},
                           {"kind": "forming", "symbol": "B", "at": "2026-11-10T11:00:00"}], {("A", "2026-11-10")})
    assert fs["n"] == 2 and fs["confirmed"] == 1 and fs["rate"] == 0.5 and "untested" in fs["label"]


# --------------------------------------------------------------------------- the desk, end to end
class FakeFeeds:
    enabled = True

    def __init__(self, sym):
        self.sym, self.calls = sym, []

    async def market_news(self, category="general"):
        self.calls.append(("market", category))
        return [N.article(None, "Stocks rally as growth beats forecasts", "", f"https://x.test/m-{category}", datetime.now(NY))]

    async def company_news(self, symbol, days=3, name=None, limit=15):
        self.calls.append(("company", symbol))
        if symbol != self.sym:
            return []
        return [N.article(symbol, f"{symbol} surges after record profit beats forecasts", "", f"https://x.test/{symbol}",
                          datetime.now(NY), "Wire")]

    async def filings(self, symbol, days=5, name=None):
        return []

    async def ipo_calendar(self, frm, to):
        return [{"date": (datetime.now(NY).date() + timedelta(days=3)).isoformat(), "symbol": "NEWCO", "name": "NewCo Inc",
                 "exchange": "NYSE", "price": "18-20", "shares": 1e7, "value": 2e8, "status": "expected"}]


@pytest.fixture()
def desk(tmp_path):
    d = _desk(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        asyncio.run(d.daily(post=False))
    yield d
    d.close()


def test_daily_pass_builds_the_screen_and_tags_every_pitch(desk):
    meta = desk.screen_meta()
    assert meta["covered"] == 24 and meta["eligible"] <= meta["covered"] and meta["breadth"]["n"] == meta["eligible"]
    assert desk.state()["last_daily"]["screen"]["covered"] == 24
    rows = desk.screen_rows()
    assert any(r.get("setups") for r in rows.values()) and not rows["SPY"]["is_stock"]
    buys = [c for c in desk.today()["candidates"] if c["rec"]["rating"] == "BUY" and c["book"] != "dual_momentum"]
    assert buys and all(set(c["grades"]) == set(RK.GRADE_KEYS) for c in buys)
    assert all(c["rec"]["daisy_rank"] in (1, 2, 3) for c in buys)
    assert desk.market_mood()["label"] in ("risk-on", "neutral", "risk-off")


def test_morning_scan_news_ipos_and_a_post_within_limits(desk):
    last = desk.store.frame("SPY").index[-1].date()
    nd = P.next_trading_day(last)
    now = datetime(nd.year, nd.month, nd.day, 8, 31, tzinfo=NY)
    near = next(s for s, r in desk.screen_rows().items() if r.get("eligible") and r.get("setups"))
    desk.s.stocks_news = desk.s.stocks_watch = True
    desk._news_feeds = FakeFeeds(near)
    posts = []

    async def fake_post(url, embeds, content=None, username="Stock Desk", files=None):
        posts.append(embeds)
        return "posted"
    desk._post_to = fake_post
    assert desk._watch_due(now) == "scan"
    meta = asyncio.run(desk.morning(now, post=False))
    assert meta["names"] > 0 and meta["news"]["new"] >= 2 and meta["posted"] is None and posts == []
    rows = {r["symbol"]: r for r in desk.watch_rows()}
    assert rows["NEWCO"]["card"]["source"] == "ipo" and rows["NEWCO"]["status"] == "watching"
    card = rows[near]["card"]
    assert card["links"] and card["links"][0]["url"] == f"https://x.test/{near}" and "In the news" in card["why"]
    assert desk.sentiment_for(near, datetime.now(NY).date().isoformat())["level"] > 0
    assert desk.db.history("watch", near)[0]["status"] in ("watching", "triggered")
    assert desk._watch_due(now) is None                                         # posting is off until it is turned on
    desk.s.stocks_watch_post = True
    assert desk._watch_due(now.replace(minute=40)) is None and desk._watch_due(now.replace(minute=46)) == "post"
    assert asyncio.run(desk.watch_post(now.replace(minute=46))) == "posted" and len(posts) == 1
    assert desk._watch_due(now.replace(minute=50)) is None                      # once a day
    e = posts[0][0]
    assert "Morning watchlist" in e["title"] and "**What changed:**" in e["description"]
    assert len(json.dumps(e)) < 6000
    cal = desk.calendar()
    assert any(r["kind"] == "ipo" and "NewCo" in r["text"] for r in cal["rows"])
    assert desk.watch_score()["horizon_sessions"] == 10


def test_live_tick_reports_changes_once_and_posts_only_when_turned_on(desk):
    last = desk.store.frame("SPY").index[-1].date()
    nd = P.next_trading_day(last)
    board = desk.board()
    assert board and board["picks"] and board["entry_date"] == nd.isoformat()
    pick = next(p for p in board["picks"] if p.get("zone") and (not p.get("t1") or p["t1"] > p["zone"][1] * 1.002))
    px = {"v": pick["price"]}

    async def quotes(symbols):
        return {s: {"price": px["v"] if s == pick["symbol"] else 50.0, "open": pick["price"], "day_high": px["v"],
                    "day_low": px["v"], "prev_close": pick["price"], "volume": None} for s in symbols}
    desk._quotes = quotes
    posts = []

    async def fake_post(url, embeds, content=None, username="Stock Desk", files=None):
        posts.append(embeds or content)
        return "posted"
    desk._post_to = fake_post
    t = datetime(nd.year, nd.month, nd.day, 10, 0, tzinfo=NY)
    first = asyncio.run(desk.live_tick(t))
    assert first["watched"] >= 1 and first["list"] == []                         # the first look only sets the state
    px["v"] = pick["zone"][1] * 1.001
    second = asyncio.run(desk.live_tick(t + timedelta(minutes=2)))
    assert [e["kind"] for e in second["list"]] == ["zone_leave"] and second["posted"] == "dashboard" and posts == []
    third = asyncio.run(desk.live_tick(t + timedelta(minutes=4)))
    assert third["list"] == []                                                   # no change, nothing said
    desk.s.stocks_live_alerts = True
    if pick.get("stop"):
        px["v"] = pick["stop"] * 0.99
        hit = asyncio.run(desk.live_tick(t + timedelta(minutes=6)))
        assert "stop" in [e["kind"] for e in hit["list"]] and hit["posted"] == "posted" and len(posts) == 1
        assert "touched its stop" in json.dumps(posts[0])
    assert [e["kind"] for e in desk.live_feed(hours=24 * 4000)][-1] == "zone_leave"
    assert desk.state()["live"]["watched"] >= 1
    quiet = asyncio.run(desk.live_tick(t.replace(hour=18)))
    assert quiet["quotes"] == 0 and quiet["list"] == []                          # no quotes outside the session


def test_explore_name_card_notes_heat_and_recap(desk):
    hits = desk.explore("T0", limit=5)
    assert hits and all(h["symbol"].startswith("T0") for h in hits) and "grades" in hits[0]
    card = desk.name_card(hits[0]["symbol"])
    assert card["verdict"] and len(card["gates"]) == 3 and card["gates"][0]["checks"][0]["check"].startswith("eligible")
    assert desk.name_card("NOSUCH") is None
    pid = desk.pitches()[0]["id"]
    desk.add_note(pid, "  Ada  L. ", "up", "Clean setup")
    desk.add_note(pid, "ada l.", "hold", None)
    desk.add_note(pid, "Grace", "up", None)
    assert desk.votes(pid) == {"up": 1, "down": 0, "hold": 1}                    # one vote per name: the latest
    assert [n["name"] for n in desk.notes(pid)] == ["Ada L.", "ada l.", "Grace"]
    with pytest.raises(ValueError):
        desk.add_note(pid, "x", "maybe", None)
    with pytest.raises(KeyError):
        desk.add_note("no-such-pitch", "x", "up", None)
    heat = desk.heat()
    assert heat["sectors"] and heat["regime"] and heat["breadth"]["n"] > 0
    rec = desk.recap(datetime.now(NY))
    assert len(rec["bullets"]) == 5 and "against SPY" in rec["bullets"][2] and "(untested)" in rec["bullets"][4]
    e = desk.recap_embed(rec)
    assert e["title"].startswith("[") or e["title"].startswith("Weekly recap") and "research, not advice" in e["footer"]["text"]
    status = json.dumps(desk.status(), default=str)
    assert '"live_alerts": false' in status and '"watch_post": false' in status   # new posts start off


# --------------------------------------------------------------------------- the API and the front page
def test_api_serves_the_pitching_desk(desk, monkeypatch, tmp_path):
    monkeypatch.setenv("ABG_ALLOW_SYNTHETIC", "true")
    monkeypatch.setenv("ABG_PROVIDER_ORDER", "synthetic")
    monkeypatch.setenv("ABG_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("ABG_AI_ENABLED", "false")
    monkeypatch.setenv("ABG_DATA_DIR", str(tmp_path / "apidata"))
    monkeypatch.setenv("ABG_MONITOR_ON_SERVE", "false")
    from fastapi.testclient import TestClient
    from abg.api.server import app
    with TestClient(app) as c:
        app.state.stock_desk = desk
        try:
            d = c.get("/api/stocks/desk").json()
            assert d["pitch_book"]["new"] > 0 and d["candidates"] and d["board"]["picks"]
            assert all(d["state"][k] is None for k in ("equity", "drawdown", "positions"))      # no paper book in the API
            assert "equity" not in json.dumps(d["candidates"]).replace('"equity": null', "")
            pid = d["board"]["picks"][0]["id"]
            p = c.get(f"/api/stocks/pitch/{pid}").json()
            assert p["memo"] and p["row"]["status"] == "new" and p["history"][0]["status"] == "new" and p["pack"]["rec"]["rating"] == "BUY"
            assert c.get("/api/stocks/pitch/nope").status_code == 404 and c.get("/api/stocks/pitch/a%20b").status_code == 400
            assert c.get("/api/stocks/pitches?status=open").json()["rows"]
            sc = c.get("/api/stocks/scorecard").json()
            assert sc["track"]["all"]["n"] == d["pitch_book"]["new"] and "forming" in sc and "watchlist" in sc
            ex = c.get("/api/stocks/explore?q=T0").json()
            assert ex["rows"] and ex["coverage"]["covered"] == 24
            assert c.get("/api/stocks/name/" + ex["rows"][0]["symbol"]).json()["gates"]
            assert c.get("/api/stocks/name/ZZZZ").status_code == 404 and c.get("/api/stocks/name/bad$sym").status_code == 400
            br = c.get("/api/stocks/bars/" + ex["rows"][0]["symbol"] + "?sessions=120").json()
            assert len(br["bars"]) == 120 and br["bars"][-1]["e200"] and br["bars"][-1]["c"] and br["source"] == "the desk's bar store"
            assert c.get("/api/stocks/bars/ZZZZ").status_code == 404                              # no bars and no provider: said plainly
            assert c.post(f"/api/stocks/notes/{pid}", json={"name": "Ada", "vote": "up", "text": "ok"}).json()["votes"]["up"] == 1
            assert c.post(f"/api/stocks/notes/{pid}", json={"name": "", "vote": "up"}).status_code == 400
            assert c.post("/api/stocks/notes/none", json={"name": "Ada", "vote": "up"}).status_code == 404
            assert c.get("/api/stocks/holdings").json()["provided"] is False
            h = c.post("/api/stocks/holdings", json={"holdings": [{"symbol": "nvda", "weight": 8}]}).json()
            assert h["holdings"] == [{"symbol": "NVDA", "weight": 0.08}]
            st = c.get("/api/stocks/settings").json()
            assert st["features"]["live_alerts"] is False and set(st["keys"]) == {"polygon", "finnhub", "anthropic", "tiingo", "twelvedata", "fmp"}
            assert all(isinstance(v, bool) for v in st["keys"].values())                          # yes/no only, never a value
            g = c.get("/api/stocks/glossary").json()
            assert "ATR" in g["terms"] and g["ranks"]["1"] == "Strong buy" and "in_zone" in g["status"]
            assert c.post("/api/stocks/live/tick").status_code == 200 and c.get("/api/stocks/live").json()["heartbeat"]
            assert c.get("/api/stocks/watchlist").json()["score"]["horizon_sessions"] == 10
            assert len(c.get("/api/stocks/recap").json()["bullets"]) == 5
            assert c.get("/api/health").json()["status"] == "ok"
            page = c.get("/").text
            assert "DAISY" in page and "/static/daisy/app.js" in page and "paper" not in page.lower()
            for f in ("daisy.css", "app.js", "ui.js", "views.js", "views2.js", "fonts/inter-latin-wght-normal.woff2"):
                assert c.get(f"/static/daisy/{f}").status_code == 200
        finally:
            app.state.stock_desk = None


def test_a_viewer_may_leave_a_note_and_nothing_else():
    from abg.ops import auth

    class Req:
        def __init__(self, method, path, token):
            self.method, self.headers, self.cookies = method, {"authorization": f"Bearer {token}"}, {}
            self.url = SimpleNamespace(path=path)
    s = SimpleNamespace(admin_token="adm", view_token="view")
    mw = auth.middleware(lambda r: s)

    async def ok(_):
        return "passed"
    run = lambda *a: asyncio.run(mw(Req(*a), ok))                                # noqa: E731
    assert run("POST", "/api/stocks/notes/p1", "view") == "passed"
    assert run("POST", "/api/stocks/holdings", "view").status_code == 403
    assert run("POST", "/api/stocks/watchlist/scan", "view").status_code == 403
    assert run("POST", "/api/stocks/holdings", "adm") == "passed"
    assert run("GET", "/api/stocks/desk", "nobody").status_code == 401


def test_the_dashboard_stays_small_and_declares_its_themes():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "abg" / "api" / "static"
    files = [root / "daisy.html"] + [root / "daisy" / f for f in ("daisy.css", "app.js", "ui.js", "views.js", "views2.js")]
    assert sum(f.stat().st_size for f in files) < 200_000                        # first load, fonts and charts aside (about 45 KB gzipped)
    app_js = (root / "daisy" / "app.js").read_text(encoding="utf-8")
    for route in ("news: V2.news", "explore: V2.explore", "portfolio: V2.portfolio", "research: V2.research"):
        assert route in app_js                                                   # the explained pages are routed (docs/23)
    v2 = (root / "daisy" / "views2.js").read_text(encoding="utf-8")
    assert "/api/stocks/analyst-pitch" in v2 and "not a rule signal" in (root / "daisy" / "views.js").read_text(encoding="utf-8")
    css = (root / "daisy" / "daisy.css").read_text(encoding="utf-8")
    for token in ("--dark-green: #213438", "--ivory: #e4d7d0", '[data-theme="light"]', "prefers-reduced-motion", ":focus-visible"):
        assert token in css
    html = (root / "daisy.html").read_text(encoding="utf-8")
    assert 'class="skip"' in html and "<main" in html and 'aria-live="polite"' in html and "cdn" not in html.lower()
    assert (root / "daisy" / "fonts" / "LICENSES.txt").exists()


# --------------------------------------------------------------------------- the whole market really is the whole market
def test_the_stock_list_follows_every_page(tmp_path):
    """The paging link's cursor must survive: losing it cut the market to the first page (about 1,400 names)."""
    from abg.swing.wide import Sources

    class Http:
        def __init__(self):
            self.calls = []

        async def get_json(self, url, provider=None, params=None, timeout=None, headers=None):
            self.calls.append((url, dict(params or {})))
            t, cur = (params or {}).get("type"), (params or {}).get("cursor")
            if cur == "P2":
                return {"results": [{"ticker": "CCC", "name": "C Co", "type": "CS"}]}
            if t == "CS":
                return {"results": [{"ticker": "AAA", "name": "A Co", "type": "CS"}, {"ticker": "BBB", "name": "B Co", "type": "CS"}],
                        "next_url": "https://api.polygon.io/v3/reference/tickers?cursor=P2"}
            return {"results": [{"ticker": "ADR1", "name": "Adr One", "type": "ADRC"}]}
    http = Http()
    src = Sources(SimpleNamespace(polygon_api_key="SECRET", swing_wide_polygon_per_min=100000, polygon_base_url=None), http)
    rows = asyncio.run(src.common_stocks("CS,ADRC"))
    assert [r["ticker"] for r in rows] == ["AAA", "BBB", "CCC", "ADR1"]
    page2 = http.calls[1]
    assert page2[1].get("cursor") == "P2" and "SECRET" not in page2[0] and "cursor" not in page2[0]


def test_morning_catchup_runs_once_when_the_evening_pass_missed_the_market_bar(tmp_path):
    d = _market_desk(tmp_path, ["BIGCO", "MIDCO"], sessions=65)
    d.s.stocks_market_catchup = "06:30"
    d.u.market = ["BIGCO"]
    day = datetime(2026, 11, 11, 7, 0, tzinfo=NY)                              # a Wednesday morning
    d._save("state.json", {"last_daily": {"as_of": "2026-11-10", "market_bar": False}})
    assert d._catchup_due(day) and not d._catchup_due(day.replace(hour=5)) and not d._catchup_due(day.replace(hour=9, minute=30))
    assert not d._catchup_due(datetime(2026, 11, 14, 7, 0, tzinfo=NY))         # Saturday
    d._save("state.json", {"last_daily": {"as_of": "2026-11-10", "market_bar": True}})
    assert not d._catchup_due(day)                                             # the pass already saw the whole market
    d._save("state.json", {"last_daily": {"as_of": "2026-11-10", "market_bar": False}})
    ran = []

    async def fake_daily(post=True, enrich=None):
        ran.append(post)
        return {"opens": 0, "market_bar": True}
    d.daily = fake_daily
    out = asyncio.run(d.catchup(day))                                          # the stub publishes any session asked for
    assert ran == [False] and out["market_bar"] is True                        # re-run, and never posted
    assert d._market_bar("2026-11-10") and not d._catchup_due(day.replace(minute=30))      # once per morning
    d.s.stocks_market_catchup = ""
    d._save("state.json", {"last_daily": {"as_of": "2026-11-10", "market_bar": False}})
    assert not d._catchup_due(day)                                             # "" switches it off
    d.close()
