"""Run the real backtester for many settings at once - cached, in parallel, holdout-aware.

Every run covers ALL sessions with a fresh adaptive book (the way the live bot runs), so the learner's state at
the start of the holdout is what it would have been.  What a caller may *see* is decided later: ``metrics`` is
computed per window, and the lab only reveals holdout numbers through ``Lab.confirm`` (counted in the ledger).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from ...alpha.backtest import MNQ_POINT, random_walk_check, run_backtest
from ...alpha.learn import AdaptiveBook
from ...alpha.strategy import AlphaParams

COMMISSION_RT = 1.24


class TimeBudget(Exception):
    """Raised when a run would pass its deadline; everything finished so far is cached, so calling again resumes."""


def phash(p: AlphaParams) -> str:
    return hashlib.sha1(json.dumps(p.to_dict(), sort_keys=True, default=str).encode()).hexdigest()[:16]


def metrics(trades: list[dict], days: list[str], commission_rt: float = COMMISSION_RT,
            point_value: float = MNQ_POINT) -> dict:
    """The numbers the gate uses, for one window.  ``ratio`` = net $ / max drawdown $ (profit per unit of risk)."""
    keep = set(days)
    tr = [t for t in trades if t["date"] in keep]
    n = len(tr)
    if not n:
        return {"trades": 0, "days": len(days), "win_rate": 0.0, "net_usd": 0.0, "max_dd_usd": 0.0, "ratio": 0.0,
                "avg_r": 0.0, "profit_factor": None, "worst_trade_usd": 0.0}
    pts = np.array([t["pts"] for t in tr], float)
    usd = pts * point_value - commission_rt
    by_day: dict[str, float] = {}
    for t, u in zip(tr, usd):
        by_day[t["date"]] = by_day.get(t["date"], 0.0) + u
    cum = np.cumsum([by_day.get(d, 0.0) for d in sorted(keep)])
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:] - cum)) if len(cum) else 0.0
    wins, losses = usd[pts > 0.5], usd[pts < -0.5]
    net = float(usd.sum())
    return {"trades": n, "days": len(days), "win_rate": round(float((pts > 0.5).mean()), 4),
            "net_usd": round(net, 2), "max_dd_usd": round(dd, 2),
            "ratio": round(net / dd, 3) if dd > 0 else (math.inf if net > 0 else 0.0),
            "avg_r": round(float(np.mean([t["r"] for t in tr])), 4),
            "profit_factor": round(float(wins.sum() / -losses.sum()), 3) if len(losses) and losses.sum() < 0 else None,
            "worst_trade_usd": round(float(usd.min()), 2)}


# --------------------------------------------------------------------------- worker side
_W: dict = {}


def _init(bars_csv: str, trend_csv: str | None, holdout_months: int) -> None:
    from .dataset import load_dataset
    _W["ds"] = load_dataset(bars_csv, trend_csv, holdout_months)


def _run_one(pdict: dict, learner: bool) -> dict:
    ds = _W["ds"]
    p = AlphaParams.from_dict(pdict)
    r = run_backtest(ds.bars, p, learner=AdaptiveBook() if learner else None, trend_daily=ds.trend_daily)
    slim = [{k: t[k] for k in ("date", "pts", "r", "setup", "side", "exit_reason", "opened", "regime")} for t in r.trades]
    return {"trades": slim, "days": [d["date"] for d in r.days]}


def _rw_one(pdict: dict) -> dict:
    return random_walk_check(n_days=120, seeds=(1, 2, 3), params=AlphaParams.from_dict(pdict))


class Runner:
    def __init__(self, bars_csv: str | Path, trend_csv: str | Path | None, holdout_months: int, cache_dir: Path,
                 fingerprint: str, workers: int | None = None, learner: bool = True):
        self.args = (str(bars_csv), str(trend_csv) if trend_csv else None, holdout_months)
        self.cache_dir = Path(cache_dir) / fingerprint
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.workers = workers or max(1, min(4, (os.cpu_count() or 2)))
        self.learner = learner
        self.mem: dict[str, dict] = {}
        self.runs = 0

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}{'' if self.learner else '_nl'}.pkl"

    def get(self, p: AlphaParams) -> dict | None:
        k = phash(p)
        if k in self.mem:
            return self.mem[k]
        f = self._path(k)
        if f.exists():
            try:
                self.mem[k] = pickle.loads(f.read_bytes())
                return self.mem[k]
            except Exception:
                return None
        return None

    def run_many(self, params: list[AlphaParams], deadline: float | None = None) -> list[dict]:
        """Backtest every not-yet-cached setting.  With ``deadline`` (a ``time.time()`` value) work is done in
        worker-sized chunks and ``TimeBudget`` is raised before a chunk that wouldn't finish in time."""
        import time as _time
        todo = {}
        for p in params:
            if self.get(p) is None:
                todo[phash(p)] = p
        items = list(todo.items())
        if items:
            chunk = 1 if (len(items) == 1 or self.workers == 1) else self.workers
            ex = None if chunk == 1 else ProcessPoolExecutor(self.workers, initializer=_init, initargs=self.args)
            per_chunk = None
            try:
                for i in range(0, len(items), chunk):
                    part = items[i:i + chunk]
                    if deadline is not None and per_chunk is not None and _time.time() + per_chunk > deadline:
                        raise TimeBudget(f"{len(items) - i} backtests left")
                    t0 = _time.time()
                    if ex is None:
                        if "ds" not in _W:
                            _init(*self.args)
                        outs = [_run_one(p.to_dict(), self.learner) for _, p in part]
                    else:
                        outs = list(ex.map(_run_one, [p.to_dict() for _, p in part], [self.learner] * len(part)))
                    dt = _time.time() - t0
                    per_chunk = dt if per_chunk is None else max(per_chunk, dt)
                    for (k, _), o in zip(part, outs):
                        self.mem[k] = o
                        self.runs += 1
                        try:
                            self._path(k).write_bytes(pickle.dumps(o))
                        except OSError:
                            pass
            finally:
                if ex is not None:
                    ex.shutdown(cancel_futures=True)
        return [self.get(p) for p in params]

    def cached(self, p: AlphaParams) -> bool:
        return self.get(p) is not None

    def random_walk(self, p: AlphaParams, deadline: float | None = None) -> dict:
        import time as _time
        f = self.cache_dir.parent / f"rw_{phash(p)}.json"
        if f.exists():
            return json.loads(f.read_text())
        if deadline is not None and _time.time() + 60 > deadline:
            raise TimeBudget("random-walk check left")
        out = _rw_one(p.to_dict())
        f.write_text(json.dumps(out))
        return out
