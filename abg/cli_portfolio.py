"""CLI commands for the saved portfolio, alert rules, signal history and the live monitor."""
from __future__ import annotations

import asyncio
import json
import signal as _signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .cli import _run, _state, app, console
from .config import Settings
from .engine import AnalysisEngine
from .errors import ABGError
from .portfolio import RULE_KINDS, PortfolioStore, snapshot
from .render import num, risk_style, signed

pf_app = typer.Typer(help="Saved portfolio: holdings, watchlist, stops/targets.", no_args_is_help=True)
alert_app = typer.Typer(help="Custom alert rules (price/change/RSI/score thresholds).", no_args_is_help=True)
app.add_typer(pf_app, name="portfolio")
app.add_typer(pf_app, name="pf", hidden=True)
app.add_typer(alert_app, name="alert")

PF = typer.Option(None, "-P", "--portfolio", help="Portfolio name (default: ABG_DEFAULT_PORTFOLIO / 'main').")


def _settings() -> Settings:
    return Settings(**_state["overrides"])


def _store() -> tuple[PortfolioStore, Settings]:
    s = _settings()
    return PortfolioStore.from_settings(s), s


def _pf(name: str | None, s: Settings) -> str:
    return name or s.default_portfolio


def _ts(date_str: str | None) -> float | None:
    if not date_str:
        return None
    return datetime.fromisoformat(date_str).replace(hour=16).timestamp()


def _fail(e: Exception) -> None:
    console.print(f"[bold red]Error:[/bold red] {getattr(e, 'message', None) or e}")
    raise typer.Exit(2)


# --------------------------------------------------------------------------- show
def render_snapshot(snap: dict) -> None:
    t = snap["totals"]
    head = Table.grid(padding=(0, 4))
    head.add_row(Text(f"{num(t['market_value'])} USD", style="bold"),
                 Text.assemble("day ", signed(t.get("day_pnl")), " (", signed(t.get("day_pct"), 2, "%"), ")"),
                 Text.assemble("unrealized ", signed(t.get("unrealized_pnl")), " (", signed(t.get("unrealized_pct"), 2, "%"), ")"),
                 Text.assemble("realized ", signed(t.get("realized_pnl"))))
    console.print(Panel(head, title=f"Portfolio · {snap['portfolio']}", border_style="cyan"))
    if snap["holdings"]:
        ht = Table(box=box.SIMPLE_HEAVY, expand=True)
        for c in ("Symbol", "Shares", "Avg cost", "Price", "Day %", "Value", "Weight", "Unreal. $", "Unreal. %",
                  "Stop / Target", "Signal", "Risk"):
            ht.add_column(c, justify="right" if c not in ("Symbol", "Signal", "Risk") else "left")
        for h in snap["holdings"]:
            st = f"{num(h.get('stop_loss'))} / {num(h.get('take_profit'))}" if (h.get("stop_loss") or h.get("take_profit")) else "—"
            ht.add_row(Text(h["symbol"], style="bold"), num(h["shares"], 4).rstrip("0").rstrip("."), num(h["avg_cost"]),
                       num(h.get("price")) if not h.get("quote_error") else Text("n/a", style="red"),
                       signed(h.get("change_pct"), 2, "%"), num(h.get("market_value")), num(h.get("weight_pct"), 1, "%"),
                       signed(h.get("unrealized_pnl")), signed(h.get("unrealized_pct"), 1, "%"), st,
                       f"{h.get('signal_label') or '—'}", Text(h.get("risk_level") or "—", style=risk_style(h.get("risk_level"))))
        console.print(ht)
    else:
        console.print("[dim]No holdings yet: `abg portfolio buy AAPL 10` (price defaults to the live quote).[/dim]")
    if snap["watchlist"]:
        wt = Table(title="Watchlist", box=box.SIMPLE, expand=False)
        for c in ("Symbol", "Price", "Day %", "Signal", "Risk", "Top setup"):
            wt.add_column(c)
        for w in snap["watchlist"]:
            wt.add_row(w["symbol"], num(w.get("price")), signed(w.get("change_pct"), 2, "%"), w.get("signal_label") or "—",
                       Text(w.get("risk_level") or "—", style=risk_style(w.get("risk_level"))), w.get("top_setup") or "—")
        console.print(wt)
    r = snap.get("risk") or {}
    if r.get("available"):
        console.print(f"[dim]Portfolio risk: vol {num(r['ann_volatility_pct'], 1)}% · 1-day VaR95 {num(r['var_95_1d_pct'])}% "
                      f"(≈ {num(r.get('var_95_1d_usd'))} USD) · diversification {num(r['diversification_ratio'])}[/dim]")
    if snap.get("rules"):
        console.print(f"[dim]{sum(1 for x in snap['rules'] if x['enabled'])} active alert rule(s) - `abg alert list`[/dim]")


