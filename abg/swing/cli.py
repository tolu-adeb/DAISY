"""``abg swing ...`` - the swing desk from the command line (the monitor runs it on its own; these are for by-hand use).

    abg swing status                  scoreboard, the market gate, open positions and working orders
    abg swing pass [--offline]        run a pass now (fresh daily bars, fills, exits) - alerts go to Discord if a webhook is set
    abg swing add "TEXT" [--author]   parse a pasted idea, show the gate's verdict, then track it (asks first)
    abg swing scan                    the desk's own setups across config/universe.txt
    abg swing digest [--period] [--post]
    abg swing research [--deep]       the R&D agent once (needs an API key; costs tokens)
"""
from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table

swing_app = typer.Typer(help="Swing desk: House of Traders ideas, graded and tracked (docs/19).", no_args_is_help=True)
console = Console()


def _desk(fetch: bool = True):
    from abg.config import Settings
    from abg.swing.desk import SwingDesk
    return SwingDesk(Settings(), None, None, fetch=fetch)


def _with_http(fn):
    """Run ``fn(desk)`` with an HTTP client so Discord posts work from the CLI too."""
    from abg.config import Settings
    from abg.engine import AnalysisEngine
    from abg.swing.desk import SwingDesk

    async def go():
        s = Settings()
        async with AnalysisEngine(s) as eng:
            return await fn(SwingDesk(s, None, eng.http))
    return asyncio.run(go())


@swing_app.command("status")
def status():
    """Scoreboard, gate, open positions and working orders."""
    d = _desk(fetch=False)
    from swingdesk.monitor import scoreboard
    sb = scoreboard(d.main_book())
    g = d.gate()
    console.print(f"[bold]Swing desk[/bold]  {sb['open']} open · {sb['watching']} working · {sb['closed']} closed "
                  f"({sb['wins']} wins{', ' + str(sb['win_rate']) + '%' if sb['win_rate'] is not None else ''}, {sb['sum_R']:+.2f}R) · "
                  f"{sb['skipped']} skipped")
    if g:
        console.print(f"gate: SPY {g['spy_close']:.2f} vs {g['sma_n']}-day {g['spy_sma']:.2f} -> market branch "
                      f"{'[green]ON[/green]' if g['market_branch'] else 'off'} · ATR limit {g['max_atr_pct']}%")
    t = Table("Ticker", "State", "Posted", "Limit", "Stop", "TP", "Last", "R / to entry")
    for i in d.ideas():
        if i["state"] not in ("FILLED", "WATCHING"):
            continue
        extra = f"{i['r']:+.2f}R" if i["state"] == "FILLED" and i["r"] is not None else (
            f"{i['to_entry_pct']:+.1f}%" if i["to_entry_pct"] is not None else "")
        t.add_row(i["symbol"], i["state"], i["posted_et"][:10], f"{i['limit'] or 0:g}", f"{i['stop_px'] or 0:g}",
                  f"{i['tp'] or 0:g}", f"{i['price'] or 0:g}", extra)
    console.print(t)


@swing_app.command("pass")
def run_pass(offline: bool = typer.Option(False, help="Use the cached prices only (no downloads, no quotes).")):
    """Run a pass now."""
    async def go(d):
        d.fetch = not offline
        return await d.run_pass("manual")
    r = _with_http(go)
    console.print(f"{len(r['alerts'])} alerts{' (first run: history graded, not posted)' if r['seed'] else ''} · "
                  f"{r['live']} live ideas · {r['symbols']} symbols · {r.get('delivered')}")
    for a in r["alerts"][:30]:
        console.print(f"  {a['text'].splitlines()[0]}")


