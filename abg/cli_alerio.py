"""``abg alerio ...`` - see what Alerio did with your copied signals, and what the terminal would do instead."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.markup import escape
from rich.table import Table

from .cli import app, console
from .config import Settings

alerio_app = typer.Typer(help="Alerio copy-trading: sync (read-only), audit, replay vs the terminal's rules, live shadow.",
                         no_args_is_help=True)
app.add_typer(alerio_app, name="alerio")


def _s() -> Settings:
    return Settings()


def _snap():
    from .routes.alerio import load_snapshot
    try:
        return load_snapshot(_s().data_dir)
    except FileNotFoundError:
        raise typer.BadParameter("no Alerio snapshot yet - run `abg alerio sync` (needs ABG_ALERIO_COOKIE) or `abg alerio import FILE`")


@alerio_app.command("sync")
def sync_cmd():
    """Pull the route, accounts, signals and Alerio's executions (read-only) into ABG_DATA_DIR/alerio/snapshot.json."""
    from .routes.alerio import sync
    snap, p = sync(_s())
    console.print(f"{len(snap['signals'])} signals · {len(snap['accounts'])} accounts → {p}")
    status_cmd()


@alerio_app.command("import")
def import_cmd(path: Path):
    """Use a snapshot saved elsewhere (same format as `sync` writes)."""
    from .routes.alerio import load_snapshot, save_snapshot
    snap = load_snapshot(path)
    console.print(f"imported {len(snap['signals'])} signals → {save_snapshot(_s().data_dir, snap)}")


@alerio_app.command("status")
def status_cmd():
    """Accounts (and whether the broker has locked them) and the route's settings at a glance."""
    snap = _snap()
    t = Table(title=f"Alerio accounts · snapshot {snap.get('exported_at', '')[:16]}", box=box.SIMPLE)
    for c in ("account", "status", "cash", "size", "risk/trade set"):
        t.add_column(c)
    for a in snap["accounts"]:
        st = a.get("status", "ok")
        t.add_row(a.get("nickname") or str(a["id"]), ("[green]ok[/]" if st == "ok" else f"[red]{st}[/] {escape(a.get('status_reason') or '')}"),
                  f"${a['cash']:,.2f}" if a.get("cash") else "—", f"{a.get('contracts')} {a.get('contract_type') or ''}",
                  f"${a['risk_per_trade']:,.0f}" if a.get("risk_per_trade") else "—")
    console.print(t)
    r = snap.get("route") or {}
    b = r.get("brackets") or {}
    console.print(f"route: entry [b]{r.get('entry_order_policy')}[/b] · alert SL/TP {b.get('alert_override')} · "
                  f"TPs {[(x['rr'], x['pct']) for x in b.get('tp') or []]} · management replies: "
                  + ("followed" if (r.get("allow_exits") or r.get("allow_sl_adjustments")) else "[red]ignored[/]")
                  + f" · close {r.get('close_at_time') or '—'} · event exclusions {r.get('market_event_exclusions') or 'none'}")


@alerio_app.command("audit")
def audit_cmd(n: int = typer.Option(15, "-n", "--n", help="Most recent signals to list.")):
    """What Alerio did with each signal and what went wrong (fills vs optimal, $ at risk vs your setting, rejections)."""
    from .routes.alerio import audit
    a = audit(_snap())
    t = Table(title=f"{a['signals']} signals · {a['live_fills']} live fills · Alerio P&L ${a['alerio_pnl']:+,.0f} · "
                    f"avg fill {a['avg_chase_pts']} pts worse than the optimal", box=box.SIMPLE)
    for c in ("date", "ET", "#", "side", "service", "Alerio", "flags"):
        t.add_column(c)
    for r in a["rows"][-n:]:
        acc = "; ".join(f"{x['account']} {x['contracts']}@{x['fill']:,.2f} ${x['pnl'] or 0:+,.0f}" for x in r["accounts"]) or "—"
        t.add_row(r["date"], r["time_et"], str(r["num"]), r["side"], f"{r['service_pts']:+.1f}" if r["service_pts"] is not None else "—",
                  escape(acc), escape("; ".join(r["flags"])))
    console.print(t)
    console.print("[bold]most common problems[/bold]: " + "; ".join(f"{k} ×{v}" for k, v in a["flag_counts"].items()))


