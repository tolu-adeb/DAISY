"""Saved portfolio, live signals, notifications, monitor and their API (all offline)."""
import asyncio
import json
from datetime import date, datetime

import httpx
import pandas as pd
import pytest

from abg.engine import AnalysisEngine, splice_quote
from abg.http import HttpClient
from abg.live import DiscordNotifier, EmailNotifier, Monitor, MonitorBusy, NotificationHub, Signal, SignalEngine
from abg.live.market_hours import NY, holidays, is_market_open, next_open
from abg.models import Quote
from abg.portfolio import PortfolioError, PortfolioStore, snapshot


@pytest.fixture
def store(tmp_path):
    s = PortfolioStore(tmp_path / "pf.sqlite3")
    yield s
    s.close()


# ---------------------------------------------------------------- store
def test_average_cost_and_realized_pnl(store):
    store.add_transaction("main", "AAPL", "BUY", 10, 100, ts=1)
    store.add_transaction("main", "AAPL", "BUY", 10, 200, fees=10, ts=2)      # cost 3010 / 20 = 150.5
    store.add_transaction("main", "AAPL", "SELL", 5, 180, fees=5, ts=3)       # realized 5*(180-150.5)-5 = 142.5
    [p] = store.positions("main")
    assert p.shares == 15 and p.avg_cost == pytest.approx(150.5) and p.realized_pnl == pytest.approx(142.5)
    assert store.realized_pnl("main") == pytest.approx(142.5)
    with pytest.raises(PortfolioError):
        store.add_transaction("main", "AAPL", "SELL", 16, 180, ts=4)          # oversell
    store.add_transaction("main", "AAPL", "SELL", 15, 190, ts=5)
    assert store.positions("main") == [] and len(store.positions("main", include_closed=True)) == 1


def test_delete_that_breaks_history_is_refused(store):
    store.add_transaction("main", "X", "BUY", 5, 10, ts=1)
    store.add_transaction("main", "X", "SELL", 5, 12, ts=2)
    with pytest.raises(PortfolioError):
        store.delete_transaction("main", 1)
    assert store.delete_transaction("main", 2) is True


def test_watchlist_rules_symbols_and_backup(store, tmp_path):
    store.add_transaction("main", "msft", "BUY", 1, 300)
    store.watch("main", "nvda")
    store.add_rule("main", "TSLA", "price_above", 300)
    store.set_position_meta("main", "MSFT", stop_loss=250)
    assert store.symbols("main") == ["MSFT", "NVDA", "TSLA"]
    with pytest.raises(PortfolioError):
        store.add_rule("main", "TSLA", "moon", 1)
    data = store.export("main")
    other = PortfolioStore(tmp_path / "other.sqlite3")
    res = other.import_(json.loads(json.dumps(data)), "copy")
    assert res["transactions"] == 1 and other.positions("copy")[0].stop_loss == 250
    assert other.symbols("copy") == ["MSFT", "NVDA", "TSLA"]
    other.close()


def test_store_persists_across_restart(tmp_path):
    a = PortfolioStore(tmp_path / "p.sqlite3")
    a.add_transaction("main", "AAPL", "BUY", 3, 100)
    a.close()
    b = PortfolioStore(tmp_path / "p.sqlite3")
    assert b.positions("main")[0].shares == 3
    b.close()


# ---------------------------------------------------------------- market calendar
def test_holidays_2026_and_2027():
    h26 = holidays(2026)
    for d in (date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
              date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)):
        assert d in h26, d
    h27 = holidays(2027)
    assert {date(2027, 3, 26), date(2027, 6, 18), date(2027, 7, 5), date(2027, 12, 24)} <= h27
    assert date(2027, 12, 31) not in holidays(2027)          # Sat New Year 2028 is not observed on Friday


def test_market_open_and_next_open():
    assert is_market_open(datetime(2026, 9, 24, 10, 0, tzinfo=NY))
    assert not is_market_open(datetime(2026, 9, 24, 16, 0, tzinfo=NY))
    assert not is_market_open(datetime(2026, 11, 26, 11, 0, tzinfo=NY))    # Thanksgiving
    assert next_open(datetime(2026, 9, 25, 17, 0, tzinfo=NY)) == datetime(2026, 9, 28, 9, 30, tzinfo=NY)
    assert next_open(datetime(2026, 4, 2, 18, 0, tzinfo=NY)).date() == date(2026, 4, 6)   # over Good Friday


