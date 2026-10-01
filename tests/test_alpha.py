"""MNQ alpha bot: data loading, strategy lifecycle, day rules, backtester integrity, journal analysis,
messages and the live bot plumbing (all offline)."""
import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from abg.alpha.backtest import random_walk_bars, run_backtest, summarize, walk_forward
from abg.alpha.bot import AlphaBot, MinuteBuilder
from abg.alpha.context import DayContext, build_context
from abg.alpha.data import NY, Bar, back_adjust, load_daily_csv, load_intraday_csv, session_date
from abg.alpha.journal import apply_rules, parse_journal, parse_signals, rules_report
from abg.alpha.learn import AdaptiveBook
from abg.alpha.messages import Renderer
from abg.alpha.strategy import AlphaParams, AlphaStrategy

D, P = date(2026, 9, 15), date(2026, 9, 14)


def _warm(px=20000.0):
    out = []
    for i in range(60):
        o = px
        px += 2 if i % 2 else -2
        out.append(Bar(datetime(2026, 9, 14, 14, 0, tzinfo=NY) + timedelta(minutes=i), o, max(o, px) + 1, min(o, px) - 1, px, 100, 1))
    return out


class Day:
    """Write a session minute by minute."""

    def __init__(self, day=D):
        self.t = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY)
        self.bars: list[Bar] = []

    def add(self, o, h, l, c, n=1):
        for _ in range(n):
            self.bars.append(Bar(self.t, o, h, l, c, 100, 1))
            self.t += timedelta(minutes=1)
        return self

    def opening_range(self, lo=20000, hi=20020):
        for i in range(15):
            self.add(20010, hi if i == 3 else 20012, lo if i == 7 else 20008, 20010)
        return self


def _run(strat, bars):
    ev = []
    for b in bars:
        ev += strat.on_bar(b)
    return ev + strat.end_day()


def _orb_day(after="up"):
    d = Day().opening_range()
    p = 20010
    for _ in range(5):                     # 09:45-09:49 strong breakout
        d.add(p, p + 5, p - 1, p + 4)
        p += 4
    for c in (20030, 20025, 20022):        # pull back toward the range high
        d.add(p, p + 1, c - 1, c)
        p = c
    d.add(20022, 20027, 20020.5, 20026)    # 09:53 retest holds, bullish close
    p = 20026
    if after == "up":
        for _ in range(30):
            d.add(p, p + 3, p - 1, p + 2.5)
            p += 2.5
    else:
        for _ in range(10):
            d.add(p, p + 1, p - 3, p - 2.5)
            p -= 2.5
    return d


def _ctx(**kw):
    base = dict(pdh=20200, pdl=19800, pdc=20000, onh=20150, onl=19900)
    base.update(kw)
    return DayContext(D, **base)


# ---------------------------------------------------------------- data
def test_daily_csv_and_roll_adjust(tmp_path):
    rows = ['"Date","Price","Open","High","Low","Vol.","Change %"']
    d = date(2026, 8, 3)
    px = 29000.0
    data = []
    while d <= date(2026, 9, 25):
        if d.weekday() < 5:
            vol = "2.40M"
            if date(2026, 9, 14) <= d <= date(2026, 9, 18):
                vol = "190.5K"
            if d >= date(2026, 9, 21):
                px_d = px + 240
            else:
                px_d = px
            data.append(f'"{d:%m/%d/%Y}","{px_d:,.2f}","{px_d:,.2f}","{px_d + 100:,.2f}","{px_d - 100:,.2f}","{vol}","0.10%"')
        d += timedelta(days=1)
    rows += reversed(data)
    f = tmp_path / "d.csv"
    f.write_text("\n".join(rows))
    df = load_daily_csv(f)
    assert df.index.is_monotonic_increasing and df["volume"].iloc[0] == 2.4e6
    adj, rolls = back_adjust(df)
    assert len(rolls) == 1 and rolls[0].stale[0] == "2026-09-14" and len(rolls[0].stale) == 5
    assert 230 < rolls[0].offset < 250                     # ~ carry for one quarter
    assert pd.Timestamp("2026-09-15") not in adj.index
    assert adj.loc["2026-09-11", "close"] == pytest.approx(29000 + rolls[0].offset)


