"""``abg pitch ...`` - the pitch desk by hand (the monitor runs the weekly batch when ABG_PITCH_ENABLED=true).

    abg pitch screen [--top 15]     rank the universe on the factor screen
    abg pitch make SYMBOL [--ai]    write a memo (saved to <data_dir>/pitch/memos; never posts)
    abg pitch list                  every pitch with its scorecard vs SPY
    abg pitch score                 mark every pitch against SPY now
"""
from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

pitch_app = typer.Typer(help="Pitch desk: factor screen, valuation, memos with sources, scorecard vs SPY.",
                        no_args_is_help=True)
console = Console()


def _run(fn):
    from abg.config import Settings
    from abg.engine import AnalysisEngine
    from abg.pitch.desk import PitchDesk

    async def go():
        s = Settings()
        async with AnalysisEngine(s) as eng:
            return await fn(PitchDesk(s, eng))
    return asyncio.run(go())


def _pct(x):
    return "-" if x is None else f"{x * 100:+.1f}%"


@pitch_app.command("screen")
def screen(top: int = typer.Option(15, "--top")):
    """Rank the universe on momentum, value, quality, growth and low volatility."""
    r = _run(lambda d: d.screen())
    t = Table(title=f"Pitch screen - {r['n']} names ({len(r['missing'])} without data)")
    for c in ("#", "symbol", "name", "composite", "momentum", "earn. yield", "margin", "growth"):
        t.add_column(c)
    for row in r["rows"][:top]:
        raw = row["raw"]
        t.add_row(str(row["rank"]), row["symbol"], (row.get("name") or "")[:28],
                  "-" if row["composite"] is None else f"{row['composite']:+.2f}", _pct(raw.get("momentum")),
                  _pct(raw.get("value")), _pct(raw.get("quality")), _pct(raw.get("growth")))
    console.print(t)


@pitch_app.command("make")
def make(symbol: str, ai: bool = typer.Option(False, "--ai/--no-ai", help="Claude write-up (API key; tokens).")):
    """Write a memo for SYMBOL (saved; never posted from the CLI)."""
    r = _run(lambda d: d.make(symbol, post=False, ai=ai))
    console.print(r["memo"])


@pitch_app.command("list")
def list_():
    """Every pitch with its scorecard vs SPY."""
    from abg.config import Settings
    from abg.pitch.desk import PitchDesk
    sc = PitchDesk(Settings(), None).scorecard()
    t = Table(title="Pitches")
    for c in ("id", "verdict", "price", "fair value", "return", "vs SPY"):
        t.add_column(c)
    for p in sc["pitches"]:
        t.add_row(p["id"], p["verdict"], f"{p['price']:.2f}", "-" if not p["fair_value"] else f"{p['fair_value']:.2f}",
                  _pct(p["return"]), _pct(p["excess"]))
    console.print(t)


@pitch_app.command("score")
def score():
    """Mark every pitch against SPY now."""
    sc = _run(lambda d: d.score())
    a = sc["all"]
    beat = "-" if a["beat_spy"] is None else f"{a['beat_spy']:.0%}"
    console.print(f"{a['n']} pitches, {a['marked']} marked, avg vs SPY {_pct(a['avg_excess'])}, beat SPY {beat}")