# ---------------------------------------------------------------- live bar splicing
def _bars():
    idx = pd.to_datetime(["2026-09-22", "2026-09-23"])
    return pd.DataFrame({"open": [100, 101], "high": [102, 103], "low": [99, 100], "close": [101, 102], "volume": [1e6, 1e6]},
                        index=idx)


def test_splice_quote_append_update_reject():
    ts = datetime(2026, 9, 24, 11, 0, tzinfo=NY)
    q = Quote("X", 104.0, "t", prev_close=102, open=102.5, day_high=104.5, day_low=102.0, volume=5e5, timestamp=ts)
    df, act = splice_quote(_bars(), q)
    assert act == "appended" and df.index[-1] == pd.Timestamp("2026-09-24") and df["close"].iloc[-1] == 104
    q2 = Quote("X", 105.0, "t", day_high=105.5, volume=7e5, timestamp=ts)
    df2, act2 = splice_quote(df, q2)
    assert act2 == "updated" and len(df2) == 3 and df2["high"].iloc[-1] == 105.5
    _, act3 = splice_quote(_bars(), Quote("X", 300.0, "t", timestamp=ts))
    assert act3 == "rejected"
    _, act4 = splice_quote(_bars(), Quote("X", 102.0, "t", timestamp=datetime(2026, 9, 21, 12, tzinfo=NY)))
    assert act4 is None


# ---------------------------------------------------------------- signal engine
def _report(label="Neutral", score=0, rsi=50, hist=0.1, sma50=100, sma200=90, level="Moderate", plays=None):
    return {"quote": {"price": 105}, "signal": {"label": label, "score": score},
            "indicators": {"rsi_14": rsi, "macd_hist": hist, "sma_50": sma50, "sma_200": sma200},
            "plays": plays or [], "risk": [{"model": "baseline", "level": level, "score": 40, "drivers": []}],
            "sentiment": {"score": 0.0, "articles": 5}, "news": []}


def test_transitions_fire_only_on_change(settings, store):
    se = SignalEngine(store, settings)
    assert se.from_report("AAPL", _report()) == []                      # first observation: silent
    sigs = se.from_report("AAPL", _report(label="Strong Bullish", score=60, rsi=75, hist=-0.2, sma50=80, level="High",
                                          plays=[{"name": "Momentum Breakout", "direction": "long", "confidence": 0.8,
                                                  "levels": {"entry": 105, "stop": 100, "target_1": 112, "target_2": 120}}]))
    kinds = {s.kind for s in sigs}
    assert {"signal_change", "setup", "rsi", "macd_cross", "ma_cross", "risk_change"} <= kinds
    assert next(s for s in sigs if s.kind == "signal_change").severity == "warning"
    for s in sigs:
        store.save_signal(s.to_dict() | {"portfolio": "main"})
    assert se.from_report("AAPL", _report(label="Strong Bullish", score=60, rsi=75, hist=-0.2, sma50=80, level="High",
                                          plays=[{"name": "Momentum Breakout", "direction": "long", "confidence": 0.8}])) == []


def test_cooldown_blocks_flapping(settings, store):
    se = SignalEngine(store, settings)
    se.from_report("A", _report(label="Neutral"))
    s1 = se.from_report("A", _report(label="Bullish"))
    for s in s1:
        store.save_signal(s.to_dict())
    se.from_report("A", _report(label="Neutral"))
    s3 = se.from_report("A", _report(label="Bullish"))                     # same key again within cooldown
    assert [s.kind for s in s1] == ["signal_change"] and not [s for s in s3 if s.kind == "signal_change"]