def test_intraday_csv_formats(tmp_path):
    tv = tmp_path / "tv.csv"
    tv.write_text("time,open,high,low,close,Volume\n1789133400,20000,20005,19995,20001,10\n1789133460,20001,20003,19999,20002,12\n")
    a = load_intraday_csv(tv)
    assert str(a.index.tz) == "America/New_York" and len(a) == 2
    nt = tmp_path / "nt.txt"
    nt.write_text("20260915 093000;20000;20005;19995;20001;10\n20260915 093100;20001;20003;19999;20002;12\n")
    b = load_intraday_csv(nt)
    assert b.index[0].hour == 9 and b.index[0].minute == 30 and b["close"].iloc[-1] == 20002
    assert session_date(pd.Timestamp("2026-09-14 18:05", tz=NY)) == date(2026, 9, 15)
    assert session_date(pd.Timestamp("2026-09-18 19:00", tz=NY)) == date(2026, 9, 21)      # Friday evening -> Monday


def test_context_bias():
    daily = pd.DataFrame({"open": [100.0] * 12, "high": [101.0 + i for i in range(12)], "low": [99.0 + i for i in range(12)],
                          "close": [100.0 + i for i in range(12)]}, index=pd.date_range("2026-09-01", periods=12))
    on = pd.DataFrame({"high": [115.0], "low": [112.0], "close": [114.0]})
    ctx = build_context(date(2026, 9, 13), daily, on)
    assert ctx.pdh == 112 and ctx.trend_d == 1 and ctx.bias == 1 and ctx.onh == 115


# ---------------------------------------------------------------- strategy
def test_orb_retest_full_lifecycle():
    s = AlphaStrategy(AlphaParams())
    assert s.start_day(_ctx(), _warm())[0]["type"] == "brief"
    ev = _run(s, _orb_day("up").bars)
    kinds = [e["type"] for e in ev]
    assert kinds.index("idea") < kinds.index("signal") < kinds.index("t1") < kinds.index("final")
    sig = next(e for e in ev if e["type"] == "signal")["trade"]
    assert sig["side"] == 1 and sig["setup"] == "orb" and sig["stop"] < sig["entry"] < sig["t1"] < sig["final"]
    assert sig["risk"] >= 12 and sig["reasons"]
    t1 = next(e for e in ev if e["type"] == "t1")
    assert t1["new_stop"] == sig["entry"]                                   # break-even at Target 1
    fin = next(e for e in ev if e["type"] == "final")
    assert fin["pts"] == pytest.approx(0.5 * (sig["t1"] - sig["entry"]) + 0.5 * (sig["final"] - sig["entry"]))
    summ = ev[-1]
    assert summ["type"] == "day_summary" and summ["n"] == 1 and summ["wins"] == 1


def test_stop_after_first_loss_and_one_trade_per_zone():
    s = AlphaStrategy(AlphaParams())
    s.start_day(_ctx(), _warm())
    ev = _run(s, _orb_day("down").bars)
    kinds = [e["type"] for e in ev]
    assert "stopped" in kinds and "done" in kinds
    done = next(e for e in ev if e["type"] == "done")
    assert "first loss" in done["why"]
    assert kinds.count("signal") == 1


def test_window_news_and_fomc_rules():
    late = AlphaParams(entry_end="09:40")
    s = AlphaStrategy(late)
    s.start_day(_ctx(), _warm())
    ev = _run(s, _orb_day("up").bars)
    assert "signal" not in [e["type"] for e in ev] and any(e["type"] == "done" and "window" in e["why"] for e in ev)
    s = AlphaStrategy(AlphaParams())
    s.start_day(_ctx(events=[{"time": "09:55", "name": "ISM", "impact": "high"}]), _warm())
    ev = _run(s, _orb_day("up").bars)
    assert "signal" not in [e["type"] for e in ev]                          # 09:53 is inside the 5-min pre-news blackout
    s = AlphaStrategy(AlphaParams(fomc_mode="skip"))
    s.start_day(_ctx(fomc=True), _warm())
    assert "signal" not in [e["type"] for e in _run(s, _orb_day("up").bars)]
    s = AlphaStrategy(AlphaParams())
    s.start_day(_ctx(fomc=True), _warm())
    sig = next(e for e in _run(s, _orb_day("up").bars) if e["type"] == "signal")
    assert sig["trade"]["size"] == 0.5


def test_sweep_reclaim_short():
    s = AlphaStrategy(AlphaParams())
    s.start_day(_ctx(onh=20030), _warm())
    d = Day().opening_range()
    d.add(20012, 20020, 20010, 20018, 3)            # 09:45-09:47 drift up under ONH 20030
    d.add(20018, 20033, 20016, 20027)               # 09:48 pokes above ONH by 3 pts and closes back below
    d.add(20027, 20028, 20020, 20021)               # 09:49 5m bar closes well below the level (bearish body)
    for _ in range(25):
        d.add(20021, 20022, 20016, 20017)
    ev = _run(s, d.bars)
    sig = next((e for e in ev if e["type"] == "signal" and e["trade"]["setup"] == "sweep"), None)
    assert sig is not None and sig["trade"]["side"] == -1 and sig["direct"]
    assert sig["trade"]["stop"] > 20033                                      # beyond the sweep high


