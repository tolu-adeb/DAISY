"""Strategy Lab: test hypotheses about the MNQ bot the honest way (docs/17).

    lab = Lab.from_paths("data/mnq_proxy_5m.csv", "data/qqq_daily.csv", data_dir)
    res = lab.test(Hypothesis(name="...", rationale="...", changes={"t1_r": 0.6}))   # search window only
    if res["verdict"] == "candidate":
        lab.confirm(res["trial_id"])          # one counted look at the locked holdout -> proposal if it holds

Pipeline per hypothesis: whitelist + coerce the changes -> backtest it, the current settings and its
neighbours (one parallel, cached batch) -> search-window gate (sample, frequency, profit, win rate,
profit/drawdown, sub-period consistency) -> stability (a neighbour also passes the headline test) ->
random-walk integrity -> ledger.  ``confirm`` adds the holdout and whole-sample checks and, if they hold,
queues a proposal.  Nothing reaches the live bot until ``approve``.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from ...alpha.strategy import AlphaParams, load_saved_params
from .dataset import Dataset, default_paths, load_dataset
from .ledger import Ledger
from .runner import Runner, TimeBudget, metrics, phash
from .space import KNOBS, Hypothesis, describe_space
from .validate import Gate, compare, confirm, headline_pass

__all__ = ["Lab", "Hypothesis", "Gate", "TimeBudget", "describe_space", "KNOBS"]


def current_params(data_dir: Path) -> AlphaParams:
    f = Path(data_dir) / "alpha_params.json"
    if f.exists():
        try:
            return load_saved_params(json.loads(f.read_text()))
        except (ValueError, OSError):
            pass
    return AlphaParams()


class Lab:
    def __init__(self, ds: Dataset, runner: Runner, ledger: Ledger, data_dir: Path, base: AlphaParams | None = None,
                 gate: Gate | None = None):
        self.ds, self.runner, self.ledger = ds, runner, ledger
        self.data_dir = Path(data_dir)
        self.base = base or current_params(self.data_dir)
        self.gate = gate or Gate()

    @classmethod
    def from_paths(cls, bars_csv=None, trend_csv=None, data_dir: str | Path = "~/.abg-terminal", holdout_months: int = 6,
                   workers: int | None = None, base: AlphaParams | None = None, gate: Gate | None = None,
                   lab_dir: str | Path | None = None) -> "Lab":
        """``data_dir``: the terminal's data dir (where the live ``alpha_params.json`` lives).  ``lab_dir``: the
        ledger + cache, by default ``lab/`` next to the bars CSV - the trial history travels with the dataset."""
        data_dir = Path(data_dir).expanduser()
        if bars_csv is None:
            bars_csv, dflt_trend = default_paths(data_dir)
            trend_csv = trend_csv or dflt_trend
        lab_dir = Path(lab_dir).expanduser() if lab_dir else Path(bars_csv).parent / "lab"
        ds = load_dataset(bars_csv, trend_csv, holdout_months)
        runner = Runner(bars_csv, trend_csv if trend_csv and Path(trend_csv).exists() else None, holdout_months,
                        lab_dir / "cache", ds.fingerprint, workers)
        return cls(ds, runner, Ledger(lab_dir / "lab.sqlite3"), data_dir, base, gate)

    # ------------------------------------------------------------------ read-only views
    def describe(self) -> dict:
        return {"data": self.ds.summary(), "gate": self.gate.to_dict(), "space": describe_space(self.base),
                "trials_so_far": self.ledger.count_trials(self.ds.fingerprint),
                "holdout_peeks_used": self.peeks_used(), "peek_budget": self.gate.peek_budget}

    def peeks_used(self) -> int:
        return self.ledger.peeks(self.ds.holdout_start, str(self.ds.holdout[-1]) if self.ds.holdout else None)

    def baseline(self) -> dict:
        b = self.runner.run_many([self.base])[0]
        out = {"search": metrics(b["trades"], [str(d) for d in self.ds.search]),
               "blocks": [metrics(b["trades"], [str(d) for d in blk]) for blk in self.ds.blocks]}
        return out

    # ------------------------------------------------------------------ test (search window only)
    def test_many(self, hyps: list[Hypothesis], source: str = "cli", deadline: float | None = None) -> list[dict]:
        """``deadline``: raise ``TimeBudget`` instead of running past it (finished backtests stay cached, so a
        second call resumes).  Nothing is written to the ledger until every backtest for the batch is done."""
        cands = [h.apply(self.base) for h in hyps]
        neigh: list[list[AlphaParams]] = []
        for h in hyps:
            ns = []
            for k, v in h.changes.items():
                cur = getattr(self.base, k)
                for nv in KNOBS[k].neighbours(v):
                    if nv == cur:                      # the current value is the baseline, not a neighbour
                        continue
                    ns.append(Hypothesis(name=h.name, rationale=h.rationale, changes={**h.changes, k: nv}).apply(self.base))
            neigh.append(ns)
        self.runner.run_many([self.base] + cands + [p for ns in neigh for p in ns], deadline=deadline)
        base_run = self.runner.get(self.base)
        if deadline is not None:                                   # random-walk checks first, so the ledger write is atomic
            for h, cp, ns in zip(hyps, cands, neigh):
                if phash(cp) == phash(self.base):
                    continue
                res = compare(self.runner.get(cp), base_run, self.ds, self.gate)
                stable = not ns or any(headline_pass(compare(self.runner.get(n), base_run, self.ds, self.gate)) for n in ns)
                if res["pass"] and stable:
                    self.runner.random_walk(cp, deadline=deadline)
        out = []
        for h, cp, ns in zip(hyps, cands, neigh):
            if phash(cp) == phash(self.base):
                out.append({"name": h.name, "verdict": "invalid", "why": "no change from the current settings"})
                continue
            res = compare(self.runner.get(cp), base_run, self.ds, self.gate)
            if ns:
                ok = [headline_pass(compare(self.runner.get(n), base_run, self.ds, self.gate)) for n in ns]
                res["checks"].append({"check": "stability", "pass": any(ok),
                                      "detail": f"{sum(ok)}/{len(ok)} neighbouring values also beat the current win rate "
                                                f"and profit/drawdown (needs at least one - not a lone spike)"})
            if all(x["pass"] for x in res["checks"]):
                rw = self.runner.random_walk(cp)
                res["checks"].append({"check": "random_walk", "pass": abs(rw["avg_r"]) < self.gate.rw_max_abs_r,
                                      "detail": f"{rw['avg_r']:+.3f}R over {rw['trades']} random-walk trades (must be ~0)"})
            res["pass"] = all(x["pass"] for x in res["checks"])
            verdict = "candidate" if res["pass"] else "rejected"
            failed = [x["check"] for x in res["checks"] if not x["pass"]]
            summary = {k: res["search"][k] for k in ("trades", "win_rate", "net_usd", "max_dd_usd", "ratio", "avg_r")}
            summary["baseline"] = {k: res["baseline"][k] for k in ("trades", "win_rate", "net_usd", "max_dd_usd", "ratio", "avg_r")}
            summary["failed"] = failed
            tid = self.ledger.add_trial(source=source, name=h.name, rationale=h.rationale, changes=h.changes,
                                        phash=phash(cp), base_phash=phash(self.base), fingerprint=self.ds.fingerprint,
                                        holdout_start=self.ds.holdout_start, verdict=verdict, summary=summary,
                                        detail=res["checks"])
            out.append({"trial_id": tid, "name": h.name, "changes": h.changes, "verdict": verdict, "failed": failed,
                        "search": res["search"], "baseline": res["baseline"], "checks": res["checks"]})
        return out

    def test(self, h: Hypothesis, source: str = "cli") -> dict:
        return self.test_many([h], source)[0]

    # ------------------------------------------------------------------ confirm (counted holdout look)
    def confirm(self, trial_id: int, deadline: float | None = None) -> dict:
        t = self.ledger.trial(trial_id)
        if not t:
            raise ValueError(f"no trial #{trial_id}")
        if t["verdict"] != "candidate":
            raise ValueError(f"trial #{trial_id} is '{t['verdict']}' - only search-window candidates may see the holdout")
        if t["fingerprint"] != self.ds.fingerprint or t["base_phash"] != phash(self.base):
            raise ValueError("the data or the current settings changed since this trial - re-test it first")
        used = self.peeks_used()
        if used >= self.gate.peek_budget:
            raise ValueError(f"holdout from {self.ds.holdout_start} is used up ({used} looks). Wait for new sessions "
                             f"(a fresh holdout) instead of re-using it")
        h = Hypothesis(name=t["name"], rationale=t["rationale"], changes=t["changes"])
        cp = h.apply(self.base)
        cand, base = self.runner.run_many([cp, self.base], deadline=deadline)
        res = confirm(cand, base, self.ds, self.gate)
        res["window"] = [self.ds.holdout_start, str(self.ds.holdout[-1])]
        self.ledger.add_peek(trial_id, self.ds.holdout_start, self.ds.fingerprint, res["pass"], res)
        out = {"trial_id": trial_id, "name": t["name"], "changes": t["changes"], "pass": res["pass"], **res,
               "peeks_used": used + 1, "peek_budget": self.gate.peek_budget,
               "trials_so_far": self.ledger.count_trials(self.ds.fingerprint)}
        if res["pass"]:
            self.ledger.set_verdict(trial_id, "confirmed")
            ev = {"search": t["summary"], "holdout": res["holdout"], "holdout_baseline": res["holdout_baseline"],
                  "full": res["full"], "full_baseline": res["full_baseline"], "checks": t["detail"] + res["checks"],
                  "trials_so_far": out["trials_so_far"], "data": self.ds.summary()}
            out["proposal_id"] = self.ledger.add_proposal(trial_id, t["name"], t["changes"], cp.to_dict(), ev)
        else:
            self.ledger.set_verdict(trial_id, "failed_holdout")
        return out

    # ------------------------------------------------------------------ human decision
    def approve(self, pid: int, note: str = "") -> Path:
        p = self.ledger.proposal(pid)
        if not p or p["status"] != "pending":
            raise ValueError(f"proposal #{pid} is not pending")
        f = self.data_dir / "alpha_params.json"
        if f.exists():
            shutil.copy2(f, f.with_suffix(f".json.bak-{int(time.time())}"))
        params = AlphaParams.from_dict(p["params"]).to_dict()
        f.write_text(json.dumps({"params": params, "source": f"strategy lab proposal #{pid} ({p['name']})",
                                 "approved": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note}, indent=1))
        self.ledger.decide(pid, "approved", note)
        return f

    def reject(self, pid: int, note: str = "") -> None:
        self.ledger.decide(pid, "rejected", note)