def test_quote_bands_stops_and_rules(settings, store):
    store.add_transaction("main", "MSFT", "BUY", 10, 100)
    store.set_position_meta("main", "MSFT", stop_loss=90, take_profit=130)
    store.add_rule("main", "MSFT", "price_below", 95)
    se = SignalEngine(store, settings)
    pos = store.positions("main")[0]
    s1 = se.from_quote("MSFT", {"price": 97, "change_pct": -3.2, "prev_close": 100.2}, None, pos)
    assert [s.kind for s in s1] == ["big_move"]
    s2 = se.from_quote("MSFT", {"price": 89, "change_pct": -11.2, "prev_close": 100.2}, None, pos)
    kinds = [s.kind for s in s2]
    assert "big_move" in kinds and "stop_hit" in kinds and "position_loss" in kinds and "custom_rule" in kinds
    assert next(s for s in s2 if s.kind == "stop_hit").severity == "critical"
    assert store.rules("main")[0].enabled is False                          # one-shot rule disabled
    s3 = se.from_quote("MSFT", {"price": 88.5, "change_pct": -11.5, "prev_close": 100.2}, None, pos)
    assert s3 == []                                                         # nothing new: same bands, stop already hit


def test_level_break(settings, store):
    se = SignalEngine(store, settings)
    lv = {"resistance": [110, 120], "support": [95, 90]}
    assert se.from_quote("Z", {"price": 100, "change_pct": 0.1}, lv) == []
    s = se.from_quote("Z", {"price": 111, "change_pct": 0.5}, lv)
    assert s and s[0].kind == "level_break" and s[0].direction == "bullish"


# ---------------------------------------------------------------- notifiers
async def test_discord_payload_and_retry(settings):
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0.01"}, json={"retry_after": 0.01})
        return httpx.Response(204)
    d = DiscordNotifier("https://discord.com/api/webhooks/x/y", HttpClient(transport=httpx.MockTransport(handler)))
    sig = Signal("AAPL", "stop_hit", "k", "critical", "bearish", "STOP HIT: AAPL", "msg", {"price": 1.5, "stop": 2})
    assert await d.send(sig) == "ok" and len(calls) == 2
    emb = calls[-1]["embeds"][0]
    assert emb["title"].startswith("[CRITICAL]") and {f["name"] for f in emb["fields"]} == {"Price", "Stop"}


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, u, p):
        self.user = u

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