@swing_app.command("add")
def add(text: str = typer.Argument(..., help="The post, as pasted."), author: str = typer.Option("manual"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Track it without asking.")):
    """Parse an idea, show the verdict, then track it."""
    async def prev(d):
        return await d.preview(text=text)
    p = _with_http(prev)
    parsed = p.get("parsed") or {}
    for w in parsed.get("warnings", []):
        console.print(f"[yellow]{w}[/yellow]")
    if not p.get("fields"):
        raise typer.Exit(1)
    console.print_json(json.dumps(p["fields"]))
    if p["errors"]:
        for e in p["errors"]:
            console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    v = p["verdict"]
    console.print(f"[bold]{'TAKE' if v['take'] else 'SKIP'}[/bold] tier {v['tier']} · limit {v['entry_px']} · stop {v['stop_px']} · "
                  f"TP {v['tp_px']} · risk {v['risk_pct']}% · {v['shares_per_10k']} sh per $10k")
    for r in v["reasons"]:
        console.print(f"  - {r}")
    if not yes and not typer.confirm("Track it?"):
        raise typer.Exit(0)

    async def go(d):
        return await d.add(p["fields"], author)
    r = _with_http(go)
    console.print(f"tracking {r['key']}")


@swing_app.command("scan")
def scan():
    """The desk's own setups across config/universe.txt (new rows go to the scanner list)."""
    async def go(d):
        return await d.run_scan()
    console.print(f"scanner: {_with_http(go)} new setups")


@swing_app.command("digest")
def digest(period: str = typer.Option("daily"), post: bool = typer.Option(False, help="Post it to ABG_SWING_WEBHOOK_URL.")):
    async def go(d):
        return await d.digest(period, post=post)
    d = _with_http(go)
    console.print(f"[bold]{d['title']}[/bold] ({d['delivered']})")
    for ln in d["lines"]:
        console.print(ln)


@swing_app.command("research")
def research(deep: bool = typer.Option(False, help="Use the deep model (Opus).")):
    """Run the R&D agent once.  It may only *propose* a change; config/strategy.yaml changes only when you commit it."""
    async def go(d):
        return await d.research(deep)
    r = _with_http(go)
    console.print(r["text"])


# ---------------------------------------------------------------- wide scan (docs/19.8)
wide_app = typer.Typer(help="Wide scan: the swing setup across every liquid US stock, ranked (docs/19.8).",
                       no_args_is_help=True)
swing_app.add_typer(wide_app, name="wide")


def _progress(p: dict) -> None:
    if p.get("step") == "bars":
        console.print(f"  bars {p['done'] + 1}/{p['of']} ({p['day']}) · about {p['eta_min']} min left", end="\r")


@wide_app.command("check")
def wide_check():
    """Try each data source once and say what works (no keys are printed)."""
    from datetime import date, timedelta

    from abg.swing import wide as W

    async def go(d):
        src, out = d.sources, []
        day = W.last_session()
        prev = W.sessions_back(day, 2)[0]

        async def one(name, have, coro, show):
            if not have:
                out.append((name, "not set", ""))
                return
            try:
                out.append((name, "ok", show(await coro)))
            except Exception as e:
                out.append((name, "failed", f"{type(e).__name__}: {getattr(e, 'message', None) or e}"[:120]))
        await one("Polygon: whole-market bars", src.polygon_key, src.grouped(prev),
                  lambda r: f"{len(r or [])} tickers on {prev}")
        await one("Polygon: today's bars", src.polygon_key, src.grouped(day),
                  lambda r: f"{len(r)} tickers on {day}" if r else f"{day} not published yet (normal before ~17:00 ET)")
        await one("Finnhub: earnings calendar", src.finnhub_key, src.earnings(date.today(), date.today() + timedelta(days=7)),
                  lambda r: f"{len(r)} companies reporting this week")
        await one("Finnhub: headlines", src.finnhub_key, src.news("AAPL", date.today() - timedelta(days=5), date.today()),
                  lambda r: f"{len(r)} AAPL headlines")
        await one("Finnhub: sector", src.finnhub_key, src.profile("NVDA"), lambda r: f"NVDA -> {r.get('industry')} ({r.get('sector_etf')})")
        await one("Claude: news reader", src.anthropic_key,
                  d.http.get_json((d.s.anthropic_base_url or "https://api.anthropic.com").rstrip("/") + "/v1/models", provider="anthropic",
                                  headers={"x-api-key": src.anthropic_key, "anthropic-version": "2023-06-01"}),
                  lambda r: "key accepted")
        return out
    t = Table("Source", "Result", "Detail")
    for name, res, det in _with_http(go):
        t.add_row(name, {"ok": "[green]ok[/green]", "failed": "[red]failed[/red]"}.get(res, f"[yellow]{res}[/yellow]"), det)
    console.print(t)


@wide_app.command("backfill")
def wide_backfill(days: int = typer.Option(130, help="Sessions of whole-market bars to keep (free plan: 5 per minute).")):
    """Download the whole market's daily bars for the last DAYS sessions (only the missing ones)."""
    from abg.swing import wide as W

    async def go(d):
        return await d.wide_bars(W.last_session(), days, _progress)
    r = _with_http(go)
    console.print(f"\nfetched {r.get('fetched', 0)} sessions{'' if r['ready'] else ' (the latest session is not published yet)'}")


@wide_app.command("run")
def wide_run(day: str = typer.Option(None, help="YYYY-MM-DD (default: the last finished session)."),
             post: bool = typer.Option(None, "--post/--no-post", help="Override ABG_SWING_WIDE_POST for this run.")):
    """Run the wide scan now and show the ranked list."""
    from datetime import date

    async def go(d):
        return await d.run_wide(date.fromisoformat(day) if day else None, post, _progress)
    r = _with_http(go)
    if not r.get("ready"):
        console.print(f"[yellow]not run: {r.get('why') or r.get('busy')}[/yellow]")
        raise typer.Exit(1)
    console.print(f"\n[bold]Wide scan {r['day']}[/bold] ({r['mode']}) · {r['universe']['liquid']} liquid stocks · breadth "
                  f"{r['breadth'].get('above_50d_pct')}% above the 50-day · {r['setups']} setups · {r['taken']} taken · "
                  f"{r['new']} new · {r['posted']} posted")
    from abg.config import Settings
    from abg.swing.desk import SwingDesk
    run = SwingDesk(Settings(), None, None, fetch=False).wide_store.latest_run() or {}
    t = Table("#", "Ticker", "Score", "Tier", "20d vs SPY", "Sector", "News", "Earnings", "Status")
    for c in run.get("candidates", [])[:25]:
        f, n = c.get("features") or {}, c.get("news") or {}
        t.add_row(str(c.get("rank") or ""), c["symbol"], f"{c.get('score') or 0:.0f}", c.get("tier") or "",
                  f"{f.get('rs20'):+.1f}%" if f.get("rs20") is not None else "", (c.get("profile") or {}).get("sector_etf") or "",
                  (n.get("why") or "")[:40], c.get("earnings") or "", c.get("status") or "")
    console.print(t)


@wide_app.command("backtest")
def wide_backtest(start: str = typer.Option("2026-07-01", help="First setup date (YYYY-MM-DD)."),
                  end: str = typer.Option(None, help="Last setup date (default: the last finished session).")):
    """Replay the wide scan from START: every liquid stock, the gate, v1 exits.  First run downloads the bars
    (~6 months at 5 calls/min: about 40 minutes); later runs only add new days."""
    from datetime import date

    async def go(d):
        return await d.wide_backtest(date.fromisoformat(start), date.fromisoformat(end) if end else None, _progress)
    r = _with_http(go)
    s = r["summary"]
    console.print(f"\n[bold]Wide scan backtest {r['start']} -> {r['end']}[/bold] · {s.get('setups', 0)} setups in "
                  f"{s.get('symbols', 0)} stocks · {s.get('taken', 0)} taken by the gate")
    t = Table("Group", "Taken", "Filled", "Never filled", "Closed", "Win %", "Avg R", "Sum R")
    rows = [("all taken", s.get("all_taken")), (f"top {d_top(s)} per day by score", s.get(f"top_{d_top(s)}_per_day")),
            ("tier A", s.get("by_tier", {}).get("A")), ("tier B", s.get("by_tier", {}).get("B")),
            ("earnings in window", s.get("earnings_in_window")), ("outside the core 85", s.get("outside_core"))]
    rows += [(f"score: {k}", v) for k, v in (s.get("by_score") or {}).items() if k != "cuts"]
    for name, x in rows:
        if x and x.get("n"):
            t.add_row(name, str(x["n"]), str(x["filled"]), str(x["never_filled"]), str(x["closed"]),
                      f"{x['win_rate']}" if x["win_rate"] is not None else "—", f"{x['avg_r']:+.2f}" if x["avg_r"] is not None else "—",
                      f"{x['sum_r']:+.2f}")
    console.print(t)
    ch = r.get("channel") or {}
    console.print(f"channel ideas (for comparison): {ch.get('closed')} closed, {ch.get('win_rate')}% wins, {ch.get('sum_R'):+.2f}R")
    for n in r.get("notes", []):
        console.print(f"[dim]- {n}[/dim]")


def d_top(s: dict) -> int:
    k = [x for x in s if x.startswith("top_") and x.endswith("_per_day")]
    return int(k[0].split("_")[1]) if k else 3