@alerio_app.command("compare")
def compare_cmd(bars: Path = typer.Option(Path("data/mnq_5m.csv"), help="Intraday NQ/MNQ bars to fill on."),
                budgets: str = typer.Option("300,400,600", help="Terminal $ risk per trade to test."),
                contracts: Optional[int] = typer.Option(None, help="Alerio size (default: what your accounts are set to)."),
                dd_limit: Optional[float] = typer.Option(None, help="Prop trailing drawdown for the breach odds."),
                out: Optional[Path] = typer.Option(None, "--json")):
    """Replay every signal on the bars: your Alerio route as configured vs the terminal's guarded route."""
    from .alpha.data import load_intraday_csv
    from .routes.alerio import compare
    s = _s()
    res = compare(_snap(), load_intraday_csv(bars), tuple(float(x) for x in budgets.split(",")), contracts,
                  dd_limit or s.alerio_dd_limit_usd)
    t = Table(title=f"{res['signals']} signals {res['from']} → {res['to']} · breach = drawdown ≥ ${res['dd_limit']:,.0f} "
                    "in a resampled month (2,000 samples)", box=box.SIMPLE)
    for c in ("rule set", "trades", "win", "net", "worst trade", "worst day", "max DD", "median month", "P(breach)"):
        t.add_column(c)
    for k, v in res["runs"].items():
        sm, br = v["summary"], v.get("breach") or {}
        if not sm.get("trades"):
            continue
        t.add_row(k, str(sm["trades"]), f"{sm['win_rate']:.0%}", f"{sm['net_usd']:+,.0f}", f"{sm['worst_trade_usd']:+,.0f}",
                  f"{sm['worst_day_usd']:+,.0f}", f"{sm['max_dd_usd']:,.0f}", f"{br.get('median_month', 0):+,.0f}",
                  f"{br.get('p_breach', 0):.1%}")
    console.print(t)
    console.print("[dim]Fills on 5-minute bars at the price quoted in each signal; Alerio's real fills averaged ~14 pts worse, "
                  "so treat the Alerio row as its best case.[/dim]")
    p = Path(s.data_dir).expanduser() / "alerio" / "compare.json"
    slim = {**{k: v for k, v in res.items() if k != "runs"},
            "runs": {k: {"summary": v["summary"], "breach": v.get("breach")} for k, v in res["runs"].items()},
            "at": datetime.now().isoformat(timespec="seconds")}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(slim, indent=1, default=str))
    if out:
        out.write_text(json.dumps(res, indent=1, default=str))


@alerio_app.command("route")
def route_cmd(name: str = typer.Option("tradingmind"), budget: Optional[float] = None):
    """Save the terminal's guarded TradingMind route (so `abg route test/replay` use it too)."""
    from .routes import load_routes, save_routes
    from .routes.alerio import guarded_route
    s = _s()
    routes = load_routes(s.data_dir)
    r = guarded_route(budget or s.alerio_budget_usd, s.alerio_max_contracts, name=name)
    r.accounts = [a.get("nickname") or str(a["id"]) for a in _snap()["accounts"]] or ["paper"]
    routes[name] = r
    console.print(f"saved route '{name}' → {save_routes(s.data_dir, routes)}")


@alerio_app.command("watch")
def watch_cmd(once: bool = typer.Option(False, help="One poll and exit.")):
    """Shadow Alerio live in this terminal window (the monitor does this itself with ABG_ALERIO_WATCH=true)."""
    import asyncio
    from .routes.watch import AlerioWatcher

    async def go():
        w = AlerioWatcher(_s())
        if once:
            from .routes.engine import NY
            w.primed = True
            for ev in await w.poll(datetime.now(NY)):
                console.print(escape(json.dumps(ev, default=str)[:300]))
            return
        console.print("watching Alerio (Ctrl+C to stop) …")
        await w.run()
    asyncio.run(go())
