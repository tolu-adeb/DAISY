"""Explained research (docs/23): analyst pitches, the deep Explore profile, the News tab, fund performance,
the strategy review and the strategy page. No network, no keys, no Discord."""
from __future__ import annotations

import asyncio
import json
import warnings
from datetime import date

import numpy as np
import pandas as pd
import pytest

from abg.stocks import analyst as A
from abg.stocks import insight as I
from abg.stocks import news as N
from abg.stocks import portfolio as PF
from abg.stocks import watchlist as WL

from test_stocks import _desk

TODAY = date(2026, 11, 10)


# --------------------------------------------------------------------------- analyst pitches
def test_analyst_input_is_validated_with_sentences_a_person_can_act_on():
    ok = A.clean({"symbol": " rklb ", "analyst": "  Ada   L ", "thesis": "Launch cadence is doubling and margins follow.", "direction": "Buy",
                  "entry": "$25.50", "stop": "23", "t1": 30, "horizon_days": "40"})
    assert ok["symbol"] == "RKLB" and ok["analyst"] == "Ada L" and ok["action"] == "BUY" and ok["entry"] == 25.5 and ok["horizon_days"] == 40
    for bad, word in (({"symbol": "bad sym"}, "ticker"), ({"symbol": "X", "analyst": ""}, "name"), ({"symbol": "X", "analyst": "A", "thesis": "short"}, "thesis"),
                      ({"symbol": "X", "analyst": "A", "thesis": "x" * 30, "direction": "short"}, "direction"),
                      ({"symbol": "X", "analyst": "A", "thesis": "x" * 30, "stop": "abc"}, "stop"),
                      ({"symbol": "X", "analyst": "A", "thesis": "x" * 30, "horizon_days": 900}, "horizon")):
        with pytest.raises(ValueError, match=word):
            A.clean(bad)


def test_level_checks_state_facts_with_their_numbers_and_never_block():
    v = {"direction": "buy", "entry": 100.0, "stop": 104.0, "t1": 103.0, "t2": 102.0, "horizon_days": 20}
    row = {"eligible": False, "why_not": "price under $5", "too_new": True, "sessions": 30, "close": 100.0, "sma200": 110.0}
    cs = A.check_levels(v, 100.0, 2.0, 1.5, row, "2026-11-20", TODAY)
    text = " | ".join(c["text"] for c in cs if not c["ok"])
    assert "a buy needs its stop under the entry" in text and "not above the first target" in text
    assert "Earnings on 2026-11-20 fall inside the holding window" in text and "Outside the rule universe: price under $5" in text
    assert "too new for the tested rules" in text and "below its 200-day average" in text
    good = A.check_levels({"direction": "buy", "entry": 100.0, "stop": 95.0, "t1": 110.0, "t2": 120.0, "horizon_days": 20}, 100.0, 2.0, 1.5,
                          {"eligible": True, "close": 100.0, "sma200": 90.0}, None, TODAY)
    assert all(c["ok"] for c in good) and any("Reward:risk to the first target is 2.00" in c["text"] for c in good)
    thin = A.check_levels({"direction": "buy", "entry": 100.0, "stop": 99.0, "t1": 100.5, "horizon_days": 5}, 100.0, 2.0, 1.5, None, None, TODAY)
    assert any("inside one day's typical move" in c["text"] for c in thin) and any("under the 1.5 minimum" in c["text"] for c in thin)
    assert any("No stop given" in c["text"] for c in A.check_levels({"direction": "buy", "horizon_days": 5}, 100.0, None, 1.5, None, None, TODAY))


