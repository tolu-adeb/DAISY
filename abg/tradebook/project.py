"""Forward projections from a finished backtest run.

Stocks (uses equity.csv):
  python -m tradebook.project stocks --run runs/stocks_base --haircut 0.5

Futures (uses daily.csv; adds funded-account evaluation odds):
  python -m tradebook.project futures --run runs/futures_base --risk-usd 100 \
      --eval-target 3000 --eval-trailing-dd 2000 --eval-daily-loss 1000 --eval-max-sessions 60

Optional: --from-date YYYY-MM-DD to project only from recent behaviour (e.g. the out-of-sample period).
Outputs projection.json, cone CSVs and projection.png in <run>/projection/.
"""
from __future__ import annotations

import argparse
import json
import os

import pandas as pd

from abg.tradebook.common.projections import project_evaluation, project_r, project_returns, save_projection


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["stocks", "futures"])
    ap.add_argument("--run", required=True)
    ap.add_argument("--haircut", type=float, default=0.5)
    ap.add_argument("--sims", type=int, default=10000)
    ap.add_argument("--block", type=float, default=5.0)
    ap.add_argument("--from-date")
    ap.add_argument("--start-equity", type=float, default=None)
    ap.add_argument("--risk-usd", type=float, default=100.0)
    ap.add_argument("--eval-target", type=float)
    ap.add_argument("--eval-trailing-dd", type=float)
    ap.add_argument("--eval-daily-loss", type=float)
    ap.add_argument("--eval-max-sessions", type=int, default=60)
    ap.add_argument("--eval-consistency", type=float, default=None, help="max share of profit from the best day, e.g. 0.5")
    ap.add_argument("--eval-dd-mode", choices=["eod", "intraday"], default="eod")
    a = ap.parse_args(argv)
    out = os.path.join(a.run, "projection")
    if a.kind == "stocks":
        eq = pd.read_csv(os.path.join(a.run, "equity.csv"), parse_dates=["date"], index_col="date")
        if a.from_date:
            eq = eq.loc[a.from_date:]
        rets = eq["equity"].pct_change().dropna()
        res = project_returns(rets, n_sims=a.sims, block=a.block, haircut=a.haircut,
                              start_equity=a.start_equity or float(eq["equity"].iloc[-1]))
        save_projection(res, out)
        h = res["haircut"]["252d"]
        print(f"12-month median return {h['return_p50']:.1%} (haircut {a.haircut:.0%}); "
              f"P(loss) {h['prob_loss']:.0%}; median max DD {h['max_dd_p50']:.1%}; 1-in-20 max DD {h['max_dd_p95']:.1%}")
    else:
        d = pd.read_csv(os.path.join(a.run, "daily.csv"), parse_dates=["date"], index_col="date")
        if a.from_date:
            d = d.loc[a.from_date:]
        res = project_r(d["net_R"], a.risk_usd, n_sims=a.sims, block=a.block, haircut=a.haircut)
        if a.eval_target and a.eval_trailing_dd:
            res["evaluation"] = project_evaluation(d, a.risk_usd, a.eval_target, a.eval_trailing_dd, a.eval_daily_loss,
                                                   a.eval_max_sessions, min(a.sims, 5000), a.block, a.haircut,
                                                   a.eval_consistency, a.eval_dd_mode)
            res["evaluation_raw"] = project_evaluation(d, a.risk_usd, a.eval_target, a.eval_trailing_dd, a.eval_daily_loss,
                                                       a.eval_max_sessions, min(a.sims, 5000), a.block, 0.0,
                                                       a.eval_consistency, a.eval_dd_mode)
        save_projection(res, out)
        h = res["haircut"]["252d"]
        print(f"12-month median {h['R_p50']:.1f}R (${h['usd_p50']:,.0f}); 5th pct {h['R_p05']:.1f}R; "
              f"P(loss) {h['prob_loss']:.0%}; 1-in-20 max DD {h['max_dd_R_p95']:.1f}R")
        if "evaluation" in res:
            e = res["evaluation"]
            print(f"evaluation (haircut): pass {e['prob_pass']:.0%}  breach {e['prob_breach']:.0%}  "
                  f"undecided {e['prob_undecided']:.0%}  median sessions to pass {e['median_sessions_to_pass']}")
    print("->", out)


if __name__ == "__main__":
    main()
