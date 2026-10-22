"""R&D desk (docs/17): digests (bot, uptime, lab, findings), Discord payloads, the schedule, and the R&D API."""
import json
from datetime import date, datetime

import pytest

from abg.agents.desk import RDDesk
from abg.agents.digest import (NY, bot_section, build_digest, findings, period_window, post_digest, recent_digests,
                               render_discord, save_digest)
from abg.alpha.bot import AlphaStore


def _store(data_dir):
    st = AlphaStore(data_dir / "alpha.sqlite3")
    # Mon Oct 19: up before the open; Tue Oct 20: late start; Wed Oct 21: nothing at all
    st.db.execute("INSERT INTO events (ts, day, type, data, msg, delivered) VALUES (?,?,?,?,?,?)",
                  (datetime(2026, 10, 19, 9, 25, tzinfo=NY).timestamp(), "2026-10-19", "brief", "{}", None, "ok"))
    st.db.execute("INSERT INTO events (ts, day, type, data, msg, delivered) VALUES (?,?,?,?,?,?)",
                  (datetime(2026, 10, 20, 10, 4, tzinfo=NY).timestamp(), "2026-10-20", "brief", "{}", None, "ok"))
    st.db.execute("INSERT INTO events (ts, day, type, data, msg, delivered) VALUES (?,?,?,?,?,?)",
                  (datetime(2026, 10, 20, 10, 5, tzinfo=NY).timestamp(), "2026-10-20", "signal", "{}", None, "late - not posted"))
    st.db.commit()
    for i, (day, pts, r) in enumerate([("2026-10-19", 6.0, 0.15), ("2026-10-19", -40.0, -1.0), ("2026-10-20", 60.0, 1.4)]):
        st.save_trade(day, {"id": i + 1, "pts": pts, "r": r, "setup": "orx", "side_label": "LONG", "exit_reason": "final",
                            "opened": f"{day}T09:50:00"})
    return st


def test_period_windows():
    assert period_window("weekly", date(2026, 10, 22))[:2] == (date(2026, 10, 19), date(2026, 10, 23))
    assert period_window("monthly", date(2026, 9, 30))[:2] == (date(2026, 9, 1), date(2026, 9, 30))
    with pytest.raises(ValueError):
        period_window("yearly", date(2026, 1, 1))


def test_bot_section_counts_trades_uptime_and_stale(tmp_path):
    _store(tmp_path)
    b = bot_section(tmp_path, date(2026, 10, 19), date(2026, 10, 23), now=datetime(2026, 10, 21, 13, 0, tzinfo=NY))
    assert b["trades"] == 3 and b["wins"] == 2 and b["win_rate"] == pytest.approx(2 / 3)
    assert b["net_usd"] == pytest.approx((6 - 40 + 60) * 2 - 3 * 1.24)
    assert b["up_at_open"] == ["2026-10-19"] and b["late_start"] == ["2026-10-20"] and b["down"] == ["2026-10-21"]
    assert b["held_back_stale"] == 1 and b["sessions"] == 3                     # Thu/Fri are in the future


def test_findings_flag_missed_opens_noise_and_the_holdout():
    d = {"bot": {"trades": 3, "win_rate": 2 / 3, "net_r": 0.55, "net_usd": 48.3, "sessions": 3, "up_at_open": ["a"],
                 "late_start": ["2026-10-20"], "down": ["2026-10-21"], "held_back_stale": 1},
         "expected": {"win_rate": 0.76, "avg_r": 0.11},
         "lab": {"trials_in_period": 0, "proposals_pending": [], "fresh_holdout_from": "2027-01-21", "holdout_looks": 6,
                 "look_budget": 6, "data_last_session": "2026-10-21"},
         "regime": {"months": 1, "to": "2026-10-21", "recent": {"trades": 20, "win_rate": 0.70, "avg_r": 0.02, "ratio": 0.5},
                    "all": {"trades": 600, "win_rate": 0.76, "avg_r": 0.11, "ratio": 10.8}}}
    f = " | ".join(findings(d))
    assert "in line with" in f and "normal noise" in f                         # 3 trades can't contradict 76%
    assert "1 of 3 sessions" in f and "2026-10-21" in f
    assert "held back as stale" in f and "2027-01-21" in f and "edge weakening" in f


def test_discord_payload_is_bounded_and_mentions_nobody():
    d = {"period": "weekly", "label": "week of Oct 19, 2026", "findings": ["x" * 5000],
         "bot": {"trades": 3, "win_rate": 0.66, "net_r": 0.5, "net_usd": 40.0, "max_dd_usd": 80.0, "sessions": 3,
                 "up_at_open": ["a"], "best": {"pts": 60, "date": "d"}, "worst": {"pts": -40, "date": "d"}}}
    p = render_discord(d)
    assert p["allowed_mentions"] == {"parse": []} and len(p["embeds"][0]["description"]) <= 3900
    assert p["embeds"][0]["title"].startswith("🧪 R&D weekly")


async def test_post_digest_uses_the_rd_webhook_only(settings):
    class Http:
        calls = []

        async def request(self, method, url, **kw):
            self.calls.append((method, url, kw))
    http = Http()
    assert "no webhook" in await post_digest(settings, http, {"period": "daily", "label": "x", "findings": []})
    s2 = settings.model_copy(update={"rd_webhook_url": "https://discord.com/api/webhooks/1/abc"})
    assert await post_digest(s2, http, {"period": "daily", "label": "x", "findings": []}) == "posted"
    m, url, kw = http.calls[0]
    assert m == "POST" and url.startswith("https://discord.com/api/webhooks/1/abc") and kw["json"]["username"] == "ABG R&D"


