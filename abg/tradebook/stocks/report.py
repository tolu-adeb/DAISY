"""Summary tables for a stocks run."""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from abg.tradebook.common.events import event_report_equity, load_events
from abg.tradebook.common.metrics import equity_stats, group_r_stats, r_stats


def summarize(res: dict, out_dir: str | None = None, events_file: str | None = None) -> dict:
    eq, trades = res["equity"], res["trades"]
    summary = {"strategy": equity_stats(eq["equity"]), "benchmark": equity_stats(eq["benchmark"])}
    yearly = eq[["equity", "benchmark"]].resample("YE").last()
    first = eq[["equity", "benchmark"]].iloc[[0]]
    yr = pd.concat([first, yearly]).pct_change().dropna()
    yr.index = yr.index.year
    summary["by_year"] = {int(k): {"strategy": float(v["equity"]), "benchmark": float(v["benchmark"])} for k, v in yr.iterrows()}
    rets = eq["equity"].pct_change()
    by_reg = {}
    for reg, g in rets.groupby(eq["regime"]):
        g = g.dropna()
        if len(g) > 5:
            by_reg[reg] = {"days": int(len(g)), "ann_return": float(g.mean() * 252), "ann_vol": float(g.std() * np.sqrt(252))}
    summary["by_regime"] = by_reg
    summary["crisis_days_share"] = float(eq["crisis"].mean())
    if not trades.empty and trades["R"].notna().any():
        tr = trades.dropna(subset=["R"])
        summary["swing_trades"] = r_stats(tr["R"])
        summary["swing_trades_by_book"] = group_r_stats(tr.rename(columns={"R": "net_R"}), "book").to_dict("records")
        summary["exit_reasons"] = tr.groupby(["book", "reason"]).size().rename("n").reset_index().to_dict("records")
    try:
        ev = load_events(events_file)
        er = event_report_equity(eq, ev, ["equity", "benchmark"])
        summary["events"] = er.to_dict("records")
    except FileNotFoundError:
        er = None
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        eq.to_csv(os.path.join(out_dir, "equity.csv"))
        trades.to_csv(os.path.join(out_dir, "trades.csv"), index=False)
        res["fills"].to_csv(os.path.join(out_dir, "fills.csv"), index=False)
        res["hedge"].to_csv(os.path.join(out_dir, "hedge_weights.csv"), index=False)
        res["flags"].to_csv(os.path.join(out_dir, "warning_flags.csv"))
        if er is not None:
            er.to_csv(os.path.join(out_dir, "events.csv"), index=False)
        with open(os.path.join(out_dir, "summary.json"), "w") as f:
            json.dump(summary, f, indent=2, default=str)
        with open(os.path.join(out_dir, "summary.md"), "w") as f:
            f.write(to_markdown(summary))
    return summary


def to_markdown(s: dict) -> str:
    def pct(x):
        return "n/a" if x is None or x != x else f"{x * 100:.1f}%"
    a, b = s["strategy"], s["benchmark"]
    lines = ["# Stocks backtest summary", "",
             "| Metric | Strategy | Benchmark |", "| --- | --- | --- |"]
    for k in ["total_return", "cagr", "ann_vol", "max_drawdown"]:
        lines.append(f"| {k} | {pct(a.get(k))} | {pct(b.get(k))} |")
    for k in ["sharpe", "sortino", "calmar"]:
        lines.append(f"| {k} | {a.get(k, float('nan')):.2f} | {b.get(k, float('nan')):.2f} |")
    lines += ["", "## By year", "", "| Year | Strategy | Benchmark |", "| --- | --- | --- |"]
    for y, v in s["by_year"].items():
        lines.append(f"| {y} | {pct(v['strategy'])} | {pct(v['benchmark'])} |")
    lines += ["", "## By regime (annualized)", "", "| Regime | Days | Return | Vol |", "| --- | --- | --- | --- |"]
    for k, v in s["by_regime"].items():
        lines.append(f"| {k} | {v['days']} | {pct(v['ann_return'])} | {pct(v['ann_vol'])} |")
    if "swing_trades_by_book" in s:
        lines += ["", "## Swing trades (R after costs)", "", "| Book | Trades | Net R | Avg R | t | 90% CI | Win rate | PF |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for r in s["swing_trades_by_book"]:
            lines.append(f"| {r['book']} | {r['trades']} | {r['net_R']:.1f} | {r['avg_R']:.3f} | {r['t_stat']:.2f} | "
                         f"{r['ci90_low']:.2f} to {r['ci90_high']:.2f} | {pct(r['win_rate'])} | {r['profit_factor']:.2f} |")
    if s.get("events"):
        lines += ["", "## Crisis and event windows", "", "| Event | Days | Strategy | Strategy max DD | Benchmark | Benchmark max DD |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for e in s["events"]:
            lines.append(f"| {e['event']} | {e['days']} | {pct(e.get('equity_return'))} | {pct(e.get('equity_max_dd'))} | "
                         f"{pct(e.get('benchmark_return'))} | {pct(e.get('benchmark_max_dd'))} |")
    return "\n".join(lines) + "\n"