@pf_app.command("show")
def pf_show(portfolio: Optional[str] = PF, risk: bool = typer.Option(True, "--risk/--no-risk"),
            analyze: bool = typer.Option(False, "--analyze", help="Also compute signal/risk per symbol (slower)."),
            as_json: bool = typer.Option(False, "--json")):
    """Holdings with live prices and P&L, watchlist and portfolio risk."""
    store, s = _store()
    name = _pf(portfolio, s)

    async def go(eng: AnalysisEngine):
        analysis = {}
        if analyze:
            from .engine import AnalyzeOptions
            for sym in store.symbols(name):
                try:
                    r = await eng.analyze(sym, AnalyzeOptions(ai=False, options=False, news=False))
                    rk = (r.get("risk") or [{}])[0]
                    plays = [p for p in r["plays"] if p["name"] != "No Clear Setup"]
                    analysis[sym] = {"signal_label": r["signal"]["label"], "signal_score": r["signal"]["score"],
                                     "risk_level": rk.get("level"), "risk_score": rk.get("score"),
                                     "top_setup": plays[0]["name"] if plays else None}
                except ABGError:
                    pass
        return await snapshot(eng, store, name, analysis=analysis, with_risk=risk)
    with console.status("Pricing portfolio…"):
        snap = _run(go)
    if as_json:
        sys.stdout.write(json.dumps(snap, indent=2, default=str) + "\n")
    else:
        render_snapshot(snap)


def _trade(side: str, symbol: str, shares: float, price: float | None, date: str | None, fees: float,
           note: str | None, portfolio: str | None) -> None:
    store, s = _store()
    name = _pf(portfolio, s)
    if price is None:
        async def go(eng):
            return (await eng.quote(symbol)).value.price
        price = _run(go)
        console.print(f"[dim]using live price {price:,.2f}[/dim]")
    try:
        t = store.add_transaction(name, symbol, side, shares, price, fees, _ts(date), note)
    except ABGError as e:
        _fail(e)
    pos = next((p for p in store.positions(name, include_closed=True) if p.symbol == t.symbol), None)
    console.print(f"[green]{side} {t.shares:g} {t.symbol} @ {t.price:,.2f}[/green] (tx #{t.id}) → now "
                  f"{pos.shares:g} shares, avg cost {pos.avg_cost:,.2f}, realized P&L {pos.realized_pnl:+,.2f}")


@pf_app.command("buy")
def pf_buy(symbol: str, shares: float, price: Optional[float] = typer.Option(None, help="Default: live quote."),
           date: Optional[str] = typer.Option(None, help="Trade date YYYY-MM-DD (default now)."),
           fees: float = 0.0, note: Optional[str] = None, portfolio: Optional[str] = PF):
    """Record a purchase."""
    _trade("BUY", symbol, shares, price, date, fees, note, portfolio)


@pf_app.command("sell")
def pf_sell(symbol: str, shares: float, price: Optional[float] = typer.Option(None, help="Default: live quote."),
            date: Optional[str] = typer.Option(None, help="Trade date YYYY-MM-DD (default now)."),
            fees: float = 0.0, note: Optional[str] = None, portfolio: Optional[str] = PF):
    """Record a sale (realized P&L uses average cost)."""
    _trade("SELL", symbol, shares, price, date, fees, note, portfolio)


@pf_app.command("watch")
def pf_watch(symbols: list[str], portfolio: Optional[str] = PF):
    """Add tickers to the watchlist (monitored for signals, no position)."""
    store, s = _store()
    try:
        added = [store.watch(_pf(portfolio, s), x) for x in symbols]
    except ABGError as e:
        _fail(e)
    console.print(f"[green]watching {', '.join(added)}[/green]")


@pf_app.command("unwatch")
def pf_unwatch(symbol: str, portfolio: Optional[str] = PF):
    store, s = _store()
    ok = store.unwatch(_pf(portfolio, s), symbol)
    console.print("[green]removed[/green]" if ok else "[yellow]not on the watchlist[/yellow]")


