"""Alerio integration: snapshot building from its API shapes, audit flags, the mirror route, replay and the live shadow."""
import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd

from abg.routes.alerio import (audit, breach_odds, build_snapshot, compare, guarded_route, mark_rejections, mirror_route,
                               signal_alerts)
from abg.routes.engine import NY

SIG = ("· SIGNAL — take the SELL 🔴 SIGNAL VALIDATED #1 — SHORT **Entry zone:** 30,703.75 – 30,761.25 · optimal **30,703.75** "
       "**Stop loss:** 30,761.5 (~58 pts) **Target 1:** 30,636.5 · _move stop_ **Final target:** 30,512.25 **Price now:** 30,677.75 "
       "⚠️ **Price is 26 pts past the entry.**")
T1 = "· TARGET 1 — move your stop to break-even 🔵 TARGET 1 HIT #1 Target 1 hit — **+67.2 pts**. **MOVE your stop to 30,703.75**"
BE = "· BREAK-EVEN ⚪ BREAK-EVEN #1 Closed at break-even — **+34.0 pts**."


def _feed():
    def rec(ts, mid, text, ref=None, flow="f1"):
        return {"timestamp": ts, "event": "received", "flow_id": flow, "log_id": mid,
                "details": {"content_preview": text, "metadata": {"message_id": mid, "reply_to_message_id": ref}}}
    return [{"entries": [rec("2026-10-01T14:02:02+00:00", "m1", SIG), rec("2026-10-01T14:04:02+00:00", "m2", T1, "m1", "f2"),
                         rec("2026-10-01T14:10:02+00:00", "m3", BE, "m1", "f3")],
             "related_entries": {"714:f1": [
                 {"event": "parse_completed", "flow_id": "f1", "details": {"parse_result": {"result": {"half_size": False},
                                                                                            "raw_response": {"half_size": True}}}},
                 {"event": "execution_result", "flow_id": "f1", "log_id": "x1", "timestamp": "2026-10-01T14:02:04+00:00",
                  "details": {"accounts": [
                      {"account_id": 353, "contracts": 8, "outcome": "failed", "data": {"result": {"reject_reason": "LiquidationOnly",
                                                                                                    "rejection_text": "Drawdown level breached"}}},
                      {"account_id": 358, "contracts": 8, "outcome": "attached", "data": {"result": {"fill_price": 30636.5}}}]}}]},
             "has_more": False}]


TRADES = {"sources": [{"trades": [{"source_trade_id": "log:f1", "total_pnl": 0, "closed_by": "broker_position", "dry_run": False,
                                   "accounts": [{"account_id": 358, "contracts": 8, "entry_price": 30636.5, "exit_price": None,
                                                 "pnl": 0, "fills": []}]}]}]}
WS = {"accounts": [{"subaccounts": [{"account_id": 353, "account_nickname": "eval#2", "risk_per_trade": 350,
                                     "instrument_settings": [{"symbol": "NQ", "contract_type": "micro", "contracts": 8}]},
                                    {"account_id": 358, "account_nickname": "FFF", "risk_per_trade": 200,
                                     "instrument_settings": [{"symbol": "NQ", "contract_type": "micro", "contracts": 8}]}]}],
      "routes": {"discord": {"1": {"product_configs": {"futures": {
          "allow_exits": False, "allow_sl_adjustments": False, "allow_trims": False, "entry_order_policy": "market", "close_at_time": "15:00",
          "brackets_ui": {"alert_override": "alert_wins", "contracts": {"NQ": {"levels": [
              {"side": "tp", "offset_value": "1", "offset_unit": "rr", "amount": "50", "stop_to_value": "10"},
              {"side": "tp", "offset_value": "1.8", "offset_unit": "rr", "amount": "50", "stop_to_value": "10"},
              {"side": "sl", "offset_value": "160", "offset_unit": "ticks"}]}}}}}}}}}
METRICS = {"accounts": [{"subaccounts": [{"account_id": 353, "cashUSD": 49540.5}, {"account_id": 358, "cashUSD": 49165.9}]}]}


def _snap():
    return mark_rejections(build_snapshot(_feed(), TRADES, METRICS, WS))


def test_snapshot_from_api_shapes():
    s = _snap()
    sig = s["signals"][0]
    assert (sig["side"], sig["opt"], sig["stop"], sig["t1"], sig["fin"], sig["px"], sig["past"]) == \
        ("SHORT", 30703.75, 30761.5, 30636.5, 30512.25, 30677.75, 26)
    assert [m[1] for m in sig["mgmt"]] == ["t1", "be"] and sig["alerio"]["raw_half"] is True
    assert s["route"]["brackets"]["tp"][0] == {"rr": 1.0, "pct": 50.0, "stop_to_ticks": 10.0}
    acc = {a["nickname"]: a for a in s["accounts"]}
    assert acc["eval#2"]["status"] == "liquidation_only" and acc["FFF"]["status"] == "ok" and acc["FFF"]["cash"] == 49165.9


