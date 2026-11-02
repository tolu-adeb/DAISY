"""scripts/carry_over.py: databases copied consistently, webhooks held until --switch, switch refused while the old
monitor is alive."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("carry_over", ROOT / "scripts" / "carry_over.py")
co = importlib.util.module_from_spec(spec)
spec.loader.exec_module(co)


def _tree(tmp: Path, beat: float = 0.0):
    term, mnq, daisy = tmp / "term", tmp / "mnq", tmp / "daisy"
    (term / "data" / "rd").mkdir(parents=True)
    (mnq / "data" / "swing" / "state").mkdir(parents=True)
    daisy.mkdir()
    c = sqlite3.connect(term / "data" / "alpha.sqlite3")
    c.execute("PRAGMA journal_mode=wal")
    c.execute("CREATE TABLE t (x)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()                                  # left open: the row lives in the WAL
    (term / "data" / "monitor.lock").write_text(json.dumps({"heartbeat": beat}))
    (term / "data" / "rd" / "state.json").write_text("{}")
    (mnq / "data" / "swing" / "state" / "book.json").write_text("[]")
    (term / ".env").write_text("ABG_FINNHUB_API_KEY=k\nABG_ALPHA_WEBHOOK_URL=https://a\nABG_SMTP_HOST=smtp\nABG_DATA_DIR=data\n")
    (mnq / ".env").write_text("ABG_SWING_WEBHOOK_URL=https://s\nABG_MNQ_PRIMARY=false\nABG_SITE_URL=x\n")
    return term, mnq, daisy, c


def test_copy_holds_then_switches(tmp_path):
    term, mnq, daisy, c = _tree(tmp_path)
    assert co.main(["--terminal", str(term), "--mnq", str(mnq), "--daisy", str(daisy)]) == 0
    assert sqlite3.connect(daisy / "data" / "alpha.sqlite3").execute("SELECT x FROM t").fetchall() == [(1,)]
    assert (daisy / "data" / "swing" / "state" / "book.json").exists() and not (daisy / "data" / "monitor.lock").exists()
    env = (daisy / ".env").read_text()
    assert "#HELD ABG_ALPHA_WEBHOOK_URL=https://a" in env and "#HELD ABG_SWING_WEBHOOK_URL=https://s" in env
    assert "#HELD ABG_SMTP_HOST=smtp" in env and "ABG_MNQ_PRIMARY" not in env and "ABG_SITE_URL" not in env
    assert "ABG_SWING_ENABLED=true" in env and env.count("ABG_DATA_DIR=") == 1
    assert co.main(["--terminal", str(term), "--mnq", str(mnq), "--daisy", str(daisy), "--switch"]) == 0
    env = (daisy / ".env").read_text()
    assert "#HELD" not in env and "ABG_ALPHA_WEBHOOK_URL=https://a" in env and env.count("ABG_SWING_ENABLED") == 1
    assert co.main(["--terminal", str(term), "--daisy", str(daisy)]) == 2          # DAISY's data is now the newest
    c.close()


def test_switch_refused_while_old_monitor_alive(tmp_path):
    term, mnq, daisy, c = _tree(tmp_path, beat=time.time())
    assert co.main(["--terminal", str(term), "--mnq", str(mnq), "--daisy", str(daisy), "--switch"]) == 2
    assert not (daisy / ".env").exists()
    c.close()


def test_lock_from_a_closed_window_counts_as_released(tmp_path):
    import socket
    d = tmp_path / "data"
    d.mkdir()
    (d / "monitor.lock").write_text(json.dumps({"pid": 999999, "host": socket.gethostname(), "heartbeat": time.time()}))
    assert co.lock_age(d) is None                                   # dead pid on this machine
    (d / "monitor.lock").write_text(json.dumps({"pid": 999999, "host": "other-pc", "heartbeat": time.time()}))
    assert co.lock_age(d) < 5                                       # another machine: trust the heartbeat
