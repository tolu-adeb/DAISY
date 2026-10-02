"""Alert routing (Alerio-style, dry run): parsing, per-account rules, sizing, brackets, replay."""
from datetime import datetime, timedelta

import pandas as pd

from abg.routes import AccountState, Route, RouteEngine, RouteRules, parse_alert
from abg.routes.engine import NY
from abg.routes.replay import as_copied_route, load_export, replay

T = datetime(2026, 10, 1, 9, 50, tzinfo=NY)
TM = ("SIGNAL VALIDATED #1 — SHORT\n**Entry zone:** 30,709 – 30,709 · optimal **30,709**\n**Stop loss:** 30,792\n"
      "**Target 1:** 30,651\n**Final target:** 30,543\n**Price now:** 30,709")


def test_parse_tradingmind_embed_and_plain_text():
    a = parse_alert(TM, ts=T)
    assert (a.action, a.side, a.entry, a.stop, a.targets, a.price) == ("entry", -1, 30709, 30792, [30651, 30543], 30709)
    b = parse_alert("BUY NQ 30,650.50 SL 30,600 TP 30,700, 30,760")
    assert (b.side, b.entry, b.stop, b.targets) == (1, 30650.5, 30600, [30700, 30760])
    assert parse_alert("long NQ 30650 sl 30610 tp 30690 30740").entry == 30650
    assert parse_alert("TARGET 1 HIT — bank half, stop to break-even").action == "breakeven"
    assert parse_alert("move stop to 30,690").new_stop == 30690
    assert parse_alert("STOPPED OUT #1 -83 pts").action == "close"
    assert parse_alert("cancel that idea").action == "cancel"
    assert parse_alert("gm, watching 30,700").action == "info"


def test_wide_stop_is_skipped_or_tightened_and_sized_by_risk():
    a = parse_alert(TM, ts=T)
    d = RouteEngine(Route("g")).on_alert(a, now=T)[0]
    assert d.verdict == "skipped" and "83 pts" in d.reasons[0]
    d = RouteEngine(Route("g", rules=RouteRules(stop_cap_mode="tighten"))).on_alert(a, now=T)[0]
    assert d.verdict == "placed" and d.contracts == 1 and d.orders[1]["price"] == 30769       # 60-pt stop, $200 budget
    d = RouteEngine(Route("g", rules=RouteRules(max_stop_pts=100, risk_per_trade_usd=1000, daily_loss_limit_usd=0))).on_alert(a, now=T)[0]
    assert d.contracts == 5                                   # 1000 // (83*2 + 1.24)
    copy = RouteEngine(as_copied_route(8)).on_alert(a, now=T)[0]
    assert copy.verdict == "placed" and copy.contracts == 8


def test_day_rules_stale_chase_and_blackout():
    r = Route("g", rules=RouteRules(max_stop_pts=100))
    eng = RouteEngine(r)
    a = parse_alert(TM, ts=T - timedelta(minutes=5))
    assert "old" in eng.on_alert(a, now=T)[0].reasons[0]                       # late alert
    a = parse_alert(TM, ts=T)
    assert "past the entry" in eng.on_alert(a, now=T, price=30670)[0].reasons[0]  # price ran 39 pts
    t2 = T.replace(hour=10, minute=45)
    assert "blackout" in eng.on_alert(parse_alert(TM, ts=t2), now=t2)[0].reasons[0]
    eng.accounts["lucid"].roll(T.date())
    eng.accounts["lucid"].lost_today = True
    assert "first-loss" in eng.on_alert(parse_alert(TM, ts=T), now=T)[0].reasons[0]


def test_daily_loss_room_shrinks_size():
    r = Route("g", rules=RouteRules(max_stop_pts=100, risk_per_trade_usd=2000, daily_loss_limit_usd=500, max_contracts=8))
    acct = AccountState("lucid")
    acct.roll(T.date())
    acct.day_pnl = -300                                      # 200 of room left
    d = RouteEngine(r, {"lucid": acct}).on_alert(parse_alert(TM, ts=T), now=T)[0]
    assert d.verdict == "placed" and d.contracts == 1 and "today's loss room" in d.reasons[-1]


def test_updates_only_tighten_the_stop():
    eng = RouteEngine(Route("g", rules=RouteRules(max_stop_pts=100, risk_per_trade_usd=1000)))
    eng.on_alert(parse_alert(TM, ts=T, id="m1"), now=T)
    d = eng.on_alert(parse_alert("move stop to 30,900", ts=T, ref="m1"), now=T)[0]
    assert d.verdict == "noop" and "widen" in d.reasons[0]
    d = eng.on_alert(parse_alert("move stop to 30,750", ts=T, ref="m1"), now=T)[0]
    assert d.verdict == "updated" and eng.accounts["lucid"].positions["m1"].stop == 30750


def _bars(prices):
    idx = pd.date_range("2026-10-01 09:30", periods=len(prices), freq="5min", tz=NY)
    return pd.DataFrame([{"open": o, "high": h, "low": l, "close": c, "volume": 1} for o, h, l, c in prices], index=idx)


def test_replay_fills_brackets_on_bars():
    flat = [(30710, 30715, 30700, 30710)] * 5                 # 09:30-09:54; the entry comes at 09:50:05
    up = [(30710, 30800, 30705, 30795)]                       # runs through the stop
    msgs = [{"ts": T + timedelta(seconds=5), "id": "m1", "ref": None, "text": TM}]
    bars = _bars(flat + up + flat)
    copy = replay(msgs, bars, as_copied_route(8))["summary"]
    guarded = replay(msgs, bars, Route("g", rules=RouteRules(stop_cap_mode="tighten")))["summary"]
    assert copy["worst_trade_usd"] < -1300 and -130 < guarded["worst_trade_usd"] < -115


def test_load_export(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"messages": [{"id": "1", "timestamp": "2026-10-01T13:50:05+00:00", "content": "", '
                 '"embeds": [{"title": "SIGNAL VALIDATED #1 — SHORT", "description": "**Stop loss:** 30,792"}]}]}')
    m = load_export(p)
    assert m[0]["ts"].hour == 9 and "SIGNAL VALIDATED" in m[0]["text"]
