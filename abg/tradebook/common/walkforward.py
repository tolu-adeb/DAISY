"""Anchored walk-forward with a trial registry.

For each test year Y: every parameter set in the grid is run on all years
before Y (the training window); the set with the best training score is then
run once on year Y. The stitched test years form the out-of-sample record.
Every run (training or test) is appended to a trial registry CSV so the number
of variants tried is always on record.
"""
from __future__ import annotations

import csv
import datetime as dt
import itertools
import json
import os
from typing import Callable


def expand_grid(grid: dict[str, list]) -> list[dict]:
    if not grid:
        return [{}]
    keys = sorted(grid)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]


def log_trial(registry: str, record: dict) -> None:
    os.makedirs(os.path.dirname(registry) or ".", exist_ok=True)
    new = not os.path.exists(registry)
    with open(registry, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["logged_at", "study", "phase", "period", "params", "score", "trades"])
        w.writerow([dt.datetime.now().isoformat(timespec="seconds"), record.get("study"), record.get("phase"),
                    record.get("period"), json.dumps(record.get("params"), sort_keys=True),
                    record.get("score"), record.get("trades")])


def anchored_walk_forward(years: list[int], grid: dict[str, list], run: Callable[[dict, int, int], dict],
                          score_key: str, first_test_year: int, registry: str, study: str,
                          min_train_trades: int = 20) -> list[dict]:
    """
    run(params, start_year, end_year) -> dict with at least {score_key, 'trades'}.
    Returns one record per test year with the chosen params and the test result.
    """
    out = []
    combos = expand_grid(grid)
    for y in [y for y in years if y >= first_test_year]:
        train_start, train_end = years[0], y - 1
        best, best_score = None, -float("inf")
        for p in combos:
            res = run(p, train_start, train_end)
            log_trial(registry, {"study": study, "phase": "train", "period": f"{train_start}-{train_end}",
                                 "params": p, "score": res.get(score_key), "trades": res.get("trades")})
            sc = res.get(score_key)
            if res.get("trades", 0) >= min_train_trades and sc is not None and sc == sc and sc > best_score:
                best, best_score = p, sc
        if best is None:
            best = combos[0]
        test = run(best, y, y)
        log_trial(registry, {"study": study, "phase": "test", "period": str(y), "params": best,
                             "score": test.get(score_key), "trades": test.get("trades")})
        out.append({"test_year": y, "params": best, "train_score": best_score, **test})
    return out
