"""AIBG pitch plan (docs/21.6): levels, valuation checks, conviction, sizing, calendar, the write-up
validator, embeds, charts, the deck and the plan scorecard. No network, no keys, no Discord."""
from __future__ import annotations

import json
import warnings
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from abg.stocks import pitch_plan as P
from abg.stocks import thesis as T
from abg.stocks.live import Candidate, LiveSignals

from test_stocks import _desk, make_cfg, make_store

PC = P.pitch_cfg({})
AS_OF = date(2026, 11, 6)                      # a Friday


def cand(book="trend_pullback", price=100.0, stop=96.0, atr=2.0, **kw):
    facts = kw.pop("facts", {})
    return Candidate(book=book, symbol="TST", action=kw.pop("action", "BUY"), shares=kw.pop("shares", 10.0),
                     reason=kw.pop("reason", "signal"), price=price, stop=stop, stop_atr=2.0, atr=atr,
                     facts=facts, **kw)


BASE = {"n": 120, "median_win_R": 1.2, "p75_win_R": 2.1, "small_sample": False}


# --------------------------------------------------------------------------- levels
def test_trend_pullback_levels_are_in_order_and_rr_is_t1_over_risk():
    c = cand()
    t = P.trade_targets(c, BASE, None, AS_OF)
    assert t["stop"] < t["entry_ref"] < t["t1"] < t["t2"]
    assert t["t1"] == pytest.approx(100 + 1.2 * 4)
    assert t["t1_kind"] == "reference level"                    # partial exits are never called tested
    r = P.risk_and_size(c, c.price, c.stop, t["t1"], 100_000, "High", PC)
    assert r["rr"] == pytest.approx((t["t1"] - 100) / (100 - 96), abs=0.01)


def test_mean_reversion_t1_is_the_5_day_average():
    c = cand("mean_reversion", facts={"sma5": 103.5})
    t = P.trade_targets(c, BASE, None, AS_OF)
    assert t["t1"] == 103.5 and t["t1_kind"] == "rule exit"
    assert t["t2"] is None or t["t2"] > t["t1"]


def test_mean_reversion_drops_a_t2_below_the_rule_exit():
    c = cand("mean_reversion", facts={"sma5": 112.0})          # p75 2.1R = 108.4 < 112
    t = P.trade_targets(c, BASE, None, AS_OF)
    assert t["t1"] == 112.0 and t["t2"] is None
    assert any("No stretch target" in n for n in t["notes"])


def test_mean_reversion_falls_back_when_the_average_is_below_entry():
    c = cand("mean_reversion", facts={"sma5": 99.0})
    t = P.trade_targets(c, BASE, None, AS_OF)
    assert t["t1"] > c.price and t["t1_kind"] == "reference level"


def test_momentum_has_no_price_trigger_only_a_review_date():
    c = cand("xs_momentum", stop=None, action="REBALANCE")
    t = P.trade_targets(c, BASE, None, AS_OF)
    assert t["t1"] is None and t["t2"] is None
    assert t["review_date"] == "2026-11-30"                     # last session of November 2026


def test_out_of_order_levels_raise():
    with pytest.raises(ValueError):                             # a negative "winning" R puts T1 below entry
        P.trade_targets(cand(), {"median_win_R": -2.0}, None, AS_OF)


# --------------------------------------------------------------------------- valuation
def _val(price=100.0):
    from abg.pitch.valuation import value
    fund = {"eps": 5.0, "forward_pe": 18.0, "pe": 20.0, "beta": 1.1, "dividend_yield": 0.012,
            "market_cap": price * 1e9, "shares_outstanding": 1e9}
    return fund, value(fund, price, [{"symbol": "P1", "pe": 22.0, "forward_pe": 19.0}])


def test_valuation_cases_recompute_and_a_tampered_case_raises():
    fund, val = _val()
    inv = P.investment_view(val, 100.0, fund, AS_OF, PC)
    assert inv["available"] and len(inv["lines"]) >= 4
    w = PC["scenario_weights"]
    pw = sum(w[k] * inv["cases"][k]["value"] for k in ("bear", "base", "bull"))
    assert inv["prob_weighted"] == pytest.approx(pw, abs=0.02)
    val["scenarios"]["base"]["value"] *= 1.10
    with pytest.raises(ValueError):
        P.investment_view(val, 100.0, fund, AS_OF, PC)


