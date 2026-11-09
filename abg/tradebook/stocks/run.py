"""Run the stocks rule book.

  python -m tradebook.stocks.run --config configs/stocks.yaml --out runs/stocks_base
  python -m tradebook.stocks.run --config configs/stocks.yaml --out runs/no_crisis --set stress.enabled=false
"""
from __future__ import annotations

import argparse
import json
import os

import pandas as pd

from abg.tradebook.common.config import load_config
from abg.tradebook.stocks.data import load_panel
from abg.tradebook.stocks.engine import Engine
from abg.tradebook.stocks.report import summarize


def build_engine(cfg: dict) -> Engine:
    d = cfg["data"]
    panel = load_panel(d["dir"], cfg["benchmark"], d.get("membership"), d.get("sectors"), d.get("sentiment"))
    macro = None
    if d.get("macro") and os.path.exists(d["macro"]):
        macro = pd.read_csv(d["macro"], parse_dates=["date"]).set_index("date").sort_index()
    return Engine(cfg, panel, macro)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--set", action="append", default=[], help="override, e.g. books.xs_momentum.top_n=30")
    ap.add_argument("--start")
    ap.add_argument("--end")
    a = ap.parse_args(argv)
    cfg = load_config(a.config, a.set)
    eng = build_engine(cfg)
    res = eng.run(a.start, a.end)
    s = summarize(res, a.out, cfg.get("events_file"))
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "config_used.json"), "w") as f:
        json.dump(cfg, f, indent=2, default=str)
    st = s["strategy"]
    print(f"CAGR {st['cagr']:.2%}  Sharpe {st['sharpe']:.2f}  MaxDD {st['max_drawdown']:.2%}  -> {a.out}/summary.md")


if __name__ == "__main__":
    main()
