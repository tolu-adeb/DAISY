"""Read-only tools over the Strategy Lab and the bot - plain functions first, Strands tools on top.

The plain functions return small JSON-able dicts (an LLM's context is not a database dump).  ``make_lab_tools``
binds them to one ``LabSession`` as Strands ``@tool``s; ``abg.agents.mcp_server`` serves the same functions over
MCP.  The only tool with a side effect is ``submit_hypotheses`` - and all it does is queue backtests for the
deterministic evaluate step.  No tool can confirm on the holdout, approve a proposal or touch live settings.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .lab import Hypothesis, Lab, describe_space

KEYS = ("trades", "win_rate", "net_usd", "max_dd_usd", "ratio", "avg_r")


def _slim(m: dict | None) -> dict:
    return {k: (m or {}).get(k) for k in KEYS}


# --------------------------------------------------------------------------- plain functions
def lab_overview(lab: Lab) -> dict:
    d = lab.describe()
    return {"data": d["data"], "gate": d["gate"], "trials_so_far": d["trials_so_far"],
            "holdout_peeks_used": d["holdout_peeks_used"], "peek_budget": d["peek_budget"],
            "rule": "a change must raise the win rate AND the profit/max-drawdown ratio vs the current settings, "
                    "keep >=80% of trades and >=90% of profit, hold in 3 of 4 sub-periods, have a neighbouring value "
                    "that also works, show no edge on random data, then hold on the locked last-6-months holdout"}


def lab_space(lab: Lab) -> list[dict]:
    return describe_space(lab.base)


def lab_baseline(lab: Lab) -> dict:
    b = lab.baseline()
    return {"search": _slim(b["search"]), "blocks": [_slim(x) for x in b["blocks"]]}


def lab_trials(lab: Lab, limit: int = 40) -> list[dict]:
    out = []
    for t in lab.ledger.trials(limit):
        s = t.get("summary") or {}
        out.append({"id": t["id"], "name": t["name"], "changes": t["changes"], "verdict": t["verdict"],
                    "failed": s.get("failed"), "search": {k: s.get(k) for k in KEYS}, "source": t["source"]})
    return out


def lab_trial(lab: Lab, trial_id: int) -> dict:
    t = lab.ledger.trial(int(trial_id))
    if not t:
        return {"error": f"no trial #{trial_id}"}
    checks = [{k: c.get(k) for k in ("check", "pass", "detail")} for c in (t.get("detail") or [])]
    return {"id": t["id"], "name": t["name"], "rationale": t["rationale"], "changes": t["changes"],
            "verdict": t["verdict"], "summary": t["summary"], "checks": checks}


def insights_digest(repo_root: str | Path | None = None, max_chars: int = 9000) -> str:
    """docs/16 (the research log) sections 1-3 - what already worked and what didn't."""
    roots = [Path(repo_root)] if repo_root else []
    roots += [Path.cwd(), Path(__file__).resolve().parents[2]]
    for r in roots:
        f = r / "docs" / "16-alpha-insights-log.md"
        if f.exists():
            txt = f.read_text(encoding="utf-8", errors="replace")
            a, b = txt.find("## 1. TL;DR"), txt.find("## 4. Detailed breakdowns")
            return (txt[a:b] if a >= 0 and b > a else txt)[:max_chars]
    return "docs/16-alpha-insights-log.md not found"


def bot_recent(data_dir: str | Path, limit: int = 20) -> dict:
    """The live bot's recent paper trades and the adaptive book, read from its own files (read-only)."""
    data_dir = Path(data_dir).expanduser()
    out: dict[str, Any] = {"trades": [], "learner": None}
    db = data_dir / "alpha.sqlite3"
    if db.exists():
        from ..alpha.bot import AlphaStore
        try:
            for t in AlphaStore(db).trades(limit):
                out["trades"].append({k: t.get(k) for k in ("date", "setup", "side_label", "entry", "exit", "exit_reason",
                                                             "pts", "r", "opened")})
        except Exception as e:                       # a locked / synced DB must not break an agent run
            out["error"] = str(e)
    lf = data_dir / "alpha_learner.json"
    if lf.exists():
        from ..alpha.learn import AdaptiveBook
        out["learner"] = AdaptiveBook.load(lf).summary()
    return out


