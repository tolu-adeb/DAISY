"""CLI for external signals: `abg ext parse | add | list | show | cancel | close | edit | update | stats | poll | discord-test`."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Optional

import typer
from rich import box
from rich.panel import Panel
from rich.table import Table

from .cli import _state, app, console
from .config import Settings
from .engine import AnalysisEngine
from .errors import ABGError
from .render import num, signed

ext_app = typer.Typer(help="External trade ideas / signals: interpret, track, relay (docs/11).", no_args_is_help=True)
app.add_typer(ext_app, name="ext")

STATUS_STYLE = {"pending": "yellow", "active": "bold green", "closed": "cyan", "invalidated": "red", "missed": "dim",
                "expired": "dim", "cancelled": "dim", "rejected": "red"}


def _fail(e: Exception) -> None:
    console.print(f"[bold red]Error:[/bold red] {getattr(e, 'message', None) or e}")
    raise typer.Exit(2)


def _with_tracker(fn):
    """Run ``fn(tracker, settings)`` with an engine, hub (dashboard feed / desktop / email) and relay."""
    from .extsignals.discord import DiscordPoller, DiscordRelay
    from .extsignals.store import ExtSignalStore
    from .extsignals.tracker import ExtSignalTracker
    from .live import NotificationHub
    from .portfolio import PortfolioStore

    s = Settings(**_state["overrides"])

    async def go():
        async with AnalysisEngine(s) as eng:
            pstore, store = PortfolioStore.from_settings(s), ExtSignalStore.from_settings(s)
            hub = NotificationHub(s, pstore, eng.http)
            relay = DiscordRelay(s, eng.http)
            tr = ExtSignalTracker(eng, store, hub, relay if relay.enabled else None, s)
            tr.poller = DiscordPoller(s, eng.http, tr, relay)
            try:
                return await fn(tr, s)
            finally:
                await tr.drain(30)
                await hub.aclose()
                store.close()
                pstore.close()
    try:
        return asyncio.run(go())
    except ABGError as e:
        _fail(e)
    except KeyboardInterrupt:
        raise typer.Exit(130)


def _age(ts: float | None) -> str:
    if not ts:
        return ""
    d = time.time() - ts
    return f"{d / 60:.0f}m" if d < 3600 else f"{d / 3600:.1f}h" if d < 86400 else f"{d / 86400:.1f}d"


def render_parsed(p: dict) -> None:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    rows = [("kind", p["kind"]), ("symbol", p.get("symbol")), ("direction", p.get("direction")),
            ("entry", f"{p.get('entry_type')}  {p.get('entry_low')} – {p.get('entry_high')}"),
            ("stop", f"{p.get('stop')} ({p.get('stop_basis')})"), ("targets", " / ".join(f"{x:g}" for x in p.get("targets") or [])),
            ("timeframe", p.get("timeframe")), ("instrument", p.get("instrument")), ("confidence", f"{p.get('confidence', 0):.0%}")]
    if p["kind"] == "update":
        rows = [("kind", "update"), ("action", p.get("action")), ("symbol", p.get("symbol")), ("new stop", p.get("new_stop")),
                ("target #", (p.get("target_index") or 0) + 1 if p.get("target_index") is not None else None),
                ("fraction", p.get("fraction"))]
    for k, v in rows:
        t.add_row(k, str(v) if v not in (None, "") else "—")
    for w in p.get("warnings") or []:
        t.add_row("[yellow]warning[/yellow]", w)
    console.print(Panel(t, title="Interpretation", border_style="cyan"))


def short_levels(i: dict) -> str:
    g = lambda x: f"{x:,.2f}".rstrip("0").rstrip(".") if x is not None else "?"  # noqa: E731
    if i["entry_type"] == "market":
        e = "mkt"
    elif i["entry_low"] == i["entry_high"]:
        e = {"breakout_above": ">", "limit_above": ">", "breakdown_below": "<", "limit_below": "<"}.get(i["entry_type"], "") + g(i["entry_low"])
    else:
        e = f"{g(i['entry_low'])}-{g(i['entry_high'])}"
    return f"{e} sl {g(i['stop'])} tp {','.join(g(t) for t in i['targets'])}"


def render_ideas(ideas: list[dict]) -> None:
    t = Table(box=box.SIMPLE_HEAVY, header_style="bold")
    for c in ("#", "Symbol", "Dir", "Status", "Plan", "Last", "To entry", "Grd", "R", "Source", "Age"):
        t.add_column(c, justify="right" if c in ("#", "Last", "R", "Age") else "left")
    for i in ideas:
        st = i["status"]
        where = (f"{i['distance_pct']:+.1f}%" if i.get("distance_pct") is not None else
                 num(i.get("entry_price")) if i.get("entry_price") else "")
        t.add_row(str(i["id"]), i["symbol"], i["direction"], f"[{STATUS_STYLE.get(st, '')}]{st}[/]", short_levels(i),
                  num(i.get("last_price")), where, i.get("grade") or "", signed(i.get("total_r"), suffix="R") if i.get("entry_price") else "",
                  (i.get("author") or i.get("source") or "")[:14], _age(i.get("created_at")))
    console.print(t)


@ext_app.command("parse")
def parse_cmd(text: str = typer.Argument(..., help="The signal message (quote it).")):
    """Show how a message is interpreted (no network, nothing is tracked)."""
    from .extsignals.parser import parse
    render_parsed(parse(text).to_dict())


@ext_app.command("add")
def add_cmd(text: str = typer.Argument(..., help="The signal message (quote it)."),
            author: Optional[str] = typer.Option(None, help="Who posted it (for per-source stats).")):
    """Interpret a message and start tracking it (or apply it as an update to a tracked idea)."""
    async def fn(tr, s):
        res = await tr.ingest(text, author=author)
        render_parsed(res["parsed"])
        color = {"tracking": "green", "updated": "cyan", "rejected": "red"}.get(res["outcome"], "yellow")
        console.print(f"[bold {color}]{res['outcome'].upper()}[/]: {res['message']}")
        if res.get("idea"):
            for e in reversed(tr.store.events(res["idea"]["id"], limit=10)):
                console.print(Panel(e["text"], title=e["title"], border_style="dim"))
    _with_tracker(fn)


@ext_app.command("list")
def list_cmd(status: str = typer.Option("open", help="open | all | closed | pending | active | final"),
             symbol: Optional[str] = None, limit: int = 50):
    """Tracked ideas."""
    from .extsignals.lifecycle import FINAL_STATES, OPEN_STATES

    async def fn(tr, s):
        st = {"all": None, "open": OPEN_STATES, "final": FINAL_STATES}.get(status, {status})
        ideas = [tr.view(i) for i in tr.store.ideas(st, symbol.upper() if symbol else None, limit)]
        if not ideas:
            console.print(f"[dim]no {status} ideas. Add one: abg ext add \"$NVDA long 117-119 sl 112 tp 130\"[/dim]")
        else:
            render_ideas(ideas)
    _with_tracker(fn)


@ext_app.command("show")
def show_cmd(idea_id: int):
    """An idea with its full decision timeline."""
    async def fn(tr, s):
        i = tr.store.get(idea_id)
        if not i:
            _fail(ValueError(f"idea #{idea_id} not found"))
        render_ideas([tr.view(i)])
        console.print(f"[dim]original message:[/dim] {i.raw[:500]}")
        for e in reversed(tr.store.events(idea_id, limit=200)):
            when = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M")
            rel = ", ".join(f"{k}:{v}" for k, v in (e.get("relayed") or {}).items())
            console.print(Panel(e["text"], title=f"{when} · {e['title']}", subtitle=rel or None, border_style="dim"))
    _with_tracker(fn)


@ext_app.command("cancel")
def cancel_cmd(idea_id: int):
    """Stop tracking an idea (closes the paper position at the live price if it was entered)."""
    async def fn(tr, s):
        i = await tr.cancel(idea_id)
        console.print(f"#{idea_id}: {i.status if i else 'not found'}")
    _with_tracker(fn)


@ext_app.command("close")
def close_cmd(idea_id: int, price: Optional[float] = typer.Option(None, help="Exit price (default: live quote)."),
              fraction: Optional[float] = typer.Option(None, help="Close only part, e.g. 0.5.")):
    """Close (or trim) an active paper position."""
    async def fn(tr, s):
        i = await tr.close(idea_id, price, fraction)
        console.print(f"#{idea_id}: {i.status if i else 'not found'} · trade {signed(i.total_r() if i else None, 'R')}")
    _with_tracker(fn)


@ext_app.command("edit")
def edit_cmd(idea_id: int, stop: Optional[float] = None,
             target: Optional[list[float]] = typer.Option(None, "--target", "-t", help="Repeat for several targets."),
             entry_low: Optional[float] = None, entry_high: Optional[float] = None,
             stop_basis: Optional[str] = typer.Option(None, help="touch | close")):
    """Change an idea's stop, targets, entry zone or stop basis."""
    async def fn(tr, s):
        i = await tr.edit(idea_id, stop=stop, targets=target, entry_low=entry_low, entry_high=entry_high, stop_basis=stop_basis)
        if i:
            render_ideas([tr.view(i)])
    _with_tracker(fn)