@pf_app.command("set")
def pf_set(symbol: str, stop: Optional[float] = typer.Option(None, help="Stop-loss price (critical alert)."),
           target: Optional[float] = typer.Option(None, help="Take-profit price."),
           note: Optional[str] = None, clear: bool = typer.Option(False, help="Remove stop/target/note."),
           portfolio: Optional[str] = PF):
    """Set a stop-loss / take-profit on a holding."""
    store, s = _store()
    store.set_position_meta(_pf(portfolio, s), symbol, stop, target, note, clear)
    console.print(f"[green]{symbol.upper()} updated[/green]")


@pf_app.command("history")
def pf_history(symbol: Optional[str] = typer.Argument(None), portfolio: Optional[str] = PF):
    """Transaction log."""
    store, s = _store()
    t = Table(box=box.SIMPLE)
    for c in ("#", "Date", "Side", "Symbol", "Shares", "Price", "Fees", "Note"):
        t.add_column(c)
    for x in store.transactions(_pf(portfolio, s), symbol.upper() if symbol else None):
        t.add_row(str(x.id), datetime.fromtimestamp(x.ts).strftime("%Y-%m-%d %H:%M"),
                  Text(x.side, style="green" if x.side == "BUY" else "red"), x.symbol, f"{x.shares:g}", num(x.price),
                  num(x.fees), x.notes or "")
    console.print(t)


@pf_app.command("remove-tx")
def pf_remove_tx(tx_id: int, portfolio: Optional[str] = PF):
    """Delete a transaction by id (see `abg portfolio history`)."""
    store, s = _store()
    try:
        ok = store.delete_transaction(_pf(portfolio, s), tx_id)
    except ABGError as e:
        _fail(e)
    console.print("[green]deleted[/green]" if ok else "[yellow]no such transaction[/yellow]")


@pf_app.command("export")
def pf_export(out: Path = typer.Option(Path("portfolio-backup.json"), "-o", "--out"), portfolio: Optional[str] = PF):
    """Back up transactions, stops, watchlist and rules to JSON."""
    store, s = _store()
    out.write_text(json.dumps(store.export(_pf(portfolio, s)), indent=2))
    console.print(f"[green]exported → {out}[/green]")


@pf_app.command("import")
def pf_import(path: Path, replace: bool = typer.Option(False, help="Wipe the portfolio first."), portfolio: Optional[str] = PF):
    """Restore from a JSON export."""
    store, s = _store()
    try:
        res = store.import_(json.loads(path.read_text()), portfolio, replace)
    except ABGError as e:
        _fail(e)
    console.print(f"[green]imported[/green] {res}")


@pf_app.command("list")
def pf_list():
    """List portfolios."""
    store, s = _store()
    console.print(", ".join(store.portfolios()) or "(none)")
    console.print(f"[dim]stored in {store.path}[/dim]")


# --------------------------------------------------------------------------- alerts
@alert_app.command("add")
def alert_add(symbol: str, kind: str = typer.Argument(..., help=" | ".join(RULE_KINDS)), value: float = typer.Argument(...),
              repeat: bool = typer.Option(False, help="Keep the rule after it fires (default: one-shot)."),
              note: Optional[str] = None, portfolio: Optional[str] = PF):
    """Add a custom alert, e.g. `abg alert add NVDA price_above 150`."""
    store, s = _store()
    try:
        r = store.add_rule(_pf(portfolio, s), symbol, kind, value, not repeat, note)
    except ABGError as e:
        _fail(e)
    console.print(f"[green]rule #{r.id}: {r.symbol} {r.kind} {r.value:g}[/green] ({'repeating' if repeat else 'one-shot'})")


@alert_app.command("list")
def alert_list(portfolio: Optional[str] = PF):
    store, s = _store()
    t = Table(box=box.SIMPLE)
    for c in ("#", "Symbol", "Rule", "Value", "Enabled", "Mode", "Note"):
        t.add_column(c)
    for r in store.rules(_pf(portfolio, s)):
        t.add_row(str(r.id), r.symbol, r.kind, f"{r.value:g}", "yes" if r.enabled else "[dim]fired/off[/dim]",
                  "one-shot" if r.one_shot else "repeat", r.note or "")
    console.print(t)


@alert_app.command("remove")
def alert_remove(rule_id: int, portfolio: Optional[str] = PF):
    store, s = _store()
    console.print("[green]removed[/green]" if store.delete_rule(_pf(portfolio, s), rule_id) else "[yellow]not found[/yellow]")


