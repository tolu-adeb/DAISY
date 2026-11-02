"""Run the systematic scanner over the cached universe and grade/replay its ideas with strategy v1."""
import sys, glob
from datetime import datetime
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from swingdesk.backtest import load_prices
from swingdesk.data_sources import symbol_of
from swingdesk.scanner import find_setups
from swingdesk.engine import Plan, run, summarize
from swingdesk.strategy import grade, load_config, plan_from_config

cfg = load_config(); plan = plan_from_config(cfg)
spy = load_prices(ROOT/"data/prices/SPY.csv")
rows = []
for f in sorted(glob.glob(str(ROOT/"data/prices/*.csv"))):
    sym = symbol_of(f)
    if sym in ("SPY", "MVST"): continue
    px = load_prices(f)
    for s in find_setups(sym, px, start="2026-07-01"):
        s["posted"] = datetime.strptime(s["posted_et"], "%Y-%m-%d %H:%M")
        asof = pd.Timestamp(s["posted"].date())
        v = grade(s, px, spy, cfg, asof=asof)
        r_all = run(s, px, plan)
        r_raw = run(s, px, Plan("posted_target"))
        rows.append({**{k: s[k] for k in ["posted_et","symbol","entry_lo","entry_hi","stop","target_lo"]},
                     "take": v.take, "tier": v.tier, "status_v1": r_all["status"], "r_v1": r_all.get("r"),
                     "status_raw": r_raw["status"], "r_raw": r_raw.get("r")})
df = pd.DataFrame(rows); df.to_csv(ROOT/"analysis/scanner_results.csv", index=False)
def stats(d, col_s, col_r):
    c = d[d[col_s].isin(["WIN","LOSS","SCRATCH"])]
    return f"n={len(c):3d} win={100*(c[col_s]=='WIN').mean():5.1f}% avgR={c[col_r].mean():+.2f} open={int((d[col_s]=='OPEN').sum())}"
print("setups found:", len(df), "| passing gate:", int(df["take"].sum()))
print("all setups, posted target :", stats(df, "status_raw", "r_raw"))
print("all setups, v1 exits      :", stats(df, "status_v1", "r_v1"))
print("gate passed, v1 exits     :", stats(df[df["take"]], "status_v1", "r_v1"))
print("gate failed, v1 exits     :", stats(df[~df["take"]], "status_v1", "r_v1"))
print(df.tail(12).to_string(index=False))
