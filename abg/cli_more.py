"""CLI: markets, calendar, risk book, backtests, training, backups."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.panel import Panel
from rich.table import Table

from .cli import _run, _state, app, console
from .config import Settings
from .render import num, signed

train_app = typer.Typer(help="Train the learned grade and risk models from history.", no_args_is_help=True)
app.add_typer(train_app, name="train")


@app.command("markets")
def markets_cmd():
    """Futures, rates & bonds, FX, commodities, crypto and volatility at a glance, plus the market regime."""
    from .markets.overview import market_regime, overview

    async def fn(eng):
        ov, rg = await asyncio.gather(overview(eng), market_regime(eng))
        for g in ov["groups"]:
            t = Table(title=g["group"], box=box.SIMPLE, title_justify="left")
            for c in ("Symbol", "Name", "Price", "Day", "$/pt", "Tick $"):
                t.add_column(c, justify="right" if c in ("Price", "Day", "$/pt", "Tick $") else "left")
            for r in g["rows"]:
                fut = r["asset_class"] in ("future", "bond_future")
                t.add_row(r["symbol"], r["name"][:28], num(r["price"], 4 if (r["price"] or 0) < 5 else 2) if r["price"] else
                          "[dim]n/a[/dim]", signed(r["change_pct"], suffix="%"), num(r["multiplier"], 0) if fut else "",
                          num(r["tick_value"], 2) if fut else "")
            console.print(t)
        c = ov["curve"]
        console.print(f"Yield curve: 3m {num(c['3m'])} · 5y {num(c['5y'])} · 10y {num(c['10y'])} · 30y {num(c['30y'])}"
                      + (f" · 10y-3m {signed(ov['spreads'].get('10y-3m'))}" if ov["spreads"].get("10y-3m") is not None else "")
                      + (" [red](inverted)[/red]" if ov["inverted"] else ""))
        console.print(Panel(rg["summary"] + ("\n" + "\n".join(rg["notes"]) if rg["notes"] else ""), title="Market regime"))
    _run(fn)


@app.command("calendar")
def calendar_cmd(days: int = typer.Option(14, help="Days ahead."),
                 symbols: Optional[list[str]] = typer.Option(None, "--symbol", "-s", help="Also look up earnings.")):
    """Macro releases (FOMC, CPI, jobs) and earnings dates."""
    from .markets.calendar import Calendar

    async def fn(eng):
        cal = Calendar(eng, eng.settings)
        evs = await cal.upcoming([x.upper() for x in symbols or []], days)
        t = Table(box=box.SIMPLE)
        for c in ("When (ET)", "Event", "Note"):
            t.add_column(c)
        for e in evs:
            t.add_row(f"{e.dt:%a %b %d} {e.time or ''}", e.name, ("estimated · " if e.estimated else "") + e.detail)
        console.print(t)
    _run(fn)


@app.command("backup")
def backup_cmd():
    """Back up the portfolio and signals databases now (the monitor also does this nightly)."""
    from .ops.backup import backup_databases
    s = Settings(**_state["overrides"])
    console.print(backup_databases(s.data_dir, s.backup_keep))


@train_app.command("grade")
def train_grade_cmd(symbols: Optional[list[str]] = typer.Option(None, "--symbol", "-s", help="Symbols (default: a 50-name basket)."),
                    years: int = 8):
    """Learn the entry grade from simulated setups (+ your saved backtests)."""
    from .train import train_grade

    async def fn(eng):
        m = await train_grade(eng, symbols, years, progress=lambda i, n, sym, k: console.print(
            f"[dim]{i}/{n} {sym}: {k} trades so far[/dim]"))
        _show_model(m, "Learned grade model")
    _run(fn)


@train_app.command("risk")
def train_risk_cmd(symbols: Optional[list[str]] = typer.Option(None, "--symbol", "-s"), years: int = 10):
    """Learn the risk model (P(10% drawdown within 21 days), forward volatility)."""
    from .train import train_risk

    async def fn(eng):
        m = await train_risk(eng, symbols, years, progress=lambda i, n, sym: console.print(f"[dim]{i}/{n} {sym}[/dim]"))
        _show_model(m, "Learned risk model")
    _run(fn)


@train_app.command("all")
def train_all_cmd(years: int = 8):
    """Train both models on the default basket."""
    train_grade_cmd(None, years)
    train_risk_cmd(None, max(years, 10))


def _show_model(m: dict, title: str) -> None:
    met = m.get("metrics") or {}
    lines = [f"saved to {m['path']}", f"trained on {m['n']} rows " + (json.dumps(m.get("sources")) if m.get("sources") else "")]
    lines += [f"{k}: {v:.4g}" if isinstance(v, float) else f"{k}: {v}" for k, v in met.items() if k != "symbols"]
    auc = met.get("test_auc")
    ok = auc is not None and auc > 0.55
    lines.append(("[green]usable[/green]" if ok else "[yellow]not better than chance out-of-sample; the rules stay in charge[/yellow]")
                 + f" (test AUC {auc if auc is None else round(auc, 3)})")
    if m.get("importance"):
        lines.append("top features: " + ", ".join(f"{x['feature']} {x['coef_r']:+.3f}" for x in m["importance"][:6]))
    console.print(Panel("\n".join(lines), title=title, border_style="green" if ok else "yellow"))


# ---------------------------------------------------------------- ext add-ons
from .cli_ext import ext_app, _with_tracker  # noqa: E402


@ext_app.command("backtest")
def backtest_cmd(file: Optional[Path] = typer.Argument(None, help=".txt (### YYYY-MM-DD [HH:MM] [author] headers), .json or .csv"),
                 discord: Optional[str] = typer.Option(None, help="Channel id to pull history from instead of a file."),
                 limit: int = typer.Option(500, help="Messages to pull from Discord."),
                 gate: Optional[str] = typer.Option(None, help="Entry-grade gate for the replay (A/B/C/D/none)."),
                 name: str = typer.Option("backtest", help="Name for the saved report.")):
    """Replay dated signals on history: win rate, R, grade calibration, per-source / per-type stats."""
    from .extsignals.backtest import Backtester, fetch_discord_history, load_messages, save_report
    if not file and not discord:
        raise typer.BadParameter("give a file or --discord CHANNEL_ID")

    async def fn(eng):
        msgs = await fetch_discord_history(eng.settings, eng.http, discord, limit) if discord else load_messages(file)
        console.print(f"{len(msgs)} messages")
        rep = await Backtester(eng, gate=gate).run(msgs, progress=lambda i, n: console.print(f"[dim]{i}/{n}[/dim]")
                                                   if i % 25 == 0 else None)
        paths = save_report(rep, eng.settings.data_dir, name)
        o = rep["overall"]
        console.print(Panel(
            f"{o['ideas']} ideas from {o['messages']} messages · fill rate {(o['fill_rate'] or 0):.0%} · "
            f"{o['closed']} closed · win rate {(o['win_rate'] or 0):.0%} · avg {(o['avg_r'] or 0):+.2f}R · "
            f"total {o['total_r']:+.2f}R · max drawdown {o['max_drawdown_r']:+.2f}R\n" + (rep["grade_note"] or "")
            + f"\nsaved: {paths['json']}", title="Backtest"))
        for key in ("grade", "pattern", "author"):
            t = Table(title=f"by {key}", box=box.SIMPLE, title_justify="left")
            for c in (key, "closed", "win rate", "avg R", "total R"):
                t.add_column(c)
            for k, a in rep["by"][key].items():
                t.add_row(k, str(a["closed"]), f"{(a['win_rate'] or 0):.0%}", signed(a["avg_r"], suffix="R"),
                          signed(a["total_r"], suffix="R"))
            console.print(t)
    _run(fn)


@ext_app.command("risk")
def risk_cmd():
    """Paper equity, open risk (heat), and prop-firm room."""
    async def fn(tr, s):
        eq = tr.gate.equity()
        g = Table.grid(padding=(0, 3))
        for k in ("equity", "day_pnl", "realized", "open_pnl", "heat", "heat_pct", "open", "high_water", "floor",
                  "drawdown_room", "daily_room"):
            v = eq.get(k)
            g.add_row(k.replace("_", " "), "—" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v)))
        console.print(Panel(g, title="Risk book" + (" · prop rules ON" if s.prop_enabled else "")))
    _with_tracker(fn)
