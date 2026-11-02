"""Driver study: what separates winning signals from losing ones, and which execution
plans lift the win rate without killing expectancy.

Writes analysis/study.json (consumed by the report) and prints tables.
"""
import json
import sys
from datetime import datetime
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from swingdesk.backtest import load_prices  # noqa: E402
from swingdesk.data_sources import price_file  # noqa: E402
from swingdesk.engine import Plan, run, summarize  # noqa: E402

res = pd.read_csv(ROOT / "analysis/results.csv")
sigs = pd.read_csv(ROOT / "data/signals.csv")
sigs["posted"] = sigs.posted_et.map(lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M"))
prices = {s: load_prices(price_file(s, ROOT / "data/prices")) for s in sigs.symbol.unique()}
out = {}

# ------------------------------------------------------------ 1. baseline + claimed scoreboard
closed = res[res.status.isin(["WIN", "LOSS"])].copy()
closed["win"] = (closed.status == "WIN").astype(int)
b = closed[closed.author == "b06wi"]
out["baseline"] = {
    "all": {"closed": len(closed), "win_rate": round(100 * closed.win.mean(), 1),
            "avg_R": round(closed.r_multiple.mean(), 2)},
    "b06wi": {"closed": len(b), "win_rate": round(100 * b.win.mean(), 1), "avg_R": round(b.r_multiple.mean(), 2),
              "avg_win_R": round(b[b.win == 1].r_multiple.mean(), 2),
              "avg_loss_R": round(b[b.win == 0].r_multiple.mean(), 2),
              "avg_win_pct": round(b[b.win == 1].pnl_pct.mean(), 2),
              "avg_loss_pct": round(b[b.win == 0].pnl_pct.mean(), 2),
              "median_bars_win": float(b[b.win == 1].bars_held.median()),
              "median_bars_loss": float(b[b.win == 0].bars_held.median())},
    "status_counts": res.status.value_counts().to_dict(),
}

# claimed "target hit" posts inside the export window (reply targets resolved by hand from the export)
claimed = ["SNDK", "V@2026-07-13", "XBI", "MRVL@2026-07-20", "INSW", "IMMX", "RDVT", "PBR", "FEIM", "HALO",
           "IDT", "PANW", "LITE", "MSFT@2026-08-31", "PLTR@2026-10-06", "HNGE", "CON", "V@2026-08-24", "NOW"]
claim_rows = []
for c in claimed:
    sym, _, d = c.partition("@")
    m = res[(res.symbol == sym) & (res.posted_et.str.startswith(d) if d else True)]
    m = m[m.author == "b06wi"].head(1)
    claim_rows.append({"claim": c, "replay_status": m.status.iloc[0] if len(m) else "n/a"})
cl = pd.DataFrame(claim_rows)
out["claimed_hits"] = {"n": len(cl), "replay": cl.replay_status.value_counts().to_dict(),
                       "rows": cl.to_dict("records")}
print("claimed target hits vs replay:\n", cl.replay_status.value_counts().to_string())

# ------------------------------------------------------------ 2. feature buckets (b06wi, stated levels)
def bucket(col, bins, labels):
    g = b.assign(bk=pd.cut(b[col], bins=bins, labels=labels)).groupby("bk", observed=True)
    return [{"bucket": str(k), "n": int(len(v)), "win_rate": round(100 * v.win.mean(), 1),
             "avg_R": round(v.r_multiple.mean(), 2)} for k, v in g]


def flag(col):
    g = b.groupby(col)
    return [{"bucket": str(k), "n": int(len(v)), "win_rate": round(100 * v.win.mean(), 1),
             "avg_R": round(v.r_multiple.mean(), 2)} for k, v in g]


feat = {
    "planned_rr": bucket("planned_rr", [0, 1.0, 1.5, 2.5, 99], ["<1", "1-1.5", "1.5-2.5", ">2.5"]),
    "stop_dist_atr": bucket("stop_dist_atr", [0, 1.5, 2.5, 4, 99], ["<1.5", "1.5-2.5", "2.5-4", ">4"]),
    "target_dist_atr": bucket("target_dist_atr", [0, 2, 3.5, 6, 99], ["<2", "2-3.5", "3.5-6", ">6"]),
    "dist_to_zone_atr": bucket("dist_to_zone_atr", [-99, 0.25, 1, 2, 99], ["in zone", "0.25-1", "1-2", ">2"]),
    "atr_pct": bucket("atr_pct", [0, 2.5, 4, 6, 99], ["<2.5%", "2.5-4%", "4-6%", ">6%"]),
    "rs20_vs_spy": bucket("rs20_vs_spy", [-999, -5, 0, 10, 999], ["<-5", "-5..0", "0..10", ">10"]),
    "sma20_slope_5d": bucket("sma20_slope_5d", [-99, 0, 1.5, 99], ["falling", "flat/up", "rising fast"]),
    "above_sma20": flag("above_sma20"),
    "spy_above_sma20": flag("spy_above_sma20"),
    "mentions_earnings": flag("mentions_earnings"),
    "kw_biotech_binary": flag("kw_biotech_binary"),
    "kw_fib": flag("kw_fib"),
    "stop_basis": flag("stop_basis"),
    "bars_to_fill": bucket("bars_to_fill", [-1, 0, 3, 10, 99], ["same/next bar", "1-3", "4-10", ">10"]),
    "price": bucket("px_at_post", [0, 10, 50, 200, 99999], ["<$10", "$10-50", "$50-200", ">$200"]),
}
out["features"] = feat
for k, v in feat.items():
    print(f"\n{k}"); print(pd.DataFrame(v).to_string(index=False))

# ------------------------------------------------------------ 3. execution-plan sweep
b_sigs = sigs[(sigs.author == "b06wi")].copy()


def eval_plan(plan, frame=b_sigs):
    rs = []
    for _, r in frame.iterrows():
        d = r.to_dict()
        x = run(d, prices[d["symbol"]], plan)
        x["posted_et"], x["symbol"] = d["posted_et"], d["symbol"]
        rs.append(x)
    return rs


plans = [Plan("as_posted"),
         Plan("as_posted_same_day", same_day=True),
         Plan("tol_0.5pct", tol_pct=0.5),
         Plan("confirm_entry", entry="confirm"),
         Plan("tp1_1R_full", tp1_r=1.0, tp1_frac=1.0),
         Plan("tp1_0.75R_full", tp1_r=0.75, tp1_frac=1.0),
         Plan("half_at_1R_be", tp1_r=1.0, tp1_frac=0.5),
         Plan("half_at_0.75R_be", tp1_r=0.75, tp1_frac=0.5),
         Plan("intraday_stops", stop="intraday"),
         Plan("time_stop_15", time_stop=15),
         Plan("confirm+half_1R", entry="confirm", tp1_r=1.0, tp1_frac=0.5),
         Plan("confirm+half_0.75R", entry="confirm", tp1_r=0.75, tp1_frac=0.5),
         Plan("half_1R+time20", tp1_r=1.0, tp1_frac=0.5, time_stop=20),
         ]
sweep = []
plan_results = {}
for p in plans:
    rs = eval_plan(p)
    plan_results[p.name] = rs
    s = summarize(rs); s["plan"] = p.name
    sweep.append(s)
sw = pd.DataFrame(sweep).set_index("plan")
out["plan_sweep"] = sw.reset_index().to_dict("records")
print("\nexecution plans (b06wi signals):\n", sw.to_string())

# ------------------------------------------------------------ 4. filters on top of the best plans
feat_cols = ["planned_rr", "stop_dist_atr", "target_dist_atr", "atr_pct", "rs20_vs_spy", "above_sma20",
             "spy_above_sma20", "mentions_earnings", "kw_biotech_binary", "dist_to_zone_atr", "px_at_post",
             "sma20_slope_5d", "dollar_vol_m"]
fmap = res.set_index(["posted_et", "symbol"])[feat_cols]

FILTERS = {
    "none": lambda f: True,
    "no_binary_events": lambda f: not f.kw_biotech_binary and not f.mentions_earnings,
    "price>=10": lambda f: f.px_at_post >= 10,
    "atr<=4%": lambda f: f.atr_pct <= 4,
    "target<=3.5ATR": lambda f: f.target_dist_atr <= 3.5,
    "rr<=1.5": lambda f: f.planned_rr <= 1.5,
    "rs>0": lambda f: f.rs20_vs_spy > 0,
    "uptrend(sma20 rising)": lambda f: f.sma20_slope_5d > 0,
    "quality: atr<=4% & no_binary & price>=10": lambda f: f.atr_pct <= 4 and not f.kw_biotech_binary
                                                       and not f.mentions_earnings and f.px_at_post >= 10,
    "quality + target<=3.5ATR": lambda f: f.atr_pct <= 4 and not f.kw_biotech_binary and not f.mentions_earnings
                                          and f.px_at_post >= 10 and f.target_dist_atr <= 3.5,
}


def filtered(rs, fn):
    keep = []
    for r in rs:
        try:
            f = fmap.loc[(r["posted_et"], r["symbol"])]
        except KeyError:
            continue
        if isinstance(f, pd.DataFrame):
            f = f.iloc[0]
        if fn(f):
            keep.append(r)
    return keep


grid = []
for pname in ["as_posted", "confirm_entry", "half_at_1R_be", "half_at_0.75R_be", "confirm+half_0.75R",
              "tp1_0.75R_full", "half_1R+time20"]:
    for fname, fn in FILTERS.items():
        s = summarize(filtered(plan_results[pname], fn)); s.update(plan=pname, filter=fname)
        grid.append(s)
gr = pd.DataFrame(grid)
out["plan_x_filter"] = gr.to_dict("records")
print("\nplan x filter:\n", gr[["plan", "filter", "closed", "win_rate", "avg_R", "sum_R", "profit_factor",
                               "open"]].to_string(index=False))

# ------------------------------------------------------------ 5. walk-forward sanity check
# choose on Jul-Aug signals, test on Sep-Oct signals (no re-fitting)
early = b_sigs[b_sigs.posted_et < "2026-09-01"]
late = b_sigs[b_sigs.posted_et >= "2026-09-01"]
wf = []
for p in [Plan("as_posted"), Plan("half_at_0.75R_be", tp1_r=0.75, tp1_frac=0.5),
          Plan("confirm+half_0.75R", entry="confirm", tp1_r=0.75, tp1_frac=0.5)]:
    for fname in ["none", "quality: atr<=4% & no_binary & price>=10", "quality + target<=3.5ATR"]:
        for label, fr in [("Jul-Aug (in-sample)", early), ("Sep-Oct (out-of-sample)", late)]:
            s = summarize(filtered(eval_plan(p, fr), FILTERS[fname])); s.update(plan=p.name, filter=fname, period=label)
            wf.append(s)
wfd = pd.DataFrame(wf)
out["walk_forward"] = wfd.to_dict("records")
print("\nwalk-forward:\n", wfd[["plan", "filter", "period", "closed", "win_rate", "avg_R", "sum_R", "open"]].to_string(index=False))

json.dump(out, open(ROOT / "analysis/study.json", "w"), indent=1, default=str)