def test_adaptive_book_pauses_and_recovers(tmp_path):
    b = AdaptiveBook()
    for _ in range(8):
        b.record("orb", "range", -1.0)
    assert not b.allowed("orb", "range") and b.allowed("orb", "trend")
    adj, note = b.adjust("orb", "range")
    assert adj < 0 and "expectancy" in note
    for _ in range(25):
        b.record("orb", "range", 1.2)
    assert b.allowed("orb", "range")
    b.save(tmp_path / "l.json")
    assert AdaptiveBook.load(tmp_path / "l.json").shrunk("orb", "range")[1] == 33


# ---------------------------------------------------------------- backtester
def test_backtest_has_no_edge_on_random_walk():
    res = run_backtest(random_walk_bars(60, seed=11))
    st = res.stats
    assert st["trades"] > 20 and abs(st["avg_r"]) < 0.35          # loose: one seed is noisy; the CLI runs 3 x 120 days
    assert {"by_setup", "by_time", "max_dd_pts", "net_usd_per_micro"} <= set(st)
    assert all(t["date"] for t in res.trades)


def test_walk_forward_runs_on_small_grid():
    wf = walk_forward(random_walk_bars(30, seed=5, minutes=5), grid={"final_r": [2.0, 3.0]}, min_trades=1)
    assert wf["best"]["final_r"] in (2.0, 3.0) and wf["test_days"][2] >= 9 and "test_stats" in wf


def test_summarize_costs():
    tr = [{"pts": 10.0, "r": 0.5, "date": "2026-09-15", "setup": "orb", "regime": "trend", "side_label": "LONG",
           "exit_reason": "final", "opened": "2026-09-15T09:50:00"}]
    st = summarize(tr, commission_rt=1.24)
    assert st["net_usd_per_micro"] == pytest.approx(10 * 2 - 1.24)


# ---------------------------------------------------------------- journal
JOURNAL = {"channel": {"name": "bot-journal"}, "messages": [
    {"id": "1", "timestamp": "2026-07-07T16:15:01+00:00", "embeds": [{"title": "📒 Trading Journal — Tue Jul 07, 2026",
     "description": "🟢 **10:02 ET · 🟥 SHORT · CONTINUATION** @ 29468.25 → **+88.25 pts** (PT3_HIT)\n"
                    "🔴 **10:41 ET · 🟩 LONG · REV** @ 29364.25 → **-40.00 pts** (STOPPED)\n"
                    "🟢 **10:55 ET · 🟥 SHORT** @ 29360.25 → **+20.00 pts**"}]},
    {"id": "2", "timestamp": "2026-07-08T16:15:01+00:00", "embeds": [{"title": "📒 Trading Journal — Wed Jul 08, 2026",
     "description": "No qualifying setups today — the engine stayed flat."}]}]}

SIGNALS = {"channel": {"name": "bot-signals"}, "messages": [
    {"id": "10", "timestamp": "2026-08-19T13:34:10+00:00", "content": "SIGNAL", "embeds": [{"title": "🔴 SIGNAL VALIDATED #1 — SHORT",
     "description": "**Entry zone:** 29,685 – 29,726.75 · optimal **29,726.75**\n**Stop loss:** 29,757.25  (~30 pts)\n\n"
                    "**Target 1:** 29,696.25\n**Final target:** 29,506.75\n**Price now:** 29,697.75"}]},
    {"id": "11", "timestamp": "2026-08-19T13:36:02+00:00", "reference": {"messageId": "10"},
     "embeds": [{"title": "🔵 TARGET 1 HIT #1", "description": "Target 1 hit — **+30.5 pts**."}]},
    {"id": "12", "timestamp": "2026-08-19T13:50:06+00:00", "reference": {"messageId": "10"},
     "embeds": [{"title": "✅ FINAL TARGET HIT #1", "description": "Final target hit — **+220.0 pts**."}]}]}


def test_journal_and_signal_parsing(tmp_path):
    jf, sf = tmp_path / "j.json", tmp_path / "s.json"
    jf.write_text(json.dumps(JOURNAL))
    sf.write_text(json.dumps(SIGNALS))
    tr, flat = parse_journal(jf)
    assert len(tr) == 3 and flat == ["2026-07-08"] and list(tr["dir"]) == ["S", "L", "S"] and tr["pts"].sum() == 68.25
    assert tr["setup"].iloc[2] == "NA"
    kept = apply_rules(tr, stop_after_loss=True)
    assert len(kept) == 2                                   # the trade after the loss is dropped
    rep = rules_report(pd.concat([tr.assign(date="2026-07-07"), tr.assign(date="2026-07-09")]))
    assert "stop after the first loss" in rep["rules"] and rep["by_trade_number"]
    sg = parse_signals(sf)
    assert len(sg) == 1 and sg["outcome"].iloc[0] == "final" and sg["pts"].iloc[0] == 220.0
    assert sg["risk"].iloc[0] == pytest.approx(30.5) and sg["t1_r"].iloc[0] == pytest.approx(1.0)


