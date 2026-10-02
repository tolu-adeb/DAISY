"""``abg route ...`` - Alerio-style alert routing (dry run): define routes, test an alert, replay history."""
from __future__ import annotations

import json
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.markup import escape
from rich.table import Table

from .cli import app, console
from .config import Settings

route_app = typer.Typer(help="Route Discord/Telegram/webhook alerts to accounts under your own risk rules (dry run).",
                        no_args_is_help=True)
app.add_typer(route_app, name="route")


def _dd() -> Path:
    return Path(Settings().data_dir).expanduser()


def _get(name: str):
    from .routes import load_routes
    routes = load_routes(_dd())
    if name not in routes:
        raise typer.BadParameter(f"no route '{name}' - `abg route add {name}` first (have: {', '.join(routes) or 'none'})")
    return routes


@route_app.command("add")
def add_cmd(name: str, source: str = typer.Option("discord"), channel: str = typer.Option(""),
            accounts: str = typer.Option("lucid", help="Comma-separated account names."),
            preset: str = typer.Option("guarded", help="guarded (the defaults) | copy (plain copier, fixed size)"),
            contracts: int = typer.Option(8, help="Size for the copy preset / the cap for guarded.")):
    """Create a route with safe defaults (dry run)."""
    from .routes import Route, RouteRules, load_routes, save_routes
    from .routes.replay import as_copied_route
    routes = load_routes(_dd())
    if preset == "copy":
        r = as_copied_route(contracts, name)
        r.accounts, r.source, r.channel = accounts.split(","), source, channel
    else:
        r = Route(name, source, channel, "dry_run", accounts.split(","), RouteRules(max_contracts=contracts))
    routes[name] = r
    console.print(f"saved → {save_routes(_dd(), routes)}")
    _show(r)


def _show(r) -> None:
    t = Table(title=f"route {r.name} · {r.source} {r.channel} · {r.mode} · accounts {', '.join(r.accounts)}", box=box.SIMPLE)
    t.add_column("rule")
    t.add_column("value")
    for k, v in r.rules.to_dict().items():
        t.add_row(k, json.dumps(v))
    console.print(t)


@route_app.command("list")
def list_cmd():
    from .routes import load_routes
    for r in load_routes(_dd()).values():
        console.print(f"[bold]{r.name}[/bold] {r.source} {r.channel} · {r.mode} · {', '.join(r.accounts)} · "
                      f"{r.rules.sizing} sizing, ${r.rules.risk_per_trade_usd:,.0f}/trade, max {r.rules.max_contracts}, "
                      f"max stop {r.rules.max_stop_pts:g} pts")


@route_app.command("show")
def show_cmd(name: str):
    _show(_get(name)[name])


@route_app.command("set")
def set_cmd(name: str, key: str, value: str):
    """Change one rule, e.g. ``abg route set lucid max_stop_pts 50`` or ``... mode dry_run``.  Values are JSON."""
    from .routes import Route, save_routes
    routes = _get(name)
    r = routes[name]
    try:
        v = json.loads(value)
    except json.JSONDecodeError:
        v = value
    if key in ("mode", "source", "channel", "accounts"):
        if key == "mode" and v == "live":
            raise typer.BadParameter("live mode needs a broker adapter - there isn't one for futures yet; use dry_run")
        d = r.to_dict()
        d[key] = v.split(",") if key == "accounts" and isinstance(v, str) else v
        routes[name] = Route.from_dict(d)
    else:
        d = r.rules.to_dict()
        if key not in d:
            raise typer.BadParameter(f"unknown rule '{key}'")
        d[key] = v
        r.rules = type(r.rules).from_dict(d)
    save_routes(_dd(), routes)
    console.print(f"{name}.{key} = {json.dumps(v)}")


