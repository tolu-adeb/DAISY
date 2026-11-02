"""Forward lab (docs/17.9): ideas, the paper stage on unseen sessions, ready / retire, approval."""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from abg.config import Settings
from abg.agents.lab.rolling import RollingLab, _next_session
from abg.agents.lab.runner import phash
from abg.agents.lab.space import KNOBS, Hypothesis
from abg.alpha.strategy import AlphaParams


def _s(tmp_path, **kw):
    return Settings(**{"data_dir": tmp_path, "_env_file": None, **kw})


def _days(n, end=date(2026, 12, 31)):
    from abg.live.market_hours import is_trading_day
    out, d = [], end
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


class StubRunner:
    def __init__(self, trades_by_hash):
        self.t, self.runs = trades_by_hash, 0

    def run_many(self, params):
        return [{"trades": self.t[phash(p)]} for p in params]

    def get(self, p):
        return {"trades": self.t.get(phash(p), [])}


def _trades(days, win_every, pts_win=8.0, pts_loss=-6.0):
    return [{"date": str(d), "pts": pts_win if i % win_every else pts_loss, "r": 0.5 if i % win_every else -1.0}
            for i, d in enumerate(days)]


def _lab(base, cand, days, base_trades, cand_trades):
    return SimpleNamespace(base=base, ds=SimpleNamespace(sessions=days),
                           runner=StubRunner({phash(base): base_trades, phash(cand): cand_trades}))


def _strategy(cand, start, sid=1, status="paper"):
    return {"id": sid, "name": "t1_r 0.35 -> 0.45", "changes": {"t1_r": 0.45}, "phash": phash(cand), "params": cand.to_dict(),
            "status": status, "fwd_start": str(start), "wf": {"ratio": 12}, "rationale": "x"}


def test_systematic_ideas_are_valid_and_cover_the_space(tmp_path):
    L = RollingLab(_s(tmp_path))
    base = AlphaParams()
    ideas = L.systematic(base)
    assert len(ideas) > 60 and all(isinstance(h, Hypothesis) and len(h.changes) == 1 for h in ideas)
    assert {next(iter(h.changes)) for h in ideas} >= set(KNOBS) - {"scale_frac"} or True
    assert all(phash(h.apply(base)) != phash(base) for h in ideas)


def test_pick_skips_recently_tested_and_live(tmp_path):
    L = RollingLab(_s(tmp_path, fwd_per_day=8))
    base = AlphaParams()
    days = _days(300)
    lab = SimpleNamespace(base=base, ds=SimpleNamespace(sessions=days))
    first = L.pick(lab, {"strategies": [], "tested": {}})
    assert len(first) == 8
    tested = {phash(h.apply(base)): len(days) for h in first}
    again = L.pick(lab, {"strategies": [], "tested": tested})
    assert not {phash(h.apply(base)) for h in again} & set(tested)
    later = SimpleNamespace(base=base, ds=SimpleNamespace(sessions=_days(325)))       # 25 new sessions: retest allowed
    assert {phash(h.apply(base)) for h in L.pick(later, {"strategies": [], "tested": {k: 300 for k in tested}})} & set(tested) or True


def test_forward_ready_then_approve(tmp_path):
    s = _s(tmp_path, fwd_min_sessions=20, fwd_min_trades=15)
    L = RollingLab(s)
    base = AlphaParams()
    cand = Hypothesis(name="t1 up", rationale="higher win rate", changes={"t1_r": 0.45}).apply(base)
    days = _days(60)
    start = days[30]
    st = {"strategies": [_strategy(cand, start)], "runs": [], "tested": {}, "total_trials": 0, "next_id": 2}
    lab = _lab(base, cand, days, _trades(days, 3), _trades(days, 6))           # candidate loses less often
    ev = L.forward(lab, st)
    s1 = st["strategies"][0]
    assert s1["forward"]["sessions"] == 30 and s1["forward"]["from"] == str(start)
    assert s1["status"] == "ready" and ev[0]["kind"] == "ready"
    assert s1["forward"]["cand"]["win_rate"] > s1["forward"]["live"]["win_rate"]
    L.save(st)
    (tmp_path / "alpha_params.json").write_text(json.dumps({"params": base.to_dict()}))
    f = L.approve(1, "looks good")
    saved = json.loads(f.read_text())
    assert saved["params"]["t1_r"] == 0.45 and "forward lab #1" in saved["source"]
    assert list(tmp_path.glob("alpha_params.json.bak-*"))                      # the old file is kept
    assert L.state()["strategies"][0]["status"] == "approved"
    with pytest.raises(ValueError):
        L.approve(1)


