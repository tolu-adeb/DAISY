"""Second pass: candidate rule-sets that target >=75% win rate, with Wilson CIs and walk-forward."""
import json, sys
from datetime import datetime
from pathlib import Path
import numpy as np, pandas as pd
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from swingdesk.backtest import load_prices
from swingdesk.data_sources import price_file
from swingdesk.engine import Plan, run, summarize

res = pd.read_csv(ROOT/"analysis/results.csv")
sigs = pd.read_csv(ROOT/"data/signals.csv"); sigs["posted"] = sigs.posted_et.map(lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M"))
prices = {s: load_prices(price_file(s, ROOT/"data/prices")) for s in sigs.symbol.unique()}
F = res.set_index(["posted_et","symbol"])

def wilson(k, n, z=1.96):
    if n == 0: return (np.nan, np.nan)
    p = k/n; d = 1+z*z/n; c = p+z*z/(2*n); m = z*np.sqrt(p*(1-p)/n+z*z/(4*n*n))
    return round(100*(c-m)/d,1), round(100*(c+m)/d,1)

RULES = {
 "none": lambda f: True,
 "atr<=4%": lambda f: f.atr_pct <= 4,
 "spy<sma20 (market pulled back)": lambda f: not f.spy_above_sma20,
 "patient (zone >=0.25 ATR below at post)": lambda f: f.dist_to_zone_atr >= 0.25,
 "atr<=4% & patient": lambda f: f.atr_pct <= 4 and f.dist_to_zone_atr >= 0.25,
 "atr<=4% & stop>=2ATR": lambda f: f.atr_pct <= 4 and f.stop_dist_atr >= 2,
 "atr<=5% & patient & stop>=2ATR": lambda f: f.atr_pct <= 5 and f.dist_to_zone_atr >= 0.25 and f.stop_dist_atr >= 2,
 "atr<=4% & spy<sma20": lambda f: f.atr_pct <= 4 and not f.spy_above_sma20,
}
PLANS = [Plan("as_posted"), Plan("tp_1R", tp1_r=1.0), Plan("tp_0.75R", tp1_r=0.75),
         Plan("half_0.75R_be", tp1_r=0.75, tp1_frac=0.5)]

def runall(frame, plan):
    out=[]
    for _, r in frame.iterrows():
        d=r.to_dict(); x=run(d, prices[d["symbol"]], plan); x["posted_et"]=d["posted_et"]; x["symbol"]=d["symbol"]; out.append(x)
    return out

def keep(rs, fn):
    k=[]
    for r in rs:
        f=F.loc[(r["posted_et"], r["symbol"])]
        f=f.iloc[0] if isinstance(f, pd.DataFrame) else f
        if fn(f): k.append(r)
    return k

rows=[]
for scope, frame in [("b06wi", sigs[sigs.author=="b06wi"]), ("all authors", sigs)]:
    for p in PLANS:
        base = runall(frame, p)
        for rn, fn in RULES.items():
            for period, sel in [("all", lambda r: True), ("Jul-Aug", lambda r: r["posted_et"] < "2026-09-01"),
                                ("Sep-Oct", lambda r: r["posted_et"] >= "2026-09-01")]:
                rs=[r for r in keep(base, fn) if sel(r)]
                s=summarize(rs); cl=[r for r in rs if r["status"] in ("WIN","LOSS","SCRATCH")]
                k=sum(r["status"]=="WIN" for r in cl); lo,hi=wilson(k,len(cl))
                s.update(scope=scope, plan=p.name, rule=rn, period=period, ci_lo=lo, ci_hi=hi)
                rows.append(s)
df=pd.DataFrame(rows)
df.to_csv(ROOT/"analysis/rule_grid.csv", index=False)
show=df[(df.scope=="b06wi")]
piv=show.pivot_table(index=["plan","rule"], columns="period", values=["win_rate","closed","avg_R"], aggfunc="first")
pd.set_option("display.width",250)
print(piv.to_string())
print()
print(df[(df.scope=="all authors")&(df.period=="all")][["plan","rule","closed","win_rate","ci_lo","ci_hi","avg_R","profit_factor","open"]].to_string(index=False))
# regime timing check
r=res[res.status.isin(["WIN","LOSS"])&(res.author=="b06wi")]
print("\nSPY<SMA20 trades by month:\n", r.assign(m=r.posted_et.str[:7]).groupby(["m","spy_above_sma20"]).status.value_counts().unstack(fill_value=0))
