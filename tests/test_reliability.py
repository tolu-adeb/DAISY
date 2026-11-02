"""Reliability and research fixes of Oct 23 (docs/14.12, 17.11, 19.9): duplicate posts, day recording, uptime,
the live-vs-backtest check, the forward lab's neighbours / no-ops / profit route / re-fit ideas, and the swing chase."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from abg.config import Settings
from abg.alpha.strategy import AlphaParams

NY = ZoneInfo("America/New_York")
SRC = Path(__file__).resolve().parents[1] / "data"


# ---------------------------------------------------------------------- the bot: no duplicate or late briefs
def _bot(tmp_path, **kw):
    from abg.alpha.bot import AlphaBot
    s = Settings(data_dir=tmp_path, alpha_webhook_url="https://discord.com/api/webhooks/1/x", _env_file=None, **kw)
    bot = AlphaBot(SimpleNamespace(http=None), s)
    sent = []

    async def post(content, embed, reply_to=None):
        sent.append(embed)
        return str(len(sent))
    bot.poster.post = post
    return bot, sent


def _brief_event(day: date) -> dict:
    from abg.alpha.context import DayContext
    from abg.alpha.strategy import AlphaStrategy
    return AlphaStrategy(AlphaParams()).start_day(DayContext(day=day, pdh=30000, pdl=29800, pdc=29900))[0]


def test_brief_posts_once_and_never_late(tmp_path, monkeypatch):
    import abg.alpha.bot as B
    day = date(2026, 10, 22)
    bot, sent = _bot(tmp_path)
    bot.day = day
    clock = {"now": datetime(2026, 10, 22, 9, 25, tzinfo=NY)}
    monkeypatch.setattr(bot, "_late_brief", lambda now=None: B.AlphaBot._late_brief(bot, clock["now"]))
    ev = _brief_event(day)
    asyncio.run(bot.emit(ev))
    asyncio.run(bot.emit(ev))                                    # the second process / second call at 9:25
    assert len(sent) == 1
    # a restart at 12:28: a fresh process with a fresh "sent" table must not post it again
    bot.store.db.execute("DELETE FROM sent")
    clock["now"] = datetime(2026, 10, 22, 12, 28, tzinfo=NY)
    asyncio.run(bot.emit(ev))
    assert len(sent) == 1
    rows = [r[0] for r in bot.store.db.execute("SELECT delivered FROM events WHERE type='brief'")]
    assert rows == ["ok", "duplicate - not posted", "late - not posted"]
    # a brand-new day, started late (10:05): the brief stays on the dashboard
    bot2, sent2 = _bot(tmp_path / "b")
    bot2.day = date(2026, 10, 23)
    assert bot2._late_brief(datetime(2026, 10, 23, 10, 5, tzinfo=NY)) and not bot2._late_brief(datetime(2026, 10, 23, 9, 25, tzinfo=NY))
    assert bot2.store.delivered_before("2026-10-23", "brief") is False


def test_daylog_keeps_the_morning_pack_and_every_bar(tmp_path):
    from abg.alpha.daylog import DayLog, bar_from
    from abg.alpha.data import Bar
    log = DayLog(tmp_path / "live", keep_days=2)
    d = date(2026, 10, 23)
    warm = [Bar(datetime(2026, 10, 22, 15, 55, tzinfo=NY), 1, 2, 0.5, 1.5, 10, 5)]
    log.start(d, {"day": "2026-10-23", "orx_mode": "follow"}, {"t1_r": 0.35}, {"stats": {}}, warm, feed="proxy")
    log.start(d, {"day": "2026-10-23", "orx_mode": "fade"}, {"t1_r": 0.35}, None, [], feed="proxy")      # a restart
    b = Bar(datetime(2026, 10, 23, 9, 30, tzinfo=NY), 100, 101, 99, 100.5, 5, 1)
    log.bar(d, b, "stream", 41.3)
    pack, rows = log.load(d)
    assert pack["ctx"]["orx_mode"] == "follow" and pack["restarts"][0]["ctx_same"] is False
    assert bar_from(pack["warm"][0]).minutes == 5 and rows[0]["source"] == "stream" and bar_from(rows[0]).close == 100.5
    for k in (24, 25, 26):
        log.start(date(2026, 10, k), {"day": f"2026-10-{k}"}, {}, None, [])
    assert log.days() == ["2026-10-25", "2026-10-26"]


def test_uptime_coverage_and_keep_awake(tmp_path):
    from abg.live.uptime import KeepAwake, Uptime
    u = Uptime(tmp_path / "uptime.json")
    d = date(2026, 10, 22)
    t = lambda h, m: datetime.combine(d, time(h, m), NY).timestamp()   # noqa: E731
    for x in range(int(t(9, 20)), int(t(10, 0)), 30):
        u.beat(x)
    for x in range(int(t(12, 28)), int(t(16, 5)), 30):
        u.beat(x)
    c = Uptime(tmp_path / "uptime.json").coverage(d)                   # read back from disk
    assert c["saw_open"] and c["gaps"] == [["09:59", "12:28"]] and 60 < c["pct"] < 75
    assert u.running_at(t(9, 45)) and not u.running_at(t(10, 15))
    assert KeepAwake.wanted(datetime(2026, 10, 22, 9, 0, tzinfo=NY)) and not KeepAwake.wanted(datetime(2026, 10, 24, 10, 0, tzinfo=NY))
    assert not KeepAwake.wanted(datetime(2026, 10, 22, 17, 0, tzinfo=NY))
    assert KeepAwake(True).update() in (False, True)                    # no-op off Windows


# ---------------------------------------------------------------------- forward lab rules
def test_neighbours_use_a_half_step_toward_live():
    from abg.agents.lab.space import KNOBS
    assert KNOBS["orx_trend_len"].neighbours(40, 50) == [30, 45]       # was [30] only (50 is live)
    assert KNOBS["orx_trend_len"].neighbours(70, 50) == [60, 80]
    assert KNOBS["t1_r"].neighbours(0.45, 0.35) == [0.4, 0.55]
    assert KNOBS["orx_trend_len"].neighbours(20, 50) == [30]            # 10 is below the range


def test_noops_are_dropped():
    from abg.agents.lab.space import Hypothesis, strip_noops
    base = AlphaParams()
    h = Hypothesis(name="Early T1 + exhaust", rationale="bank early and skip extended breaks", changes={"t1_r": base.t1_r, "orx_max_ext": 0.4})
    h2 = strip_noops(h, base)
    assert h2.changes == {"orx_max_ext": 0.4}
    assert strip_noops(Hypothesis(name="same", rationale="nothing changes here", changes={"t1_r": base.t1_r}), base) is None
    keep = Hypothesis(name="new t1", rationale="a real change to test", changes={"t1_r": 0.45})
    assert strip_noops(keep, base) is keep
    assert strip_noops(Hypothesis(name="setups", rationale="same list as live", changes={"setups": list(base.setups)}), base) is None


def test_profit_route():
    from dataclasses import replace

    from abg.agents.lab.validate import Gate, _win_check
    live = {"win_rate": 0.7637, "net_usd": 5726.6, "ratio": 11.18}
    cand = {"win_rate": 0.7674, "net_usd": 7532.0, "ratio": 13.48}          # orx_trend_len 40 on the real data, Oct 23
    strict = _win_check(cand, live, Gate())
    assert not strict["pass"] and strict["route"] == "win rate"
    g = replace(Gate(), alt_ratio_gain=0.15, alt_profit_gain=0.10, alt_wr_slack=0.005)
    ok = _win_check(cand, live, g)
    assert ok["pass"] and ok["route"] == "profit" and "profit route" in ok["detail"]
    assert not _win_check({**cand, "win_rate": 0.75}, live, g)["pass"]           # win rate dropped too far
    assert not _win_check({**cand, "ratio": 12.0}, live, g)["pass"]              # not enough better on profit/drawdown
    assert _win_check({**cand, "win_rate": 0.78}, live, g)["route"] == "win rate"


def test_forward_pick_refit_first_retry_and_noops(tmp_path):
    from abg.agents.lab.rolling import RollingLab
    (tmp_path / "refit.json").write_text(json.dumps({"at": "2026-10-25T19:00", "suggest": {"orx_k": 0.6}}))
    L = RollingLab(Settings(data_dir=tmp_path, fwd_per_day=8, _env_file=None))
    home = tmp_path / "lab" / "rolling"
    with open(home / "trials.jsonl", "w") as fh:
        for name, ch, failed in (("orx_trend_len 50 -> 40", {"orx_trend_len": 40}, ["win_rate", "stability"]),
                                 ("bad", {"t1_r": 1.2}, ["profit", "win_rate", "consistency"])):
            fh.write(json.dumps({"name": name, "changes": ch, "failed": failed, "verdict": "rejected", "data_to": "2026-10-22"}) + "\n")
    base = AlphaParams()
    lab = SimpleNamespace(base=base, ds=SimpleNamespace(sessions=list(range(578))), gate=L.gate())
    st = {"strategies": [], "tested": {}}
    L._on_gate_change(lab, st)
    assert [r["changes"] for r in st["retry_first"]] == [{"orx_trend_len": 40}] and st["gate_key"]
    agent = [__import__("abg.agents.lab.space", fromlist=["Hypothesis"]).Hypothesis(
        name="T1 0.35 + filter", rationale="bank early and filter (from Claude)", changes={"t1_r": base.t1_r, "orx_max_ext": 0.45})]
    picked = L.pick(lab, st, agent)
    assert picked[0].changes == {"orx_k": 0.6} and L.origins[id(picked[0])] == "weekly re-fit"
    assert picked[1].changes == {"orx_trend_len": 40} and L.origins[id(picked[1])] == "re-test (new rules)"
    claude = next(h for h in picked if "orx_max_ext" in h.changes)
    assert claude.changes == {"orx_max_ext": 0.45} and st["noops_dropped"] >= 1
    L._on_gate_change(lab, st)                                      # same rules again: nothing re-queued
    assert st["retry_first"]


# ---------------------------------------------------------------------- swing: chase (wide ideas only)
def _px(closes, start=date(2026, 7, 1)):
    days = pd.bdate_range(start, periods=len(closes))
    c = np.array(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.4, "low": c - 0.4, "close": c, "volume": 1000.0}, index=days)


def _sig(px, author="wide"):
    return {"symbol": "X", "direction": "long", "entry_type": "zone", "entry_lo": 99.0, "entry_hi": 100.0,
            "target_lo": 110.0, "target_hi": 111.0, "stop": 96.0, "stop_basis": "intraday", "author": author,
            "posted": datetime.combine(px.index[19].date(), time(16, 31)), "posted_et": f"{px.index[19].date()} 16:31"}


def test_chase_buys_the_next_open_only_when_asked():
    from dataclasses import replace

    import abg.swing  # noqa: F401  (puts swingdesk/ on the path)

    from swingdesk.engine import run
    from swingdesk.strategy import plan_from_config
    # 20 quiet sessions around 104-105 (ATR ~1-1.5), posted at 104; then it drifts up and runs to the target
    closes = [104 + 0.5 * np.sin(i) for i in range(20)] + [100.8, 101.5, 103, 105, 107, 109, 111, 112]
    px = _px(closes)
    sig = _sig(px)
    base = plan_from_config()
    r0 = run(sig, px, base)
    assert r0["status"] == "NOFILL_RAN"                                 # the audited plan: missed, don't chase
    r1 = run(sig, px, replace(base, chase_atr=1.0))
    assert r1.get("chased") and r1["status"] == "WIN" and r1["fill_date"] == str(px.index[21].date())
    # a touch of the zone first is the normal limit fill, chase or not
    px2 = _px([104 + 0.5 * np.sin(i) for i in range(20)] + [99.8, 101, 104, 108, 111])
    assert not run(_sig(px2), px2, replace(base, chase_atr=1.0)).get("chased")


def test_monitor_uses_a_plan_per_author():
    from dataclasses import replace

    import abg.swing  # noqa: F401

    from swingdesk.monitor import update
    from swingdesk.strategy import plan_from_config
    closes = [104 + 0.5 * np.sin(i) for i in range(20)] + [100.8, 101.5, 103, 105, 107, 109, 111, 112]
    px = _px(closes)
    spy = _px([500 + 0.1 * i for i in range(len(closes))])
    rows = []
    for author in ("wide", "b06wi"):
        s = _sig(px, author)
        rows.append({k: v for k, v in s.items() if k != "posted"} | {"levels_source": "test", "note": ""})
    sigs = pd.DataFrame(rows)
    sigs.loc[1, "posted_et"] = sigs.loc[0, "posted_et"].replace("16:31", "16:30")
    book, _ = update(sigs, {"X": px}, spy, book={}, plans={"wide": replace(plan_from_config(), chase_atr=1.0)})
    states = {v["author"]: v["state"] for v in book.values()}
    assert states == {"wide": "WIN", "b06wi": "NOFILL_RAN"}                 # same setup: only the wide idea chases
    assert next(v for v in book.values() if v["author"] == "wide")["result"].get("chased")


# ---------------------------------------------------------------------- live vs backtest
def test_compare_pairs_trades():
    from abg.agents.lab.parity import compare
    a = [{"setup": "orx", "side": -1, "opened": "10:15", "entry": 1, "exit": "final", "r": 1.587}]
    b = [{"setup": "orx", "side": -1, "opened": "10:20", "entry": 1, "exit": "final", "r": 1.5}]
    assert compare(a, b)["same"]
    assert not compare(a, [])["same"] and compare(a, [])["only_first"]
    flip = [{**b[0], "side": 1}]
    c = compare(a, flip)
    assert not c["same"] and c["only_first"] and c["only_second"]


@pytest.mark.skipif(not (SRC / "mnq_proxy_5m.csv").exists(), reason="no dataset")
def test_parity_check_on_a_recorded_day(tmp_path):
    from abg.agents.lab import parity
    from abg.agents.lab.dataset import load_dataset
    from abg.alpha.bot import AlphaStore
    from abg.alpha.daylog import DayLog
    df = pd.read_csv(SRC / "mnq_proxy_5m.csv", parse_dates=["datetime"])
    keep = sorted(df.datetime.dt.date.unique())[:140]
    (tmp_path / "ds").mkdir()
    df[df.datetime.dt.date.isin(keep)].to_csv(tmp_path / "ds" / "b.csv", index=False)
    ds = load_dataset(tmp_path / "ds" / "b.csv", SRC / "qqq_daily.csv", 0)
    p = AlphaParams()
    # find a day the backtest trades
    for d in reversed(ds.sessions[-30:]):
        bt = parity.backtest_day(ds.bars, ds.trend_daily, p, d)
        if bt["trades"]:
            break
    day = str(d)
    # "live" = exactly what the backtest saw (5-min bars, its context), so every step must match
    store = AlphaStore(tmp_path / "alpha.sqlite3")
    brief = {"type": "brief", "ts": None, "ctx": bt["ctx"].to_dict(), "params": p.to_dict()}
    store.add_event(day, brief, None, "ok")
    for t in bt["trades"]:
        store.add_event(day, {"type": "signal", "trade": t, "ts": t["opened"]}, None, "ok")
        store.save_trade(day, t)
    log = DayLog(tmp_path / "live")
    log.start(d, bt["ctx"].to_dict(), p.to_dict(), None, bt["warm"])
    for b in bt["bars"]:
        log.bar(d, b, "stream", None)
    r = parity.check(tmp_path, day, ds.bars, ds.trend_daily)
    assert r["verdict"] == "match" and r["recorded"] and all(s["same"] for s in r["steps"]), r
    assert len(r["steps"]) == 5 and abs(r["scale"] - 1) < 1e-6
    # the bot logged nothing during the trade (it wasn't receiving bars): live has no trade
    store.db.execute("DELETE FROM trades")
    store.db.commit()
    r2 = parity.check(tmp_path, day, ds.bars, ds.trend_daily)
    assert r2["verdict"] == "differs" and r2["causes"]
    assert [st["same"] for st in r2["steps"]] == [False, False, True, True, True]     # only "live vs its own replay" differs
    # an older version of the strategy (settings it didn't have) is flagged, not blamed on the data
    store.db.execute("DELETE FROM events")
    store.add_event(day, {**brief, "params": {k: v for k, v in p.to_dict().items() if k != "orx_policy"}}, None, "ok")
    import shutil
    shutil.rmtree(tmp_path / "live")
    r3 = parity.check(tmp_path, day, ds.bars, ds.trend_daily)
    assert r3["verdict"] == "older strategy" and "orx_policy" in r3["older_strategy"]
    parity.save(tmp_path, r)
    s = parity.summary(tmp_path)
    assert s["judged"] == 1 and s["matched"] == 1


def test_parity_api_and_refit_suggestion_only(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from abg.api.rd_routes import router
    s = Settings(data_dir=tmp_path, _env_file=None)
    app = FastAPI()
    app.include_router(router)
    app.state.monitor = SimpleNamespace(forward=None, running=False, uptime=None, keep_awake=None)
    app.state.engine = SimpleNamespace(settings=s)
    c = TestClient(app)
    v = c.get("/api/rd/parity").json()
    assert v["summary"]["days"] == 0 and isinstance(v["uptime"], list) and v["healthcheck"] is False
    assert c.post("/api/rd/parity/run").status_code == 404           # no forward lab in this test app
    assert Settings(_env_file=None).alpha_refit_adopt is False
