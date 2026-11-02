"""Swing desk inside the MNQ Bot (docs/19): seed replay = the audit's numbers, intake, add / cancel, Discord delivery,
the schedule, and the dashboard API.  Offline: prices come from swingdesk/data/prices (through 2026-10-21)."""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from abg.config import Settings

NY = ZoneInfo("America/New_York")


def _settings(tmp_path, **kw):
    base = dict(data_dir=tmp_path / "data", swing_enabled=True, anthropic_api_key=None, _env_file=None)
    base.update(kw)
    return Settings(**base)


class FakeHttp:
    def __init__(self):
        self.posts = []

    async def request(self, method, url, provider=None, json=None, **kw):
        self.posts.append({"method": method, "url": url, "json": json})
        return SimpleNamespace(status_code=200)


@pytest.fixture
def desk(tmp_path):
    from abg.swing.desk import SwingDesk
    d = SwingDesk(_settings(tmp_path), None, None, fetch=False)
    asyncio.run(d.run_pass("seed"))
    return d


def test_seed_reproduces_the_audit_and_posts_nothing(tmp_path):
    from abg.swing.desk import SwingDesk
    from swingdesk.monitor import load_book, scoreboard
    http = FakeHttp()
    d = SwingDesk(_settings(tmp_path, swing_webhook_url="https://discord.com/api/webhooks/1/x"), None, http, fetch=False)
    assert (tmp_path / "data" / "swing" / "data" / "signals.csv").exists()          # seeded from the repo copy
    r = asyncio.run(d.run_pass("seed"))
    sb = scoreboard(load_book())
    assert (sb["taken"], sb["closed"], sb["wins"], sb["win_rate"], sb["sum_R"]) == (63, 40, 32, 80.0, 14.25)
    assert (sb["open"], sb["watching"], sb["skipped"]) == (7, 4, 25)
    assert r["seed"] and len(r["alerts"]) > 100 and http.posts == []                # history is never posted
    assert d.alerts() == [] and len(d.alerts(include_history=True)) == len(r["alerts"])
    again = asyncio.run(d.run_pass("manual"))
    assert again["alerts"] == [] and not again["seed"]                              # replay only alerts on changes


def test_live_book_matches_the_report(desk):
    ideas = {i["symbol"]: i for i in desk.ideas() if i["state"] in ("FILLED", "WATCHING")}
    assert {s for s, i in ideas.items() if i["state"] == "FILLED"} == {"PNTG", "COIN", "BITX", "BXSL", "AMD", "MET", "LLY"}
    assert {s for s, i in ideas.items() if i["state"] == "WATCHING"} == {"CTSH", "BRK.B", "KO", "MA"}
    assert ideas["AMD"]["limit"] == 570 and ideas["AMD"]["tp"] == 622.5 and ideas["AMD"]["r"] == pytest.approx(-0.25, abs=0.01)
    g = desk.gate()
    assert g["spy_close"] == pytest.approx(773.09) and g["spy_sma"] == pytest.approx(770.88, abs=0.01) and not g["market_branch"]


def test_intake_reads_channel_terminal_and_partial_posts():
    from abg.swing.intake import parse_text, validate
    ch = parse_text("CTSH | Cognizant\nEntry Area: 55 - 60\nTarget: 70\nStop Loss (SL): 51 (daily close)", None)
    assert ch["source"] == "channel format" and not ch["missing"]
    f = ch["fields"]
    assert (f["symbol"], f["entry_lo"], f["entry_hi"], f["target_lo"], f["stop"], f["stop_basis"]) == ("CTSH", 55, 60, 70, 51, "close")
    t = parse_text("$NVDA swing long entry zone 117.50-119, SL 112, TP1 130 TP2 138", None)
    assert t["source"] == "terminal parser"
    assert (t["fields"]["entry_lo"], t["fields"]["entry_hi"], t["fields"]["stop"], t["fields"]["target_lo"], t["fields"]["target_hi"]) \
        == (117.5, 119, 112, 130, 138)
    p = parse_text("AAPL long 180-182 target 195", None)
    assert p["missing"] == ["stop"] and any("missing: stop" in w for w in p["warnings"])
    u = parse_text("TP1 hit on NVDA, moving stop to breakeven", None)
    assert u["fields"] is None and any("update" in w for w in u["warnings"])
    assert validate({**t["fields"], "stop": 125}) == ["stop 125 is on the wrong side of the entry 119"]


