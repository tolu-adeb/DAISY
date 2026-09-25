"""External signals: parser, lifecycle state machine, tracker orchestration, Discord in/out, API."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd
import pytest

from abg.config import Settings
from abg.engine import AnalysisEngine
from abg.extsignals import ExtSignalStore, ExtSignalTracker, Idea, Obs, parse, step, summary_stats
from abg.extsignals import commentary as cm
from abg.extsignals.discord import DiscordPoller, DiscordRelay, message_text
from abg.extsignals.lifecycle import trigger_fill
from abg.http import HttpClient
from abg.models import Capability, PriceHistory, Quote
from abg.providers.base import Provider


# =========================================================================== parser
@pytest.mark.parametrize("text,sym,d,et,lo,hi,stop,tg", [
    ("$NVDA swing long 🟢 entry zone 117.50-119, SL 112, TP1 130 TP2 138", "NVDA", "long", "zone", 117.5, 119, 112, [130, 138]),
    ("BUY AAPL @ 180 - 182 | Stop: 175 (daily close) | Targets: 190 / 195 / 200", "AAPL", "long", "zone", 180, 182, 175, [190, 195, 200]),
    ("Short SPY below 505, stop 512, target 490, 480", "SPY", "short", "breakdown_below", 505, 505, 512, [490, 480]),
    ("TSLA breakout over 252 -> 265 / 280, invalidation 244", "TSLA", "long", "breakout_above", 252, 252, 244, [265, 280]),
])
def test_parser_formats(text, sym, d, et, lo, hi, stop, tg):
    p = parse(text)
    assert p.kind == "idea" and p.trackable
    assert (p.symbol, p.direction, p.entry_type) == (sym, d, et)
    assert (p.entry_low, p.entry_high, p.stop) == (lo, hi, stop)
    assert p.targets == tg


def test_parser_close_basis_and_updates():
    assert parse("BUY AAPL @ 180 - 182 | Stop: 175 (daily close) | Targets: 190").stop_basis == "close"
    u = parse("TP1 hit on NVDA, moving stop to breakeven")
    assert (u.kind, u.action, u.symbol, u.target_index) == ("update", "target_hit", "NVDA", 0)
    assert parse("AAPL stopped out").action == "stop_hit"
    assert parse("NVDA: raise stop to 121").new_stop == 121
    assert parse("good morning everyone").kind == "none"


# =========================================================================== lifecycle
def mk(**kw) -> Idea:
    base = dict(symbol="X", direction="long", entry_type="zone", entry_low=98.0, entry_high=100.0, stop=95.0,
                targets=[105.0, 110.0], id=1, stop_initial=95.0, shares=10)
    base.update(kw)
    return Idea(**base)


def q(price, ts=None):
    return Obs(ts or time.time(), price, price, price)


def test_zone_entry_partials_breakeven_and_close():
    i = mk()
    assert step(i, q(103)) == []
    ev = step(i, q(101.2))
    assert [e["type"] for e in ev] == ["approaching"] and step(i, q(101.1)) == []      # heads-up only once
    ev = step(i, q(99.5))
    assert [e["type"] for e in ev] == ["entry"] and i.entry_price == 99.5 and i.status == "active"
    ev = step(i, q(105.4))
    assert [e["type"] for e in ev] == ["target_hit", "stop_moved"]
    assert ev[0]["price"] == 105.0                        # resting limit fills at the target on live quotes
    assert i.stop == 99.5 and i.remaining == pytest.approx(0.5)
    ev = step(i, q(99.4))
    assert [e["type"] for e in ev] == ["breakeven_stop"] and i.status == "closed"
    assert i.realized_r == pytest.approx(0.5 * 5.5 / 4.5 - 0.5 * 0.1 / 4.5, rel=1e-6)


def test_final_target_and_trailing_stop():
    i = mk(targets=[105.0, 110.0, 115.0])
    step(i, q(99))
    step(i, q(110.5))                                      # TP1 + TP2 in one jump, stop trails to TP1
    assert i.targets_hit == [0, 1] and i.stop == 105.0
    ev = step(i, q(104))
    assert ev[0]["type"] == "trailing_stop" and i.status == "closed" and i.realized_r > 0


def test_bar_stop_checked_before_target_and_gap_fills():
    i = mk()
    step(i, q(99))
    ev = step(i, Obs(time.time(), 100, 106, 94, open=100, bar=True))   # both touched in one bar: assume the stop
    assert [e["type"] for e in ev] == ["stop_hit"] and ev[0]["price"] == 95
    j = mk()
    step(j, q(99))
    ev = step(j, Obs(time.time(), 93, 94, 92, open=93, bar=True))      # gapped below the stop: fill at the open
    assert ev[0]["price"] == 93


def test_pre_entry_outcomes():
    i = mk()
    assert step(i, Obs(time.time(), 94, 96, 94, open=94, bar=True))[0]["type"] == "invalidated"   # gapped through stop
    j = mk()
    assert step(j, q(106))[0]["type"] == "missed"                                                  # ran to TP1 first
    k = mk(expires_at=time.time() - 1)
    assert step(k, q(103))[0]["type"] == "expired"


def test_short_breakdown_and_close_basis_stop():
    i = mk(direction="short", entry_type="breakdown_below", entry_low=50.0, entry_high=50.0, stop=53.0,
           stop_initial=53.0, targets=[46.0], stop_basis="close")
    assert trigger_fill(i, q(50.5)) is None
    step(i, q(49.8))
    assert i.status == "active" and i.entry_price == 49.8
    assert step(i, q(53.5)) == []                           # close-basis: intraday poke above the stop is ignored
    ev = step(i, Obs(time.time(), 53.2, 53.5, 52, open=52, bar=True))
    assert ev[0]["type"] == "stop_hit" and ev[0]["price"] == 53.2


def test_entry_blocked_then_allowed():
    i = mk()
    ev = step(i, q(99), allow_entry=False)
    assert [e["type"] for e in ev] == ["entry_blocked"] and i.status == "pending"
    assert step(i, q(99), allow_entry=False) == []          # reported once
    assert step(i, q(99))[0]["type"] == "entry"


def test_summary_stats_and_grade_gate():
    a, b = mk(), mk()
    for x, px in ((a, 105.2), (b, 94)):
        step(x, q(99))
        step(x, q(px))
    step(a, q(99))                                           # a: TP1 then breakeven -> small win
    st = summary_stats([a, b, mk(status="rejected")])
    assert st["closed"] == 2 and st["wins"] == 1 and st["losses"] == 1 and st["rejected"] == 1 and st["ideas"] == 2
    assert cm.grade_ok("B", "C") and not cm.grade_ok("D", "C") and cm.grade_ok("D", "none")


# =========================================================================== tracker (scripted market)
class Market(Provider):
    name = "market"
    label = "Scripted market"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTE})

    def __init__(self, settings, price=100.0):
        super().__init__(settings, http=None)
        self.price = price
        self.bars: pd.DataFrame | None = None

    async def get_quote(self, symbol):
        return Quote(symbol=symbol, price=self.price, source=self.name, prev_close=self.price)

    async def get_history(self, symbol, start, end, interval="1d"):
        if self.bars is not None:
            return PriceHistory.from_frame(symbol, self.bars, self.name, interval)
        idx = pd.bdate_range(end=end - timedelta(days=1), periods=500)
        r = np.random.default_rng(3).normal(0.0004, 0.012, len(idx))
        c = self.price * np.exp(np.cumsum(r) - np.cumsum(r)[-1])
        df = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6}, index=idx)
        return PriceHistory.from_frame(symbol, df, self.name, interval)


class FakeRelay:
    name, enabled = "fake", True

    def __init__(self):
        self.sent = []

    async def send(self, idea, kind, x):
        self.sent.append((kind, x["title"]))
        return "ok", f"m{len(self.sent)}"

    def describe(self):
        return {"enabled": True}


@pytest.fixture
async def tracker(tmp_path):
    s = Settings(provider_order="market", cache_enabled=False, cache_dir=tmp_path / "c", data_dir=tmp_path / "d",
                 ai_enabled=False, max_retries=0, hedge_delay=0.0, anthropic_api_key=None, ext_min_entry_grade="none",
                 _env_file=None)
    m = Market(s)
    eng = AnalysisEngine(s, providers=[m])
    tr = ExtSignalTracker(eng, ExtSignalStore.from_settings(s), None, FakeRelay(), s)
    tr.market = m
    yield tr
    await tr.drain()
    tr.store.close()
    await eng.aclose()


async def test_tracker_ingest_enter_targets_and_relay(tracker):
    tr = tracker
    r = await tr.ingest("$ABC swing long entry zone 96-98, SL 92, TP1 104 TP2 108", author="desk")
    assert r["outcome"] == "tracking"
    i = r["idea"]
    assert i["status"] == "pending" and i["grade"] in "ABCD" and i["shares"] == 16   # $100 risk / $6 per share (zone top 98 - stop 92)
    assert i["expires_at"] > time.time() + 40 * 86400
    await tr.on_quotes({"ABC": {"price": 97.0}})
    await tr.on_quotes({"ABC": {"price": 104.5}})
    idea = tr.store.get(i["id"])
    assert idea.status == "active" and idea.targets_hit == [0] and idea.stop == 97.0
    types = [e["type"] for e in reversed(tr.store.events(i["id"]))]
    assert types == ["ingested", "entry", "target_hit", "stop_moved"]
    ev = tr.store.events(i["id"])[-2]                       # the entry event carries the explanation
    assert "ENTRY ABC LONG" in ev["title"] and "Why:" in ev["text"] and ev["data"]["explain"]["plan"]
    await tr.drain()
    assert [k for k, _ in tr.relay.sent] == types and tr.store.get(i["id"]).relay_ref == "m1"
    assert all(e["relayed"].get("fake") == "ok" for e in tr.store.events(i["id"]))


async def test_tracker_updates_duplicates_and_rejections(tracker):
    tr = tracker
    r = await tr.ingest("$ABC long 95-97 sl 91 tp 105", author="desk")
    iid = r["idea"]["id"]
    assert (await tr.ingest("$ABC long 95-97 sl 91 tp 105", author="desk"))["outcome"] == "duplicate"
    u = await tr.ingest("ABC: raise stop to 93", author="desk")
    assert u["outcome"] == "updated" and tr.store.get(iid).stop == 93 and tr.store.get(iid).stop_initial == 93
    assert (await tr.ingest("$ABC long 10-11 sl 9 tp 12"))["outcome"] == "rejected"          # 90% from price
    assert (await tr.ingest("$ABC long 99-100 sl 101 tp 105"))["outcome"] == "rejected"      # stop above entry
    assert (await tr.ingest("hello team"))["outcome"] == "ignored"
    assert (await tr.ingest("stop to breakeven", author="nobody"))["outcome"] == "unmatched"
    assert (await tr.ingest("moving stop to 94", author="desk"))["outcome"] == "updated"     # no ticker: desk's only open idea
    assert tr.store.get(iid).stop == 94
    c = await tr.ingest("cancel the ABC long", author="desk")
    assert c["outcome"] == "updated" and tr.store.get(iid).status == "cancelled"
    assert tr.stats()["rejected"] == 2


async def test_tracker_defaults_market_entry_and_source_close(tracker):
    tr = tracker
    r = await tr.ingest("$ABC long here, target 110", author="desk")
    i = tr.store.get(r["idea"]["id"])
    assert i.status == "active" and i.entry_price == 100 and i.flags.get("default_stop") and i.stop < 100
    tr.market.price = 103
    res = await tr.ingest("closing ABC here", author="desk")
    i = tr.store.get(i.id)
    assert res["outcome"] == "updated" and i.status == "closed" and i.realized_pct == pytest.approx(3.0)


async def test_grade_gate_blocks_entry(tracker):
    tr = tracker
    tr.s.ext_min_entry_grade = "A"
    r = await tr.ingest("$ABC long 98-101 sl 97 tp 101.5", author="desk")     # price already in the zone, poor R:R
    i = tr.store.get(r["idea"]["id"])
    assert i.grade != "A" and i.status == "pending" and i.flags.get("blocked_at")
    assert "entry_blocked" in [e["type"] for e in tr.store.events(i.id)]


async def test_catch_up_replays_missed_daily_bars(tracker):
    tr = tracker
    r = await tr.ingest("$ABC long 94-95 sl 90 tp 103", author="desk")
    i = tr.store.get(r["idea"]["id"])
    i.last_checked_at = i.created_at = time.time() - 6 * 86400
    tr.store.save(i)
    today = datetime.now(ZoneInfo("America/New_York")).date()          # the tracker's "today" is the NY session date
    days = pd.bdate_range(end=today - timedelta(days=1), periods=4)
    tr.market.bars = pd.DataFrame({"open": [100, 97, 95.5, 97], "high": [101, 98, 97, 103.5],
                                   "low": [98, 96, 94.2, 96.5], "close": [99, 97, 96, 99.5], "volume": 1e6}, index=days)
    n = await tr.catch_up()
    i = tr.store.get(i.id)
    assert n >= 2 and i.status == "closed" and i.entry_price == 95 and i.targets_hit == [0]
    assert any("daily bar" in e["text"] for e in tr.store.events(i.id))


# =========================================================================== discord
async def test_discord_poller_ingests_and_relays(tmp_path):
    posts = []
    msgs = [{"id": "11", "content": "", "author": {"id": "u1", "username": "caller"},
             "embeds": [{"title": "New swing", "description": "$ABC long entry 96-98 SL 92 TP 104"}]},
            {"id": "12", "content": "gm", "author": {"id": "u1", "username": "caller"}},
            {"id": "13", "content": "raise stop to 94", "author": {"id": "u1", "username": "caller"},
             "message_reference": {"message_id": "11", "channel_id": "C1"}}]

    def handler(req: httpx.Request):
        if req.url.path.endswith("/users/@me"):
            assert req.headers["authorization"] == "Bot T"
            return httpx.Response(200, json={"id": "bot1", "username": "abg"})
        if req.url.path.endswith("/channels/C1/messages"):
            after = req.url.params.get("after")
            if after is None:
                return httpx.Response(200, json=[{"id": "10", "content": "old", "author": {"id": "u1"}}])
            return httpx.Response(200, json=[m for m in msgs if int(m["id"]) > int(after)][::-1])
        if req.url.path.endswith("/channels/C1"):
            return httpx.Response(200, json={"id": "C1", "name": "swing-alerts"})
        if "webhooks" in req.url.path:
            posts.append(json.loads(req.content))
            return httpx.Response(200, json={"id": f"r{len(posts)}"})
        return httpx.Response(404)

    s = Settings(provider_order="market", cache_enabled=False, cache_dir=tmp_path / "c", data_dir=tmp_path / "d",
                 ai_enabled=False, max_retries=0, discord_bot_token="T", ext_discord_channel_ids="C1",
                 ext_relay_webhook_url="https://discord.com/api/webhooks/999/abc", ext_min_entry_grade="none", _env_file=None)
    http = HttpClient(transport=httpx.MockTransport(handler))
    eng = AnalysisEngine(s, providers=[Market(s)], http=http)
    relay = DiscordRelay(s, http)
    tr = ExtSignalTracker(eng, ExtSignalStore.from_settings(s), None, relay, s)
    poller = DiscordPoller(s, http, tr, relay)
    try:
        assert (await poller.poll_once())["messages"] == 0          # first start: cursor set to "now", history skipped
        assert tr.store.get_kv("discord:C1:after") == "10"
        st = await poller.poll_once()
        assert st == {"messages": 3, "ideas": 1, "updates": 1, "ignored": 1}
        idea = tr.store.ideas()[0]
        assert idea.channel_name == "#swing-alerts" and idea.author == "caller" and idea.stop == 94
        await tr.drain()
        assert len(posts) == 2 and posts[0]["embeds"][0]["title"].startswith("📥 Tracking ABC")
        assert posts[0]["allowed_mentions"] == {"parse": []} and any(f["name"] == "Why" for f in posts[0]["embeds"][0]["fields"])
        assert tr.store.get(idea.id).relay_ref == "r1"
        assert (await poller.poll_once())["messages"] == 0          # nothing new
        assert poller._own({"webhook_id": "999"})                  # never re-ingests our own relays
    finally:
        tr.store.close()
        await eng.aclose()


def test_message_text_flattens_embeds_and_forwards():
    m = {"content": "hi", "embeds": [{"title": "T", "fields": [{"name": "SL", "value": "5"}]}],
         "message_snapshots": [{"message": {"content": "fwd $X long 1-2"}}]}
    assert message_text(m) == "hi\nT\nSL: 5\nfwd $X long 1-2"


# =========================================================================== API
def test_ext_api(monkeypatch, tmp_path):
    for k, v in {"ABG_ALLOW_SYNTHETIC": "true", "ABG_PROVIDER_ORDER": "synthetic", "ABG_CACHE_DIR": str(tmp_path),
                 "ABG_AI_ENABLED": "false", "ABG_DATA_DIR": str(tmp_path / "data"), "ABG_MONITOR_ON_SERVE": "false",
                 "ABG_EXT_MIN_ENTRY_GRADE": "none"}.items():
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient
    from abg.api.server import app
    with TestClient(app) as c:
        p = c.post("/api/ext/parse", json={"text": "$NVDA long 117-119 sl 112 tp 130"}).json()
        assert p["trackable"] and p["parsed"]["symbol"] == "NVDA"
        price = c.get("/api/quote/AAPL").json()["quote"]["price"]
        r = c.post("/api/ext/ingest", json={"text": f"$AAPL long {price * .95:.2f}-{price * .97:.2f} sl {price * .9:.2f} "
                                                   f"tp {price * 1.1:.2f}", "author": "desk"}).json()
        assert r["outcome"] == "tracking"
        iid = r["idea"]["id"]
        lst = c.get("/api/ext/ideas?status=open").json()
        assert [i["id"] for i in lst["ideas"]] == [iid] and lst["stats"]["pending"] == 1
        d = c.get(f"/api/ext/ideas/{iid}").json()
        assert d["events"][0]["type"] == "ingested"
        e = c.post(f"/api/ext/ideas/{iid}/edit", json={"stop": round(price * .92, 2)}).json()
        assert e["idea"]["stop"] == round(price * .92, 2)
        assert c.post(f"/api/ext/ideas/{iid}/close", json={}).status_code == 400          # not entered yet
        assert c.post(f"/api/ext/ideas/{iid}/cancel").json()["idea"]["status"] == "cancelled"
        assert c.get("/api/ext/ideas/999").status_code == 404
        assert c.get("/api/ext/status").json()["discord"]["enabled"] is False


async def test_monitor_tracks_external_ideas_without_a_portfolio(tmp_path):
    from abg.live import Monitor, NotificationHub
    from abg.portfolio import PortfolioStore
    s = Settings(provider_order="market", cache_enabled=False, cache_dir=tmp_path / "c", data_dir=tmp_path / "d",
                 ai_enabled=False, max_retries=0, hedge_delay=0.0, ext_min_entry_grade="none", notify_desktop=False,
                 _env_file=None)
    m = Market(s)
    eng = AnalysisEngine(s, providers=[m])
    store = PortfolioStore.from_settings(s)
    hub = NotificationHub(s, store, eng.http)
    mon = Monitor(eng, store, hub)
    try:
        r = await mon.ext.ingest("$ABC long 96-98 sl 92 tp 104", author="desk")
        m.price = 97.0
        await mon.quote_sweep()
        i = mon.ext.store.get(r["idea"]["id"])
        assert i.status == "active" and i.last_price == 97.0
        kinds = [x["kind"] for x in store.signals(mon.pf, limit=10)]
        assert "ext_entry" in kinds and "ext_ingested" in kinds          # also in the dashboard signal feed
        assert mon.status()["external"]["active"] == 1
    finally:
        await hub.aclose()
        mon.ext.store.close()
        store.close()
        await eng.aclose()