# --------------------------------------------------------------------------- one research run's shared state
@dataclass
class LabSession:
    lab: Lab
    max_rounds: int = 3
    max_per_round: int = 5
    max_model_calls: int = 40
    source: str = "agent"
    round: int = 0
    pending: list[Hypothesis] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)          # every evaluated hypothesis this run
    decision: dict = field(default_factory=dict)               # critic: {"decision": revise|confirm|stop, ...}
    confirmations: list[dict] = field(default_factory=list)
    model_calls: int = 0
    notes: list[str] = field(default_factory=list)
    trajectory: list[str] = field(default_factory=list)       # "role:tool" for every tool call (audit / evals)
    stage: str = "research"                                    # research | evaluate | critic | confirm | report | done
    run_id: str = ""

    _STATE = ("max_rounds", "max_per_round", "max_model_calls", "source", "round", "results", "decision",
              "confirmations", "model_calls", "notes", "trajectory", "stage", "run_id")

    def to_state(self) -> dict:
        d = {k: getattr(self, k) for k in self._STATE}
        d["pending"] = [h.model_dump(mode="json") for h in self.pending]
        return d

    def load_state(self, d: dict) -> "LabSession":
        for k in self._STATE:
            if k in d:
                setattr(self, k, d[k])
        self.pending = [Hypothesis(**h) for h in d.get("pending", [])]
        return self

    def open_candidates(self) -> list[dict]:
        """Candidates from earlier runs on this data + settings that never had their holdout look."""
        lab = self.lab
        from .lab.runner import phash
        bp, fp = phash(lab.base), lab.ds.fingerprint
        mine = {r.get("trial_id") for r in self.results}
        out = []
        for t in lab.ledger.trials(1000):
            if t["verdict"] == "candidate" and t["fingerprint"] == fp and t["base_phash"] == bp and t["id"] not in mine:
                s = t.get("summary") or {}
                out.append({"trial_id": t["id"], "name": t["name"], "changes": t["changes"], "source": t["source"],
                            "search": {k: s.get(k) for k in KEYS}})
        return out

    def summary(self) -> dict:
        return {"round": self.round, "open_candidates_from_earlier_runs": self.open_candidates(), "evaluated": [{"trial_id": r.get("trial_id"), "name": r["name"], "changes": r.get("changes"),
                                                   "verdict": r["verdict"], "failed": r.get("failed"),
                                                   "search": _slim(r.get("search")), "baseline": _slim(r.get("baseline"))}
                                                  for r in self.results],
                "decision": self.decision, "confirmations": [{k: c.get(k) for k in ("trial_id", "name", "pass", "proposal_id",
                                                                                   "peeks_used", "peek_budget")}
                                                             | {"checks": [{k: x.get(k) for k in ("check", "pass", "detail")}
                                                                           for x in c.get("checks", [])]}
                                                             for c in self.confirmations],
                "model_calls": self.model_calls, "notes": self.notes, "trajectory": self.trajectory}


