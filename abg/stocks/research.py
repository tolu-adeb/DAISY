"""R&D for the stock desk: does the rule system pass its own bar on *your* data?

This replaces the MNQ refit as what the R&D desk works on.  Three studies, all from the Stocks Rule
Book, section 10, run on the bar store the live desk uses:

``ablations``      experiments 1-6: the full system, each book alone, fixed weights instead of Hedge,
                   no crisis mode, no volatility target, and costs doubled.  Answers "which part carries
                   the edge" and "does it survive realistic costs".
``walk_forward``   anchored walk-forward over a small, pre-registered grid: for each test year, pick
                   the best parameters on every earlier year, then run them once on that year.  The
                   stitched test years are the only honest out-of-sample record.  Every trial is
                   logged, so the number of variants tried is always on the report.
``early_warning``  which stress flags actually led drawdowns on this data, at what false-alarm rate.

The pass bars (section 10.4) are written down here, before any result is seen, and the report says
plainly which ones fail.  Nothing in this module changes a live parameter: findings are proposals.
The rule book's own instruction holds - agents propose, code decides, the owner approves.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from abg.tradebook.common.config import with_overrides
from abg.tradebook.common.metrics import equity_stats, r_stats
from abg.tradebook.common.walkforward import anchored_walk_forward
from abg.tradebook.stocks.books import PanelIndicators
from abg.tradebook.stocks.engine import Engine

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

# Section 10.4 pass bars, fixed before looking at any result.
PASS_BARS = {
    "wf_sharpe_min": 0.7,          # stitched walk-forward Sharpe, and above the benchmark's
    "max_dd_max": 0.25,            # and shallower than the benchmark's
    "positive_years_min": 0.60,    # share of test years with a positive return
    "swing_ci_low_min": 0.0,       # swing books: 90% interval of avg R above 0 after costs x2
}

# A deliberately small grid with an economic reason for each value. Every extra cell is another trial
# the out-of-sample result has to be deflated for, so this stays short unless you widen it on purpose.
DEFAULT_GRID = {
    "books.trend_pullback.rsi_pullback": [35, 40, 45],
    "books.xs_momentum.top_n": [10, 20],
}

SWING_BOOKS = ("trend_pullback", "mean_reversion")


def _clean(x):
    """JSON-safe: NaN and inf become None so the report never claims a number it doesn't have."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (np.floating, float)):
        v = float(x)
        return None if not math.isfinite(v) else round(v, 6)
    if isinstance(x, np.integer):
        return int(x)
    return x


class Lab:
    """One panel and one indicator set, shared by every run in a study."""

    def __init__(self, cfg: dict, panel, macro=None):
        self.cfg = cfg
        self.panel = panel
        self.macro = macro
        self.ind = PanelIndicators(panel)
        self.runs = 0
        self.seconds = 0.0

    def run(self, cfg: dict | None = None, start=None, end=None) -> dict:
        t0 = time.time()
        eng = Engine(cfg or self.cfg, self.panel, self.macro, ind=self.ind)
        res = eng.run(start or (cfg or self.cfg).get("start"), end)
        self.runs += 1
        self.seconds += time.time() - t0
        return res

    def stats(self, res: dict) -> dict:
        eq = res["equity"]
        s = {"strategy": equity_stats(eq["equity"]), "benchmark": equity_stats(eq["benchmark"])}
        tr = res["trades"]
        if len(tr) and "R" in tr and tr["R"].notna().any():
            closed = tr.dropna(subset=["R"])
            s["swing"] = r_stats(closed["R"])
            s["swing_by_book"] = {b: r_stats(g["R"]) for b, g in closed.groupby("book")}
        else:
            s["swing"], s["swing_by_book"] = {"trades": 0}, {}
        yearly = eq[["equity", "benchmark"]].resample("YE").last()
        first = eq[["equity", "benchmark"]].iloc[[0]]
        yr = pd.concat([first, yearly]).pct_change().dropna()
        s["years"] = {int(k.year): {"strategy": float(v["equity"]), "benchmark": float(v["benchmark"])}
                      for k, v in yr.iterrows()}
        s["crisis_share"] = float(eq["crisis"].mean())
        return s


