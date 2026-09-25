"""Command-line interface:  ``abg <command> ...``  (run ``abg --help``)."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Awaitable, Callable, Optional, TypeVar

import typer
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.table import Table

from . import __version__
from .config import Settings
from .engine import AnalysisEngine, AnalyzeOptions
from .errors import ABGError
from .render import console, num, render_chain, render_compare, render_providers, render_report, signed

T = TypeVar("T")
app = typer.Typer(add_completion=False, no_args_is_help=True, rich_markup_mode="rich",
                  help="[bold cyan]ABG Intelligence Terminal[/bold cyan] v3 — multi-source stock analysis "
                       "(AI Business Group).")
cache_app = typer.Typer(help="Inspect or clear the local data cache.")
app.add_typer(cache_app, name="cache")

_state: dict = {"overrides": {}}
err_console = Console(stderr=True)   # spinners/logs never pollute --json stdout


@app.callback()
def main(demo: bool = typer.Option(False, "--demo", help="Allow simulated data when no real provider answers."),
         no_cache: bool = typer.Option(False, "--no-cache", help="Bypass the cache for this run."),
         csv_dir: Optional[Path] = typer.Option(None, "--csv-dir", help="Folder of <SYMBOL>.csv files (csv provider)."),
         providers: Optional[str] = typer.Option(None, "--providers", help="Priority list, e.g. 'polygon,yahoo,stooq'."),
         log_level: str = typer.Option("WARNING", "--log-level", help="DEBUG, INFO, WARNING, ERROR.")):
    ov = {"log_level": log_level}
    if demo:
        ov["allow_synthetic"] = True
    if no_cache:
        ov["cache_enabled"] = False
    if csv_dir:
        ov["csv_dir"] = csv_dir
    if providers:
        ov["provider_order"] = providers
    _state["overrides"] = ov
    logging.basicConfig(level=log_level.upper(), handlers=[RichHandler(console=err_console, show_path=False)],
                        format="%(message)s")


def _run(fn: Callable[[AnalysisEngine], Awaitable[T]]) -> T:
    """Create an engine, run ``fn`` and always close it; convert errors to clean exits."""
    async def go():
        async with AnalysisEngine(Settings(**_state["overrides"])) as eng:
            return await fn(eng)
    try:
        return asyncio.run(go())
    except ABGError as e:
        console.print(f"[bold red]Error:[/bold red] {e.message}")
        if getattr(e, "errors", None):
            for a in e.errors:
                console.print(f"  [dim]- {a['provider']}: {a['error']}[/dim]")
        console.print("[dim]Tip: `abg providers` shows which sources are configured; `--demo` enables simulated data.[/dim]")
        raise typer.Exit(2)
    except KeyboardInterrupt:
        raise typer.Exit(130)


def _dump(obj, save: Optional[Path]) -> None:
    text = json.dumps(obj, indent=2, default=str)
    if save:
        save.write_text(text)
        console.print(f"[green]saved {save}[/green]")
    else:
        sys.stdout.write(text + "\n")


# --------------------------------------------------------------------------- analyze
@app.command()
def analyze(symbol: str = typer.Argument(..., help="Ticker, e.g. AAPL"),
            period: str = typer.Option("1y", "-p", "--period", help="1mo 3mo 6mo ytd 1y 2y 5y 10y max"),
            interval: str = typer.Option("1d", "-i", "--interval", help="1d 1wk 1mo 1h 30m 15m 5m"),
            source: Optional[str] = typer.Option(None, "-s", "--source", help="Force a provider for price data."),
            ai: bool = typer.Option(True, "--ai/--no-ai", help="Claude insight (needs ANTHROPIC_API_KEY)."),
            news: bool = typer.Option(True, "--news/--no-news"),
            options: bool = typer.Option(True, "--options/--no-options"),
            expiry: Optional[str] = typer.Option(None, help="Option expiry YYYY-MM-DD."),
            account: Optional[float] = typer.Option(None, help="Account size for position sizing on the top setup."),
            risk_pct: float = typer.Option(1.0, help="% of account risked per trade (with --account)."),
            as_json: bool = typer.Option(False, "--json", help="Print the raw JSON report."),
            save: Optional[Path] = typer.Option(None, "--save", help="Write JSON report to this file.")):
    """Full analysis: technicals, setups, stats, sentiment, options/Greeks, risk and AI insight."""
    async def go(eng: AnalysisEngine):
        o = AnalyzeOptions(period=period, interval=interval, source=source, ai=ai, news=news, options=options,
                           expiry=date.fromisoformat(expiry) if expiry else None)
        with err_console.status(f"Analysing {symbol.upper()}…", spinner="dots"):
            r = await eng.analyze(symbol, o)
        if account:
            from .risk.baseline import position_size
            top = next((p for p in r["plays"] if p.get("levels")), None)
            if top and r.get("risk"):
                r["risk"][0].setdefault("metrics", {})["position_sizing"] = position_size(
                    account, risk_pct, top["levels"]["entry"], top["levels"]["stop"])
        return r
    r = _run(go)
    if as_json or save:
        _dump(r, save)
        if as_json:
            return
    render_report(r)
    ps = ((r.get("risk") or [{}])[0].get("metrics") or {}).get("position_sizing")
    if account and not ps:
        console.print("[dim]Position sizing: no directional setup with entry/stop levels right now.[/dim]")
    if ps:
        console.print(f"[bold]Position sizing[/bold] (risking {risk_pct}% of {num(account, 0)}): {ps['shares']} shares · "
                      f"value {num(ps['position_value'])} ({num(ps['position_pct_of_account'], 1)}% of account) · "
                      f"max loss at stop {num(ps['risk_amount'])}")


@app.command()
def predict(symbol: str = typer.Argument(..., help="Ticker, e.g. AAPL"),
            horizon: int = typer.Option(63, "-h", "--horizon", help="Primary horizon in trading days (21=1m, 63=3m, 126=6m, 252=1y)."),
            paths: int = typer.Option(5000, help="Simulated paths (more = smoother, slower)."),
            source: Optional[str] = typer.Option(None, "-s", "--source"),
            as_json: bool = typer.Option(False, "--json")):
    """Monte Carlo prediction: simulated future returns, scenarios, thesis, confidence and recommendation."""
    async def go(eng: AnalysisEngine):
        with err_console.status(f"Simulating {paths:,} paths for {symbol.upper()}…", spinner="dots"):
            return await eng.analyze(symbol, AnalyzeOptions(period="2y", source=source, ai=False, options=False,
                                                            forecast_horizon=horizon, forecast_paths=paths))
    r = _run(go)
    if as_json:
        _dump({"symbol": r["symbol"], "forecast": {k: v for k, v in r["forecast"].items() if k != "fan"}}, None)
        return
    q = r["quote"]
    console.print(f"[bold cyan]{r['symbol']}[/bold cyan] {r.get('name') or ''} · {num(q.get('price'))} "
                  f"({q.get('change_pct') or 0:+.2f}%) · signal {r['signal']['label']} ({r['signal']['score']:+.0f}) · "
                  f"risk {(r.get('risk') or [{}])[0].get('level', 'n/a')}")
    from .render import render_forecast
    render_forecast(r["forecast"])
    for w in r.get("warnings") or []:
        console.print(f"[yellow]⚠ {w}[/yellow]")


@app.command()
def compare(symbols: list[str] = typer.Argument(..., help="Two or more tickers"),
            period: str = typer.Option("1y", "-p", "--period"),
            as_json: bool = typer.Option(False, "--json")):
    """Side-by-side comparison with correlation matrix and equal-weight portfolio risk."""
    res = _run(lambda eng: eng.compare(symbols, period))
    if as_json:
        _dump({k: v for k, v in res.items() if k != "reports"}, None)
    else:
        render_compare(res)


@app.command()
def quote(symbols: list[str] = typer.Argument(...), source: Optional[str] = typer.Option(None, "-s", "--source")):
    """Latest quote(s)."""
    async def go(eng):
        return await asyncio.gather(*(eng.quote(s, source) for s in symbols), return_exceptions=True)
    res = _run(go)
    t = Table(box=None)
    for c in ("Symbol", "Price", "Change", "%", "Volume", "Source"):
        t.add_column(c)
    for s, f in zip(symbols, res):
        if isinstance(f, Exception):
            t.add_row(s.upper(), f"[red]{getattr(f, 'message', f)}[/red]")
        else:
            q = f.value
            t.add_row(q.symbol, num(q.price), signed(q.change), signed(q.change_pct, 2, "%"), num(q.volume, 0),
                      f"{f.provenance.provider} ({f.provenance.cache})")
    console.print(t)


@app.command()
def watch(symbols: list[str] = typer.Argument(...), every: float = typer.Option(15.0, help="Refresh seconds.")):
    """Live-updating quote board (Ctrl+C to exit)."""
    async def go(eng: AnalysisEngine):
        def table(rows):
            t = Table(title="ABG watch", box=None)
            for c in ("Symbol", "Price", "Change", "%", "Source", "Latency"):
                t.add_column(c)
            for s, f in rows:
                if isinstance(f, Exception):
                    t.add_row(s, f"[red]{getattr(f, 'message', f)[:60]}[/red]")
                else:
                    t.add_row(f.value.symbol, num(f.value.price), signed(f.value.change), signed(f.value.change_pct, 2, "%"),
                              f.provenance.provider, f"{f.provenance.latency_ms:.0f}ms")
            return t
        with Live(console=console, refresh_per_second=2) as live:
            while True:
                res = await asyncio.gather(*(eng.quote(s, use_cache=False) for s in symbols), return_exceptions=True)
                live.update(table(list(zip([s.upper() for s in symbols], res))))
                await asyncio.sleep(every)
    _run(go)


@app.command("options")
def options_cmd(symbol: str, expiry: Optional[str] = typer.Option(None, help="YYYY-MM-DD"),
                strikes: int = typer.Option(8, help="Strikes each side of ATM.")):
    """Option chain with IV and full Greeks (model chain if no market data)."""
    r = _run(lambda eng: eng.analyze(symbol, AnalyzeOptions(ai=False, news=False, fundamentals=False, benchmark=False,
                                                              risk=False, expiry=date.fromisoformat(expiry) if expiry else None)))
    o = r.get("options") or {}
    if not o.get("available"):
        console.print("[red]No options data.[/red]")
        raise typer.Exit(1)
    s = o["summary"]
    console.print(f"[bold]{r['symbol']}[/bold] spot {num(o['underlying_price'])} · expiry {str(o['selected_expiry'])[:10]} "
                  f"({o['days_to_expiry']}d) · ATM IV {num((s.get('atm_iv') or 0) * 100, 1)}% · "
                  f"expected move ±{num(s.get('expected_move'))} · source {o['source']}"
                  + (" [yellow](MODEL)[/yellow]" if o.get("model_generated") else ""))
    console.print("[dim]expirations: " + ", ".join(str(e)[:10] for e in o["expirations"][:12]) + "[/dim]")
    render_chain(o, rows=strikes)


@app.command()
def news(symbol: str, limit: int = typer.Option(15)):
    """Latest headlines with sentiment scores."""
    from .analysis.sentiment import analyze_news

    async def go(eng):
        f = await eng.news(symbol, limit)
        return f, analyze_news(f.value)
    f, agg = _run(go)
    console.print(f"[bold]{symbol.upper()}[/bold] sentiment {agg['label']} ({num(agg['score'])}) · "
                  f"{agg['articles']} articles · model {agg['model']} · source {f.provenance.provider}")
    for n in f.value:
        console.print(signed(n.sentiment), f" {n.title} [dim]— {n.publisher or ''} {str(n.published_at or '')[:16]}[/dim]")


@app.command()
def bs(spot: float = typer.Option(..., help="Underlying price"), strike: float = typer.Option(...),
       days: float = typer.Option(..., help="Calendar days to expiry"), vol: float = typer.Option(..., help="IV %, e.g. 30"),
       rate: float = typer.Option(4.0, help="Risk-free %"), div: float = typer.Option(0.0, help="Dividend yield %"),
       price: Optional[float] = typer.Option(None, help="If given, solve implied vol from this option price"),
       kind: str = typer.Option("call", help="call | put")):
    """Black-Scholes calculator: price + Greeks, or implied vol from a price."""
    from .analysis import options as opt
    T, r, q = days / 365, rate / 100, div / 100
    sigma = vol / 100
    if price is not None:
        sigma = float(opt.implied_vol(price, spot, strike, T, r, q, kind))
        console.print(f"Implied vol: [bold]{sigma * 100:.3f}%[/bold]")
    p = float(opt.bs_price(spot, strike, T, r, sigma, q, kind))
    g = {k: float(v) for k, v in opt.greeks(spot, strike, T, r, sigma, q, kind).items()}
    t = Table(title=f"{kind.upper()} K={strike} T={days:g}d σ={sigma * 100:.2f}%", box=None)
    for c in ("Price", "Delta", "Gamma", "Theta/day", "Vega/1pt", "Rho/1%", "P(ITM)"):
        t.add_column(c, justify="right")
    t.add_row(num(p, 4), num(g["delta"], 4), num(g["gamma"], 5), num(g["theta"], 4), num(g["vega"], 4),
              num(g["rho"], 4), f"{g['prob_itm']:.1%}")
    console.print(t)


# --------------------------------------------------------------------------- providers / risk / features
@app.command()
def providers(probe: Optional[str] = typer.Option(None, help="Live-test every configured provider with this ticker."),
              capability: str = typer.Option("quote", help="quote | history | news | options | fundamentals")):
    """Show provider configuration, circuit-breaker state and health (optionally probe them)."""
    from .models import Capability

    async def go(eng: AnalysisEngine):
        pr = await eng.router.probe(probe.upper(), Capability(capability)) if probe else None
        return eng.status(), pr
    status, pr = _run(go)
    render_providers(status["providers"])
    if pr:
        t = Table(title=f"Probe: {capability} {probe.upper()}", box=None)
        for c in ("Provider", "OK", "Latency", "Detail"):
            t.add_column(c)
        for x in sorted(pr, key=lambda x: (not x["ok"], x["latency_ms"])):
            t.add_row(x["provider"], "[green]✓[/green]" if x["ok"] else "[red]✗[/red]", f"{x['latency_ms']:.0f}ms", x["detail"])
        console.print(t)
    ai = status["ai"]
    console.print(f"[dim]AI insight: {'configured' if ai['configured'] else 'not configured (set ANTHROPIC_API_KEY)'} · "
                  f"model {ai['model']} · risk models: {', '.join(m['name'] for m in status['risk_models'])} · "
                  f"feature schema v{status['feature_schema']}[/dim]")


@app.command()
def features(symbols: list[str] = typer.Argument(...), period: str = typer.Option("5y", "-p", "--period"),
             labels: bool = typer.Option(False, "--labels", help="Add forward-looking y_* training targets."),
             out: Path = typer.Option(Path("features.csv"), "-o", "--out", help=".csv or .parquet")):
    """Export the risk-model feature matrix (schema-versioned) for training / research."""
    import pandas as pd

    async def go(eng: AnalysisEngine):
        frames = []
        for s in symbols:
            try:
                frames.append(await eng.feature_frame(s, period, labels))
            except ABGError as e:
                console.print(f"[yellow]{s}: {e.message}[/yellow]")
        return frames
    frames = _run(go)
    if not frames:
        raise typer.Exit(1)
    df = pd.concat(frames)
    from .risk.features import FEATURE_SCHEMA_VERSION
    if out.suffix == ".parquet":
        df.to_parquet(out)
    else:
        df.to_csv(out, index_label="date")
    out.with_suffix(".schema.json").write_text(json.dumps({"schema_version": FEATURE_SCHEMA_VERSION,
                                                           "columns": list(df.columns)}, indent=2))
    console.print(f"[green]wrote {len(df):,} rows × {df.shape[1]} cols → {out}[/green] (schema v{FEATURE_SCHEMA_VERSION})")


@app.command()
def schema():
    """Print the risk feature schema."""
    from .risk.features import FEATURES, FEATURE_SCHEMA_VERSION
    t = Table(title=f"Feature schema v{FEATURE_SCHEMA_VERSION}", box=None)
    for c in ("Name", "Group", "Unit", "Hist.", "Description"):
        t.add_column(c)
    for f in FEATURES:
        t.add_row(f.name, f.group, f.unit, "✓" if f.timeseries else "", f.description)
    console.print(t)


@app.command("analyze-csv")
def analyze_csv(path: Path, symbol: Optional[str] = typer.Option(None, help="Label (defaults to file name)."),
                period: str = typer.Option("max", "-p", "--period"), as_json: bool = typer.Option(False, "--json")):
    """Analyse a local CSV (MacroTrends / Yahoo / Nasdaq / Investing.com / generic formats auto-detected)."""
    sym = (symbol or path.stem.split("_")[0].split(" ")[0]).upper()[:15]
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    r = _run(lambda eng: eng.analyze_csv(text, sym, AnalyzeOptions(period=period, news=False, options=False,
                                                                   fundamentals=False, benchmark=False, ai=False)))
    _dump(r, None) if as_json else render_report(r, show_chain=False)


@app.command()
def serve(host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8000), reload: bool = False,
          monitor: bool = typer.Option(True, "--monitor/--no-monitor", help="Also run the live portfolio monitor.")):
    """Start the REST API + web dashboard (http://127.0.0.1:8000)."""
    import os

    import uvicorn
    for k, v in _state["overrides"].items():
        os.environ[f"ABG_{k.upper()}"] = str(v)
    os.environ["ABG_MONITOR_ON_SERVE"] = "true" if monitor else "false"
    console.print(f"[bold cyan]ABG Intelligence Terminal[/bold cyan] dashboard → http://{host}:{port}   (API docs: /docs)")
    uvicorn.run("abg.api.server:app", host=host, port=port, reload=reload, log_level="warning")


@cache_app.command("stats")
def cache_stats():
    console.print_json(data=_run(lambda eng: asyncio.sleep(0, eng.cache.stats())))


@cache_app.command("clear")
def cache_clear(prefix: str = typer.Argument("", help="Optional key prefix, e.g. 'history:AAPL'")):
    n = _run(lambda eng: asyncio.sleep(0, eng.cache.clear(prefix)))
    console.print(f"cleared {n} entries")


@app.command()
def version():
    """Print version."""
    console.print(f"abg-terminal {__version__}")


from . import cli_portfolio  # noqa: E402,F401  (registers portfolio / alert / signals / monitor commands)
from . import cli_ext  # noqa: E402,F401  (registers `abg ext ...` external-signal commands)

if __name__ == "__main__":  # pragma: no cover
    app()