@pytest.mark.parametrize("bad", [{"dividend_yield": 1.2},                 # percent, not a fraction
                                 {"eps": 0.05},                            # cents, not dollars
                                 {"market_cap": 100.0 * 1e6}])             # millions vs units
def test_unit_mismatches_raise(bad):
    fund, _ = _val()
    with pytest.raises(ValueError):
        P.check_units({**fund, **bad}, 100.0)


# --------------------------------------------------------------------------- conviction and size
def _conv(**kw):
    base = dict(gates={"a": True, "b": True}, ml_prob=None, ml_threshold=None, upside=0.1, regime="bull",
                flags_on=0, earnings_in_hold=False, has_fundamentals=True, horizons_disagree=False,
                small_sample=False, pcfg=PC)
    base.update(kw)
    return P.conviction(**base)


def test_conviction_is_monotonic_and_clamped():
    good, bad = _conv(), _conv(horizons_disagree=True, earnings_in_hold=True, flags_on=4, small_sample=True)
    assert good["score"] > bad["score"]
    assert _conv(flags_on=1)["score"] >= _conv(flags_on=3)["score"]
    worst = _conv(gates={"a": False}, upside=-0.9, regime="bear", flags_on=50, earnings_in_hold=True,
                  has_fundamentals=False, horizons_disagree=True, small_sample=True)
    assert 0 <= worst["score"] <= 100 and 0 <= good["score"] <= 100
    assert worst["bucket"] == "Low"


def test_size_never_exceeds_the_cap():
    c = cand(price=100.0, stop=99.9)                            # tiny risk would ask for a huge position
    r = P.risk_and_size(c, 100.0, 99.9, 101.0, 100_000, "High", PC)
    assert r["pct_of_fund"] <= PC["max_position_pct"] + 1e-9 and r["bound"] == "capped"


# --------------------------------------------------------------------------- calendar
def test_next_trading_day_skips_weekends_and_holidays():
    assert P.next_trading_day(date(2026, 11, 6)) == date(2026, 11, 9)      # Fri -> Mon
    assert P.next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)    # Thanksgiving
    assert P.next_trading_day(date(2026, 12, 24)) == date(2026, 12, 28)    # Christmas, weekend