# --------------------------------------------------------------------------- ablations
def ablations(lab: Lab, start=None, end=None) -> list[dict]:
    """Section 10.2, experiments 1-6. Each row: what was changed and what it did."""
    base = lab.cfg
    books = [n for n, p in base["books"].items() if p.get("enabled", True)]
    variants: list[tuple[str, str, dict]] = [("baseline", "the full system as configured", {})]
    for b in books:
        if b == "crisis_alpha":
            continue
        ov = {f"books.{o}.enabled": (o == b) for o in books if o != "crisis_alpha"}
        variants.append((f"only_{b}", f"{b} alone (crisis book still on)", ov))
    variants += [
        ("fixed_weights", "equal fixed weights instead of Hedge", {"allocation.method": "fixed"}),
        ("no_crisis", "crisis mode off", {"stress.enabled": False}),
        ("no_vol_target", "no volatility target", {"allocation.vol_target": None}),
        ("costs_x2", "slippage doubled", {"costs.slippage_bps": 2 * float(base.get("costs", {}).get("slippage_bps", 5.0))}),
    ]
    out = []
    for key, what, ov in variants:
        try:
            res = lab.run(with_overrides(base, ov), start, end)
            s = lab.stats(res)
        except Exception as e:                      # one broken variant must not lose the study
            log.warning("ablation %s failed: %s", key, e)
            out.append({"variant": key, "what": what, "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        st, sw = s["strategy"], s["swing"]
        out.append({"variant": key, "what": what, "overrides": ov,
                    "cagr": st.get("cagr"), "sharpe": st.get("sharpe"), "max_dd": st.get("max_drawdown"),
                    "calmar": st.get("calmar"), "bench_sharpe": s["benchmark"].get("sharpe"),
                    "bench_cagr": s["benchmark"].get("cagr"), "swing_trades": sw.get("trades", 0),
                    "swing_avg_R": sw.get("avg_R"), "swing_ci90_low": sw.get("ci90_low"),
                    "crisis_share": s["crisis_share"]})
    return _clean(out)


# --------------------------------------------------------------------------- walk-forward
def walk_forward(lab: Lab, grid: dict | None = None, first_test_year: int | None = None,
                 last_test_year: int | None = None, registry: str | Path | None = None,
                 score: str = "sharpe", min_train_years: int = 5) -> dict:
    """Anchored walk-forward. Stitches the test years into one out-of-sample equity curve."""
    grid = grid if grid is not None else DEFAULT_GRID
    dates = lab.panel.dates
    start_year = pd.Timestamp(lab.cfg.get("start") or dates[0]).year
    years = sorted({y for y in dates.year if y >= start_year})
    if len(years) < min_train_years + 1:
        return {"skipped": f"need at least {min_train_years + 1} years of bars from {start_year} "
                           f"(have {len(years)}); backfill more history first"}
    ft = first_test_year or years[min_train_years]
    if last_test_year:
        years = [y for y in years if y <= last_test_year]
    last_full = years[-1]
    if dates[-1] < pd.Timestamp(f"{last_full}-12-15"):
        years = years[:-1]                           # a partial final year is not a test year
    if not years or ft > years[-1]:
        return {"skipped": f"no complete test year at or after {ft}"}

    curves: list[pd.Series] = []
    trades: list[pd.DataFrame] = []

    def run(params: dict, y0: int, y1: int) -> dict:
        cfg = with_overrides(lab.cfg, params)
        res = lab.run(cfg, f"{y0}-01-01", f"{y1}-12-31")
        s = equity_stats(res["equity"]["equity"])
        tr = res["trades"]
        s["trades"] = int(tr["R"].notna().sum()) if len(tr) and "R" in tr else 0
        s["_res"] = res
        return s

    def scored(params, y0, y1):
        s = run(params, y0, y1)
        s.pop("_res", None)
        return s

    reg = str(registry) if registry else str(Path("wf_trials.csv"))
    rows = anchored_walk_forward(years, grid, scored, score, ft, reg, study="stocks_wf", min_train_trades=1)
    # re-run each chosen test year once more to keep its curve and trades (cheap: one run per year)
    for r in rows:
        s = run(r["params"], r["test_year"], r["test_year"])
        res = s["_res"]
        eq = res["equity"]["equity"]
        curves.append(eq / eq.iloc[0])
        if len(res["trades"]):
            trades.append(res["trades"])
    if not curves:
        return {"skipped": "walk-forward produced no test years"}

    stitched = []
    level = 1.0
    for c in curves:
        stitched.append(c * level)
        level = float(c.iloc[-1] * level)
    oos = pd.concat(stitched)
    oos = oos[~oos.index.duplicated(keep="last")]
    bench = lab.panel.close[lab.cfg["benchmark"]].reindex(oos.index).ffill()
    oos_stats = equity_stats(oos)
    bench_stats = equity_stats(bench)
    tr = pd.concat(trades) if trades else pd.DataFrame()
    swing = tr[tr["book"].isin(SWING_BOOKS)] if len(tr) else tr
    sw_stats = r_stats(swing["R"]) if len(swing) and swing["R"].notna().any() else {"trades": 0}
    pos_years = [r for r in rows if (r.get("total_return") or 0) > 0]
    try:
        n_trials = sum(1 for _ in open(reg)) - 1
    except OSError:
        n_trials = None
    return _clean({
        "grid": grid, "score": score, "first_test_year": ft, "test_years": [r["test_year"] for r in rows],
        "per_year": [{"year": r["test_year"], "params": r["params"], "train_score": r.get("train_score"),
                      "return": r.get("total_return"), "sharpe": r.get("sharpe"),
                      "max_dd": r.get("max_drawdown"), "trades": r.get("trades")} for r in rows],
        "oos": oos_stats, "benchmark": bench_stats, "swing": sw_stats,
        "positive_years": len(pos_years) / len(rows) if rows else None,
        "trials_logged": n_trials, "registry": reg,
    })


# --------------------------------------------------------------------------- early warning
def early_warning(lab: Lab, horizon: int = 60, crash: float = -0.10, events_file: str | None = None) -> dict:
    from abg.tradebook.stocks import stress as st
    eng = Engine(lab.cfg, lab.panel, lab.macro, ind=lab.ind)
    close = lab.panel.close[lab.cfg["benchmark"]]
    study = st.flag_study(eng.flags, close, horizon, crash)
    keep = study[(study["lift"] > 1.5)] if len(study) else study
    leads = None
    if events_file:
        try:
            from abg.tradebook.common.events import load_events
            lt = st.event_lead_table(eng.flags, load_events(events_file))
            leads = lt.to_dict("records")
        except Exception as e:
            log.info("early warning: no event leads (%s)", e)
    return _clean({"horizon_days": horizon, "crash": crash, "flags": study.to_dict("records"),
                   "worth_keeping": list(keep["flag"]) if len(keep) else [],
                   "crisis_share": float(eng.crisis.mean()) if len(eng.crisis) else None,
                   "event_leads": leads})


# --------------------------------------------------------------------------- verdict
def verdict(wf: dict, abl: list[dict]) -> dict:
    """Each pass bar, checked. ``None`` means the study did not produce the number."""
    checks: dict[str, dict] = {}
    if wf and not wf.get("skipped"):
        o, b = wf.get("oos") or {}, wf.get("benchmark") or {}
        sh, bsh = o.get("sharpe"), b.get("sharpe")
        checks["walk-forward Sharpe >= 0.7 and above the benchmark's"] = {
            "value": sh, "bench": bsh,
            "pass": None if sh is None else bool(sh >= PASS_BARS["wf_sharpe_min"] and (bsh is None or sh > bsh))}
        dd, bdd = o.get("max_drawdown"), b.get("max_drawdown")
        checks["max drawdown no worse than 25% and shallower than the benchmark's"] = {
            "value": dd, "bench": bdd,
            "pass": None if dd is None else bool(dd >= -PASS_BARS["max_dd_max"] and (bdd is None or dd > bdd))}
        py = wf.get("positive_years")
        checks["at least 60% of test years positive"] = {
            "value": py, "pass": None if py is None else bool(py >= PASS_BARS["positive_years_min"])}
    x2 = next((a for a in abl or [] if a.get("variant") == "costs_x2"), None)
    if x2 is not None and not x2.get("error"):
        lo = x2.get("swing_ci90_low")
        checks["swing books: 90% interval of avg R above 0 with costs doubled"] = {
            "value": lo, "trades": x2.get("swing_trades"),
            "pass": None if lo is None else bool(lo > PASS_BARS["swing_ci_low_min"])}
    passed = [k for k, v in checks.items() if v["pass"] is True]
    failed = [k for k, v in checks.items() if v["pass"] is False]
    if not checks:
        overall = "not enough data"
    elif failed:
        overall = "fails"
    elif len(passed) == len(checks):
        overall = "passes"
    else:
        overall = "incomplete"
    return {"overall": overall, "checks": checks, "passed": passed, "failed": failed}


def findings(abl: list[dict], wf: dict, ew: dict) -> list[str]:
    """Plain-English observations. Each one is a proposal, never an automatic change."""
    out: list[str] = []
    base = next((a for a in abl if a.get("variant") == "baseline" and not a.get("error")), None)
    if base:
        alone = [a for a in abl if a.get("variant", "").startswith("only_") and not a.get("error")]
        if alone:
            best = max(alone, key=lambda a: a.get("sharpe") or -9)
            worst = min(alone, key=lambda a: a.get("sharpe") or 9)
            out.append(f"Best single book: {best['variant'][5:]} (Sharpe {_f(best.get('sharpe'))}); "
                       f"weakest: {worst['variant'][5:]} (Sharpe {_f(worst.get('sharpe'))}). "
                       f"The full system: {_f(base.get('sharpe'))}.")
            if (worst.get("sharpe") or 0) < 0:
                out.append(f"{worst['variant'][5:]} lost money on its own over this window. Proposal: "
                           f"keep it in for diversification only if the baseline beats the system "
                           f"without it; otherwise switch it off with ABG_STOCKS_BOOKS.")
        for key, label in (("fixed_weights", "Hedge allocation"), ("no_crisis", "crisis mode"),
                           ("no_vol_target", "the volatility target")):
            v = next((a for a in abl if a.get("variant") == key and not a.get("error")), None)
            if v and base.get("sharpe") is not None and v.get("sharpe") is not None:
                d = base["sharpe"] - v["sharpe"]
                verdict_ = "adds" if d > 0.05 else "costs" if d < -0.05 else "makes little difference to"
                out.append(f"{label.capitalize()} {verdict_} risk-adjusted return here "
                           f"(Sharpe {_f(base['sharpe'])} with it, {_f(v['sharpe'])} without).")
        x2 = next((a for a in abl if a.get("variant") == "costs_x2" and not a.get("error")), None)
        if x2 and base.get("cagr") is not None and x2.get("cagr") is not None:
            out.append(f"Doubling slippage moves CAGR from {_p(base['cagr'])} to {_p(x2['cagr'])}.")
    if wf and not wf.get("skipped"):
        o = wf.get("oos") or {}
        out.append(f"Out of sample ({len(wf.get('test_years') or [])} test years, "
                   f"{wf.get('trials_logged') or '?'} trials logged): CAGR {_p(o.get('cagr'))}, "
                   f"Sharpe {_f(o.get('sharpe'))}, max drawdown {_p(o.get('max_drawdown'))}.")
        picks = {json.dumps(r["params"], sort_keys=True) for r in wf.get("per_year") or []}
        if len(picks) > 1:
            out.append(f"The best parameters changed across test years ({len(picks)} different picks): "
                       f"the grid is choosing between near-equal settings, which is a sign not to tune it "
                       f"further.")
    elif wf and wf.get("skipped"):
        out.append(f"Walk-forward skipped: {wf['skipped']}.")
    if ew and ew.get("flags"):
        keep = ew.get("worth_keeping") or []
        out.append(f"Early-warning flags with lift above 1.5 on this data: "
                   f"{', '.join(keep) if keep else 'none'}. Proposal: restrict stress.flags to those once "
                   f"the history covers at least two real drawdowns.")
    return out


def _f(x, d=2):
    return "—" if x is None else f"{x:.{d}f}"


def _p(x, d=1):
    return "—" if x is None else f"{x * 100:+.{d}f}%"


# --------------------------------------------------------------------------- the whole study
def run_study(cfg: dict, panel, macro=None, out_dir: str | Path | None = None,
              grid: dict | None = None, first_test_year: int | None = None,
              do_wf: bool = True, do_ablation: bool = True, do_ew: bool = True) -> dict:
    t0 = time.time()
    lab = Lab(copy.deepcopy(cfg), panel, macro)
    out_dir = Path(out_dir) if out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    abl = ablations(lab) if do_ablation else []
    wf = walk_forward(lab, grid, first_test_year,
                      registry=(out_dir / "trials.csv") if out_dir else None) if do_wf else {}
    ew = early_warning(lab, events_file=cfg.get("events_file")) if do_ew else {}
    rep = {
        "at": datetime.now(NY).isoformat(timespec="seconds"),
        "data": {"first_day": str(panel.dates[0].date()), "last_day": str(panel.dates[-1].date()),
                 "symbols": len(panel.tickers), "start": cfg.get("start")},
        "ablations": abl, "walk_forward": wf, "early_warning": ew,
        "verdict": verdict(wf, abl), "findings": findings(abl, wf, ew),
        "pass_bars": PASS_BARS, "runs": lab.runs, "seconds": round(time.time() - t0, 1),
    }
    if out_dir:
        (out_dir / "report.json").write_text(json.dumps(_clean(rep), indent=1, default=str), encoding="utf-8")
        (out_dir / "report.md").write_text(render_md(rep), encoding="utf-8")
    return _clean(rep)


# --------------------------------------------------------------------------- rendering
def render_md(rep: dict) -> str:
    v = rep.get("verdict") or {}
    d = rep.get("data") or {}
    L = ["# Stock desk — research report", "",
         f"*{rep.get('at')} · bars {d.get('first_day')} to {d.get('last_day')} · {d.get('symbols')} symbols · "
         f"{rep.get('runs')} runs in {rep.get('seconds')}s*", "",
         f"## Verdict: **{v.get('overall', '—')}**", ""]
    for name, c in (v.get("checks") or {}).items():
        mark = "x" if c["pass"] else " " if c["pass"] is False else "?"
        val = c.get("value")
        shown = "—" if val is None else (f"{val:.2f}" if "Sharpe" in name else f"{val * 100:.1f}%")
        L.append(f"- [{mark}] {name} — {shown}")
    L += ["", "Pass bars were fixed before any result was seen (Stocks Rule Book §10.4).", ""]
    if rep.get("findings"):
        L += ["## Findings", ""] + [f"- {x}" for x in rep["findings"]] + [""]
    abl = rep.get("ablations") or []
    if abl:
        L += ["## Ablations", "", "| Variant | CAGR | Sharpe | Max DD | Swing trades | Swing avg R |",
              "|---|---|---|---|---|---|"]
        for a in abl:
            if a.get("error"):
                L.append(f"| {a['variant']} | error: {a['error'][:60]} | | | | |")
                continue
            L.append(f"| {a['variant']} | {_p(a.get('cagr'))} | {_f(a.get('sharpe'))} | {_p(a.get('max_dd'))} | "
                     f"{a.get('swing_trades') or 0} | {_f(a.get('swing_avg_R'), 3)} |")
        b = abl[0]
        L += ["", f"Benchmark over the same window: CAGR {_p(b.get('bench_cagr'))}, "
                  f"Sharpe {_f(b.get('bench_sharpe'))}.", ""]
    wf = rep.get("walk_forward") or {}
    if wf and not wf.get("skipped"):
        L += ["## Walk-forward (out of sample)", "", "| Year | Params | Return | Sharpe | Max DD |",
              "|---|---|---|---|---|"]
        for r in wf.get("per_year") or []:
            ps = ", ".join(f"{k.split('.')[-1]}={val}" for k, val in (r.get("params") or {}).items())
            L.append(f"| {r['year']} | {ps} | {_p(r.get('return'))} | {_f(r.get('sharpe'))} | {_p(r.get('max_dd'))} |")
        L += ["", f"{wf.get('trials_logged') or '?'} trials logged in `{Path(str(wf.get('registry'))).name}`. "
                  f"A best-of-N result is optimistic by construction; read it against that count.", ""]
    elif wf.get("skipped"):
        L += ["## Walk-forward", "", f"Skipped: {wf['skipped']}", ""]
    ew = rep.get("early_warning") or {}
    if ew.get("flags"):
        L += ["## Early-warning flags", "",
              f"Target: the benchmark falls {abs(ew['crash']) * 100:.0f}% or more within {ew['horizon_days']} days.", "",
              "| Flag | Days on | Precision | Lift | Recall |", "|---|---|---|---|---|"]
        for f in ew["flags"]:
            L.append(f"| {f['flag']} | {_p(f.get('days_on_share'), 0).lstrip('+')} | "
                     f"{_p(f.get('precision'), 0).lstrip('+')} | {_f(f.get('lift'))} | "
                     f"{_p(f.get('recall'), 0).lstrip('+')} |")
        L.append("")
    L += ["---", "", "*Findings are proposals. Nothing here changes a live parameter; "
                     "edit configs/stocks.yaml to adopt one.*"]
    return "\n".join(L)


def embed(rep: dict) -> dict:
    v = rep.get("verdict") or {}
    color = {"passes": 0x2E9B5B, "fails": 0xB3403A}.get(v.get("overall"), 0x8A8F98)
    checks = "\n".join(f"{'✅' if c['pass'] else '❌' if c['pass'] is False else '❔'} {name}"
                       for name, c in (v.get("checks") or {}).items()) or "not enough data for the pass bars yet"
    fields = [{"name": "Pass bars", "value": checks[:1020], "inline": False}]
    if rep.get("findings"):
        fields.append({"name": "Findings (proposals only)", "inline": False,
                       "value": "\n".join(f"• {x}" for x in rep["findings"][:5])[:1020]})
    d = rep.get("data") or {}
    return {"title": f"Stock desk research — {v.get('overall', '—')}",
            "description": f"Bars {d.get('first_day')} → {d.get('last_day')}, {d.get('symbols')} symbols, "
                           f"{rep.get('runs')} runs.",
            "color": color, "fields": fields,
            "footer": {"text": "Full report on the dashboard (R&D tab). Nothing here changes a live setting."},
            "timestamp": rep.get("at")}