@ext_app.command("update")
def update_cmd():
    """One tracking pass now (catch up missed days, live quotes, re-grade).  The monitor does this continuously."""
    from .portfolio.valuation import fetch_quotes

    async def fn(tr, s):
        n = await tr.catch_up()
        syms = tr.symbols()
        if syms:
            n += await tr.on_quotes(await fetch_quotes(tr.engine, syms, use_cache=False))
            n += await tr.review()
        console.print(f"{len(syms)} symbols checked · {n} events")
        render_ideas([tr.view(i) for i in tr.store.open_ideas()])
    _with_tracker(fn)


@ext_app.command("stats")
def stats_cmd():
    """Track record of every source: win rate, average R, profit factor."""
    async def fn(tr, s):
        st = tr.stats()
        g = Table.grid(padding=(0, 3))
        for k in ("ideas", "pending", "active", "closed", "never_filled", "wins", "losses"):
            g.add_row(k.replace("_", " "), str(st[k]))
        g.add_row("win rate", f"{st['win_rate']:.0%}" if st["win_rate"] is not None else "—")
        g.add_row("avg R", signed(st["avg_r"], suffix="R") if st["avg_r"] is not None else "—")
        g.add_row("total R", signed(st["total_r"], suffix="R"))
        g.add_row("profit factor", f"{st['profit_factor']:.2f}" if st["profit_factor"] else "—")
        g.add_row("paper P&L", signed(st["realized_pnl"]))
        console.print(Panel(g, title="External signals: track record", border_style="cyan"))
        t = Table("Source", "Ideas", "Triggered", "Closed", "Win rate", "Avg R", "Total R", box=box.SIMPLE)
        for k, b in st["by_source"].items():
            t.add_row(k, str(b["ideas"]), str(b["triggered"]), str(b["closed"]),
                      f"{b['win_rate']:.0%}" if b["win_rate"] is not None else "—",
                      signed(b["avg_r"], suffix="R") if b["avg_r"] is not None else "—", signed(b["total_r"], suffix="R"))
        console.print(t)
    _with_tracker(fn)


