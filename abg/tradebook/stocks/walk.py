"""Anchored walk-forward for the stocks rule book.

  python -m tradebook.stocks.walk --config configs/stocks.yaml --grid configs/grid_stocks.yaml \
      --first-test-year 2012 --out runs/stocks_wf

The grid YAML maps dotted config keys to lists of values, e.g.
  books.xs_momentum.top_n: [10, 20, 40]
  books.trend_pullback.rsi_pullback: [35, 40, 45]
Score = Sharpe ratio of the run (change with --score cagr|calmar|sharpe).
Every training and test run is logged to <out>/trials.csv.
"""
from __future__ import annotations

import argparse
import json
import os

import pandas as pd
import yaml

from abg.tradebook.common.config import load_config, with_overrides
from abg.tradebook.common.metrics import equity_stats
from abg.tradebook.common.walkforward import anchored_walk_forward
from abg.tradebook.stocks.data import load_panel
from abg.tradebook.stocks.engine import Engine


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--grid", required=True)
    ap.add_argument("--first-test-year", type=int, required=True)
    ap.add_argument("--last-test-year", type=int, default=None, help="keep later years untouched as the holdout")
    ap.add_argument("--out", required=True)
    ap.add_argument("--score", default="sharpe")
    ap.add_argument("--set", action="append", default=[])
    a = ap.parse_args(argv)
    cfg = load_config(a.config, a.set)
    grid = yaml.safe_load(open(a.grid))
    d = cfg["data"]
    panel = load_panel(d["dir"], cfg["benchmark"], d.get("membership"), d.get("sectors"), d.get("sentiment"))
    start_year = pd.Timestamp(cfg.get("start") or panel.dates[0]).year
    years = sorted({y for y in panel.dates.year if y >= start_year})
    if a.last_test_year:
        years = [y for y in years if y <= a.last_test_year]

    def run(params, y0, y1):
        c = with_overrides(cfg, params)
        eng = Engine(c, panel)
        res = eng.run(f"{y0}-01-01", f"{y1}-12-31")
        s = equity_stats(res["equity"]["equity"])
        s["trades"] = int(len(res["trades"]) + len(res["fills"]))
        return s

    os.makedirs(a.out, exist_ok=True)
    out = anchored_walk_forward(years, grid, run, a.score, a.first_test_year,
                                os.path.join(a.out, "trials.csv"), study=os.path.basename(a.out), min_train_trades=1)
    df = pd.DataFrame(out)
    df.to_csv(os.path.join(a.out, "walk_forward.csv"), index=False)
    with open(os.path.join(a.out, "walk_forward.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(df[["test_year", "params", a.score, "cagr", "max_drawdown"]].to_string(index=False))


if __name__ == "__main__":
    main()