def test_build_and_save_digest_without_a_lab(settings, tmp_path):
    _store(settings.data_dir)
    d = build_digest(settings, "weekly", date(2026, 10, 19), with_note=False, now=datetime(2026, 10, 23, 17, 0, tzinfo=NY))
    assert d["bot"]["trades"] == 3 and d["findings"]
    save_digest(settings, d, "preview")
    assert recent_digests(settings)[0]["label"] == "week of Oct 19, 2026"


class _Mon:
    def __init__(self, s):
        self.s, self.engine = s, type("E", (), {"http": None})()


def test_desk_schedule_and_once_per_period(settings):
    s = settings.model_copy(update={"rd_daily_time": "12:20"})
    desk = RDDesk(_Mon(s))
    keys = lambda t: [k for _, k, _ in desk.due(t)]
    assert keys(datetime(2026, 10, 22, 12, 0, tzinfo=NY)) == []                           # before the bot is flat
    assert keys(datetime(2026, 10, 22, 12, 25, tzinfo=NY)) == ["rd:daily:2026-10-22"]
    assert "rd:weekly:2026-W43" in keys(datetime(2026, 10, 23, 16, 35, tzinfo=NY))
    assert keys(datetime(2026, 10, 24, 9, 5, tzinfo=NY)) == ["rd:research:2026-W43"]       # Saturday
    jobs = desk.due(datetime(2026, 11, 2, 8, 30, tzinfo=NY))                               # Mon Nov 2 = 1st weekday
    assert ("monthly", "rd:monthly:2026-10") in [(j, k) for j, k, _ in jobs]
    assert desk.due(datetime(2026, 11, 26, 13, 0, tzinfo=NY)) == []                         # Thanksgiving: no session
    assert desk._once("rd:daily:2026-10-22") and not desk._once("rd:daily:2026-10-22")
    assert not json.loads(desk.state_path.read_text()).get("webhook")                       # state holds keys, not secrets


def test_rd_api_tab(monkeypatch, tmp_path):
    monkeypatch.setenv("ABG_ALLOW_SYNTHETIC", "true")
    monkeypatch.setenv("ABG_PROVIDER_ORDER", "synthetic")
    monkeypatch.setenv("ABG_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("ABG_AI_ENABLED", "false")
    monkeypatch.setenv("ABG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABG_MONITOR_ON_SERVE", "false")
    from fastapi.testclient import TestClient

    from abg.api.server import app
    with TestClient(app) as c:
        r = c.get("/api/rd").json()
        assert r["lab"] is None and "hint" in r and r["desk"]["webhook"] is False
        assert c.get("/api/rd/digest?period=daily").status_code == 200
        assert c.get("/api/rd/digest?period=yearly").status_code == 400
        assert c.post("/api/rd/digest?period=daily").status_code == 400           # no webhook configured
        assert c.get("/api/rd/report/../../etc").status_code in (400, 404)
        html = c.get("/").text
        assert 'data-view="rd"' in html and "/static/rd.js" in html


async def test_dataset_refresh_appends_only_finished_sessions(tmp_path):
    import pandas as pd

    from abg.agents.lab.dataset import refresh_proxy_csv

    def session(d, base):
        idx = pd.date_range(f"{d} 09:30", f"{d} 15:55", freq="5min", tz="America/New_York")
        return pd.DataFrame({"open": base, "high": base + 1, "low": base - 1, "close": base, "volume": 100}, index=idx)
    old = session("2026-10-20", 700.0)
    csv = tmp_path / "mnq_proxy_5m.csv"
    out = old.copy()
    for c in ("open", "high", "low", "close"):
        out[c] = out[c] * 40.0
    out.index = out.index.tz_localize(None)
    out.rename_axis("datetime").reset_index().assign(datetime=lambda x: x.datetime.dt.strftime("%Y-%m-%d %H:%M:%S")).to_csv(csv, index=False)
    q = pd.concat([session("2026-10-20", 700.0), session("2026-10-21", 710.0), session("2026-10-22", 720.0).iloc[:30]])
    q.index = q.index.tz_convert("UTC").tz_localize(None)                       # engine histories are naive UTC

    class Res:
        def __init__(self, df):
            self.value = type("V", (), {"df": df})()

    class Engine:
        async def history(self, symbol, period="60d", interval="5m", use_cache=True):
            return Res(q)
    res = await refresh_proxy_csv(Engine(), csv, None, now=datetime(2026, 10, 22, 13, 0, tzinfo=NY))
    assert res["added_sessions"] == 1 and res["ratio"] == 40.0 and res["last_session"] == "2026-10-21"
    df = pd.read_csv(csv)
    assert df["datetime"].str[:10].value_counts().to_dict() == {"2026-10-20": 78, "2026-10-21": 78}
    assert df["close"].iloc[-1] == 710.0 * 40.0
    again = await refresh_proxy_csv(Engine(), csv, None, now=datetime(2026, 10, 22, 13, 0, tzinfo=NY))
    assert again["added_sessions"] == 0                                         # idempotent