# ---------------------------------------------------------------- messages
def test_every_event_renders():
    s = AlphaStrategy(AlphaParams())
    ev = s.start_day(_ctx(events=[{"time": "10:00", "name": "ISM", "impact": "high"}]), _warm())
    ev += _run(s, _orb_day("up").bars)
    s2 = AlphaStrategy(AlphaParams())
    s2.start_day(_ctx(), _warm())
    ev += _run(s2, _orb_day("down").bars)
    r = Renderer("TEST", "123")
    kinds = set()
    for e in ev:
        out = r.render(e)
        if out is None:
            continue
        content, emb = out
        kinds.add(e["type"])
        assert emb["title"] and "TEST" in emb["title"] and emb["footer"]["text"]
        json.dumps(emb)
    assert {"brief", "idea", "signal", "t1", "scaleout", "final", "stopped", "done", "day_summary"} <= kinds
    content, emb = r.render(next(e for e in ev if e["type"] == "signal"))
    assert content.startswith("<@&123>") and "Stop loss" in emb["description"] and "Why this trade" in emb["description"]
    brief = r.render(ev[0])[1]
    assert brief["title"].startswith("⚠️ NEWS DAY") and any(f["name"] == "📰 Heads-up" for f in brief["fields"])


# ---------------------------------------------------------------- live bot plumbing
def test_minute_builder():
    mb = MinuteBuilder()
    t0 = datetime(2026, 9, 15, 9, 31, 5, tzinfo=NY).timestamp()
    assert mb.add(t0, 500.0, 500.5, 499.5) is None
    assert mb.add(t0 + 20, 501.0, 501.2, 500.0) is None
    b = mb.add(t0 + 60, 502.0, 502.0, 502.0)
    assert b.ts.minute == 31 and b.open == 500.0 and b.high == 501.2 and b.low == 499.5 and b.close == 501.0


class FakePoster:
    enabled, mode, sent, errors = True, "bot", 0, ()

    def __init__(self):
        self.posts, self.edits = [], []

    async def post(self, content, embed, reply_to=None):
        self.posts.append((embed["title"], reply_to))
        return str(len(self.posts))

    async def edit(self, msg_id, embed):
        self.edits.append((msg_id, embed["title"]))
        return True


async def test_bot_threads_replies_and_stores(settings):
    from abg.engine import AnalysisEngine
    async with AnalysisEngine(settings) as eng:
        bot = AlphaBot(eng, settings)
        bot.poster = FakePoster()
        bot.day = D
        bot.strat = AlphaStrategy(AlphaParams(), bot.learner)
        bot.brief = bot.strat.start_day(_ctx(), _warm())[0]
        await bot.emit(bot.brief)
        for b in _orb_day("up").bars:
            await bot.process(b, scaled=True)
        await bot.end_day()
        titles = [t for t, _ in bot.poster.posts]
        sig_i = next(i for i, t in enumerate(titles) if "SIGNAL VALIDATED" in t)
        t1 = next((t, r) for t, r in bot.poster.posts if "TARGET 1" in t)
        assert t1[1] == str(sig_i + 1)                       # management replies under the signal
        assert bot.poster.posts[sig_i][1] == next(str(i + 1) for i, t in enumerate(titles) if "TRADE IDEA" in t)
        assert bot.store.trades()[0]["setup"] == "orb"
        assert (settings.data_dir / "alpha_learner.json").exists()
        st = bot.status()
        assert st["feed"] == "proxy" and st["learner"] and st["events"]


def test_api_alpha_endpoint(monkeypatch, tmp_path):
    for k, v in {"ABG_ALLOW_SYNTHETIC": "true", "ABG_PROVIDER_ORDER": "synthetic", "ABG_CACHE_DIR": str(tmp_path / "c"),
                 "ABG_DATA_DIR": str(tmp_path / "d"), "ABG_MONITOR_ON_SERVE": "false", "ABG_NOTIFY_DESKTOP": "false",
                 "ABG_ALPHA_ENABLED": "true"}.items():
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient
    from abg.api.server import app
    with TestClient(app) as c:
        r = c.get("/api/alpha").json()
        assert r["status"]["enabled"] is True and r["status"]["feed"] == "proxy" and r["trades"] == []