@route_app.command("test")
def test_cmd(name: str, text: str, price: Optional[float] = typer.Option(None, help="Market price now (default: the alert's)."),
             at: Optional[str] = typer.Option(None, help="Pretend time, e.g. '2026-10-01 09:50' (ET).")):
    """Parse one alert and show what each account on the route would do (nothing is sent)."""
    from .routes import RouteEngine, parse_alert
    from .routes.engine import NY
    from .routes.log import append
    from .alpha.context import calendar_events
    routes = _get(name)
    now = datetime.fromisoformat(at).replace(tzinfo=NY) if at else datetime.now(NY)
    t0 = _time.perf_counter()
    a = parse_alert(text, ts=now)
    ms = (_time.perf_counter() - t0) * 1000
    console.print(f"[bold]parsed[/bold] in {ms:.1f} ms → {a.action} "
                  + (f"{'LONG' if a.side > 0 else 'SHORT'} {a.symbol} entry {a.entry} stop {a.stop} targets {a.targets}"
                     if a.action == "entry" else "") + (f"  [dim]{'; '.join(a.notes)}[/dim]" if a.notes else ""))
    eng = RouteEngine(routes[name], events_for=lambda d: calendar_events(d, Settings()))
    ds = eng.on_alert(a, now=now, price=price)
    for d in ds:
        console.print(("[green]" if d.verdict == "placed" else "[yellow]") + escape(d.line()) + "[/]")
        for o in d.orders:
            console.print(f"   {o['type']:11} {o['side']:4} {o['qty']} @ {o['price']}"
                          + (f" → when reached, stop to entry {o['then_stop']:+g} pts" if o.get("then_stop") is not None else ""))
        for c in d.checks:
            console.print(f"   [dim]{'✓' if c[1] else '✗'} {c[0]}: {c[2]}[/dim]")
    append(_dd(), [{"kind": "test", "route": name, "parse_ms": round(ms, 2), "alert": a.to_dict(),
                    "decisions": [d.to_dict() for d in ds]}])


@route_app.command("replay")
def replay_cmd(name: str, export: Path = typer.Argument(..., help="DiscordKit JSON export of the channel."),
               bars: Path = typer.Option(Path("data/mnq_5m.csv"), help="Intraday bars to fill on."),
               copy_contracts: int = typer.Option(8, help="Size of the 'as copied' comparison."),
               trades: bool = typer.Option(False, help="List every trade."),
               out: Optional[Path] = typer.Option(None, "--json")):
    """Replay a channel export through a route and fill it on bars; compare with plain copying."""
    from .alpha.data import load_intraday_csv
    from .routes.replay import as_copied_route, load_export, replay
    routes = _get(name)
    msgs = load_export(export)
    b = load_intraday_csv(bars)
    lo, hi = b.index[0], b.index[-1]
    msgs = [m for m in msgs if lo <= m["ts"] <= hi]
    console.print(f"{len(msgs)} messages inside the bar data ({lo:%Y-%m-%d} → {hi:%Y-%m-%d})")
    res = {"as copied": replay(msgs, b, as_copied_route(copy_contracts)), name: replay(msgs, b, routes[name])}
    t = Table(title="Replay", box=box.SIMPLE)
    for c in ("route", "trades", "win rate", "net $", "worst trade", "worst day", "max DD", "PF"):
        t.add_column(c)
    for k, v in res.items():
        s = v["summary"]
        if not s.get("trades"):
            t.add_row(k, "0", "", "", "", "", "", "")
            continue
        t.add_row(k, str(s["trades"]), f"{s['win_rate']:.0%}", f"{s['net_usd']:+,.0f}", f"{s['worst_trade_usd']:+,.0f}",
                  f"{s['worst_day_usd']:+,.0f}", f"{s['max_dd_usd']:,.0f}", str(s["profit_factor"]))
    console.print(t)
    skipped = {}
    for l in res[name]["log"]:
        if l["verdict"] == "skipped":
            key = l["reasons"][0].split(" (")[0] if l["reasons"] else "?"
            skipped[key] = skipped.get(key, 0) + 1
    if skipped:
        console.print("[bold]skipped because[/bold]: " + "; ".join(f"{k} ×{v}" for k, v in sorted(skipped.items(), key=lambda x: -x[1])))
    if trades:
        for k, v in res.items():
            console.print(f"[bold]{k}[/bold]")
            for tr in v["trades"]:
                console.print(f"  {tr['opened'][:16]} {'L' if tr['side'] > 0 else 'S'} {tr['contracts']} @ {tr['entry']:,.2f} "
                              f"{tr['usd']:+,.0f}  [dim]{' | '.join(tr['events'])}[/dim]")
    if out:
        out.write_text(json.dumps(res, indent=1, default=str))


@route_app.command("log")
def log_cmd(n: int = typer.Option(20, "-n", "--n")):
    from .routes.log import tail
    for r in tail(_dd(), n):
        for d in r.get("decisions", []):
            console.print(f"{d.get('ts', '')[:19]} {r.get('route')} \\[{d['account']}] {d['alert_action']} → {d['verdict']} "
                          f"{d.get('contracts') or ''} {'; '.join(d.get('reasons') or [])}")