async def test_email_batches_and_sends_critical_immediately(settings, monkeypatch):
    import abg.live.notify as n
    monkeypatch.setattr(n.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    s = settings.model_copy(update={"smtp_host": "smtp.test", "smtp_user": "me@x.com", "smtp_password": "pw",
                                    "email_to": "a@x.com,b@x.com"})
    e = EmailNotifier(s, "info", batch_seconds=0.2)
    await e.send(Signal("A", "k", "1", "warning", "bullish", "A up", "m"))
    await e.send(Signal("B", "k", "1", "info", "bearish", "B down", "m"))
    assert FakeSMTP.sent == []
    await asyncio.sleep(0.4)
    assert len(FakeSMTP.sent) == 1 and "2 ABG signals" in FakeSMTP.sent[0]["Subject"]
    assert FakeSMTP.sent[0]["To"] == "a@x.com, b@x.com"
    await e.send(Signal("C", "stop_hit", "1", "critical", "bearish", "STOP HIT: C", "m"))
    assert len(FakeSMTP.sent) == 2 and FakeSMTP.sent[1]["Subject"].startswith("[CRITICAL]")


async def test_hub_persists_broadcasts_and_isolates_failures(settings, store):
    class Boom:
        name, min_severity = "boom", "info"

        async def send(self, sig):
            raise RuntimeError("webhook down")

        async def aclose(self):
            pass

    class Ok:
        name, min_severity = "ok", "warning"
        got = []

        async def send(self, sig):
            Ok.got.append(sig)
            return "ok"

        async def aclose(self):
            pass
    hub = NotificationHub(settings.model_copy(update={"notify_desktop": False}), store, None, extra=[Boom(), Ok()])
    q = hub.subscribe()
    await hub.publish(Signal("A", "k", "1", "info", "neutral", "info sig", "m"))
    await hub.publish(Signal("A", "k", "2", "warning", "neutral", "warn sig", "m"))
    await hub.aclose()
    assert [x["data"]["title"] for x in [q.get_nowait(), q.get_nowait()]] == ["info sig", "warn sig"]
    assert [s.title for s in Ok.got] == ["warn sig"]                  # min severity respected
    rows = store.signals("main")
    assert rows[0]["delivered"]["ok"] == "ok" and rows[0]["delivered"]["boom"].startswith("error")


def test_price_cross_rearms_and_ignores_chop(settings, store):
    r = store.add_rule("main", "XYZ", "price_cross", 100, one_shot=False)
    se = SignalEngine(store, settings)                  # re-arm buffer 0.2% -> 100.20 / 99.80
    assert se.price_rules("XYZ", 98) == []              # first look only records the side
    assert se.price_rules("XYZ", 99.5) == []
    [up] = se.price_rules("XYZ", 100.1)
    assert up.kind == "price_cross" and up.direction == "bullish" and "crossed above 100.00" in up.title
    assert se.price_rules("XYZ", 99.95) == []           # chop around the line: no ping-pong alerts
    assert se.price_rules("XYZ", 100.05) == []
    assert se.price_rules("XYZ", 101) == []             # clear of the level -> re-armed
    [down] = se.price_rules("XYZ", 99)
    assert down.direction == "bearish" and "Repeating" in down.message
    assert store.rules("main")[0].enabled                 # repeating rule stays on
    assert store.rule_dicts("main")[0]["state"]["side"] == "below"
    assert store.delete_rule("main", r.id) and store.get_state("main", "XYZ", f"rule:{r.id}") is None


def test_directional_cross_seeding_and_wicks(settings, store):
    se = SignalEngine(store, settings)
    up = store.add_rule("main", "AAA", "price_cross_up", 100)
    hint = store.seed_cross(up, 105)                      # already above: needs to come back below first
    assert "back below first" in hint
    assert se.price_rules("AAA", 101) == []
    assert se.price_rules("AAA", 99) == []               # a cross down doesn't fire an up-only rule
    [sig] = se.price_rules("AAA", 100.4)
    assert sig.direction == "bullish" and store.rules("main", "AAA")[0].enabled is False   # one-shot
    assert se.price_rules("AAA", 90) == [] and se.price_rules("AAA", 110) == []            # disabled now

    dn = store.add_rule("main", "BBB", "price_cross_down", 50)
    store.seed_cross(dn, 52)
    [w] = se.price_rules("BBB", 51, high=52.5, low=49.5)  # a streamed batch poked through and bounced
    assert w.data["wick"] and "touched 49.50" in w.message

    store.add_rule("main", "CCC", "price_above", 50)
    [hit] = se.price_rules("CCC", 49.8, high=50.3)        # plain threshold rules use the tick high too
    assert hit.kind == "custom_rule"


def test_sweep_quote_can_leave_price_rules_to_the_stream(settings, store):
    store.add_rule("main", "DDD", "price_cross", 10)
    store.add_rule("main", "DDD", "change_above", 5)
    se = SignalEngine(store, settings)
    se.price_rules("DDD", 9)
    out = se.from_quote("DDD", {"price": 11, "change_pct": 6}, price_rules=False)
    assert [s.kind for s in out if s.kind in ("custom_rule", "price_cross")] == ["custom_rule"]   # change rule only
    assert [s.kind for s in se.price_rules("DDD", 11)] == ["price_cross"]


async def test_monitor_fires_cross_alerts_from_ticks(settings, store):
    store.watch("main", "EEE")
    store.add_rule("main", "EEE", "price_cross", 20)
    async with AnalysisEngine(settings) as eng:
        hub = NotificationHub(settings.model_copy(update={"notify_desktop": False}), store, eng.http)
        mon = Monitor(eng, store, hub)
        await mon._on_ticks({"EEE": {"price": 19.5, "tick_high": 19.6, "tick_low": 19.4, "ts": 0}})
        await mon._on_ticks({"EEE": {"price": 20.4, "tick_high": 20.5, "tick_low": 19.9, "ts": 0}})
        sigs = store.signals("main", 10)
        assert [x["kind"] for x in sigs] == ["price_cross"] and mon.signals_emitted == 1
        await hub.aclose()


# ---------------------------------------------------------------- monitor
async def test_monitor_sweeps_and_single_instance_lock(settings, store):
    store.add_transaction("main", "AAPL", "BUY", 10, 1.0)
    store.set_position_meta("main", "AAPL", take_profit=2.0)
    store.watch("main", "MSFT")
    async with AnalysisEngine(settings) as eng:
        hub = NotificationHub(settings.model_copy(update={"notify_desktop": False}), store, eng.http)
        mon = Monitor(eng, store, hub)
        sigs = await mon.analysis_sweep()
        assert {"AAPL", "MSFT"} <= set(mon.analysis) and mon.last_snapshot["totals"]["positions"] == 1
        assert any(s.kind == "target_hit" for s in sigs)
        # a second monitor on the same data dir must refuse to start while the first holds the lock
        assert mon.lock.acquire()
        other = Monitor(eng, store, hub)
        other.lock.me = {"pid": -1, "host": "elsewhere"}
        with pytest.raises(MonitorBusy):
            await other.run()
        mon.lock.release()
        # run the loop briefly, poke it, stop it
        task = asyncio.ensure_future(mon.run())
        await asyncio.sleep(0.5)
        mon.poke(analysis=True)
        await asyncio.sleep(0.5)
        mon.stop()
        await asyncio.wait_for(task, 5)
        assert mon.sweeps >= 2 and not mon.running and mon.lock.holder() is None
        await hub.aclose()


async def test_snapshot_totals(settings, store):
    store.add_transaction("main", "AAPL", "BUY", 2, 50)
    async with AnalysisEngine(settings) as eng:
        snap = await snapshot(eng, store, "main")
    h = snap["holdings"][0]
    assert h["market_value"] == pytest.approx(2 * h["price"]) and h["weight_pct"] == pytest.approx(100)
    assert snap["totals"]["unrealized_pnl"] == pytest.approx(h["market_value"] - 100)


# ---------------------------------------------------------------- API
def test_portfolio_api_and_sse(monkeypatch, tmp_path):
    for k, v in {"ABG_ALLOW_SYNTHETIC": "true", "ABG_PROVIDER_ORDER": "synthetic", "ABG_CACHE_DIR": str(tmp_path / "c"),
                 "ABG_DATA_DIR": str(tmp_path / "d"), "ABG_AI_ENABLED": "false", "ABG_MONITOR_ON_SERVE": "false",
                 "ABG_NOTIFY_DESKTOP": "false"}.items():
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient
    from abg.api.server import app
    with TestClient(app) as c:
        assert c.post("/api/portfolio/transactions", json={"symbol": "aapl", "side": "buy", "shares": 3, "price": 100,
                                                            "date": "2026-01-05"}).status_code == 200
        assert c.post("/api/portfolio/transactions", json={"symbol": "MSFT", "side": "BUY", "shares": 2}).json()["price"] > 0
        r = c.post("/api/portfolio/transactions", json={"symbol": "MSFT", "side": "SELL", "shares": 99})
        assert r.status_code == 400 and r.json()["error"]["code"] == "portfolio_error"
        assert c.post("/api/portfolio/watchlist", json={"symbol": "nvda"}).json() == {"symbol": "NVDA"}
        assert c.put("/api/portfolio/positions/AAPL", json={"stop_loss": 10_000}).json()["ok"]
        assert c.post("/api/alerts", json={"symbol": "NVDA", "kind": "price_above", "value": 1}).status_code == 200
        cr = c.post("/api/alerts", json={"symbol": "NVDA", "kind": "price_cross", "value": 1}).json()
        assert "above 1" in cr["hint"] and "crosses down" in cr["hint"]
        assert next(r for r in c.get("/api/alerts").json()["rules"] if r["id"] == cr["id"])["state"]["side"] == "above"
        snap = c.get("/api/portfolio").json()
        assert snap["totals"]["positions"] == 2 and snap["watchlist"][0]["symbol"] == "NVDA" and snap["risk"]["available"]
        run = c.post("/api/monitor/refresh").json()
        assert run["mode"] == "ran one sweep"
        sigs = c.get("/api/signals").json()
        assert {"stop_hit", "custom_rule"} <= {s["kind"] for s in sigs}
        assert c.post("/api/signals/ack").json()["acknowledged"] >= 2
        assert c.get("/api/portfolio/export").json()["format"] == "abg-portfolio-v1"
        tx = c.get("/api/portfolio/transactions").json()
        assert len(tx) == 2
        # the monitor/status endpoint works while stopped
        assert c.get("/api/monitor").json()["running"] is False