# --------------------------------------------------------------------------- the desk path
@pytest.fixture(scope="module")
def packs(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("plan")
    st, u = make_store(tmp)
    cfg = make_cfg(u)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ls = LiveSignals(cfg, st, u).build(warn=False)
        ls.run()
    cands, state = ls.candidates(), ls.state()
    from abg.stocks.desk import StockDesk
    ctx = StockDesk._pitch_context(type("X", (), {"cfg": cfg, "store": st})(), ls, state)
    out = {}
    for c in cands:
        if c.is_entry and c.book in ("trend_pullback", "mean_reversion") and "buy" not in out:
            fund = {"name": "Test Co", "sector": "Technology", "eps": c.price / 20, "pe": 20.0,
                    "forward_pe": 18.0, "beta": 1.1, "description": "Test Co makes test equipment for labs.",
                    "week52_low": c.price * 0.8, "week52_high": c.price * 1.1}
            peers = [{"symbol": "P1", "pe": 22.0, "forward_pe": 19.0, "eps": 2.0},
                     {"symbol": "P2", "pe": 18.0, "forward_pe": 16.0}]
            out["buy"] = T.build_pack(c, state, cfg, fund=fund, peers=peers,
                                      ctx={**ctx, "bars": st.frame(c.symbol)})
            out["buy_bars"] = st.frame(c.symbol)
        if c.action == "SELL" and "sell" not in out:
            out["sell"] = T.build_pack(c, state, cfg, ctx=ctx)
    if "sell" not in out:                                        # an exit made by hand from a held name
        b = out["buy"]
        sell = Candidate(book=b["book"], symbol=b["symbol"], action="SELL", shares=-10.0, reason="trend_break",
                         price=b["price"], atr=b["atr"])
        mine = {"id": "old-pitch", "as_of": "2026-01-02", "entry_open": b["price"] * 0.95, "entry_date": "2026-01-05",
                "plan": {"stop": b["price"] * 0.9}}
        hold = {**ctx, "open_pitches": {b["symbol"]: {b["book"]: mine}}}
        out["sell"] = T.build_pack(sell, state, cfg, ctx=hold)
    out["cfg"], out["state"] = cfg, state
    yield out
    st.close()


def test_buy_memo_follows_the_aibg_order(packs):
    md = T.render_md(packs["buy"])
    order = ["## 01 Company Overview", "## 02 Investment Thesis", "### The thesis — core statement",
             "### Valuation cross-check and price targets", "## 03 Data & Supporting Analysis",
             "## 04 Recommendation", "### The trade plan", "### What would make this wrong",
             "### Why this fits our fund", "### Questions for the team", "## Market context"]
    pos = [md.find(h) for h in order]
    assert all(p >= 0 for p in pos), [h for h, p in zip(order, pos) if p < 0]
    assert pos == sorted(pos)
    assert "Research only" in md and "Research, not advice." in md
    assert "paper" not in md.lower() and "equity" not in md.lower().replace("equity analyst", "")
    assert "untested refinement" in md.lower()
    rec = packs["buy"]["rec"]
    assert rec["rating"] in ("BUY", "ADD") and rec["one_liner"].startswith(("Buy", "Add"))
    assert packs["buy"]["peers"][0]["symbol"] == "P1"            # peers kept for the comps table


def test_sell_memo_explains_the_exit(packs):
    p = packs["sell"]
    assert p["rec"]["rating"] in ("SELL", "TRIM")
    md = T.render_md(p)
    assert "### Why we're exiting" in md and "## 04 Recommendation" in md


def test_pitch_desk_avoid_gets_a_rating():
    from abg.pitch import memo as M
    from abg.pitch.valuation import value
    fund = {"name": "Dear Co", "sector": "Utilities", "eps": 1.0, "forward_pe": 40.0, "beta": 1.0}
    val = value(fund, 100.0, [])                                 # priced far above any earnings value
    pack = M.build_pack("DEAR", 100.0, fund, {"rank": 90, "composite": -1.2, "z": {}}, 100, val, [], {}, None)
    assert pack["verdict"] == "PASS" and pack["rec"]["rating"] == "AVOID"
    md = M.render_md(pack)
    assert "**Rating: AVOID — Do not initiate**" in md and "Research only" in md


def test_embeds_stay_within_discord_limits(packs):
    for k in ("buy", "sell"):
        if k not in packs:
            continue
        e = T.embed(packs[k])
        assert len(e["title"]) <= 256 and len(e.get("description", "")) <= 4096
        assert len(e["fields"]) <= 25
        assert all(0 < len(f["value"]) <= 1024 and len(f["name"]) <= 256 for f in e["fields"])
        total = len(e["title"]) + len(e.get("description", "")) + sum(len(f["name"]) + len(f["value"]) for f in e["fields"])
        assert total <= 6000


# --------------------------------------------------------------------------- write-up validator
def test_validator_rejects_made_up_numbers_tickers_dates_and_hype(packs):
    from abg.stocks import writeup as W
    p = packs["buy"]
    honest = {"core_thesis": f"{p['symbol']} closed at ${p['price']:.2f}; this is the setup the book buys."}
    bad = {"bull_case": "Revenue will reach $48.2B next year.", "bear_case": "Like NVDA, it could halve.",
           "valuation_view": "Earnings on 2026-02-17 could change everything.", "moat": "This will skyrocket."}
    ok, rej = W.validate({**honest, **bad}, p)
    assert "core_thesis" in ok
    assert {r["field"] for r in rej} == set(bad)


# --------------------------------------------------------------------------- charts and deck
def test_charts_render_or_degrade(packs, tmp_path):
    from abg.stocks import charts as CH
    if not CH.available():
        assert CH.render_all(packs["buy"], packs["buy_bars"], tmp_path) == {}
        return
    out = CH.render_all(packs["buy"], packs["buy_bars"], tmp_path)
    assert "price" in out and (tmp_path / out["price"]).stat().st_size > 10_000
    if packs["buy"]["plan"]["investment"]["available"]:
        assert "valuation" in out
    assert "peers" in out


def test_deck_fills_every_placeholder(packs, tmp_path):
    from abg.stocks import charts as CH, deck as DK
    if not DK.available():
        pytest.skip("python-pptx not installed")
    p = json.loads(json.dumps(packs["buy"], default=str))       # as saved by the desk
    if CH.available():
        p["charts"] = CH.render_all(packs["buy"], packs["buy_bars"], tmp_path)
    out = DK.build_deck(p, tmp_path / "deck.pptx", charts_dir=tmp_path)
    assert out.exists() and DK.leftovers(out) == []
    from pptx import Presentation
    text = "\n".join(sh.text_frame.text for s in Presentation(str(out)).slides for sh in s.shapes if sh.has_text_frame)
    assert p["symbol"] in text and "DAISY (quant team)" in text and p["rec"]["rating"] in text
    assert "Lena" not in text and "AbbVie" not in text


def test_deck_refuses_leftover_placeholders(packs, tmp_path, monkeypatch):
    from abg.stocks import deck as DK
    if not DK.available():
        pytest.skip("python-pptx not installed")
    real = DK.deck_content
    monkeypatch.setattr(DK, "deck_content", lambda pack: {**real(pack), "sector": "{{sector}}"})
    with pytest.raises(ValueError):
        DK.build_deck(packs["buy"], tmp_path / "x.pptx")


# --------------------------------------------------------------------------- plan scorecard
def _bars(prices, start="2026-11-09"):
    idx = pd.bdate_range(start, periods=len(prices))
    o, h, l, c = zip(*prices)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1e6}, index=idx)


