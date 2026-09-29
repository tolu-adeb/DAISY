"""Instruments & futures sizing, calendar, portfolio risk gate / prop rules, real-time stream, Discord
commands, auth, backups, broker bridge, entry confirmation, AI parsing, learned models, charts, scheduler."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd
import pytest

from abg.config import Settings
from abg.engine import AnalysisEngine
from abg.extsignals import ExtSignalStore, ExtSignalTracker, Idea, Obs, step
from abg.http import HttpClient
from abg.markets.calendar import Calendar, builtin_events
from abg.markets.instruments import canonical, is_session_open, size_position, spec_for
from test_extsignals import FakeRelay, Market

NY = ZoneInfo("America/New_York")


def S(tmp_path, **kw) -> Settings:
    base = dict(provider_order="market", cache_enabled=False, cache_dir=tmp_path / "c", data_dir=tmp_path / "d",
                ai_enabled=False, max_retries=0, hedge_delay=0.0, anthropic_api_key=None, ext_min_entry_grade="none",
                ext_confirm_timeframe="none", ext_market_filter=False, ext_auto_earnings=False, _env_file=None)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
async def tr(tmp_path):
    s = S(tmp_path)
    m = Market(s)
    eng = AnalysisEngine(s, providers=[m])
    t = ExtSignalTracker(eng, ExtSignalStore.from_settings(s), None, FakeRelay(), s)
    t.market = m
    yield t
    await t.drain()
    t.store.close()
    await eng.aclose()


# =========================================================================== instruments
def test_symbols_and_specs():
    assert canonical("NQ") == "NQ=F" and canonical("NQZ26") == "NQ=F" and canonical("/MNQ") == "MNQ=F"
    assert canonical("CL") == "CL" and canonical("CL", True) == "CL=F" and canonical("/CL") == "CL=F"   # Colgate vs crude
    assert canonical("BTC") == "BTC-USD" and canonical("EURUSD") == "EURUSD=X" and canonical("^TNX") == "^TNX"
    nq, zn = spec_for("NQ"), spec_for("ZN")
    assert (nq.multiplier, nq.tick_value, nq.micro) == (20, 5.0, "MNQ") and zn.tick_value == 15.625
    assert spec_for("TLT").asset_class == "bond_etf" and spec_for("^VIX").asset_class == "volatility"


def test_sizing_futures_micro_and_crypto():
    z = size_position(spec_for("NQ"), 1500, 18250, 18190)          # 60 pts x $20 = $1,200 per contract
    assert z["units"] == 1 and z["risk"] == 1200
    z = size_position(spec_for("NQ"), 500, 18250, 18190)
    assert z["units"] == 0 and "4 MNQ" in z["note"]
    assert size_position(spec_for("BTC-USD"), 100, 60000, 58000)["units"] == pytest.approx(0.05)
    assert size_position(spec_for("AAPL"), 100, 200, 190)["units"] == 10


def test_sessions():
    fut = spec_for("ES")
    assert not is_session_open(fut, datetime(2026, 10, 3, 12, tzinfo=NY))            # Saturday
    assert is_session_open(fut, datetime(2026, 10, 4, 19, tzinfo=NY))               # Sunday evening: Globex open
    assert not is_session_open(fut, datetime(2026, 10, 5, 17, 30, tzinfo=NY))       # daily halt (16:00-17:00 CT)
    assert is_session_open(spec_for("BTC-USD"), datetime(2026, 10, 3, 12, tzinfo=NY))


async def test_futures_idea_sized_in_micros_with_dollar_pnl(tr):
    tr.market.price = 18230.0
    tr.s.ext_account_size = 50_000                                 # $500 risk: 1 NQ (70 pts = $1,400) is too big
    r = await tr.ingest("NQ long 18200-18220 sl 18150 tp 18400, 1-2 day hold", author="desk")
    i = tr.store.get(r["idea"]["id"])
    inst = i.meta["instrument"]
    assert i.symbol == "NQ=F" and inst["contract"] == "MNQ" and i.flags["multiplier"] == 2 and i.shares >= 1
    await tr.on_quotes({"NQ=F": {"price": 18215.0}})
    await tr.on_quotes({"NQ=F": {"price": 18401.0}})
    i = tr.store.get(i.id)
    assert i.realized_pnl > 0 and i.realized_pnl == pytest.approx(
        sum((i.targets[k] - i.entry_price) for k in range(1)) * i.shares * 2, rel=0.6)


# =========================================================================== calendar
def test_calendar_official_dates_and_estimates(tmp_path):
    evs = builtin_events(date(2026, 10, 1), date(2027, 1, 31))
    names = {(e.date, e.name) for e in evs}
    assert ("2026-10-14", "CPI (inflation)") in names and ("2026-10-28", "FOMC rate decision") in names
    assert ("2027-01-27", "FOMC rate decision") in names
    est = [e for e in evs if e.estimated]
    assert all(e.date >= "2027-01-01" for e in est) and ("2027-01-08", "Jobs report (nonfarm payrolls)") in names
    (tmp_path / "calendar.json").write_text(json.dumps([{"date": "2027-01-13", "time": "08:30", "name": "CPI (inflation)"}]))
    evs = builtin_events(date(2027, 1, 1), date(2027, 1, 31), tmp_path / "calendar.json")
    cpi = [e for e in evs if e.name.startswith("CPI")]
    assert [e.date for e in cpi] == ["2027-01-13"] and not cpi[0].estimated          # your date replaces the estimate


def test_blackout_window():
    c = Calendar()
    assert c.blackout(datetime(2026, 10, 14, 8, 10, tzinfo=NY)).name.startswith("CPI")
    assert c.blackout(datetime(2026, 10, 14, 8, 40, tzinfo=NY)) is not None
    assert c.blackout(datetime(2026, 10, 14, 9, 0, tzinfo=NY)) is None
    assert c.blackout(datetime(2026, 10, 28, 13, 45, tzinfo=NY)).name.startswith("FOMC")


async def test_earnings_lookup_finnhub(tmp_path):
    seen = []

    def h(req):
        seen.append(req.url.params.get("symbol"))
        return httpx.Response(200, json={"earningsCalendar": [{"date": "2026-10-29", "hour": "amc", "symbol": "AAPL"}]})
    s = S(tmp_path, finnhub_api_key="k")
    eng = AnalysisEngine(s, providers=[Market(s)], http=HttpClient(transport=httpx.MockTransport(h)))
    try:
        cal = Calendar(eng, s)
        e = await cal.earnings("AAPL")
        assert e["date"] == "2026-10-29" and e["hour"] == "amc"
        await cal.earnings("AAPL")
        assert seen == ["AAPL"]                                      # cached
        assert await cal.earnings("NQ=F") is None                    # no earnings for futures
    finally:
        await eng.aclose()


async def test_macro_blackout_blocks_entry(tr, monkeypatch):
    from abg.markets.calendar import Event
    monkeypatch.setattr(tr.calendar, "blackout", lambda **kw: Event("2026-10-14", "08:30", "CPI (inflation)"))
    r = await tr.ingest("$ABC long 99-101 sl 95 tp 110")
    i = tr.store.get(r["idea"]["id"])
    evs = {e["type"]: e for e in tr.store.events(i.id)}
    assert i.status == "pending" and "CPI" in evs["entry_blocked"]["text"]


# =========================================================================== risk gate
async def test_heat_positions_and_resize(tr):
    tr.s.ext_max_heat_pct = 1.5                                      # $150 of a $10k account
    a = await tr.ingest("$AAA long 99-101 sl 95 tp 110", author="x")   # price 100: inside, enters (~$100 risk)
    assert tr.store.get(a["idea"]["id"]).status == "active"
    b = await tr.ingest("$BBB long 99-101 sl 95 tp 110", author="x")
    bi = tr.store.get(b["idea"]["id"])
    assert bi.status == "active" and "reduced" in bi.flags.get("resized", "") and bi.shares < 16
    tr.s.ext_max_open_positions = 2
    c = await tr.ingest("$CCC long 99-101 sl 95 tp 110", author="x")
    evs = {e["type"]: e for e in tr.store.events(c["idea"]["id"])}
    assert tr.store.get(c["idea"]["id"]).status == "pending" and "already open" in evs["entry_blocked"]["text"]


async def test_correlated_ideas_blocked(tr):
    tr.s.ext_max_correlated = 1                                      # Market fixture: every symbol has the same path
    await tr.ingest("$AAA long 99-101 sl 95 tp 110", author="x")
    b = await tr.ingest("$BBB long 99-101 sl 95 tp 110", author="x")
    evs = {e["type"]: e for e in tr.store.events(b["idea"]["id"])}
    assert "move with BBB" in evs["entry_blocked"]["text"] or "move with" in evs["entry_blocked"]["text"]


async def test_prop_daily_loss_and_warnings(tr):
    s = tr.s
    s.prop_enabled, s.prop_account_size, s.prop_daily_loss_limit, s.prop_max_drawdown = True, 50_000, 1000, 2500
    s.ext_max_heat_pct = 50
    eq = tr.gate.equity()
    assert eq["daily_room"] == pytest.approx(1000) and eq["floor"] == pytest.approx(47_500)
    tr.store.set_kv(f"equity:day:{datetime.now(NY).date().isoformat()}", str(50_950))   # already -950 today
    ok, why, budget = await tr.gate.check(Idea("ABC", "long", "zone", 99, 101, 95, [110], id=99, shares=100), 100)
    assert ok and budget is not None and budget < 100 and "daily loss" in why
    w = tr.gate.limits()
    assert any(x["key"] == "daily" and x["level"] == "warn" for x in w)
    assert not any(x["key"] == "daily" for x in tr.gate.limits())    # once per day and level


# =========================================================================== real-time stream
async def test_price_stream_against_fake_server():
    import websockets
    from abg.live.stream import PriceStream, stream_symbol
    assert stream_symbol("AAPL") == "AAPL" and stream_symbol("BTC-USD") == "BINANCE:BTCUSDT"
    assert stream_symbol("EURUSD=X") == "OANDA:EUR_USD" and stream_symbol("NQ=F") is None
    subs, got = [], []

    async def server(ws):
        async for raw in ws:
            m = json.loads(raw)
            subs.append(m)
            if m["type"] == "subscribe" and m["symbol"] == "AAPL":
                await ws.send(json.dumps({"type": "ping"}))
                await ws.send(json.dumps({"type": "trade", "data": [{"s": "AAPL", "p": 100.0, "t": 1, "v": 1},
                                                                     {"s": "AAPL", "p": 98.5, "t": 2, "v": 1},
                                                                     {"s": "AAPL", "p": 99.2, "t": 3, "v": 1}]}))

    async def on_flush(batch):
        got.append(batch)
    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        st = PriceStream("k", on_flush, flush_seconds=0.1, url=f"ws://127.0.0.1:{port}")
        rest = st.set_symbols(["AAPL", "NQ=F"])
        assert rest == ["NQ=F"]
        task = asyncio.ensure_future(st.run())
        for _ in range(40):
            await asyncio.sleep(0.05)
            if got:
                break
        st.stop()
        await asyncio.wait_for(task, 5)
    b = got[0]["AAPL"]
    assert (b["price"], b["tick_high"], b["tick_low"], b["n"]) == (99.2, 100.0, 98.5, 3)
    assert {"type": "subscribe", "symbol": "AAPL"} in subs


async def test_tick_wick_triggers_stop(tr):
    tr.market.price = 100.0
    r = await tr.ingest("$ABC long 99-101 sl 95 tp 110")
    iid = r["idea"]["id"]
    assert tr.store.get(iid).status == "active"
    await tr.on_quotes({"ABC": {"price": 99.0, "tick_high": 99.5, "tick_low": 94.8}})   # wick through 95 between flushes
    assert tr.store.get(iid).status == "closed" and tr.store.get(iid).close_reason == "stop hit"


# =========================================================================== discord commands
async def test_gateway_commands_and_admin_gate(tr):
    import websockets
    from abg.extsignals.gateway import DiscordGateway
    calls = []

    def h(req):
        calls.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path.endswith("/applications/@me"):
            return httpx.Response(200, json={"id": "app1"})
        if req.url.path.endswith("/channels/C1"):
            return httpx.Response(200, json={"id": "C1", "guild_id": "G1"})
        return httpx.Response(200, json={})
    tr.s.discord_bot_token, tr.s.ext_discord_channel_ids, tr.s.discord_admin_ids = "T", "C1", "42"
    http = HttpClient(transport=httpx.MockTransport(h))

    def inter(iid, sub, user, opts=None):
        return {"op": 0, "t": "INTERACTION_CREATE", "s": 2, "d": {
            "id": iid, "token": f"tok{iid}", "type": 2, "member": {"user": {"id": user, "username": "u"}},
            "data": {"name": "abg", "options": [{"name": sub, "type": 1, "options": opts or []}]}}}

    async def server(ws):
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 30000}}))
        ident = json.loads(await ws.recv())
        assert ident["op"] == 2 and ident["d"]["token"] == "T"
        await ws.send(json.dumps({"op": 0, "t": "READY", "s": 1, "d": {"session_id": "s"}}))
        await ws.send(json.dumps(inter("1", "track", "42", [{"name": "text", "type": 3,
                                                               "value": "$ABC long 99-101 sl 95 tp 110"}])))
        await asyncio.sleep(1.0)                                    # let /track finish before the read-only query
        await ws.send(json.dumps(inter("2", "cancel", "7", [{"name": "id", "type": 4, "value": 1}])))
        await ws.send(json.dumps(inter("3", "ideas", "7")))
        await asyncio.sleep(3)

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        gw = DiscordGateway(tr.s, http, tr, url=f"ws://127.0.0.1:{port}")
        task = asyncio.ensure_future(gw.run())
        for _ in range(80):
            await asyncio.sleep(0.05)
            if gw.handled >= 3:
                break
        gw.stop()
        await asyncio.wait_for(task, 10)
    await http.aclose()
    assert ("PUT", "/api/v10/applications/app1/guilds/G1/commands") in [(m, p) for m, p, _ in calls]
    edits = {p.split("/")[-3]: b for m, p, b in calls if m == "PATCH"}
    assert "tracking" in edits["tok1"]["embeds"][0]["description"]
    assert edits["tok2"]["embeds"][0]["title"] == "Not allowed"                    # user 7 isn't an admin
    acks = [b for m, p, b in calls if p.endswith("/callback")]
    assert acks[1]["data"] == {"flags": 64}                                        # the refusal is ephemeral
    assert "#1 ABC" in edits["tok3"]["embeds"][0]["description"]
    assert tr.store.get(1).status != "cancelled"


# =========================================================================== auth / backup / broker
def test_api_tokens(monkeypatch, tmp_path):
    for k, v in {"ABG_ALLOW_SYNTHETIC": "true", "ABG_PROVIDER_ORDER": "synthetic", "ABG_CACHE_DIR": str(tmp_path),
                 "ABG_AI_ENABLED": "false", "ABG_DATA_DIR": str(tmp_path / "data"), "ABG_MONITOR_ON_SERVE": "false",
                 "ABG_ADMIN_TOKEN": "admin-secret", "ABG_VIEW_TOKEN": "view-secret"}.items():
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient
    from abg.api.server import app
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200                               # always open
        assert c.get("/api/ext/ideas").status_code == 401
        v = {"Authorization": "Bearer view-secret"}
        assert c.get("/api/ext/ideas", headers=v).status_code == 200
        assert c.post("/api/ext/parse", json={"text": "x"}, headers=v).status_code == 403
        a = {"Authorization": "Bearer admin-secret"}
        assert c.post("/api/ext/parse", json={"text": "$NVDA long 1-2 sl 0.5 tp 3"}, headers=a).status_code == 200
        r = c.post("/api/auth/login", json={"token": "view-secret"})
        assert r.status_code == 200 and r.json()["role"] == "view" and "abg_token" in r.cookies
        assert c.get("/api/auth/me").json()["role"] == "view"                         # cookie now carries the role
        assert c.post("/api/auth/login", json={"token": "nope"}).status_code == 401


def test_backup_and_retention(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    con = sqlite3.connect(d / "signals.sqlite3")
    con.execute("create table t(x)")
    con.execute("insert into t values (1)")
    con.commit()
    (d / "backups" / "2020-01-01").mkdir(parents=True)
    from abg.ops.backup import backup_databases
    res = backup_databases(d, keep_days=14)
    copy = sqlite3.connect(res["dir"] + "/signals.sqlite3")
    assert copy.execute("select x from t").fetchone() == (1,) and "2020-01-01" in res["removed"]


async def test_alpaca_bridge_mirrors_events(tmp_path):
    from abg.brokers.alpaca import PAPER, AlpacaBroker
    calls = []

    def h(req):
        calls.append((req.method, str(req.url), json.loads(req.content) if req.content else None))
        return httpx.Response(200, json={"id": f"o{len(calls)}"})
    s = S(tmp_path, broker="alpaca_paper", alpaca_key_id="k", alpaca_secret_key="s")
    http = HttpClient(transport=httpx.MockTransport(h))
    b = AlpacaBroker(s, http)
    i = Idea("ABC", "long", "zone", 99, 101, 95, [105, 110], id=5, shares=20, meta={"instrument": {"asset_class": "stock"}})
    step(i, Obs(time.time(), 100, 100, 100))
    assert (await b.on_event(i, {"type": "entry", "fills": [{"frac": 1.0}], "ts": 1})).startswith("ok")
    assert calls[0][1] == f"{PAPER}/v2/orders" and calls[0][2]["qty"] == "20" and calls[1][2]["type"] == "stop"
    step(i, Obs(time.time(), 105.5, 105.5, 105.5))
    await b.on_event(i, {"type": "target_hit", "fraction": 0.5, "target_index": 0, "ts": 2})
    assert any(c[0] == "DELETE" for c in calls) and any(c[2] and c[2].get("qty") == "10" and c[2]["side"] == "sell"
                                                        for c in calls)
    fut = Idea("NQ=F", "long", "zone", 1, 2, 0.5, [3], id=6, meta={"instrument": {"asset_class": "future"}})
    assert (await b.on_event(fut, {"type": "entry"})).startswith("skipped")
    await http.aclose()


# =========================================================================== confirmation, AI, learning, charts
async def test_one_hour_confirmation(tr):
    from abg.extsignals.confirm import confirm_entry
    tr.s.ext_confirm_timeframe = "1h"
    i = Idea("ABC", "long", "zone", 98, 100, 95, [110], id=1)
    idx = pd.date_range(end=pd.Timestamp.utcnow().floor("h") - pd.Timedelta(hours=2), periods=30, freq="h")
    c = np.linspace(105, 99.4, 30)
    df = pd.DataFrame({"open": c + 0.2, "high": c + 0.4, "low": c - 0.3, "close": c, "volume": 1e5}, index=idx)
    tr._intraday = {("ABC", "1h"): (time.time(), df)}
    ok, why = await confirm_entry(tr, i, 99.4)
    assert not ok and "waiting" in why
    df2 = df.copy()
    df2.iloc[-1, df2.columns.get_loc("open")] = 99.0
    df2.iloc[-1, df2.columns.get_loc("close")] = 100.2                        # bullish bar closing above the prior high
    df2.iloc[-1, df2.columns.get_loc("high")] = 100.3
    tr._intraday = {("ABC", "1h"): (time.time(), df2)}
    ok, why = await confirm_entry(tr, i, 100.2)
    assert ok and "confirmation" in why
    i.flags["zone_touched_at"] = time.time() - 30 * 3600
    tr._intraday = {("ABC", "1h"): (time.time(), df)}
    assert (await confirm_entry(tr, i, 99.4))[0]                              # waited too long: enter anyway


async def test_ai_parse_rejects_invented_numbers(tmp_path):
    from abg.extsignals.ai import SignalAI
    reply = {"signals": [{"symbol": "NVDA", "direction": "long", "entry_type": "zone", "entry_low": 117.5,
                          "entry_high": 119, "stop": 112, "targets": [130]},
                         {"symbol": "AMD", "direction": "long", "entry_type": "zone", "entry_low": 150, "entry_high": 152,
                          "stop": 140, "targets": [999]}]}

    def h(req):
        return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(reply)}]})
    s = S(tmp_path, anthropic_api_key="k", ai_enabled=True)
    http = HttpClient(transport=httpx.MockTransport(h))
    ai = SignalAI(s, http)
    out = await ai.parse("thinking NVDA here, buying 117.5 to 119, out under 112, looking for 130. AMD 150-152 stop 140")
    await http.aclose()
    assert [p.symbol for p in out] == ["NVDA"] and out[0].meta["parsed_by"] == "ai"       # AMD's 999 wasn't in the post


def test_grade_model_learns_signal():
    from abg.extsignals.learn import FEATURES, GradeModel
    rng = np.random.default_rng(0)
    rows = []
    for k in range(600):
        f = {n: 0.0 for n in FEATURES}
        f["trend_aligned"] = float(rng.random() < 0.5)
        f["rr_first"] = float(rng.uniform(0.5, 3))
        r = (0.8 if f["trend_aligned"] else -0.6) + rng.normal(0, 0.6)
        rows.append({"ts": k, "features": f, "r": r})
    m = GradeModel.train(rows)
    assert m["metrics"]["test_auc"] > 0.75 and m["importance"][0]["feature"] == "trend_aligned"
    gm = GradeModel(m)
    good = gm.predict({**{n: 0.0 for n in FEATURES}, "trend_aligned": 1.0, "rr_first": 2.0})
    bad = gm.predict({**{n: 0.0 for n in FEATURES}, "trend_aligned": 0.0, "rr_first": 2.0})
    assert gm.usable and good["exp_r"] > bad["exp_r"] and good["grade"] in "AB" and bad["grade"] == "D"


def test_risk_model_trains_and_assesses():
    from abg.risk.features import TIMESERIES_FEATURES, FeatureVector
    from abg.risk.interface import RiskContext
    from abg.risk.learned import LearnedRiskModel, train_risk_model
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2012-01-02", periods=2500)
    fr = pd.DataFrame({f: rng.normal(size=len(idx)) for f in TIMESERIES_FEATURES}, index=idx)
    fr["vol_60d"] = rng.uniform(0.1, 0.9, len(idx))
    fr["y_fwd_max_loss_21d"] = np.where(rng.random(len(idx)) < fr["vol_60d"] * 0.5, 0.15, 0.03)
    fr["y_fwd_vol_21d"] = fr["vol_60d"] + rng.normal(0, 0.05, len(idx))
    fr["symbol"] = "X"
    m = train_risk_model(fr)
    assert m["metrics"]["test_auc"] > 0.6 and m["metrics"]["test_vol_corr"] > 0.8
    model = LearnedRiskModel(m)
    fv = FeatureVector("X", {f: 0.0 for f in TIMESERIES_FEATURES} | {"vol_60d": 0.85}, datetime.now())
    a = model.assess(fv, RiskContext("X", pd.DataFrame(), pd.Series(dtype=float)))
    low = model.assess(FeatureVector("X", {f: 0.0 for f in TIMESERIES_FEATURES} | {"vol_60d": 0.12}, datetime.now()),
                       RiskContext("X", pd.DataFrame(), pd.Series(dtype=float)))
    assert a.score > low.score and a.metrics["prob_drawdown_10pct_21d"] > low.metrics["prob_drawdown_10pct_21d"]


def test_chart_png():
    pytest.importorskip("matplotlib")
    from abg.extsignals.charts import render
    idx = pd.bdate_range(end=date.today(), periods=100)
    c = np.linspace(90, 110, 100)
    df = pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c + 0.3, "volume": 1e6}, index=idx)
    i = Idea("ABC", "long", "zone", 99, 101, 95, [110, 120], id=3, targets_hit=[], flags={"added_targets": [110]})
    png = render(i, df)
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


async def test_weekly_recap_posts_once(tr):
    from abg.live.scheduler import Scheduler
    posted = []

    async def post(title, lines, sev="info", key="", embed=None):
        posted.append((title, lines))
    tr.post_portfolio = post
    await tr.ingest("$ABC long 99-101 sl 95 tp 110")
    await tr.on_quotes({"ABC": {"price": 110.5}})
    mon = type("M", (), {"s": tr.s, "ext": tr, "engine": tr.engine, "last_quote_sweep": time.time(), "stream": None})()
    sch = Scheduler(mon)
    await sch.weekly_recap()
    assert posted and posted[0][0].startswith("📊") and "1 closed" in posted[0][1][0]
    assert sch._once("recap:x") and not sch._once("recap:x")
