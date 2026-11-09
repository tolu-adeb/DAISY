"""Anchored walk-forward for the futures rule book.

  python -m tradebook.futures.walk --config configs/futures.yaml --grid configs/grid_futures.yaml \
      --first-test-year 2020 --out runs/futures_wf

Score = live net R per session (change with --score avg_R|net_R|t_stat). Trials are logged to trials.csv.
Sessions are built once; each grid point re-runs only the engine.
"""
from __future__ import annotations

import argparse
import json
import os

import pandas as pd
import yaml

from abg.tradebook.common.config import load_config, with_overrides
from abg.tradebook.common.metrics import r_stats
from abg.tradebook.common.walkforward import anchored_walk_forward
from abg.tradebook.futures.engine import FuturesEngine
from abg.tradebook.futures.run import load


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--grid", required=True)
    ap.add_argument("--first-test-year", type=int, required=True)
    ap.add_argument("--last-test-year", type=int, default=None, help="keep later years untouched as the holdout")
    ap.add_argument("--out", required=True)
    ap.add_argument("--score", default="r_per_session")
    ap.add_argument("--set", action="append", default=[])
    a = ap.parse_args(argv)
    cfg = load_config(a.config, a.set)
    grid = yaml.safe_load(open(a.grid))
    sessions, daily, vix = load(cfg)
    years = sorted({s.date.year for s in sessions})
    if a.last_test_year:
        years = [y for y in years if y <= a.last_test_year]

    def run(params, y0, y1):
        c = with_overrides(cfg, params)
        res = FuturesEngine(c, sessions, daily, vix).run(f"{y0}-01-01", f"{y1}-12-31")
        tr, d = res["trades"], res["daily"]
        live = tr[tr["live"]] if len(tr) else tr
        st = r_stats(live["live_R"]) if len(live) else {"trades": 0}
        st["r_per_session"] = float(d["net_R"].mean()) if len(d) else float("nan")
        return st

    os.makedirs(a.out, exist_ok=True)
    out = anchored_walk_forward(years, grid, run, a.score, a.first_test_year, os.path.join(a.out, "trials.csv"),
                                study=os.path.basename(a.out), min_train_trades=20)
    df = pd.DataFrame(out)
    df.to_csv(os.path.join(a.out, "walk_forward.csv"), index=False)
    with open(os.path.join(a.out, "walk_forward.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    cols = [c for c in ["test_year", "params", a.score, "trades", "net_R", "avg_R", "t_stat"] if c in df.columns]
    print(df[cols].to_string(index=False))


if __name__ == "__main__":
    main()
