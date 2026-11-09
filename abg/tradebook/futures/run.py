"""Run the futures rule book.

  python -m tradebook.futures.run --config configs/futures.yaml --out runs/futures_base
  python -m tradebook.futures.run --config configs/futures.yaml --out runs/no_daytype --set day_type.enabled=false
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

from abg.tradebook.common.config import load_config
from abg.tradebook.common.events import event_report_r, load_events
from abg.tradebook.common.metrics import group_r_stats, r_stats
from abg.tradebook.futures.data import build_sessions, load_minutes
from abg.tradebook.futures.engine import FuturesEngine


def load(cfg: dict):
    d = cfg["data"]
    m = load_minutes(d["minutes"], d.get("tz_in"), d.get("tz", "America/New_York"))
    sessions, daily = build_sessions(m, cfg["session"].get("rth_start", "09:30"), cfg["session"].get("rth_end", "16:00"))
    vix = None
    if d.get("vix_daily") and os.path.exists(d["vix_daily"]):
        v = pd.read_csv(d["vix_daily"], parse_dates=["date"]).set_index("date")["close"]
        vix = v
    return sessions, daily, vix


def summarize(res: dict, cfg: dict, out: str | None = None) -> dict:
    tr, daily = res["trades"], res["daily"]
    risk_usd = cfg["risk"].get("risk_usd", 100.0)
    live = tr[tr["live"]] if not tr.empty else tr
    shadow = tr[~tr["live"]] if not tr.empty else tr
    s = {"sessions": int(len(daily)),
         "live": r_stats(live["live_R"]) if len(live) else {"trades": 0},
         "live_usd": float(live["live_R"].sum() * risk_usd) if len(live) else 0.0,
         "daily_R": {"mean": float(daily["net_R"].mean()), "sd": float(daily["net_R"].std()),
                     "worst": float(daily["net_R"].min()), "best": float(daily["net_R"].max()),
                     "daily_stop_hits": int(daily["daily_stop_hit"].sum())} if len(daily) else {},
         "shadow_by_setup": group_r_stats(shadow, "setup").to_dict("records") if len(shadow) else [],
         "live_by_setup": group_r_stats(live.rename(columns={"net_R": "raw_R", "live_R": "net_R"}), "setup").to_dict("records") if len(live) else [],
         "live_by_day_type": group_r_stats(live.rename(columns={"net_R": "raw_R", "live_R": "net_R"}), "day_type").to_dict("records") if len(live) else [],
         "shadow_by_setup_and_day_type": shadow.groupby(["setup", "day_type"])["net_R"].agg(["count", "sum", "mean"]).round(3)
            .reset_index().to_dict("records") if len(shadow) else []}
    if len(daily):
        yr = daily.groupby(daily.index.year)["net_R"].agg(["sum", "count"])
        s["by_year"] = {int(k): {"net_R": float(v["sum"]), "sessions": int(v["count"])} for k, v in yr.iterrows()}
        cum = daily["net_R"].cumsum()
        s["max_dd_R"] = float((cum - cum.cummax().clip(lower=0)).min())
        s["crisis_sessions"] = int(daily["crisis"].sum())
    try:
        ev = event_report_r(daily, load_events(cfg.get("events_file")))
        s["events"] = ev.to_dict("records")
    except (FileNotFoundError, KeyError):
        ev = None
    if out:
        os.makedirs(out, exist_ok=True)
        tr.to_csv(os.path.join(out, "trades.csv"), index=False)
        daily.to_csv(os.path.join(out, "daily.csv"))
        if ev is not None:
            ev.to_csv(os.path.join(out, "events.csv"), index=False)
        with open(os.path.join(out, "summary.json"), "w") as f:
            json.dump(s, f, indent=2, default=str)
        with open(os.path.join(out, "summary.md"), "w") as f:
            f.write(to_markdown(s, risk_usd))
    return s


def to_markdown(s: dict, risk_usd: float) -> str:
    L = s["live"]
    lines = ["# Futures backtest summary", "", f"Sessions: {s['sessions']}  |  1R = ${risk_usd:,.0f}", ""]
    if L.get("trades"):
        lines += ["| Live metric | Value |", "| --- | --- |",
                  f"| Trades | {L['trades']} |", f"| Net R | {L['net_R']:.1f} (${s['live_usd']:,.0f}) |",
                  f"| Avg R / trade | {L['avg_R']:.3f} (t = {L['t_stat']:.2f}) |",
                  f"| 90% CI avg R | {L['ci90_low']:.3f} to {L['ci90_high']:.3f} |",
                  f"| Win rate | {L['win_rate']:.1%} |", f"| Profit factor | {L['profit_factor']:.2f} |",
                  f"| Max drawdown (daily, R) | {s.get('max_dd_R', float('nan')):.1f} |",
                  f"| Daily stop hits | {s['daily_R'].get('daily_stop_hits', 0)} |",
                  f"| Crisis sessions | {s.get('crisis_sessions', 0)} |"]
    def table(title, rows, key):
        out = ["", f"## {title}", "", f"| {key} | Trades | Net R | Avg R | t | 90% CI | Win | PF |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for r in rows:
            out.append(f"| {r[key]} | {r['trades']} | {r['net_R']:.1f} | {r['avg_R']:.3f} | {r['t_stat']:.2f} | "
                       f"{r['ci90_low']:.2f} to {r['ci90_high']:.2f} | {r['win_rate']:.0%} | {r['profit_factor']:.2f} |")
        return out
    lines += table("Live by setup", s["live_by_setup"], "setup")
    lines += table("Live by day type", s["live_by_day_type"], "day_type")
    lines += table("Shadow (every setup, every session, unfiltered)", s["shadow_by_setup"], "setup")
    lines += ["", "## Shadow net R by setup and day type", "", "| Setup | Day type | Trades | Net R | Avg R |", "| --- | --- | --- | --- | --- |"]
    for r in s["shadow_by_setup_and_day_type"]:
        lines.append(f"| {r['setup']} | {r['day_type']} | {r['count']} | {r['sum']:.1f} | {r['mean']:.3f} |")
    if s.get("by_year"):
        lines += ["", "## By year", "", "| Year | Sessions | Net R |", "| --- | --- | --- |"]
        for y, v in s["by_year"].items():
            lines.append(f"| {y} | {v['sessions']} | {v['net_R']:.1f} |")
    if s.get("events"):
        lines += ["", "## Crisis and event windows", "", "| Event | Sessions | Trades | Net R | Worst session R | Max DD R |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for e in s["events"]:
            lines.append(f"| {e['event']} | {e['sessions']} | {e['trades']} | {e['net_R']:.1f} | {e['worst_session_R']:.1f} | {e['max_dd_R']:.1f} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--start")
    ap.add_argument("--end")
    a = ap.parse_args(argv)
    cfg = load_config(a.config, a.set)
    sessions, daily, vix = load(cfg)
    eng = FuturesEngine(cfg, sessions, daily, vix)
    res = eng.run(a.start or cfg.get("start"), a.end or cfg.get("end"))
    s = summarize(res, cfg, a.out)
    with open(os.path.join(a.out, "config_used.json"), "w") as f:
        json.dump(cfg, f, indent=2, default=str)
    L = s["live"]
    if L.get("trades"):
        print(f"live trades {L['trades']}  net {L['net_R']:.1f}R  avg {L['avg_R']:.3f}R  t {L['t_stat']:.2f}  "
              f"maxDD {s.get('max_dd_R', 0):.1f}R -> {a.out}/summary.md")
    else:
        print("no live trades ->", a.out)


if __name__ == "__main__":
    main()