def test_add_grades_tracks_and_cancels(desk):
    from abg.swing import DESK_ROOT
    from abg.swing.intake import parse_text
    seed_csv = (DESK_ROOT / "data" / "signals.csv").read_bytes()
    f = parse_text("MSFT | Microsoft\nEntry Area: 400 - 410\nTarget: 470\nStop Loss: 380 (daily close)", None)["fields"]
    prev = asyncio.run(desk.preview(fields=f))
    assert not prev["errors"] and prev["verdict"]["entry_px"] == 410 and prev["last"]["date"] == "2026-10-21"
    out = asyncio.run(desk.add(f, "tester", posted_et="2026-10-21 18:00"))
    assert out["key"] == "MSFT@2026-10-21 18:00" and out["idea"]["state"] in ("WATCHING", "SKIPPED")
    assert [a["kind"] for a in out["alerts"]] == ["GRADED"]
    assert desk.alerts(5)[0]["delivered"] == "dashboard"                             # no webhook -> dashboard only
    with pytest.raises(ValueError):
        asyncio.run(desk.add(f, "tester", posted_et="2026-10-21 18:00"))           # same idea twice
    if out["idea"]["state"] == "WATCHING":
        asyncio.run(desk.cancel(out["key"]))
        assert next(i for i in desk.ideas() if i["key"] == out["key"])["state"] == "CANCELLED"
        assert asyncio.run(desk.run_pass("manual"))["alerts"] == []                 # stays cancelled
    with pytest.raises(ValueError):
        asyncio.run(desk.add({**f, "stop": 420}, "tester"))                          # stop above the entry
    assert (DESK_ROOT / "data" / "signals.csv").read_bytes() == seed_csv              # live adds never touch the repo copy
    assert (desk.home / "data" / "signals.csv").read_text().count("tester,MSFT") == 1


def test_discord_delivery_budget_and_scanner_noise(tmp_path):
    from abg.swing.desk import SwingDesk
    http = FakeHttp()
    d = SwingDesk(_settings(tmp_path, swing_webhook_url="https://discord.com/api/webhooks/1/x", swing_max_posts=2), None, http,
                  fetch=False)
    al = [{"kind": "FILLED", "key": f"X{i}@d", "symbol": f"X{i}", "text": f"FILLED X{i}\nbody", "author": "b06wi", "take": True}
          for i in range(4)]
    al.append({"kind": "GRADED", "key": "S@d", "symbol": "S", "text": "SKIP S", "author": "scanner", "take": False})
    counts = asyncio.run(d._deliver(al, seed=False, kind="intraday"))
    assert counts == {"posted": 2, "summarised": 2, "dashboard": 1}
    assert len(http.posts) == 3 and http.posts[-1]["json"]["embeds"][0]["title"] == "+2 more swing alerts"
    assert all(p["url"].endswith("?wait=true") and p["json"]["allowed_mentions"] == {"parse": []} for p in http.posts)


def test_schedule(tmp_path):
    from abg.swing.desk import SwingDesk
    d = SwingDesk(_settings(tmp_path, anthropic_api_key="k"), None, None, fetch=False)
    at = lambda *a: datetime(*a, tzinfo=NY)                                       # noqa: E731
    assert d.schedule(at(2026, 10, 24, 10, 0))["due"] == ["seed"]                   # Saturday: only the first-run seed
    d._set(seeded=1.0)
    assert d.schedule(at(2026, 10, 22, 10, 0))["due"] == ["intraday"]
    assert d.schedule(at(2026, 10, 22, 8, 0))["due"] == []
    assert d.schedule(at(2026, 10, 22, 16, 25))["due"] == ["close"]
    assert d.schedule(at(2026, 10, 26, 10, 0))["due"] == ["intraday"]
    assert d.schedule(at(2026, 11, 26, 10, 0))["due"] == []                         # Thanksgiving
    assert d.schedule(at(2026, 10, 25, 12, 0))["due"] == []                         # research is off by default
    d.s = _settings(tmp_path, anthropic_api_key="k", swing_research="weekly")
    assert d.schedule(at(2026, 10, 25, 12, 0))["due"] == ["research"]


def test_variants_replay_the_audit_table(desk):
    rows = {r["name"]: r for r in asyncio.run(desk.variants())}
    v1 = rows["v1 as configured"]["all"]
    assert (v1["closed"], v1["wins"], v1["sum_R"]) == (40, 32, 14.25)
    both = rows["gate needs both conditions"]["all"]
    assert both["taken"] < v1["taken"]
    assert rows["take profit 0.5R"]["all"]["sum_R"] < v1["sum_R"]


