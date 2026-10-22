"""The Strategy Lab research run: Strands agents propose and review, code evaluates and decides.

    researcher (LLM) --submit_hypotheses--> evaluate (code: Lab.test_many) --> critic (LLM) --decide-->
        revise -> next round  |  confirm -> Lab.confirm (code: one counted holdout look)  |  stop
    --> reporter (LLM, or a template without a model) -> data/lab/reports/run-*.md

The routing between steps is plain Python on purpose: it must be deterministic and testable, and every number an
agent sees comes from the backtester.  The agents only ever hold read-only tools plus two "requests"
(``submit_hypotheses`` queues backtests; ``decide`` records the critic's call) - confirming on the holdout is
executed here, and only for trials code already marked as candidates.  Proposals still wait for ``abg lab approve``.

Roles are injectable (``Roles``), so tests and evals can run the loop with scripted roles and no API key.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .lab import Lab
from .tools import KEYS, LabSession, make_lab_tools

SOP_DIR = Path(__file__).parent / "sops"


def load_sop(name: str) -> str:
    return (SOP_DIR / f"{name}.sop.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- guard rails (Strands hooks)
class BudgetHook:
    """Strands hook provider: records every tool call (the run's trajectory), counts model calls and cancels tool
    calls once a step has used its budget - an agent can't loop forever or run up the bill."""

    def __init__(self, session: LabSession, role: str, max_tool_calls: int = 12):
        self.session, self.role, self.max_tool_calls, self.calls = session, role, max_tool_calls, 0

    def register_hooks(self, registry, **_: Any) -> None:
        from strands.hooks import AfterModelCallEvent, BeforeToolCallEvent
        registry.add_callback(BeforeToolCallEvent, self._before_tool)
        registry.add_callback(AfterModelCallEvent, self._after_model)

    def reset(self) -> None:
        self.calls = 0

    def _before_tool(self, event) -> None:
        name = (event.tool_use or {}).get("name", "?")
        self.calls += 1
        self.session.trajectory.append(f"{self.role}:{name}")
        if self.calls > self.max_tool_calls:
            event.cancel_tool = f"tool budget for this step ({self.max_tool_calls} calls) is used - finish your reply now"
        elif self.session.model_calls >= self.session.max_model_calls:
            event.cancel_tool = "the run's model-call budget is used - finish your reply now"

    def _after_model(self, event) -> None:
        self.session.model_calls += 1


# --------------------------------------------------------------------------- roles
@dataclass
class Roles:
    """Three callables taking a prompt and returning the agent's reply text.  Tools act on the shared session."""
    researcher: Callable[[str], str]
    critic: Callable[[str], str]
    reporter: Callable[[str], str] | None = None
    model_id: str = "scripted"


def build_model(settings, model_id: str | None = None):
    from strands.models.anthropic import AnthropicModel
    if not settings.anthropic_api_key:
        raise RuntimeError("the research agents need ABG_ANTHROPIC_API_KEY in .env (or run `abg lab test` by hand)")
    mid = model_id or settings.lab_model or settings.anthropic_model
    client = {"api_key": settings.anthropic_api_key}
    if settings.anthropic_base_url and "api.anthropic.com" not in settings.anthropic_base_url:
        client["base_url"] = settings.anthropic_base_url
    return AnthropicModel(client_args=client, model_id=mid, max_tokens=settings.lab_max_tokens), mid


def strands_roles(session: LabSession, settings, model_id: str | None = None, repo_root: str | Path | None = None,
                  model=None, memory_dir: str | Path | None = None) -> Roles:
    """Real Strands agents (Anthropic).  ``model`` may be injected (tests).  ``memory_dir``: persist each agent's
    conversation with a Strands ``FileSessionManager`` so a paused run (``--resume``) continues where it stopped."""
    from strands import Agent

    mid = model_id or "injected"
    if model is None:
        model, mid = build_model(settings, model_id)
    tools = make_lab_tools(session, repo_root)
    hooks = {r: BudgetHook(session, r, n) for r, n in (("researcher", 14), ("critic", 10), ("reporter", 4))}

    def memory(role: str) -> dict:
        if not memory_dir or not session.run_id:
            return {}
        from strands.session.file_session_manager import FileSessionManager
        return {"agent_id": role,
                "session_manager": FileSessionManager(session_id=f"{session.run_id}-{role}", storage_dir=str(memory_dir))}

    agents = {
        role: Agent(model=model, name=role, system_prompt=load_sop(sop), tools=tools[role], hooks=[hooks[role]],
                    callback_handler=None, **memory(role))
        for role, sop in (("researcher", "strategy-research"), ("critic", "strategy-critic"), ("reporter", "strategy-report"))
    }

    def call(role: str) -> Callable[[str], str]:
        def run(prompt: str) -> str:
            if session.model_calls >= session.max_model_calls:
                session.notes.append(f"{role} skipped: model-call budget used")
                return ""
            hooks[role].reset()
            return str(agents[role](prompt)).strip()
        return run

    return Roles(call("researcher"), call("critic"), call("reporter"), mid)


# --------------------------------------------------------------------------- prompts (data, not instructions to code)
def _compact(results: list[dict]) -> str:
    rows = []
    for r in results:
        s = r.get("search") or {}
        rows.append({"trial_id": r.get("trial_id"), "name": r["name"], "changes": r.get("changes"),
                     "verdict": r["verdict"], "failed": r.get("failed"), **{k: s.get(k) for k in KEYS}})
    return json.dumps(rows, default=str)


def researcher_prompt(session: LabSession) -> str:
    n = session.round + 1
    if n == 1:
        return (f"Round {n} of {session.max_rounds}. Orient yourself with the tools (overview, baseline, research_log, "
                f"past_trials, search_space), then submit up to {session.max_per_round} hypotheses.")
    last = [r for r in session.results if r.get("round") == session.round]
    conf = session.confirmations[-1] if session.confirmations else None
    parts = [f"Round {n} of {session.max_rounds}.",
             f"Last round's results (from the backtester): {_compact(last)}",
             f"Critic: {json.dumps(session.decision)}"]
    if conf:
        parts.append("Holdout check for trial %s: %s - %s" % (conf.get("trial_id"), "PASSED" if conf.get("pass") else "FAILED",
                     "; ".join(c["detail"] for c in conf.get("checks", []) if not c["pass"]) or "all checks held"))
    parts.append(f"Submit up to {session.max_per_round} new hypotheses that address this feedback.")
    return "\n".join(parts)


def critic_prompt(session: LabSession) -> str:
    return (f"Round {session.round} was evaluated. Review it with round_results / trial_detail and call decide once. "
            f"Open candidates from earlier runs (never checked on the holdout): "
            f"{json.dumps([c['trial_id'] for c in session.open_candidates()])}. "
            f"Holdout looks used: {session.lab.peeks_used()} of {session.lab.gate.peek_budget}; "
            f"rounds left after this one: {session.max_rounds - session.round}.")


def reporter_prompt(session: LabSession) -> str:
    return "Write the run report now (call round_results and overview first)."


def template_report(session: LabSession) -> str:
    """The report without a model - also the fallback when the reporter fails."""
    lab = session.lab
    base = (session.results[0].get("baseline") if session.results else None) or {}
    lines = ["# Strategy Lab run", ""]
    props = [c for c in session.confirmations if c.get("pass")]
    if props:
        c = props[-1]
        lines.append(f"**Result:** trial #{c['trial_id']} ({c['name']}) held on the holdout - proposal "
                     f"#{c.get('proposal_id')} is waiting for `abg lab approve`.")
    else:
        lines.append("**Result:** nothing was confirmed; the live settings stay as they are.")
    lines += ["", "| trial | change | trades | win rate | net $ | max DD $ | net/DD | verdict |", "|---|---|---|---|---|---|---|---|"]
    if base:
        lines.append(f"| - | current settings | {base.get('trades')} | {base.get('win_rate', 0):.1%} | {base.get('net_usd', 0):,.0f} | "
                     f"{base.get('max_dd_usd', 0):,.0f} | {base.get('ratio', 0):.2f} | baseline |")
    for r in session.results:
        s = r.get("search") or {}
        if not s:
            continue
        ch = ", ".join(f"{k}={v}" for k, v in (r.get("changes") or {}).items())
        lines.append(f"| {r.get('trial_id')} | {ch} | {s.get('trades')} | {s.get('win_rate', 0):.1%} | {s.get('net_usd', 0):,.0f} | "
                     f"{s.get('max_dd_usd', 0):,.0f} | {s.get('ratio', 0):.2f} | {r['verdict']}"
                     f"{' (' + ', '.join(r.get('failed') or []) + ')' if r.get('failed') else ''} |")
    for c in session.confirmations:
        lines.append("")
        lines.append(f"Holdout look for trial #{c['trial_id']}: {'passed' if c.get('pass') else 'failed'} - " +
                     "; ".join(f"{x['check']}: {x['detail']}" for x in c.get("checks", [])))
    lines += ["", f"Trials in the ledger: {lab.ledger.count_trials(lab.ds.fingerprint)}; holdout looks used: "
              f"{lab.peeks_used()} of {lab.gate.peek_budget}; model calls this run: {session.model_calls}."]
    if session.notes:
        lines += ["", "Notes: " + "; ".join(session.notes)]
    return "\n".join(lines)


# --------------------------------------------------------------------------- the run
def run_research(lab: Lab, make_roles: Callable[[LabSession], Roles], rounds: int = 3, per_round: int = 5,
                 max_model_calls: int = 60, log: Callable[[str], None] = lambda s: None,
                 report_dir: str | Path | None = None) -> dict:
    """``make_roles(session)`` builds the roles bound to this run's session (e.g. ``lambda s: strands_roles(s, settings)``)."""
    session = LabSession(lab, max_rounds=rounds, max_per_round=per_round, max_model_calls=max_model_calls)
    return run_research_session(lab, make_roles(session), session, log, report_dir)


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def run_research_session(lab: Lab, roles: Roles, session: LabSession, log: Callable[[str], None] = lambda s: None,
                         report_dir: str | Path | None = None, state_path: str | Path | None = None,
                         deadline: float | None = None) -> dict:
    """Drive one research run as a small state machine (research -> evaluate -> critic -> confirm -> report).

    With ``state_path`` the session is saved after every stage; with ``deadline`` the run pauses (returns
    ``status="paused"``) instead of starting work it can't finish - call again with the same state to resume.
    Backtests already done are cached, and the ledger is only written once a whole batch is evaluated."""
    from .lab import TimeBudget

    if not session.source or session.source == "agent":
        session.source = f"agent:{roles.model_id}"
    session.run_id = session.run_id or new_run_id()

    def save() -> None:
        if state_path:
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(state_path).write_text(json.dumps(session.to_state(), default=str), encoding="utf-8")

    def out_of_time(margin: float) -> bool:
        return deadline is not None and time.time() + margin > deadline

    while session.stage != "done":
        st = session.stage
        if st == "research":
            if session.round >= session.max_rounds:
                session.stage = "report"
                continue
            if out_of_time(120):
                break
            session.pending, session.decision = [], {}
            log(f"round {session.round + 1}: researcher")
            roles.researcher(researcher_prompt(session))
            if not session.pending:
                session.notes.append(f"round {session.round + 1}: no valid hypotheses submitted")
                session.stage = "report"
            else:
                session.stage = "evaluate"
        elif st == "evaluate":
            names = [h.name for h in session.pending]
            log(f"round {session.round + 1}: backtesting {len(names)} hypotheses - {names}")
            try:
                res = lab.test_many(session.pending, source=session.source, deadline=deadline)   # CODE judges
            except TimeBudget as e:
                log(f"paused: {e}")
                break
            session.round += 1
            for r in res:
                r["round"] = session.round
            session.results += res
            session.pending = []
            log(f"round {session.round}: " + ", ".join(f"{r['name']}={r['verdict']}" for r in res))
            session.stage = "critic"
        elif st == "critic":
            if out_of_time(120):
                break
            session.decision = {}
            log(f"round {session.round}: critic")
            roles.critic(critic_prompt(session))
            d = session.decision.get("decision")
            if not d:
                session.notes.append(f"round {session.round}: critic made no decision - stopping")
            session.stage = {"confirm": "confirm", "revise": "research"}.get(d or "stop", "report")
        elif st == "confirm":
            tid = session.decision.get("trial_id")
            try:
                conf = lab.confirm(int(tid), deadline=deadline)                # CODE: one counted holdout look
            except TimeBudget as e:
                log(f"paused: {e}")
                break
            except (ValueError, TypeError) as e:
                session.notes.append(f"confirm of trial {tid} refused: {e}")
                session.stage = "report"
                save()
                continue
            session.confirmations.append(conf)
            log(f"holdout look for trial {tid}: {'PASSED' if conf['pass'] else 'failed'}")
            session.stage = "report" if conf["pass"] else "research"
        elif st == "report":
            if roles.reporter and out_of_time(90):
                break
            report = ""
            if roles.reporter:
                try:
                    report = roles.reporter(reporter_prompt(session))
                except Exception as e:                                          # a report must never lose a run
                    session.notes.append(f"reporter failed: {e}")
            report = (report.strip() + "\n\n---\n\n" if report.strip() else "") + template_report(session)
            path = None
            if report_dir:
                path = Path(report_dir) / f"run-{session.run_id}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(report, encoding="utf-8")
            session.notes.append(f"report: {path}" if path else "report written")
            session.stage = "done"
            save()
            return {"status": "done", "session": session.summary(), "report": report,
                    "report_path": str(path) if path else None}
        save()
    save()
    return {"status": "paused", "stage": session.stage, "session": session.summary(), "report": "", "report_path": None}