@ext_app.command("poll")
def poll_cmd():
    """Read new messages from the configured Discord channels once and ingest them."""
    async def fn(tr, s):
        if not tr.poller.enabled:
            _fail(ValueError("set ABG_DISCORD_BOT_TOKEN and ABG_EXT_DISCORD_CHANNEL_IDS in .env"))
        console.print(await tr.poller.poll_once())
    _with_tracker(fn)


@ext_app.command("discord-test")
def discord_test_cmd(send: bool = typer.Option(False, "--send", help="Also post a test embed through the relay.")):
    """Check the bot token, channel access and relay configuration."""
    async def fn(tr, s):
        res = await tr.poller.check()
        console.print(res)
        if send:
            if tr.relay is None:
                _fail(ValueError("relay not configured: set ABG_EXT_RELAY_WEBHOOK_URL (or ABG_DISCORD_WEBHOOK_URL), "
                                 "or ABG_EXT_RELAY_MODE=reply with a bot token"))
            from .extsignals.lifecycle import Idea
            demo = Idea("TEST", "long", "zone", 100, 101, 95, [110, 120], id=0, source="test")
            x = {"title": "🧪 ABG signal relay test", "summary": "If you can read this, relays to this channel work.",
                 "why": ["decisions (entries, targets, stops, exits) appear here with the reasoning"], "plan": [],
                 "risks": [], "fields": {"Mode": tr.relay.mode}}
            demo.channel_id = (s.ext_channel_list() or [None])[0]
            console.print("relay:", await tr.relay.send(demo, "ingested", x))
    _with_tracker(fn)