@alert_app.command("kinds")
def alert_kinds():
    for k, v in RULE_KINDS.items():
        console.print(f"[bold]{k:14}[/bold] {v}")


# --------------------------------------------------------------------------- signals / monitor
@app.command("signals")
def signals_cmd(limit: int = typer.Option(30), symbol: Optional[str] = None, portfolio: Optional[str] = PF):
    """Recent signals (saved history)."""
    store, s = _store()
    t = Table(box=box.SIMPLE, expand=True)
    for c in ("Time (ET)", "Sev.", "Signal", "Detail", "Sent to"):
        t.add_column(c, overflow="fold")
    for x in store.signals(_pf(portfolio, s), limit, symbol.upper() if symbol else None):
        sev = {"critical": "bold white on red", "warning": "yellow", "info": "cyan"}[x["severity"]]
        from .live.market_hours import NY
        t.add_row(datetime.fromtimestamp(x["ts"]).astimezone(NY).strftime("%m-%d %H:%M"), Text(x["severity"], style=sev),
                  Text(x["title"], style="bold"), x["message"],
                  ", ".join(f"{k}:{'ok' if v in ('ok', 'queued') else 'x'}" for k, v in x["delivered"].items()) or "dashboard")
    console.print(t)


@app.command("monitor")
def monitor_cmd(portfolio: Optional[str] = PF, once: bool = typer.Option(False, help="Run one full sweep and exit."),
                interval: Optional[float] = typer.Option(None, help="Override quote sweep seconds.")):
    """Run the live monitor in this window: sweeps the portfolio and sends signals (Ctrl+C to stop)."""
    from .live import ConsoleNotifier, Monitor, NotificationHub
    ov = dict(_state["overrides"])
    if interval:
        ov["monitor_quote_interval"] = interval
    s = Settings(**ov)
    store = PortfolioStore.from_settings(s)

    async def go():
        async with AnalysisEngine(s) as eng:
            hub = NotificationHub(s, store, eng.http, extra=[ConsoleNotifier(console)])
            mon = Monitor(eng, store, hub, portfolio)
            syms = store.symbols(mon.pf)
            chans = ", ".join(c["name"] for c in hub.describe() if c.get("configured"))
            console.print(Panel(f"Portfolio [bold]{mon.pf}[/bold] · {len(syms)} symbols: {', '.join(syms) or '(none - add some first)'}\n"
                                f"Channels: {chans}\nQuotes every {s.monitor_quote_interval:g}s · full analysis every "
                                f"{s.monitor_analysis_interval / 60:g} min · market {'OPEN' if mon.status()['market']['open'] else 'closed'}",
                                title="ABG live monitor", border_style="green"))
            try:
                if once:
                    await mon.analysis_sweep()
                else:
                    loop = asyncio.get_running_loop()
                    for sig in (_signal.SIGINT, _signal.SIGTERM):
                        try:
                            loop.add_signal_handler(sig, mon.stop)
                        except (NotImplementedError, RuntimeError):   # Windows: Ctrl+C raises KeyboardInterrupt instead
                            pass
                    task = asyncio.ensure_future(mon.run())
                    last = 0
                    while not task.done():
                        await asyncio.sleep(1)
                        if mon.sweeps != last:
                            last = mon.sweeps
                            st = mon.status()
                            console.print(f"[dim]{time.strftime('%H:%M:%S')} sweep #{mon.sweeps} · {len(st['symbols'])} symbols · "
                                          f"{mon.signals_emitted} signals so far · market {'open' if st['market']['open'] else 'closed'}"
                                          f"{' · ' + str(len(st['errors'])) + ' recent errors' if st['errors'] else ''}[/dim]")
                    task.result()
            finally:
                await hub.aclose()
    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        console.print("[dim]monitor stopped[/dim]")
    except ABGError as e:
        _fail(e)


@app.command("notify-test")
def notify_test(channel: str = typer.Option("all", help="all | discord | email | desktop")):
    """Send a test alert through the configured channels."""
    from .live import NotificationHub
    s = _settings()
    store = PortfolioStore.from_settings(s)

    async def go(eng):
        hub = NotificationHub(s, store, eng.http)
        try:
            return hub.describe(), await hub.test(channel)
        finally:
            await hub.aclose()
    desc, res = _run(go)
    for d in desc:
        state = "[green]configured[/green]" if d.get("configured") else f"[dim]off - {d.get('hint')}[/dim]"
        console.print(f"{d['name']:10} {state}  {('→ ' + str(res.get(d['name']))) if d['name'] in res else ''}")
