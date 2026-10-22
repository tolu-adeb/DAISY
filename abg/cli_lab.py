"""CLI: ``abg lab ...`` - the Strategy Lab (docs/17) and ``abg mcp``.

    abg lab info                         data windows, gate, trials so far, holdout looks left
    abg lab baseline                     the current settings on the search window
    abg lab test "name" "why" t1_r=0.6   backtest one hypothesis (search window only)
    abg lab trials / abg lab trial 12    the ledger
    abg lab confirm 12                   ONE counted look at the locked holdout for a candidate
    abg lab run                          the Strands research agents (needs ABG_ANTHROPIC_API_KEY)
    abg lab proposals / approve 3 / reject 3
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer
from rich import box
from rich.table import Table

from .cli import _state, app, console
from .config import Settings

lab_app = typer.Typer(help="Strategy Lab: test bot changes against a locked holdout; research agents (docs/17).",
                      no_args_is_help=True)
app.add_typer(lab_app, name="lab")

_BARS = typer.Option(None, "--bars", help="Intraday bars CSV (default data/mnq_proxy_5m.csv).")
_TREND = typer.Option(None, "--trend", help="Daily CSV for the trend history (default data/qqq_daily.csv).")


def _settings() -> Settings:
    return Settings(**_state["overrides"])


def _lab(bars: Optional[Path], trend: Optional[Path], workers: Optional[int] = None):
    from .agents.lab import Lab
    s = _settings()
    return Lab.from_paths(bars, trend, s.data_dir, s.lab_holdout_months, workers=workers)


def _fmt(m: dict | None) -> list[str]:
    m = m or {}
    return [str(m.get("trades", "")), f"{m.get('win_rate', 0):.1%}", f"{m.get('net_usd', 0):,.0f}",
            f"{m.get('max_dd_usd', 0):,.0f}", f"{m.get('ratio', 0):.2f}", f"{m.get('avg_r', 0):+.3f}"]


def _table(title: str) -> Table:
    t = Table(title=title, box=box.SIMPLE, title_justify="left")
    for c in ("", "trades", "win", "net $", "max DD $", "net/DD", "avg R"):
        t.add_column(c, justify="right" if c else "left")
    return t


def _checks(checks: list[dict]) -> None:
    for c in checks:
        console.print(f"  {'[green]PASS[/green]' if c['pass'] else '[red]FAIL[/red]'} {c['check']:<22} {c['detail']}")


@lab_app.command("info")
def info_cmd(bars: Optional[Path] = _BARS, trend: Optional[Path] = _TREND):
    """Data windows, the gate, trials so far and holdout looks used."""
    lab = _lab(bars, trend)
    d = lab.describe()
    console.print_json(json.dumps({k: d[k] for k in ("data", "gate", "trials_so_far", "holdout_peeks_used", "peek_budget")}))


@lab_app.command("space")
def space_cmd():
    """Settings a hypothesis may change."""
    from .agents.lab import describe_space
    from .agents.lab import current_params
    t = Table(box=box.SIMPLE)
    for c in ("setting", "current", "range / choices", "meaning"):
        t.add_column(c)
    for k in describe_space(current_params(_settings().data_dir)):
        rng = k["range"] or k["choices"] or ""
        t.add_row(k["name"], str(k["current"]), str(rng), k["help"])
    console.print(t)


@lab_app.command("baseline")
def baseline_cmd(bars: Optional[Path] = _BARS, trend: Optional[Path] = _TREND):
    """The current settings on the search window and its four sub-periods (never the holdout)."""
    lab = _lab(bars, trend)
    b = lab.baseline()
    t = _table(f"current settings - search window {lab.ds.search[0]} -> {lab.ds.search[-1]}")
    t.add_row("search window", *_fmt(b["search"]))
    for blk, m in zip(lab.ds.blocks, b["blocks"]):
        t.add_row(f"{blk[0]} -> {blk[-1]}", *_fmt(m))
    console.print(t)


def _parse_changes(items: List[str]) -> dict:
    out = {}
    for it in items:
        if "=" not in it:
            raise typer.BadParameter(f"expected setting=value, got {it!r}")
        k, v = it.split("=", 1)
        try:
            out[k.strip()] = json.loads(v)
        except ValueError:
            out[k.strip()] = v
    return out


@lab_app.command("test")
def test_cmd(name: str, rationale: str, changes: List[str] = typer.Argument(..., help="setting=value ..."),
             bars: Optional[Path] = _BARS, trend: Optional[Path] = _TREND,
             workers: Optional[int] = typer.Option(None, help="Parallel backtests (default: CPUs, max 4).")):
    """Backtest one hypothesis on the search window (it, the current settings and its neighbours)."""
    from .agents.lab import Hypothesis
    lab = _lab(bars, trend, workers)
    h = Hypothesis(name=name, rationale=rationale, changes=_parse_changes(changes))
    r = lab.test(h, source="cli")
    if r["verdict"] == "invalid":
        console.print(f"[yellow]{r['why']}[/yellow]")
        raise typer.Exit(1)
    t = _table(f"trial #{r['trial_id']} {h.name}: {h.label()}")
    t.add_row("current", *_fmt(r["baseline"]))
    t.add_row("hypothesis", *_fmt(r["search"]))
    console.print(t)
    _checks(r["checks"])
    console.print(f"verdict: [bold]{r['verdict']}[/bold]" + ("  -> `abg lab confirm %d` spends one holdout look" % r["trial_id"]
                                                             if r["verdict"] == "candidate" else ""))


@lab_app.command("trials")
def trials_cmd(limit: int = typer.Option(30), bars: Optional[Path] = _BARS, trend: Optional[Path] = _TREND):
    """The trial ledger (every hypothesis ever tested on this dataset, winners and losers)."""
    from .agents.tools import lab_trials
    lab = _lab(bars, trend)
    t = Table(box=box.SIMPLE)
    for c in ("#", "source", "name", "changes", "trades", "win", "net/DD", "verdict", "failed"):
        t.add_column(c)
    for x in lab_trials(lab, limit):
        s = x["search"]
        t.add_row(str(x["id"]), x["source"] or "", x["name"], json.dumps(x["changes"]), str(s.get("trades") or ""),
                  f"{s.get('win_rate') or 0:.1%}", f"{s.get('ratio') or 0:.2f}", x["verdict"], ", ".join(x["failed"] or []))
    console.print(t)
    console.print(f"{lab.ledger.count_trials(lab.ds.fingerprint)} trials on this dataset; holdout looks used "
                  f"{lab.peeks_used()} of {lab.gate.peek_budget}")


@lab_app.command("trial")
def trial_cmd(trial_id: int, bars: Optional[Path] = _BARS, trend: Optional[Path] = _TREND):
    """Every check for one trial."""
    from .agents.tools import lab_trial
    console.print_json(json.dumps(lab_trial(_lab(bars, trend), trial_id), default=str))


@lab_app.command("confirm")
def confirm_cmd(trial_id: int, bars: Optional[Path] = _BARS, trend: Optional[Path] = _TREND):
    """Score a search-window candidate on the locked holdout - one counted look; a pass queues a proposal."""
    lab = _lab(bars, trend)
    try:
        r = lab.confirm(trial_id)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    t = _table(f"trial #{trial_id} {r['name']} - holdout {r['window'][0]} -> {r['window'][1]}")
    t.add_row("holdout: current", *_fmt(r["holdout_baseline"]))
    t.add_row("holdout: hypothesis", *_fmt(r["holdout"]))
    t.add_row("all sessions: current", *_fmt(r["full_baseline"]))
    t.add_row("all sessions: hypothesis", *_fmt(r["full"]))
    console.print(t)
    _checks(r["checks"])
    console.print(f"holdout looks used: {r['peeks_used']} of {r['peek_budget']}")
    if r["pass"]:
        console.print(f"[green]confirmed[/green] -> proposal #{r['proposal_id']}: `abg lab approve {r['proposal_id']}`")
    else:
        console.print("[yellow]did not hold on the holdout - not proposed[/yellow]")


@lab_app.command("run")
def run_cmd(rounds: Optional[int] = typer.Option(None), per_round: Optional[int] = typer.Option(None),
            model: Optional[str] = typer.Option(None, help="Claude model id (default ABG_LAB_MODEL / ABG_ANTHROPIC_MODEL)."),
            bars: Optional[Path] = _BARS, trend: Optional[Path] = _TREND, workers: Optional[int] = typer.Option(None),
            resume: bool = typer.Option(False, help="Continue the paused run."),
            new: bool = typer.Option(False, help="Start over even if a run is paused."),
            max_seconds: Optional[int] = typer.Option(None, help="Pause (resumably) before this many seconds pass."),
            no_llm_report: bool = typer.Option(False, help="Template report instead of the reporter agent.")):
    """Research agents (Strands + Claude): researcher -> backtests -> critic -> (holdout) -> report."""
    import time as _time
    t0 = _time.time()
    try:
        from .agents.graph import run_research_session, strands_roles
        from .agents.tools import LabSession
    except ImportError as e:
        console.print(f"[red]{e}[/red] - install the agents extra: pip install -e .[agents]")
        raise typer.Exit(1)
    s = _settings()
    lab = _lab(bars, trend, workers)
    runs = Path(lab.ledger.path).parent / "runs"
    state = runs / "current.json"
    session = LabSession(lab, max_rounds=rounds or s.lab_rounds, max_per_round=per_round or s.lab_per_round,
                         max_model_calls=s.lab_max_model_calls)
    prev = json.loads(state.read_text()) if state.exists() else None
    if prev and prev.get("stage") != "done" and not (resume or new):
        console.print(f"[yellow]run {prev.get('run_id')} is paused at '{prev.get('stage')}' - use --resume or --new[/yellow]")
        raise typer.Exit(1)
    if resume:
        if not prev or prev.get("stage") == "done":
            console.print("[yellow]nothing to resume[/yellow]")
            raise typer.Exit(1)
        session.load_state(prev)
    if not session.run_id:
        from .agents.graph import new_run_id
        session.run_id = new_run_id()
    try:
        roles = strands_roles(session, s, model, memory_dir=runs / "agents")
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    if no_llm_report:
        roles.reporter = None
    console.print(f"[dim]run {session.run_id} ({'resumed at ' + session.stage if resume else 'new'}); model {roles.model_id}; "
                  f"{session.max_rounds} rounds x {session.max_per_round} hypotheses; holdout looks used "
                  f"{lab.peeks_used()} of {lab.gate.peek_budget}[/dim]")
    out = run_research_session(lab, roles, session, log=lambda m: console.print(f"[dim]{m}[/dim]"),
                               report_dir=runs.parent / "reports", state_path=state,
                               deadline=(t0 + max_seconds) if max_seconds else None)
    if out["status"] == "paused":
        console.print(f"[yellow]paused at '{out['stage']}' - run `abg lab run --resume` to continue[/yellow]")
        return
    console.print(out["report"])
    if out["report_path"]:
        console.print(f"[dim]report saved: {out['report_path']}[/dim]")


@lab_app.command("eval")
def eval_cmd(model: Optional[str] = typer.Option(None), bars: Optional[Path] = _BARS, trend: Optional[Path] = _TREND):
    """Strands Evals regression suite for the critic agent, built from real ledger trials (uses the API, not the holdout)."""
    from .agents.evals import run_critic_evals
    rep = run_critic_evals(_lab(bars, trend), _settings(), model)
    try:
        rep.run_display()
    except Exception:
        console.print(rep)
    rows = getattr(rep, "cases", None) or []
    failed = [r for r in rows if isinstance(r, dict) and not r.get("test_pass", True)]
    if failed:
        raise typer.Exit(1)


@lab_app.command("proposals")
def proposals_cmd(bars: Optional[Path] = _BARS, trend: Optional[Path] = _TREND):
    """Confirmed changes waiting for a person."""
    lab = _lab(bars, trend)
    ps = lab.ledger.proposals()
    if not ps:
        console.print("no proposals")
    for p in ps:
        ev = p.get("evidence") or {}
        console.print(f"#{p['id']} [{p['status']}] {p['name']} {json.dumps(p['changes'])}")
        for k in ("holdout", "holdout_baseline", "full", "full_baseline"):
            m = ev.get(k) or {}
            console.print(f"   {k:<17} win {m.get('win_rate', 0):.1%}  net ${m.get('net_usd', 0):,.0f}  "
                          f"max DD ${m.get('max_dd_usd', 0):,.0f}  net/DD {m.get('ratio', 0):.2f}")


@lab_app.command("approve")
def approve_cmd(pid: int, note: str = typer.Option("", help="Why you approve it."),
                bars: Optional[Path] = _BARS, trend: Optional[Path] = _TREND):
    """Write a confirmed proposal to alpha_params.json (the old file is backed up). Restart the monitor after."""
    lab = _lab(bars, trend)
    try:
        f = lab.approve(pid, note)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]approved[/green] -> {f}  (restart `abg monitor` to load it; loss controls stay at their defaults)")


@lab_app.command("reject")
def reject_cmd(pid: int, note: str = typer.Option(""), bars: Optional[Path] = _BARS, trend: Optional[Path] = _TREND):
    """Reject a proposal."""
    _lab(bars, trend).reject(pid, note)
    console.print("rejected")


@app.command("mcp")
def mcp_cmd(bars: Optional[Path] = _BARS, trend: Optional[Path] = _TREND):
    """Serve the Strategy Lab + bot state over MCP (stdio) for Claude / Cowork / other agents."""
    from .agents.mcp_server import main
    main(str(bars) if bars else None, str(trend) if trend else None)