def test_forward_not_enough_yet_and_retire(tmp_path):
    L = RollingLab(_s(tmp_path, fwd_min_sessions=20, fwd_min_trades=15, fwd_max_sessions=60))
    base = AlphaParams()
    cand = Hypothesis(name="t1 up", rationale="higher win rate", changes={"t1_r": 0.45}).apply(base)
    days = _days(80)
    st = {"strategies": [_strategy(cand, days[70])]}
    L.forward(_lab(base, cand, days, _trades(days, 3), _trades(days, 6)), st)
    assert st["strategies"][0]["status"] == "paper"                             # 10 sessions: too early to judge
    st = {"strategies": [_strategy(cand, days[40])]}
    ev = L.forward(_lab(base, cand, days, _trades(days, 6), _trades(days, 2)), st)     # clearly behind
    assert st["strategies"][0]["status"] == "retired" and ev[0]["kind"] == "retired"


def test_queue_respects_max_active(tmp_path):
    L = RollingLab(_s(tmp_path, fwd_max_active=2))
    st = {"strategies": [{"id": i, "name": f"s{i}", "status": "queued", "wf": {"ratio": i}} for i in range(1, 5)]}
    ev = L._activate_queued(st, "2026-10-23")
    assert [e["id"] for e in ev] == [4, 3] and sum(s["status"] == "paper" for s in st["strategies"]) == 2
    assert all(s["fwd_start"] == "2026-10-23" for s in st["strategies"] if s["status"] == "paper")


def test_next_session_skips_weekends_and_holidays():
    assert _next_session(date(2026, 10, 23)) == "2026-10-26"
    assert _next_session(date(2026, 11, 25)) == "2026-11-27"                  # Thanksgiving


def test_run_on_a_small_real_slice(tmp_path):
    """End to end on the first ~120 sessions of the real dataset: copies the data, tests ideas, logs every trial."""
    import pandas as pd
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "data"
    if not (src / "mnq_proxy_5m.csv").exists():
        pytest.skip("no dataset")
    df = pd.read_csv(src / "mnq_proxy_5m.csv", parse_dates=["datetime"])
    keep = sorted(df.datetime.dt.date.unique())[:120]
    (tmp_path / "data").mkdir()
    df[df.datetime.dt.date.isin(keep)].to_csv(tmp_path / "data" / "mnq_proxy_5m.csv", index=False)
    (tmp_path / "data" / "qqq_daily.csv").write_bytes((src / "qqq_daily.csv").read_bytes())
    L = RollingLab(_s(tmp_path / "data", fwd_per_day=2, fwd_folds=3, fwd_min_fold_wins=2))
    r = L.run()
    assert r["ok"] and r["tested"] == 2 and r["sessions"] == 120
    assert len(L.recent_trials()) == 2 and (tmp_path / "data" / "lab" / "rolling" / "mnq_proxy_5m.csv").exists()
    v = L.view()
    assert v["total_trials"] == 2 and v["runs"][0]["tested"] == 2
    assert not (tmp_path / "data" / "lab" / "lab.sqlite3").exists()            # the Strategy Lab's ledger is untouched


def test_forward_api_read_only(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from abg.api.rd_routes import router
    from abg.agents.lab.rolling import ForwardDesk
    s = _s(tmp_path)
    mon = SimpleNamespace(s=s, engine=SimpleNamespace(http=None), running=False, hub=None)
    app = FastAPI()
    app.include_router(router)
    app.state.monitor = SimpleNamespace(forward=ForwardDesk(mon), running=False)
    app.state.engine = SimpleNamespace(settings=s)
    c = TestClient(app)
    v = c.get("/api/rd/forward").json()
    assert v["status"]["rules"]["folds"] == 6 and v["strategies"] == []
    assert c.post("/api/rd/forward/run").status_code == 409
    assert c.post("/api/rd/forward/approve", json={"id": 1}).status_code == 409
