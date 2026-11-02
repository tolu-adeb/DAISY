"""Paper ideas you chose to test, waiting for the forward lab (``data/papers/queue.json``).

The forward lab takes them on its next run (``take``), tests them like any other idea, and ``settle`` records the
verdict - or the earlier result when the same settings were already tested recently (they aren't re-run).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from abg.papers import PAPERS_DIR


def _path(data_dir) -> Path:
    p = Path(data_dir).expanduser() / PAPERS_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p / "queue.json"


def load(data_dir) -> dict:
    try:
        q = json.loads(_path(data_dir).read_text(encoding="utf-8"))
        return {"pending": list(q.get("pending") or []), "done": list(q.get("done") or [])}
    except (OSError, ValueError):
        return {"pending": [], "done": []}


def save(data_dir, q: dict) -> None:
    p = _path(data_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"pending": q["pending"], "done": q["done"][-300:]}, default=str, indent=1),
        encoding="utf-8",
    )
    tmp.replace(p)


def key(paper_id, idea_id) -> str:
    return f"{int(paper_id)}:{idea_id}"


def add(data_dir, item: dict) -> None:
    q = load(data_dir)
    k = key(item["paper_id"], item["idea_id"])
    q["pending"] = [x for x in q["pending"] if key(x["paper_id"], x["idea_id"]) != k] + [
        {**item, "queued_at": time.time()}
    ]
    save(data_dir, q)


def finish(data_dir, item: dict, result: dict) -> None:
    q = load(data_dir)
    k = key(item["paper_id"], item["idea_id"])
    q["pending"] = [x for x in q["pending"] if key(x["paper_id"], x["idea_id"]) != k]
    q["done"].append({**item, "result": {**result, "at": time.time()}})
    save(data_dir, q)


def take(data_dir) -> list:
    """Pending ideas as checked ``Hypothesis`` objects (an idea that no longer validates is finished as invalid)."""
    from abg.papers.analyse import hypothesis_of

    out = []
    for item in load(data_dir)["pending"]:
        try:
            out.append(hypothesis_of(item))
        except Exception as e:  # noqa: BLE001 - pydantic/knob errors: finished as invalid, not retried
            finish(data_dir, item, {"status": "invalid", "why": f"no longer valid: {e}"[:200]})
    return out


def trial_for(trials: list[dict], ph: str) -> dict | None:
    """Most recent trial row with these exact settings (``trials`` newest first, as ``recent_trials`` returns)."""
    return next((t for t in trials if t.get("phash") == ph), None)


def outcome(trial: dict | None, lab_state: dict, ph: str, earlier: bool = False) -> dict:
    strat = next(
        (s for s in reversed(lab_state.get("strategies") or []) if s.get("phash") == ph), None
    )
    res = {"status": "already_tested" if earlier else "tested"}
    if trial:
        res.update(
            verdict=trial.get("verdict"),
            failed=(trial.get("failed") or [])[:6],
            data_to=trial.get("data_to"),
            search={
                k: (trial.get("search") or {}).get(k)
                for k in ("trades", "win_rate", "net_usd", "ratio")
            },
            live={
                k: (trial.get("live") or {}).get(k)
                for k in ("trades", "win_rate", "net_usd", "ratio")
            },
        )
    if strat:
        res.update(strategy_id=strat.get("id"), strategy_status=strat.get("status"))
    return res


def settle(data_dir, lab_state: dict, trials: list[dict]) -> int:
    """After a forward-lab run: finish every pending idea the lab has now tested.  Returns how many."""
    q = load(data_dir)
    tested = lab_state.get("tested") or {}
    n = 0
    for item in list(q["pending"]):
        ph = item.get("phash")
        if (
            ph and ph in tested and tested[ph] != item.get("tested_before")
        ):  # tested by a run after it was queued
            finish(data_dir, item, outcome(trial_for(trials, ph), lab_state, ph))
            n += 1
    return n