def test_audit_flags_what_went_wrong_on_oct_1():
    a = audit(_snap())
    flags = " | ".join(a["rows"][0]["flags"])
    for needle in ("size down", "management message", "eval#2 rejected: LiquidationOnly", "67.2 pts worse",
                   "risked $2,000 vs $200", "Target 1"):
        assert needle in flags, needle
    assert a["avg_chase_pts"] == 67.25


def test_mirror_route_matches_alerio_settings():
    r = mirror_route(_snap()).rules
    assert (r.sizing, r.contracts, r.entry_type, r.alert_override, r.follow_management, r.auto_close) == \
        ("fixed", 8, "market", "override", False, "15:00")
    assert r.trims[0].sl_after == 2.5 and r.runner_target_r == 1.8 and r.default_stop_pts == 40


def _bars(day="2026-10-01", start="09:30", path=None):
    idx = pd.date_range(f"{day} {start}", periods=len(path), freq="5min", tz=NY)
    return pd.DataFrame([{"open": c, "high": h, "low": l, "close": c, "volume": 1} for h, l, c in path], index=idx)


def test_compare_mirror_vs_guarded_on_a_stopped_short():
    s = _snap()
    s["signals"][0]["mgmt"] = []                                     # let the bars decide
    flat = [(30690, 30670, 30680)] * 7
    path = flat + [(30800, 30680, 30790)] + [(30795, 30780, 30790)] * 6   # 10:05 bar runs through the stop
    res = compare(s, _bars(path=path), budgets=(400,), samples=200)
    m, g = res["runs"]["alerio (as configured)"], res["runs"]["terminal $400/trade"]
    assert m["summary"]["worst_trade_usd"] < -1300                    # 8 micros x 84 pts
    assert g["summary"]["trades"] == 0                                # 84-pt stop from the market price: skipped
    assert 0 <= m["breach"]["p_breach"] <= 1
    s["signals"][0]["px"] = 30705.0                                   # in the zone: 57-pt stop -> sized to $400
    g = compare(s, _bars(path=path), budgets=(400,), samples=200)["runs"]["terminal $400/trade"]
    assert -420 < g["summary"]["worst_trade_usd"] < -300


def test_breach_odds():
    tr = [{"opened": "2026-09-01T10:00", "usd": -1000.0}, {"opened": "2026-09-02T10:00", "usd": 300.0}]
    b = breach_odds(tr, ["2026-09-01", "2026-09-02"], dd_limit=2000, samples=500)
    assert b["p_breach"] > 0.5 and b["samples"] == 500


def test_signal_alerts_timeline():
    msgs, alerts = signal_alerts(_snap())
    assert [a.action for a in alerts] == ["entry", "breakeven", "close"]
    assert alerts[1].price == 30636.5 and alerts[2].price == 30703.75 and alerts[0].ts.hour == 10


def test_watcher_shadows_new_signals_and_flags_locked_accounts(tmp_path):
    from abg.routes.watch import AlerioWatcher

    class S:
        data_dir, alerio_cookie, alerio_poll_sec = tmp_path, "c", 20
        alerio_budget_usd, alerio_max_contracts, alerio_webhook_url, alpha_webhook_url = 400.0, 8, None, None

    w = AlerioWatcher(S())
    w.snapshot = _snap()
    pages = [{"entries": [], "related_entries": {}}, _feed()[0]]

    async def fake_get(path, **kw):
        return pages.pop(0) if pages else {"entries": []}
    w._get = fake_get
    now = datetime(2026, 10, 1, 10, 2, 5, tzinfo=NY)
    asyncio.run(w.poll(now))                                          # primes on an empty feed
    out = asyncio.run(w.poll(now))
    shadows = [e for e in out if e["kind"] == "shadow" and e["alert"]["action"] == "entry"]
    paper = next(e["decision"] for e in shadows if e["decision"]["account"] == "paper")
    assert paper["verdict"] == "skipped" and "84 pts" in paper["reasons"][0]      # Oct 1: the terminal would have passed
    locked = next(e["decision"] for e in shadows if e["decision"]["account"] == "eval#2")
    assert locked["verdict"] == "skipped" and "liquidation-only" in locked["reasons"][0]
    assert any(e["kind"] == "alerio-exec" and e["reject"] == "LiquidationOnly" for e in out)


def test_api_alerio(monkeypatch, tmp_path):
    from abg.routes.alerio import save_snapshot
    for k, v in {"ABG_ALLOW_SYNTHETIC": "true", "ABG_PROVIDER_ORDER": "synthetic", "ABG_CACHE_DIR": str(tmp_path / "c"),
                 "ABG_DATA_DIR": str(tmp_path / "d"), "ABG_MONITOR_ON_SERVE": "false", "ABG_NOTIFY_DESKTOP": "false"}.items():
        monkeypatch.setenv(k, v)
    save_snapshot(tmp_path / "d", _snap())
    from fastapi.testclient import TestClient
    from abg.api.server import app
    with TestClient(app) as c:
        r = c.get("/api/alerio").json()
        assert r["snapshot"]["accounts"][0]["nickname"] == "eval#2" and r["audit"]["signals"] == 1 and r["watch"]["enabled"] is False
