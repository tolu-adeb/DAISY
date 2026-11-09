"""Early-warning study: which indicators flagged crises in advance, and at what false-alarm cost?

  python -m tradebook.stocks.early_warning --config configs/stocks.yaml --out runs/early_warning

Writes:
  flag_study.csv   per flag: share of days on, base rate, precision, lift, recall, forward returns on/off
  event_leads.csv  per event: stress score 20/5/0 days before the start and each flag's lead in trading days
  stress.csv       daily flags, stress score and crisis mode
"""
from __future__ import annotations

import argparse
import os

from abg.tradebook.common.config import load_config
from abg.tradebook.common.events import load_events
from abg.tradebook.stocks import stress as st
from abg.tradebook.stocks.run import build_engine


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--crash", type=float, default=-0.10)
    ap.add_argument("--set", action="append", default=[])
    a = ap.parse_args(argv)
    cfg = load_config(a.config, a.set)
    eng = build_engine(cfg)
    os.makedirs(a.out, exist_ok=True)
    close = eng.panel.close[eng.bench]
    study = st.flag_study(eng.flags, close, a.horizon, a.crash)
    study.to_csv(os.path.join(a.out, "flag_study.csv"), index=False)
    leads = st.event_lead_table(eng.flags, load_events(cfg.get("events_file")))
    leads.to_csv(os.path.join(a.out, "event_leads.csv"), index=False)
    out = eng.flags.copy()
    out["stress"] = eng.stress
    out["crisis_mode"] = eng.crisis
    out.to_csv(os.path.join(a.out, "stress.csv"))
    print(study.to_string(index=False))
    print()
    print(leads.to_string(index=False))


if __name__ == "__main__":
    main()
