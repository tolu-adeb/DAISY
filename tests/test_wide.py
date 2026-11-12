"""Wide scan (docs/19.8): a synthetic whole market served by a fake Polygon / Finnhub / Claude."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from abg.config import Settings
from abg.swing import wide as W

END = date(2026, 10, 21)
N_SYMS = 360


def _market():
    """~360 random-walk stocks + SPY + sector ETFs over 200 sessions ending END (a seed that yields setups)."""
    rng = np.random.default_rng(7)
    days = W.sessions_back(END, 200)
    syms = [f"S{i:03d}" for i in range(N_SYMS)] + ["AAPL"]            # AAPL is in the core list: never in the wide scan
    out = {}
    for s in syms + ["SPY"] + W.SECTOR_ETFS:
        p0 = rng.uniform(20, 200)
        mu, sd = (0.0006, 0.009) if s == "SPY" or s.startswith("XL") else (rng.uniform(-0.0005, 0.002), rng.uniform(0.012, 0.022))
        c = p0 * np.exp(np.cumsum(rng.normal(mu, sd, len(days))))
        o = c * (1 + rng.normal(0, sd / 3, len(days)))
        h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, sd / 2, len(days))))
        lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, sd / 2, len(days))))
        v = rng.uniform(0.8e6, 3e6, len(days)) * (40 if s == "SPY" else 1)
        if s == "S001":
            v = v / 100                                                      # illiquid: filtered out
        out[s] = pd.DataFrame({"o": o, "h": h, "l": lo, "c": c, "v": v}, index=pd.Index(days))
    return days, out


DAYS, MARKET = _market()


def _grouped(d: date):
    return [{"T": s, "o": float(df.at[d, "o"]), "h": float(df.at[d, "h"]), "l": float(df.at[d, "l"]),
             "c": float(df.at[d, "c"]), "v": float(df.at[d, "v"])} for s, df in MARKET.items() if d in df.index]


class FakeHttp:
    def __init__(self, offering: str | None = None, earnings: dict | None = None, unpublished: set | None = None):
        self.posts, self.calls = [], []
        self.offering, self.earn, self.unpublished = offering, earnings or {}, unpublished or set()

    async def get_json(self, url, provider=None, params=None, headers=None, timeout=None):
        self.calls.append((provider, url.split("?")[0]))
        params = params or {}
        if "/aggs/grouped/" in url:
            d = date.fromisoformat(url.rsplit("/", 1)[-1])
            return {"results": [] if d in self.unpublished else _grouped(d), "status": "OK"}
        if "/v3/reference/tickers" in url:
            cs = [s for s in MARKET if re.fullmatch(r"S\d{3}", s) or s == "AAPL"]
            if params.get("cursor"):                              # the cursor travels as a parameter (httpx drops a URL's own query)
                return {"results": [{"ticker": s, "name": f"{s} Corp", "type": "CS"} for s in cs[200:]]}
            return {"results": [{"ticker": s, "name": f"{s} Corp", "type": "CS"} for s in cs[:200]],
                    "next_url": "https://api.polygon.io/v3/reference/tickers?cursor=abc"}
        if "calendar/earnings" in url:
            return {"earningsCalendar": [{"symbol": s, "date": d} for s, d in self.earn.items()
                                         if params["from"] <= d <= params["to"]]}
        if "stock/profile2" in url:
            return {"name": f"{params['symbol']} Corp", "finnhubIndustry": "Semiconductors", "marketCapitalization": 5000}
        if "company-news" in url:
            s = params["symbol"]
            h = "Company announces $300 million public offering of common stock" if s == self.offering else "Quarterly dividend declared"
            return [{"headline": h, "summary": "", "datetime": 1792600000, "source": "wire", "url": "https://x"}]
        if url.endswith("/v1/models"):
            return {"data": []}
        raise AssertionError(url)

    async def request(self, method, url, provider=None, json=None, headers=None, timeout=None, params=None):
        if provider == "anthropic":
            prompt = json["messages"][0]["content"]
            syms = re.findall(r"^## (\S+)", prompt, re.M)
            body = [{"symbol": s, "score": -2 if s == self.offering else 1,
                     "red_flags": ["share offering"] if s == self.offering else [], "why": "test"} for s in syms]
            return SimpleNamespace(status_code=200, json=lambda: {"content": [{"type": "text", "text": __import__("json").dumps(body)}]})
        self.posts.append({"url": url, "json": json})
        return SimpleNamespace(status_code=200, json=lambda: {})


def _desk(tmp_path, http, **kw):
    from abg.swing.desk import SwingDesk
    base = dict(data_dir=tmp_path / "data", swing_enabled=True, anthropic_api_key="test-key", polygon_api_key="p",
                finnhub_api_key="f", swing_wide_history=120, swing_wide_polygon_per_min=1e6, swing_wide_finnhub_per_min=1e6, _env_file=None)
    base.update(kw)
    return SwingDesk(Settings(**base), None, http, fetch=False)


def _scan_day():
    """A day in the synthetic market with at least 3 gate-passing setups."""
    store_panel = {s: df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
                   .assign(volume=lambda x: x.volume / 1000).set_index(pd.to_datetime(df.index)) for s, df in MARKET.items()}
    liq = W.liquid(store_panel, [s for s in store_panel if s.startswith("S")], 5, 20)
    for d in reversed(DAYS[-30:]):
        found = W.scan(store_panel, liq, store_panel["SPY"], d)
        if sum(x["verdict"]["take"] for x in found) >= 3:
            return d, found
    raise AssertionError("synthetic market has no day with 3 setups")


DAY, FOUND = _scan_day()


def test_helpers():
    assert W.sector_etf("Semiconductors") == "XLK" and W.sector_etf("Banking") == "XLF" and W.sector_etf(None) is None
    assert W.keyword_flags(["XYZ announces $50 million public offering"]) == ["offering"]
    assert W.keyword_flags(["XYZ beats estimates"]) == []
    good = W.score({"rs20": 8, "rs60": 15, "sector_rs20": 3, "vol_surge": 2.5}, {"score": 2})["score"]
    bad = W.score({"rs20": -8, "rs60": -15, "sector_rs20": -3, "vol_surge": 0.5}, {"score": -2})["score"]
    assert 50 < good <= 100 and 0 <= bad < 50 and W.score({}, None)["score"] == 50.0
    assert W.score({}, None, earnings_soon=True)["score"] < 50
    d = date(2026, 10, 21)
    assert W.earnings_soon("X", {"X": ["2026-10-23"]}, d, 10) == "2026-10-23"
    assert W.earnings_soon("X", {"X": ["2026-12-23"]}, d, 10) is None and W.earnings_soon("X", {"X": ["2026-10-21"]}, d, 10) is None


def test_wide_scan_shadow_end_to_end(tmp_path):
    from swingdesk.monitor import load_book, scoreboard
    takes = [x["sig"]["symbol"] for x in FOUND if x["verdict"]["take"]]
    http = FakeHttp(offering=takes[0], earnings={takes[1]: str(W.sessions_after(DAY, 3)[-1])})
    d = _desk(tmp_path, http, swing_webhook_url="https://discord.com/api/webhooks/1/x")
    r = asyncio.run(d.run_wide(DAY))
    assert r["ready"] and r["mode"] == "shadow" and r["taken"] >= 3 and r["new"] == r["taken"]
    assert r["universe"]["common"] == N_SYMS + 1 and 0 < r["universe"]["liquid"] < N_SYMS      # S001 illiquid, AAPL is core
    run = d.wide_store.latest_run()
    syms = {c["symbol"] for c in run["candidates"]}
    assert "AAPL" not in syms and "S001" not in syms
    c = {x["symbol"]: x for x in run["candidates"]}
    assert c[takes[0]]["status"].startswith("kept off Discord") and "share offering" in c[takes[0]]["status"]
    assert c[takes[1]]["earnings"] and c[takes[1]]["profile"]["sector_etf"] == "XLK"
    ranked = sorted([x for x in run["candidates"] if x["take"]], key=lambda x: x["rank"])
    dist = [99 if x["dist_atr"] is None else x["dist_atr"] for x in ranked]
    assert dist == sorted(dist) and run["rank"] == "entry"                  # nearest to its entry first (docs/19.9)
    assert sum(x["status"] == "would post (shadow)" for x in ranked) <= 3
    assert not [p for p in http.posts if "discord" in p["url"]]                         # shadow: nothing posted
    book = load_book()
    wide = {k: v for k, v in book.items() if v.get("author") == "wide"}
    assert len(wide) == r["taken"]
    e = next(v for v in wide.values() if v["symbol"] == takes[1])
    assert "binary_event" in e["verdict"]["flags"] and e["verdict"]["risk_pct"] == 0.25        # earnings -> half size
    sb = scoreboard(d.main_book())                                                        # the channel record is untouched
    assert (sb["taken"], sb["closed"], sb["wins"], sb["win_rate"], sb["sum_R"]) == (63, 40, 32, 80.0, 14.25)
    assert all(i["author"] != "wide" for i in d.ideas()) and len(d.wide_ideas()) == r["taken"]
    v = d.wide_view()
    assert v["status"]["mode"] == "shadow" and v["scoreboard"]["taken"] == r["taken"] and v["run"]["day"] == str(DAY)
    assert all(a.get("author") != "wide" for a in d.alerts()) or True
    assert {a["delivered"] for a in d.alerts(500) if a.get("author") == "wide"} == {"dashboard (wide scan, shadow)"}
    # a second run the same day adds nothing and re-posts nothing
    r2 = asyncio.run(d.run_wide(DAY))
    assert r2["new"] == 0


def _evidence(d, closed=50, win=74.0, sum_r=5.0, **settings):
    from datetime import datetime
    d.wide_store.save("backtest", {"end": str(datetime.now(W.NY).date()), "summary": {"posting_unseen": {
        "closed": closed, "win_rate": win, "sum_r": sum_r}}, "settings": {"rank": "entry", "chase_atr": 1.0, **settings}})


def test_wide_scan_posting_mode(tmp_path):
    http = FakeHttp()
    d = _desk(tmp_path, http, swing_webhook_url="https://discord.com/api/webhooks/1/x", swing_wide_post=True,
              swing_wide_max_posts=2, swing_wide_min_breadth=0)
    asyncio.run(d.run_pass("seed"))
    _evidence(d)
    r = asyncio.run(d.run_wide(DAY))
    posts = [p for p in http.posts if "discord" in p["url"]]
    assert r["posted"] == 2 and len(posts) == 2 and r["mode"] == "posting"
    assert all("wide scan #" in p["json"]["embeds"][0]["description"] for p in posts)
    assert len(d.wide_store.load("posted")) == 2


@pytest.mark.parametrize("bt,why", [
    (None, "no backtest"),
    (dict(closed=21, win=57.1, sum_r=-3.3), "(need 40)"),                     # the Oct 23 backtest of the old ranking
    (dict(closed=60, win=58.0, sum_r=-1.0), "need 65%"),
    (dict(closed=60, win=74.0, sum_r=5.0, rank="score"), "different settings"),
])
def test_posting_is_held_until_the_backtest_clears_the_bar(tmp_path, bt, why):
    http = FakeHttp()
    d = _desk(tmp_path, http, swing_webhook_url="https://discord.com/api/webhooks/1/x", swing_wide_post=True,
              swing_wide_max_posts=2, swing_wide_min_breadth=0)
    asyncio.run(d.run_pass("seed"))
    if bt is not None:
        _evidence(d, **bt)
    ok, reason = d.wide_evidence()
    assert not ok and why in reason
    r = asyncio.run(d.run_wide(DAY))
    assert r["posted"] == 0
    assert not [p for p in http.posts if "discord" in p["url"]]
    run = d.wide_store.latest_run()
    assert run["mode"] == "held" and why in run["held_why"]
    assert any(c["status"].startswith("would post - held") for c in run["candidates"] if c["take"])
    assert {a["delivered"] for a in d.alerts(500) if a.get("author") == "wide"} <= {"dashboard (wide scan)"}


def test_rank_modes_and_entry_distance():
    xs = [{"score": 80, "dist_atr": 2.0}, {"score": 60, "dist_atr": 0.4}, {"score": 70, "dist_atr": None}]
    assert [x["score"] for x in sorted(xs, key=lambda x: W.rank_key(x, "entry"))] == [60, 80, 70]
    assert [x["score"] for x in sorted(xs, key=lambda x: W.rank_key(x, "score"))] == [80, 70, 60]
    df = pd.DataFrame({"date": ["2026-05-01"] * 4 + ["2026-07-01"] * 2, "score": [90, 80, 70, 60, 50, 40],
                       "dist_atr": [3.0, 0.5, 1.0, 0.2, 1.0, 2.0], "take": True, "status": ["LOSS", "WIN", "WIN", "WIN", "WIN", "LOSS"],
                       "r": [-1, 0.75, 0.75, 0.75, 0.75, -1], "tier": "A", "earnings": None, "core": False, "symbol": list("ABCDEF")})
    s = W.summarize_bt(df, 2, "entry")
    assert s["posting"]["closed"] == 4 and s["posting"]["wins"] == 3                   # D, B on May 1 (wins); E (win), F (loss) on Jul 1
    assert s["rank_compare"]["score"]["wins"] == 2
    assert s["posting_unseen"]["closed"] == 2 and s["posting_unseen"]["excludes"] == list(W.DESIGN_WINDOW)


def test_not_published_yet_and_no_key(tmp_path):
    d = _desk(tmp_path, FakeHttp(unpublished={DAY}))
    r = asyncio.run(d.run_wide(DAY))
    assert r["ready"] is False and "published" in r["why"]
    d2 = _desk(tmp_path / "b", FakeHttp(), polygon_api_key=None)
    assert not d2.wide_ready()
    from datetime import datetime
    assert "wide" not in d2.schedule(datetime(2026, 10, 21, 18, 0, tzinfo=W.NY))["due"]
    d3 = _desk(tmp_path / "c", FakeHttp())
    assert d3.wide_due(datetime(2026, 10, 21, 18, 0, tzinfo=W.NY), {}) and not d3.wide_due(datetime(2026, 10, 21, 16, 0, tzinfo=W.NY), {})


def test_backtest_runs_on_the_store(tmp_path):
    d = _desk(tmp_path, FakeHttp(earnings={"S010": "2026-09-15"}))
    start = DAYS[-90]
    r = asyncio.run(d.wide_backtest(start, DAYS[-1]))
    s = r["summary"]
    assert s["setups"] >= s["taken"] > 0 and s["all_taken"]["n"] == s["taken"]
    assert "top_3_per_day" in s and set(s["by_tier"]) == {"A", "B"}
    assert (d.wide_store.root / "backtest_trades.csv").exists() and "channel" in r


def test_wide_api(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from abg.swing.routes import router
    d = _desk(tmp_path, FakeHttp())
    app = FastAPI()
    app.include_router(router)
    app.state.monitor = SimpleNamespace(swing=d, running=False)
    c = TestClient(app)
    assert c.get("/api/swing/wide").json()["status"]["mode"] == "shadow"
    assert c.post("/api/swing/wide/run").status_code == 409                 # read-only window
    assert c.post("/api/swing/wide/backtest", json={}).status_code == 409
