"""Strands Evals for the Strategy Lab's critic agent (``abg lab eval``).

The critic is the agent with the most leverage - it decides whether to spend a scarce holdout look - so it gets a
regression suite built from REAL trials in the ledger.  Each case hands the critic one round of results and checks
its decision with deterministic evaluators (no LLM judge needed):

* never ``confirm`` a trial the gate rejected;
* don't confirm a candidate that resembles one that already failed the holdout (same knobs, nearby values);
* always read ``round_results`` before calling ``decide``, and call ``decide`` exactly once.

Nothing here touches the holdout: the critic only *records* a decision; code would act on it in a real run.
"""
from __future__ import annotations

from typing import Any

from .lab import Lab
from .tools import LabSession


def _as_result(t: dict) -> dict:
    s = t.get("summary") or {}
    return {"trial_id": t["id"], "name": t["name"], "changes": t["changes"],
            "verdict": "candidate" if t["verdict"] in ("candidate", "failed_holdout", "confirmed") else t["verdict"],
            "failed": s.get("failed"), "search": {k: s.get(k) for k in ("trades", "win_rate", "net_usd", "max_dd_usd", "ratio", "avg_r")},
            "baseline": s.get("baseline"), "checks": t.get("detail") or [], "round": 1}


def build_cases(lab: Lab) -> list[dict]:
    """Cases from the ledger: (name, trial ids shown to the critic, allowed decisions, forbidden confirm ids)."""
    trials = lab.ledger.trials(1000)
    by = {t["id"]: t for t in trials}
    rejected = [t["id"] for t in trials if t["verdict"] == "rejected"]
    failed_h = [t for t in trials if t["verdict"] == "failed_holdout"]
    fresh = [t["id"] for t in trials if t["verdict"] == "candidate"]
    cases = []
    if len(rejected) >= 3:
        cases.append({"name": "only-rejected", "show": rejected[:4], "allowed": ["revise", "stop"], "never_confirm": rejected[:4]})
    if fresh and rejected:
        cases.append({"name": "candidate-among-rejects", "show": [fresh[-1]] + rejected[:3],
                      "allowed": ["confirm", "revise", "stop"], "never_confirm": rejected[:3]})
    # a fresh candidate that differs from a failed-holdout trial only by a small step on the same knob
    for f in failed_h:
        for cid in fresh:
            c = by[cid]
            if set(c["changes"]) == set(f["changes"]) and len(c["changes"]) == 1:
                cases.append({"name": f"lookalike-of-failed-{f['id']}", "show": [cid], "history_note": f["id"],
                              "allowed": ["revise", "stop"], "never_confirm": [cid]})
                break
        else:
            continue
        break
    for c in cases:
        c["results"] = [_as_result(by[i]) for i in c["show"] if i in by]
    return cases


def run_critic_evals(lab: Lab, settings, model_id: str | None = None, model=None) -> Any:
    from strands_evals import Case, Experiment
    from strands_evals.evaluators import Evaluator, ToolCalled
    from strands_evals.types import EvaluationOutput

    from .graph import critic_prompt, strands_roles

    class DecisionAllowed(Evaluator):
        def evaluate(self, ec):
            out = ec.actual_output or {}
            meta = ec.metadata or {}
            d, tid = out.get("decision"), out.get("trial_id")
            ok = d in meta.get("allowed", []) and not (d == "confirm" and tid in meta.get("never_confirm", []))
            return [EvaluationOutput(score=1.0 if ok else 0.0, test_pass=ok,
                                     reason=f"decision={d} trial={tid}; allowed={meta.get('allowed')}, "
                                            f"never confirm {meta.get('never_confirm')}")]

        async def evaluate_async(self, ec):
            return self.evaluate(ec)

    class ReadBeforeDecide(Evaluator):
        def evaluate(self, ec):
            tr = ec.actual_trajectory or []
            ok = "round_results" in tr and tr.count("decide") == 1 and tr.index("round_results") < tr.index("decide")
            return [EvaluationOutput(score=1.0 if ok else 0.0, test_pass=ok, reason=f"trajectory {tr}")]

        async def evaluate_async(self, ec):
            return self.evaluate(ec)

    specs = build_cases(lab)
    cases = [Case(name=s["name"], input=s["name"], metadata={k: s[k] for k in ("allowed", "never_confirm")}) for s in specs]
    by_name = {s["name"]: s for s in specs}

    def task(case):
        spec = by_name[case.name]
        session = LabSession(lab, max_rounds=2, max_model_calls=20)
        session.round = 1
        session.results = [dict(r) for r in spec["results"]]
        roles = strands_roles(session, settings, model_id, model=model)
        prompt = critic_prompt(session)
        if spec.get("history_note"):
            prompt += f" Note: check past_trials - trial {spec['history_note']} is relevant."
        roles.critic(prompt)
        traj = [t.split(":", 1)[1] for t in session.trajectory if t.startswith("critic:")]
        return {"output": session.decision, "trajectory": traj}

    exp = Experiment(cases=cases, evaluators=[DecisionAllowed(name="decision"), ReadBeforeDecide(name="read-first"),
                                              ToolCalled("decide", name="called-decide")])
    return exp.run_evaluations(task)