def test_api(desk):
    from abg.swing.routes import router
    app = FastAPI()
    app.include_router(router)
    app.state.monitor = SimpleNamespace(swing=desk, running=True)
    c = TestClient(app)
    r = c.get("/api/swing").json()
    assert r["scoreboard"]["taken"] == 63 and r["gate"]["sma_n"] == 20 and len(r["ideas"]) == 88
    assert r["status"]["seeded"] and not r["status"]["webhook"]
    rec = c.get("/api/swing/record").json()
    assert rec["curve"][-1]["cum"] == pytest.approx(14.25, abs=0.01) and {g["group"] for g in rec["by_tier"]} <= {"A", "B"}
    p = c.post("/api/swing/parse", json={"text": "KO | Coca-Cola\nEntry Area: 90 - 91\nTarget: 97\nStop Loss: 86"}).json()
    assert p["fields"]["symbol"] == "KO" and p["verdict"]["entry_px"] == 91
    assert c.post("/api/swing/ideas", json={"fields": {**p["fields"], "stop": 99}}).status_code == 400
    assert c.post("/api/swing/ideas/cancel", json={"key": "nope"}).status_code == 404
    res = c.get("/api/swing/research").json()
    assert res["config"]["cfg"]["exit"]["take_profit_r"] == 0.75 and res["proposals"] == []
    assert c.get("/api/swing/variants").json()["variants"][0]["all"]["closed"] == 40


def test_repo_checks_out_on_windows():
    """Windows refuses CON/PRN/AUX/NUL/COM1-9/LPT1-9 (any extension) and <>:"|?* - one such path breaks every pull."""
    import re
    import subprocess
    root = Path(__file__).resolve().parents[1]
    paths = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout.split()
    bad = re.compile(r'^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?$', re.I)
    assert paths
    assert [p for p in paths if any(bad.match(x) or re.search(r'[<>:"|?*\\]', x) or x.endswith((".", " "))
                                    for x in p.split("/"))] == []
    from swingdesk.data_sources import price_file, symbol_of
    assert price_file("CON", "x").name == "CON_.csv" and price_file("COIN", "x").name == "COIN.csv"
    assert price_file("BRK.B", "x").name == "BRK.B.csv"
    assert symbol_of("x/CON_.csv") == "CON" and symbol_of("x/COIN.csv") == "COIN"


def test_lock_dead_holder_and_readonly_takeover(tmp_path):
    """A lock left by a process that has exited is free at once; a read-only dashboard takes over by itself and
    can't run swing actions while it is read-only."""
    import json
    import os
    import socket
    import subprocess
    import sys
    import time

    from abg.live.monitor import MonitorLock, _pid_alive
    p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
    assert _pid_alive(os.getpid()) and not _pid_alive(p.pid)
    lock = tmp_path / "monitor.lock"
    lock.write_text(json.dumps({"pid": p.pid, "host": socket.gethostname(), "heartbeat": time.time()}))
    assert MonitorLock(lock).holder() is None and MonitorLock(lock).acquire()
    lock.write_text(json.dumps({"pid": os.getppid(), "host": socket.gethostname(), "heartbeat": time.time()}))
    assert MonitorLock(lock).holder() is not None                       # alive -> still held
    lock.write_text(json.dumps({"pid": 1, "host": "another-pc", "heartbeat": time.time() - 999}))
    assert MonitorLock(lock).holder() is None                           # stale heartbeat -> free


def test_readonly_dashboard_takes_over(tmp_path, monkeypatch):
    import json
    import os
    import socket
    import time

    from mnq.app import api as api_mod
    monkeypatch.setenv("ABG_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ABG_ALPHA_ENABLED", "false")
    monkeypatch.setenv("ABG_RD_ENABLED", "false")
    monkeypatch.setenv("ABG_SWING_ENABLED", "true")
    monkeypatch.setenv("ABG_FINNHUB_API_KEY", "")
    holder = {"pid": os.getppid(), "host": socket.gethostname()}         # a live process that isn't us
    (tmp_path / "monitor.lock").write_text(json.dumps({**holder, "heartbeat": time.time()}))
    monkeypatch.setattr("abg.swing.desk.SwingDesk.run", lambda self: asyncio.sleep(3600))
    app = api_mod.app
    monkeypatch.setattr(app.state, "monitor_retry_s", 0.2, raising=False)
    with TestClient(app) as c:
        time.sleep(0.5)
        assert "read-only" in (app.state.monitor_note or "")
        assert c.post("/api/swing/run").status_code == 409
        assert c.post("/api/swing/digest", json={"period": "daily"}).status_code == 200   # preview is fine
        (tmp_path / "monitor.lock").write_text(json.dumps({**holder, "heartbeat": time.time() - 999}))
        for _ in range(50):
            if app.state.monitor.running:
                break
            time.sleep(0.1)
        assert app.state.monitor.running and app.state.monitor_note is None
        assert json.loads((tmp_path / "monitor.lock").read_text())["pid"] == os.getpid()
