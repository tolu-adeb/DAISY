"""The Strategy Lab and the forward lab feeding each other (docs/17.10): ideas flow, evidence stays clean."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import pytest

from abg.config import Settings
from abg.agents.lab import shared
from abg.agents.lab.ledger import Ledger
from abg.agents.lab.runner import phash
from abg.agents.lab.space import Hypothesis
from abg.alpha.strategy import AlphaParams

SRC = Path(__file__).resolve().parents[1] / "data"


def _ledger(data_dir: Path, rows: list[dict], looks: dict | None = None) -> Ledger:
    led = Ledger(data_dir / "lab" / "lab.sqlite3")
    for r in rows:
        tid = led.add_trial(source="test", name=r["name"], rationale="because it should help the win rate",
                            changes=r["changes"], phash="x", base_phash=r.get("base", "b"), fingerprint=r.get("fp", "f"),
                            holdout_start=r.get("holdout_start", "2026-04-22"), verdict=r["verdict"],
                            summary={"failed": r.get("failed", []), "ratio": r.get("ratio", 10), "win_rate": 0.77}, detail=[])
        for _ in range((looks or {}).get(r["name"], 0)):
            led.add_peek(tid, r.get("holdout_start", "2026-04-22"), "f", False, {})
    return led


def _forward(data_dir: Path, rows: list[dict]) -> None:
    home = data_dir / "lab" / "rolling"
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "trials.jsonl", "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({"at": time.time(), "name": r["name"], "changes": r["changes"], "verdict": r["verdict"],
                                 "failed": r.get("failed", []), "data_to": r["data_to"], "search": {"ratio": 9}}) + "\n")


def test_key_matches_across_spellings():
    assert shared.key({"t1_r": 0.45, "setups": ["orx", "vwap"]}) == shared.key({"setups": ("orx", "vwap"), "t1_r": 0.45})
    assert shared.key({"t1_r": 0.45}) != shared.key({"t1_r": 0.55})


def test_registry_seen_through_and_counts(tmp_path):
    _ledger(tmp_path, [{"name": "T1 at 0.45R", "changes": {"t1_r": 0.45}, "verdict": "candidate"},
                       {"name": "T1 at 0.55R", "changes": {"t1_r": 0.55}, "verdict": "failed_holdout"}],
            looks={"T1 at 0.55R": 1})
    _forward(tmp_path, [{"name": "t1_r 0.35 -> 0.45", "changes": {"t1_r": 0.45}, "verdict": "rejected", "data_to": "2026-10-21",
                         "failed": ["win_rate"]}])
    reg = shared.registry(tmp_path)
    e = reg[shared.key({"t1_r": 0.45})]
    assert [x["lab"] for x in e["events"]] == ["strategy", "forward"]
    assert e["seen_through"] == "2026-10-21"                     # the forward lab saw everything to Oct 21
    assert shared.seen_through(tmp_path, {"t1_r": 0.55}) == str(pd.Timestamp.now().date())     # looked at the holdout today
    lab_only = shared.lab_trials(tmp_path)[0]
    assert lab_only["seen_through"] == "2026-04-21"              # search window only: the day before the holdout
    c = shared.counts(tmp_path)
    assert (c["strategy_lab"], c["forward_lab"], c["total"], c["distinct_ideas"]) == (2, 1, 3, 2)


def test_forward_lab_tests_lab_ideas_first_and_skips_lab_rejections(tmp_path):
    from types import SimpleNamespace

    from abg.agents.lab.rolling import RollingLab
    _ledger(tmp_path, [
        {"name": "near miss trail", "changes": {"trail_atr": 1.75}, "verdict": "rejected", "failed": ["stability"], "ratio": 12},
        {"name": "bad idea", "changes": {"t1_r": 1.2}, "verdict": "rejected", "failed": ["profit", "win_rate"]},
        {"name": "T1 at 0.45R", "changes": {"t1_r": 0.45}, "verdict": "candidate", "ratio": 11},
        {"name": "Old setting", "changes": {"orx_min_trend_atr": 0.5}, "verdict": "candidate"},
        {"name": "T1 at 0.55R", "changes": {"t1_r": 0.55}, "verdict": "failed_holdout"},
    ])
    L = RollingLab(Settings(data_dir=tmp_path, fwd_per_day=8, _env_file=None))
    st = {"strategies": [], "tested": {}}
    ideas, rejected = L.from_strategy_lab(st)
    assert [h.name for h in ideas] == ["T1 at 0.45R", "T1 at 0.55R", "near miss trail"]   # candidate, holdout fail, near miss
    assert shared.key({"t1_r": 1.2}) in rejected and list(st["lab_import_skipped"]) == ["4"]   # setting no longer exists
    base = AlphaParams()
    lab = SimpleNamespace(base=base, ds=SimpleNamespace(sessions=list(range(300))))
    picked = L.pick(lab, st)
    assert [h.name for h in picked[:3]] == ["T1 at 0.45R", "T1 at 0.55R", "near miss trail"][: 4]
    assert all(shared.key(h.changes) != shared.key({"t1_r": 1.2}) for h in picked)
    assert {L.origins.get(id(h)) for h in picked[:3]} == {"strategy lab #3", "strategy lab #5", "strategy lab #1"}


@pytest.mark.skipif(not (SRC / "mnq_proxy_5m.csv").exists(), reason="no dataset")
def test_holdout_look_counts_only_unseen_sessions(tmp_path):
    from abg.agents.lab import Lab
    df = pd.read_csv(SRC / "mnq_proxy_5m.csv", parse_dates=["datetime"])
    keep = sorted(df.datetime.dt.date.unique())[:260]
    d = tmp_path / "data"
    (d / "lab").mkdir(parents=True)
    df[df.datetime.dt.date.isin(keep)].to_csv(d / "mnq_proxy_5m.csv", index=False)
    (d / "qqq_daily.csv").write_bytes((SRC / "qqq_daily.csv").read_bytes())
    lab = Lab.from_paths(d / "mnq_proxy_5m.csv", d / "qqq_daily.csv", d, holdout_months=6, workers=1)
    hold = [str(x) for x in lab.ds.holdout]
    h = Hypothesis(name="T1 at 0.45R", rationale="bank sooner to lift the win rate", changes={"t1_r": 0.45})
    tid = lab.ledger.add_trial(source="test", name=h.name, rationale=h.rationale, changes=h.changes, phash=phash(h.apply(lab.base)),
                               base_phash=phash(lab.base), fingerprint=lab.ds.fingerprint, holdout_start=lab.ds.holdout_start,
                               verdict="candidate", summary={}, detail=[])
    # the forward lab picked the same idea using data deep into the holdout: too few unseen sessions -> refused, no look used
    _forward(d, [{"name": "fw", "changes": {"t1_r": 0.45}, "verdict": "candidate", "data_to": hold[-20]}])
    with pytest.raises(ValueError, match="only 19 holdout"):
        lab.confirm(tid)
    assert lab.peeks_used() == 0
    # picked using data up to 60 sessions before the end: the look counts only the last 59
    (d / "lab" / "rolling" / "trials.jsonl").unlink()
    _forward(d, [{"name": "fw", "changes": {"t1_r": 0.45}, "verdict": "candidate", "data_to": hold[-60]}])
    res = lab.confirm(tid)
    assert res["judged_on"][0] == hold[-59] and res["window"][0] == hold[0] and res["unseen_after"] == hold[-60] and res["holdout"]["days"] == 59
    assert lab.peeks_used() == 1
    q = lab.holdout_queue()
    assert q == [] or all("eligible" in x for x in q)