PLAN = {"stop": 96.0, "t1": 104.0, "t2": 108.0, "max_hold": 5, "chase_above": 102.0,
        "horizon": None, "prob_weighted": None}


def test_mark_plan_t1_then_t2():
    b = _bars([(100, 101, 99, 100), (100, 104.5, 99.5, 104), (104, 108.2, 103, 108)])
    m = P.mark_plan(PLAN, "2026-11-06", b)
    assert m["entry_open"] == 100 and m["t1_hit"] == "2026-11-10" and m["outcome"] == "T2"
    assert m["r_now"] == pytest.approx(2.0)


def test_mark_plan_counts_a_two_sided_day_as_the_stop():
    b = _bars([(100, 105, 95, 100)])
    m = P.mark_plan(PLAN, "2026-11-06", b)
    assert m["outcome"] == "stop" and m["r_now"] == -1.0 and m["t1_hit"] is None


def test_mark_plan_time_exit_and_chase_flag():
    b = _bars([(103, 103.5, 101, 102)] * 6)
    m = P.mark_plan(PLAN, "2026-11-06", b)
    assert m["outcome"] == "time" and m["chase_skip"] is True
    assert P.mark_plan(PLAN, "2026-11-06", b.iloc[:0]) is None


def test_scorecard_splits_by_bucket_and_tested_vs_refined(tmp_path):
    d = _desk(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import asyncio
        asyncio.run(d.daily(post=False))
    rows = d.pitches()
    assert rows and all(r.get("plan") for r in rows)
    # pretend the pitches are a week old so every one gets marked on the stored bars
    last = d.store.frame("SPY").index[-6].date().isoformat()
    for r in rows:
        r["as_of"] = last
        d.book.save(r)
    sc = d.scorecard()
    assert sc["track"]["all"]["n"] == len(rows) and sc["track"]["all"]["marked"] == len(rows)
    assert all(r["status"] in ("active", "closed") and r["entry_open"] for r in sc["pitches"])
    assert all(d.book.history(r["id"])[0]["status"] == "new" for r in rows)     # the lifecycle is kept
    assert sc["plan"]["tested"]["n"] == len(rows)
    assert sc["plan"]["tested"]["marked"] == len(rows)
    assert set(sc["plan"]["by_bucket"]) <= {"High", "Medium", "Low"}
    assert sc["plan"]["refined"]["n"] <= sc["plan"]["tested"]["n"]
    assert (tmp_path / "stocks" / "memos").exists()
    assert list((tmp_path / "stocks" / "memos").glob("*.json"))             # packs saved for decks
    d.store.close()


def test_discord_post_attaches_charts_as_multipart(tmp_path):
    import asyncio
    d = _desk(tmp_path)
    sent = {}

    class Resp:
        status_code = 200

    class Raw:
        async def post(self, url, data=None, files=None, timeout=None):
            sent.update(url=url, payload=json.loads(data["payload_json"]), files=files)
            return Resp()

    class HTTP:
        raw = Raw()

        async def request(self, *a, **kw):
            raise AssertionError("a post with files must go multipart")

    d.engine = type("E", (), {"http": HTTP()})()
    png = tmp_path / "x-price.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    res = asyncio.run(d._post_to("https://discord.example/api/webhooks/1/abc",
                                 [{"title": "t", "image": {"url": "attachment://x-price.png"}}], files=[png]))
    assert res == "posted" and sent["url"].endswith("?wait=true")
    assert sent["payload"]["attachments"][0]["filename"] == "x-price.png"
    assert list(sent["files"]) == ["files[0]"]
    d.store.close()