@pytest.fixture()
def desk(tmp_path):
    d = _desk(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        asyncio.run(d.daily(post=False))
    yield d
    d.close()


def test_an_analyst_pitch_is_filed_labelled_and_scored_apart(desk):
    px = desk.screen_rows()["T01"]["close"]
    body = {"symbol": "T01", "analyst": "Ada L", "thesis": "Margins are expanding and the market has not priced it in yet.",
            "direction": "buy", "stop": px * 0.94, "t1": px * 1.10, "t2": px * 1.2, "horizon_days": 30}
    posts = []

    async def fake_post(url, embeds, content=None, username="Stock Desk", files=None):
        posts.append(embeds)
        return "posted"
    desk._post_to = fake_post
    out = asyncio.run(desk.analyst_pitch(body))
    assert out["posted"] == "dashboard" and posts == []                          # filing a pitch does not post by default
    assert out["id"].endswith("-analyst-T01") and out["of"] >= 4 and out["rating"] == "BUY"
    row = desk.book.get(out["id"])
    assert row["book"] == "analyst" and row["analyst"] == "Ada L" and row["status"] == "new" and row["plan"]["t1"] == pytest.approx(px * 1.10)
    assert desk.book.history(out["id"])[0]["note"] == "analyst pitch by Ada L"
    md = desk.memo(out["id"])
    assert "> **Analyst pitch.** This is Ada L's idea, not a rule signal." in md and "### DAISY's checks on the analyst's levels" in md
    assert "signal from the close, to be acted on at the next open" not in md and "## 04 Recommendation" in md and "paper" not in md.lower()
    pack = desk.pack(out["id"])
    assert pack["book_label"] == "Analyst pitch" and pack["analyst"]["thesis"].startswith("Margins") and pack["plan"]["targets"]["t1_kind"] == "analyst target"
    with pytest.raises(FileExistsError):
        asyncio.run(desk.analyst_pitch(body))                                    # one per name per session
    with pytest.raises(ValueError, match="no price history"):
        asyncio.run(desk.analyst_pitch({**body, "symbol": "NOPE"}))
    sc = desk.scorecard()
    assert set(sc["track"]["by_kind"]) == {"analyst", "rule"} and sc["track"]["by_kind"]["analyst"]["n"] == 1
    assert out["id"] not in [p["id"] for p in (desk.board() or {}).get("picks", [])]          # never in the rules' top picks
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        asyncio.run(desk.daily(post=False))
    mine = [c for c in desk.today()["candidates"] if c["symbol"] == "T01" and c["book"] != "analyst"]
    assert all(c["rec"]["rating"] != "ADD" for c in mine)                         # an analyst pitch does not change a rule's rating
    watch = asyncio.run(desk.analyst_pitch({**body, "symbol": "T02", "direction": "watch"}))
    assert desk.book.get(watch["id"])["plan"]["stop"] is None                    # non-buys are tracked on time only
    assert asyncio.run(desk.analyst_pitch({**body, "symbol": "T03"}, post=True))["posted"] == "dashboard"   # no webhook: stays on the dashboard


# --------------------------------------------------------------------------- the explained profile
def test_summary_sentences_carry_the_numbers_they_rest_on():
    row = {"close": 120.0, "chg_1d": 0.012, "chg_21d": -0.05, "dist_200": 0.18, "mom_pct": 0.87}
    s = I.summarize("XYZ", row, {"total_return_pct": 30.0, "benchmark_return_pct": 12.0, "ann_volatility_pct": 42.0, "max_drawdown_pct": -25.0},
                    {"forward_pe": 22.0}, {"level": 0.4, "n": 5}, "Not pitched: no rule is close.", {"upside": 0.2})
    text = " ".join(x["text"] for x in s)
    assert "XYZ last closed at $120.00, up 1.2% on the day and down 5.0% over the last month." in text
    assert "returned +30.0% against the market's +12.0%, ahead by 18.0 points" in text and "beats 87% of eligible US stocks" in text
    assert "22.0 times next year's expected earnings" in text and "fair value 20% above the price" in text
    assert "a volatile stock: 42% annual volatility" in text and "Recent news is positive" in text and text.endswith("Not pitched: no rule is close.")
    assert s[0]["numbers"] == {"close": 120.0, "chg_1d": 0.012, "chg_21d": -0.05}
    assert I.summarize("XYZ", None, None, None, None, None) == []                 # nothing known: nothing said
    g = I.grade_reasons(row, {"forward_pe": 22.0, "extra": {"operating_margin": 0.3}}, 20.0, {"level": 0.4, "n": 5})
    assert "beats 87%" in g["momentum"] and "1.10 times its peers" in g["value"] and "Operating margin of 30%" in g["quality"]
    assert "not in data" in I.grade_reasons({}, None, None, None)["value"]


def test_profile_degrades_to_what_we_have_and_is_cached_for_the_day(desk):
    p = asyncio.run(desk.profile("T01"))
    assert p["available"] is False and p["summary"] and p["valuation"] is None and p["consensus"] is None
    assert set(p["grades"]) == {"momentum", "trend", "value", "quality", "sentiment"} and "momentum" in p["grade_reasons"]
    assert (desk.dir / "profiles" / "T01.json").exists() and asyncio.run(desk.profile("T01"))["day"] == p["day"]
    assert asyncio.run(desk.profile("NOSUCH")) is None


# --------------------------------------------------------------------------- news tab
def test_news_categories_are_rules_and_the_brief_is_built_from_numbers():
    art = lambda t, sym=None, **kw: {"title": t, "symbol": sym, **kw}                # noqa: E731
    assert N.categorize(art("Anthropic files for IPO as AI chip demand soars")) == ["ipos", "ai_tech"]
    assert N.categorize(art("NVDA beats on earnings", "NVDA"), {"NVDA"}, set(), {"NVDA"}) == ["holdings", "earnings", "ai_tech"]
    assert N.categorize(art("Fed holds interest rates steady")) == ["markets"]
    assert N.categorize(art("Acme agrees to buy Widget Co in $2B deal")) == ["deals"]
    assert N.categorize(art("XYZ filed: material event (8-K)", "XYZ", kind="filing"), set(), {"XYZ"}) == ["filings", "pitches"]
    assert N.categorize(art("Local bakery wins award")) == ["markets"]              # unlabelled general news falls to markets
    b = N.brief({"label": "risk-on", "score": 0.5, "why": ["breadth 70%"]},
                {"sectors": [{"label": "Tech", "chg_1d": 0.02}, {"label": "Energy", "chg_1d": -0.01}], "breadth": {"n": 2000, "new_highs": 80, "new_lows": 12}},
                [], {"articles": 40, "holdings": 3})
    assert len(b) == 4 and "1 of 2 sectors rose" in b[1] and "Tech led (+2.0%)" in b[1] and "80 new 52-week highs and 12 new lows" in b[2]
    assert "40 headlines" in b[3] and "3 of them about names the fund holds" in b[3]
    assert "after the first daily pass" in N.brief(None, None, None)[0]


def test_newsfeed_puts_holdings_first_and_filters(desk):
    desk.portfolio.add({"symbol": "T05", "shares": 10, "cost": 100, "opened": "2024-01-10"})
    now = pd.Timestamp.now(tz="America/New_York")
    N.store(desk.db, [N.article(None, "Fed signals a rate cut as inflation cools", "", "https://x.test/fed", now.to_pydatetime()),
                      N.article("T05", "T05 beats on earnings and raises guidance", "", "https://x.test/t05", (now - pd.Timedelta(hours=3)).to_pydatetime()),
                      N.article("T09", "T09 to acquire rival in merger", "", "https://x.test/t09", (now - pd.Timedelta(hours=1)).to_pydatetime())])
    nf = desk.newsfeed()
    assert [i["symbol"] for i in nf["items"]][0] == "T05" and nf["items"][0]["mine"] == "holding"       # the fund's names lead
    counts = {c["key"]: c["count"] for c in nf["categories"]}
    assert counts["holdings"] == 1 and counts["earnings"] == 1 and counts["deals"] == 1 and counts["markets"] == 1
    assert [i["symbol"] for i in desk.newsfeed(category="deals")["items"]] == ["T09"]
    assert [i["title"] for i in desk.newsfeed(q="rate cut")["items"]] == ["Fed signals a rate cut as inflation cools"]
    hn = desk.holdings_news()
    assert [g["symbol"] for g in hn["groups"]] == ["T05"] and hn["groups"][0]["items"][0]["url"] == "https://x.test/t05"
    assert hn["groups"][0]["line"].startswith("T05: ") and "at the last close" in hn["groups"][0]["line"]


# --------------------------------------------------------------------------- fund performance
def _px(vals, start="2026-01-05"):
    return pd.Series(vals, index=pd.bdate_range(start, periods=len(vals)), dtype=float)


def test_performance_is_time_weighted_so_a_deposit_is_not_a_gain():
    spy = _px(np.linspace(100, 110, 10))
    a = _px([50, 50, 55, 55, 55, 55, 55, 55, 55, 55])
    rows = [{"symbol": "A", "shares": 10, "cost": 50.0, "opened": "2026-01-05", "status": "open"}]
    base = PF.performance(rows, [], 0.0, lambda s: a, spy)
    assert base["available"] and base["stats"]["total_return"] == pytest.approx(0.10) and base["stats"]["bench_return"] == pytest.approx(0.10)
    assert base["stats"]["value_now"] == 550.0 and base["series"][0]["fund"] == 1.0 and len(base["series"]) == 10
    flows = [{"day": "2026-01-12", "amount": 1000.0}]
    dep = PF.performance(rows, flows, 1000.0, lambda s: a, spy)
    assert dep["stats"]["value_now"] == 1550.0 and dep["stats"]["total_return"] == pytest.approx(0.10)     # the deposit added no return
    assert not any("negative" in x for x in dep["assumptions"])
    sold = [{"symbol": "A", "shares": 10, "cost": 50.0, "opened": "2026-01-05", "status": "closed", "closed": "2026-01-08", "exit_price": 60.0}]
    out = PF.performance(sold, [], 600.0, lambda s: a, spy)
    assert out["stats"]["total_return"] == pytest.approx(0.20) and out["stats"]["value_now"] == 600.0      # realised at the exit price
    assert out["stats"]["max_drawdown"] <= 0 and PF.performance([], [], 0, lambda s: a, spy)["available"] is False
    gap = PF.performance(rows, [{"day": "2026-01-12", "amount": 1000.0}], 0.0, lambda s: a, spy)
    assert any("does not cover every purchase" in x for x in gap["assumptions"])
    nopx = PF.performance(rows, [], 0.0, lambda s: None, spy)
    assert nopx["stats"]["total_return"] == pytest.approx(0.0) and any("No stored prices for A" in x for x in nopx["assumptions"])


def test_review_reports_observations_with_evidence_never_orders():
    view = {"open": [{"symbol": "AAA", "opened": "2026-10-01", "last": 90.0, "gain_pct": -0.10, "weight": 0.25, "pitch_id": "p-aaa", "status": "open"},
                     {"symbol": "BBB", "opened": "2026-10-01", "last": 60.0, "gain_pct": 0.20, "weight": 0.05, "status": "open"}], "closed": []}
    pitches = [{"id": "p-aaa", "symbol": "AAA", "book": "trend_pullback", "as_of": "2026-09-30", "status": "closed", "closed_at": "2026-10-20",
                "close_reason": "stop", "plan": {"stop": 95.0}, "track": {"ret": -0.05}},
               {"id": "p-ccc", "symbol": "CCC", "book": "mean_reversion", "as_of": "2026-10-05", "status": "closed", "plan": {}, "track": {"ret": 0.08}}]
    screen = {"AAA": {"close": 90.0, "sma200": 100.0}, "BBB": {"close": 60.0, "sma200": 50.0}}
    obs = {o["title"]: o for o in PF.review(view, pitches, screen, 0.10)}
    assert "1 of 2 open positions" in obs["How much of the portfolio came from DAISY's ideas"]["finding"]
    assert "AAA: last $90.00 against a stop of $95.00 (-5.3%)" in obs["Positions held below their pitch's stop"]["evidence"]
    assert "Still held after DAISY closed the pitch" in obs and "Holdings below their 200-day average" in obs
    assert obs["Positions larger than the pitch size cap"]["evidence"] == ["AAA: 25.0% of the fund"]
    assert "2.00 to 1" in obs["Winners against losers"]["evidence"][1]
    assert "-5.0%" in obs["Pitches the fund took against those it passed on"]["finding"] and "+8.0%" in obs["Pitches the fund took against those it passed on"]["finding"]
    assert all(o["suggestion"] and o["evidence"] for o in obs.values())
    assert PF.review({"open": [], "closed": []}, pitches, screen) == []
    b = PF.breakdown({"open": [{"symbol": "AAA", "weight": 0.25, "gain": -100.0, "gain_pct": -0.1, "status": "open"},
                               {"symbol": "BBB", "weight": 0.05, "gain": 300.0, "gain_pct": 0.2, "status": "open"}], "closed": []}, {"AAA": "Technology"}, 0.10)
    assert b["contributions"][0]["symbol"] == "BBB" and b["contributions"][0]["share"] == 1.5 and b["concentration"]["over_cap"] == ["AAA"]
    assert b["sectors"][0] == {"sector": "Technology", "weight": 0.25} and b["sectors"][1]["sector"] == "not in data"


def test_portfolio_page_data_and_cash_flows(desk):
    desk.portfolio.add({"symbol": "T01", "shares": 10, "cost": 250, "opened": "2024-01-10"})
    f = desk.portfolio.add_flow("2024-01-02", "2,500", "dues")
    assert f["amount"] == 2500.0 and desk.portfolio.flows()[0]["note"] == "dues"
    with pytest.raises(ValueError):
        desk.portfolio.add_flow("2024-01-02", 0)
    full = desk.portfolio_full()
    assert full["performance"]["available"] and full["performance"]["series"] and full["breakdown"]["concentration"]["top1"] == 1.0
    assert full["news"]["groups"][0]["symbol"] == "T01" and full["flows"] and full["review"]
    assert desk.portfolio.delete_flow(f["id"]) and desk.portfolio.flows() == []
    json.dumps(full, default=str)


# --------------------------------------------------------------------------- strategy page and research ideas
def test_strategy_page_explains_each_book_with_its_record(desk):
    s = desk.strategy()
    keys = [b["key"] for b in s["books"]]
    assert keys[:3] == ["trend_pullback", "mean_reversion", "xs_momentum"]
    tp = s["books"][0]
    assert len(tp["steps"]) == 4 and tp["works"] and tp["struggles"] and tp["diagram"] == "pullback" and tp["base"].get("n") is not None
    assert "risk-off regime" in s["regime"]["explain"] and s["conviction"]["weights"]["start"] == 50 and s["rank"][1] == "Strong buy"
    assert len(s["timeline"]) >= 5 and all(t["why"] for t in s["timeline"])
    json.dumps(s, default=str)


def test_research_ideas_come_from_outside_the_rule_universe_and_say_so():
    wc = WL.watch_cfg({})
    screen = {"LIQ": {"eligible": True, "is_stock": True, "close": 50, "sma50": 40, "chg_21d": 0.5, "adv20_usd": 9e7, "sessions": 300},
              "SMALL": {"eligible": False, "is_stock": True, "close": 8, "sma50": 6, "chg_21d": 0.4, "adv20_usd": 5e6, "sessions": 300, "why_not": "trades under $20M a day"},
              "NEWCO": {"eligible": False, "is_stock": True, "close": 30, "sma50": 25, "chg_21d": 0.2, "adv20_usd": 4e7, "sessions": 45, "why_not": "only 45 sessions of history"},
              "DEAD": {"eligible": False, "is_stock": True, "close": 8, "sma50": 9, "chg_21d": 0.4, "adv20_usd": 5e6, "sessions": 300},
              "PENNY": {"eligible": False, "is_stock": True, "close": 0.5, "sma50": 0.4, "chg_21d": 0.9, "adv20_usd": 5e6, "sessions": 300},
              "LATE": {"eligible": False, "is_stock": True, "close": 8, "sma50": 6, "chg_21d": 0.6, "adv20_usd": 5e6, "sessions": 300,
                       "why_not": "in the rule universe; its bar for this session is published overnight"},
              "ETF": {"eligible": False, "is_stock": False, "close": 8, "sma50": 6, "chg_21d": 0.4, "adv20_usd": 5e6, "sessions": 300}}
    assert WL.find_ideas(screen, wc) == ["SMALL", "NEWCO"]                         # strongest first; rule names and junk left out
    cards = WL.build(screen, lambda r: [], [], TODAY, {})
    by = {c["symbol"]: c for c in cards}
    assert by["SMALL"]["source"] == "idea" and by["SMALL"]["strategy"]["label"] == "Research idea (untested)" and by["SMALL"]["drank"] == 4
    assert "The rules cannot pitch it (trades under $20M a day)" in by["SMALL"]["why"] and "LIQ" not in by


# --------------------------------------------------------------------------- the API
def test_api_for_the_new_pages(desk, monkeypatch, tmp_path):
    for k, v in (("ABG_ALLOW_SYNTHETIC", "true"), ("ABG_PROVIDER_ORDER", "synthetic"), ("ABG_CACHE_DIR", str(tmp_path / "c")),
                 ("ABG_AI_ENABLED", "false"), ("ABG_DATA_DIR", str(tmp_path / "api")), ("ABG_MONITOR_ON_SERVE", "false")):
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient
    from abg.api.server import app
    px = desk.screen_rows()["T04"]["close"]
    with TestClient(app) as c:
        app.state.stock_desk = desk
        try:
            r = c.post("/api/stocks/analyst-pitch", json={"symbol": "T04", "analyst": "Grace", "thesis": "A clean base and improving breadth in its group.",
                                                          "stop": px * 0.95, "t1": px * 1.1})
            assert r.status_code == 200 and r.json()["id"].endswith("-analyst-T04") and r.json()["posted"] == "dashboard"
            assert c.post("/api/stocks/analyst-pitch", json={"symbol": "T04", "analyst": "Grace", "thesis": "A clean base and improving breadth."}).status_code == 409
            assert c.post("/api/stocks/analyst-pitch", json={"symbol": "T04", "analyst": "", "thesis": "x"}).status_code == 400
            assert c.get(f"/api/stocks/pitch/{r.json()['id']}").json()["pack"]["book_label"] == "Analyst pitch"
            sent = []                                                             # the setting posts every analyst pitch; the box posts one

            async def fake_post(url, embeds, content=None, username="Stock Desk", files=None):
                sent.append(embeds[0]["title"])
                return "posted"
            desk._post_to, desk.s.stocks_webhook_url, desk.s.stocks_post_analyst = fake_post, "https://x.test/hook", True
            idea = {"analyst": "Grace", "thesis": "A clean base and improving breadth in its group."}
            assert c.post("/api/stocks/analyst-pitch", json={"symbol": "T06", **idea}).json()["posted"] == "posted"
            desk.s.stocks_post_analyst = False
            assert c.post("/api/stocks/analyst-pitch", json={"symbol": "T08", **idea}).json()["posted"] == "dashboard"
            assert c.post("/api/stocks/analyst-pitch", json={"symbol": "T10", "post": True, **idea}).json()["posted"] == "posted"
            assert len(sent) == 2 and all("Analyst pitch by Grace" in t for t in sent)
            assert c.get("/api/stocks/profile/T04").json()["summary"] and c.get("/api/stocks/profile/ZZZZ").status_code == 404
            nf = c.get("/api/stocks/newsfeed").json()
            assert len(nf["categories"]) == 8 and nf["brief"] and c.get("/api/stocks/newsfeed?category=nope").status_code == 400
            assert c.get("/api/stocks/strategy").json()["books"]
            assert c.post("/api/stocks/portfolio/flow", json={"day": "2024-01-02", "amount": 500}).json()["flow"]["amount"] == 500.0
            assert c.post("/api/stocks/portfolio/flow", json={"amount": "abc"}).status_code == 400
            pf = c.get("/api/stocks/portfolio").json()
            assert {"performance", "breakdown", "review", "news", "flows", "totals", "log"} <= set(pf)
            assert "regime_note" in c.get("/api/stocks/desk").json()
        finally:
            app.state.stock_desk = None
    from types import SimpleNamespace
    from abg.ops import auth

    class Req:
        def __init__(self, method, path, token):
            self.method, self.headers, self.cookies, self.url = method, {"authorization": f"Bearer {token}"}, {}, SimpleNamespace(path=path)
    mw = auth.middleware(lambda r: SimpleNamespace(admin_token="adm", view_token="view"))

    async def ok(_):
        return "passed"
    assert asyncio.run(mw(Req("POST", "/api/stocks/analyst-pitch", "view"), ok)).status_code == 403      # filing a pitch is an admin action
    assert asyncio.run(mw(Req("POST", "/api/stocks/portfolio/flow", "view"), ok)).status_code == 403
    assert asyncio.run(mw(Req("GET", "/api/stocks/newsfeed", "view"), ok)) == "passed"