def make_lab_tools(session: LabSession, repo_root: str | Path | None = None) -> dict[str, list]:
    """Strands tools bound to ``session``: {'researcher': [...], 'critic': [...], 'reporter': [...]}"""
    from strands import tool

    lab = session.lab

    @tool
    def overview() -> dict:
        """The lab's data windows, the validation gate, trials so far and holdout looks used."""
        return lab_overview(lab)

    @tool
    def search_space() -> list:
        """Every setting a hypothesis may change, with its current value, allowed range and meaning."""
        return lab_space(lab)

    @tool
    def baseline() -> dict:
        """The current settings' numbers on the search window (and its four sub-periods)."""
        return lab_baseline(lab)

    @tool
    def past_trials(limit: int = 40) -> list:
        """Recent hypotheses already tested (all sources) with their verdicts - don't repeat them."""
        return lab_trials(lab, limit)

    @tool
    def forward_lab() -> dict:
        """The forward lab's results (docs/17.10): ideas it tested on ALL sessions (walk-forward) and the ones paper
        trading on sessions after they were picked.  A paper record is the cleanest evidence there is; an idea the
        forward lab rejected on all data rarely deserves a lab trial."""
        from abg.agents.lab import shared
        return {"recent": [t for t in shared.history_for_prompt(lab.data_dir, 40) if t["lab"] == "forward"],
                "paper": [{k: s.get(k) for k in ("id", "name", "changes", "status", "fwd_start", "forward")}
                          for s in shared.forward_strategies(lab.data_dir) if s.get("status") in ("paper", "ready")],
                "counts": shared.counts(lab.data_dir)}

    @tool
    def holdout_queue() -> list:
        """Spend holdout looks in this order: candidates ranked by their forward-lab paper record, each with the
        holdout sessions it hasn't seen.  Ineligible ones (fewer than 40 unseen) would be refused without a look."""
        return lab.holdout_queue()

    @tool
    def trial_detail(trial_id: int) -> dict:
        """Every check for one trial, with the numbers."""
        return lab_trial(lab, trial_id)

    @tool
    def research_log() -> str:
        """The project's research log (docs/16): what already helped, what didn't, and the traps."""
        return insights_digest(repo_root)

    @tool
    def submit_hypotheses(hypotheses: list[dict]) -> dict:
        """Queue 1-5 hypotheses for backtesting this round.  Each: {"name": str, "rationale": str (why it should
        work - mechanism, not a pattern you saw), "changes": {setting: value} (1-4 settings from search_space)}."""
        if session.round >= session.max_rounds:
            return {"accepted": 0, "error": f"research budget used ({session.max_rounds} rounds) - stop"}
        ok, errors = [], []
        tested = {json.dumps(t["changes"], sort_keys=True, default=list) for t in lab.ledger.trials(500)}
        from abg.agents.lab import shared
        fwd_rejected = {t["key"] for t in shared.forward_trials(lab.data_dir) if t["verdict"] == "rejected" and len(t["failed"]) >= 2}
        for i, h in enumerate(hypotheses[: session.max_per_round]):
            try:
                hy = Hypothesis(**h)
            except Exception as e:  # pydantic validation -> tell the agent exactly what to fix
                errors.append(f"#{i + 1}: {e}")
                continue
            from abg.agents.lab.space import strip_noops
            h2 = strip_noops(hy, lab.base)
            if h2 is None:
                errors.append(f"#{i + 1} '{hy.name}': every change equals the current setting - nothing to test")
                continue
            if h2 is not hy:
                errors.append(f"#{i + 1} '{hy.name}': dropped "
                              f"{', '.join(k for k in hy.changes if k not in h2.changes)} (already the current value)")
                hy = h2
            if json.dumps(hy.changes, sort_keys=True, default=list) in tested:
                errors.append(f"#{i + 1} '{hy.name}': already tested - see past_trials")
                continue
            if shared.key(hy.changes) in fwd_rejected:
                errors.append(f"#{i + 1} '{hy.name}': the forward lab already rejected it on all sessions - see forward_lab")
                continue
            ok.append(hy)
        if len(hypotheses) > session.max_per_round:
            errors.append(f"only the first {session.max_per_round} were considered")
        session.pending = ok
        return {"accepted": len(ok), "names": [h.name for h in ok], "errors": errors}

    @tool
    def round_results() -> dict:
        """Everything evaluated in this run so far, plus the baseline."""
        return session.summary()

    @tool
    def decide(decision: str, reason: str, trial_id: int | None = None) -> dict:
        """Critic's call after reviewing this round: 'revise' (another round of hypotheses), 'confirm' (look at the
        locked holdout for ONE candidate trial_id - this run's candidates or open ones from earlier runs listed in
        round_results; uses scarce budget) or 'stop'."""
        d = decision.strip().lower()
        if d not in ("revise", "confirm", "stop"):
            return {"error": "decision must be revise, confirm or stop"}
        if d == "confirm":
            cands = {r.get("trial_id") for r in session.results if r["verdict"] == "candidate"}
            cands |= {c["trial_id"] for c in session.open_candidates()}
            if trial_id not in cands:
                return {"error": f"trial {trial_id} is not an open candidate (candidates: {sorted(c for c in cands if c)})"}
        if d == "revise" and session.round >= session.max_rounds:
            d, reason = "stop", reason + " (round budget used)"
        session.decision = {"decision": d, "reason": reason, "trial_id": trial_id}
        return {"recorded": session.decision}

    return {"researcher": [overview, search_space, baseline, past_trials, forward_lab, research_log, submit_hypotheses],
            "critic": [round_results, trial_detail, overview, past_trials, forward_lab, holdout_queue, research_log, decide],
            "reporter": [round_results, overview]}
