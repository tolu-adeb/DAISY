"""Replay every parsed signal and write analysis/results.csv + a console summary."""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from swingdesk.backtest import load_prices, simulate, features_at  # noqa: E402
from swingdesk.data_sources import price_file  # noqa: E402

raw = json.load(open(ROOT / "data/signals_raw.json"))
texts = {}
for s in raw["signals"]:
    texts.setdefault((s["ts"][:16].replace("T", " "), s["ticker"]), s["text"])

sigs = pd.read_csv(ROOT / "data/signals.csv")
spy = load_prices(ROOT / "data/prices/SPY.csv")
rows = []
for _, r in sigs.iterrows():
    sig = r.to_dict()
    sig["posted"] = datetime.strptime(sig["posted_et"], "%Y-%m-%d %H:%M")
    px = load_prices(price_file(sig['symbol'], ROOT / "data/prices"))
    res = simulate(sig, px)
    feat = features_at(sig, px, spy)
    text = next((t for (ts, tk), t in texts.items() if ts == sig["posted_et"]), "").lower()
    feat.update({
        "mentions_earnings": bool(re.search(r"earnings|report[s]? q|pdufa|fda decision|reports q", text)),
        "kw_fib": "fib" in text or "0.618" in text or "golden pocket" in text,
        "kw_choch": "choch" in text or "change of character" in text,
        "kw_breakout": "breakout" in text or "broke above" in text or "break above" in text,
        "kw_biotech_binary": bool(re.search(r"clinical|biotech|fda|binary", text)),
        "text_len": len(text),
    })
    out = {k: sig[k] for k in ["posted_et", "author", "symbol", "direction", "entry_type", "entry_lo",
                                "entry_hi", "target_lo", "target_hi", "stop", "stop_basis", "levels_source"]}
    out.update(res.__dict__)
    out["notes"] = "; ".join(res.notes)
    out.update(feat)
    rows.append(out)

df = pd.DataFrame(rows)
df.to_csv(ROOT / "analysis/results.csv", index=False)

closed = df[df.status.isin(["WIN", "LOSS"])]
print("status counts:\n", df.status.value_counts().to_string())
print(f"\nclosed trades: {len(closed)}  wins: {(closed.status=='WIN').sum()}  "
      f"win rate: {100*(closed.status=='WIN').mean():.1f}%  avg R: {closed.r_multiple.mean():.2f}  "
      f"sum R: {closed.r_multiple.sum():.2f}")
for a, g in closed.groupby("author"):
    print(f"  {a}: n={len(g)} win%={100*(g.status=='WIN').mean():.1f} avgR={g.r_multiple.mean():.2f}")
opn = df[df.status == "OPEN"]
print(f"\nopen (filled, unresolved): {len(opn)}  mark-to-market avg R {opn.r_multiple.mean():.2f}")
print(df[["posted_et", "author", "symbol", "status", "fill_date", "exit_date", "r_multiple", "pnl_pct",
          "bars_held", "mfe_r", "mae_r", "notes"]].to_string())
