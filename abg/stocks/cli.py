"""``abg stocks ...`` - the stock desk by hand (the monitor runs it when ABG_STOCKS_ENABLED=true).

    abg stocks universe                 what is followed, and how complete the bar store is
    abg stocks universe --index sp500   load the real S&P 500 list (needs an FMP key)
    abg stocks universe --file list.txt load a symbol list from disk
    abg stocks backfill                 fill missing daily history (run this first)
    abg stocks sweep [--page 60]        one paged refresh pass
    abg stocks market [--max-calls N]   every US stock: whole-market bars from Polygon -> market tier
    abg stocks signals [--post]         today's candidates; --post sends them to Discord
    abg stocks memo ID                  print one pitch's full write-up
    abg stocks score                    mark every pitch against the benchmark
    abg stocks rules                    the active rule set
    abg stocks backtest [--years 10]    replay the books over the stored history

``signals`` never posts unless you pass --post, so it is safe to run while you are still watching it.
"""
from __future__ import annotations

import asyncio
import warnings

import typer
from rich.console import Console
from rich.table import Table

from .live import BOOK_SHORT

stocks_app = typer.Typer(help="Stock desk: the four-book rule system, run on your own bar store.",
                         no_args_is_help=True)
console = Console()


def _run(fn):
    from abg.config import Settings
    from abg.engine import AnalysisEngine
    from abg.stocks.desk import StockDesk

    async def go():
        s = Settings()
        async with AnalysisEngine(s) as eng:
            d = StockDesk(s, eng)
            try:
                return await fn(d)
            finally:
                d.store.close()
    return asyncio.run(go())


def _pct(x, d=1):
    return "-" if x is None else f"{x * 100:+.{d}f}%"


def _px(x):
    return "-" if x is None else f"{x:,.2f}"


# --------------------------------------------------------------------------- universe / data
@stocks_app.command("universe")
def universe(index: str = typer.Option(None, "--index", help="sp500 | nasdaq100 | dowjones (needs an FMP key)"),
             file: str = typer.Option(None, "--file", help="a symbol list: one per line, or CSV first column"),
             extra: str = typer.Option(None, "--extra", help="comma list of extra names to sweep"),
             missing: bool = typer.Option(False, "--missing", help="list only symbols short of history")):
    """Show or change which symbols the desk follows."""
    async def go(d):
        if index:
            out = await d.sweeper.refresh_index(index.strip().lower())
            console.print(f"[green]loaded {out['symbols']} symbols for {out['index']}[/] "
                          f"({out['sectors']} with a sector)")
        if file:
            from .universe import read_symbol_file
            syms = read_symbol_file(file)
            n = d.u.save_index(d.store, "custom", syms, source=f"file:{file}")
            console.print(f"[green]loaded {n} symbols from {file}[/]")
        if extra:
            n = d.u.save_extra(d.store, [x for x in extra.split(",")])
            console.print(f"[green]{n} extra symbol(s) added to the sweep[/]")
        # read everything off the store before _run closes it
        need = int(getattr(d.s, "stocks_min_bars", 250))
        cov = d.store.coverage()
        st_ = d.u.stored
        short = [(s, (cov[st_(s)].bars if st_(s) in cov else 0), (cov[st_(s)].error if st_(s) in cov else "no bars"))
                 for s in d.u.core() if st_(s) not in cov or cov[st_(s)].bars < need]
        return d.u.describe(), d.store.stats(), d.sweeper.status(), short, need
    u, stats, sw, short, need = _run(go)

    t = Table(title="Universe", box=None)
    for k in ("index", "source", "as_of", "stocks", "etfs", "gauges", "extra", "core", "wide"):
        t.add_row(k, str(u.get(k)))
    console.print(t)
    if u["seed"]:
        console.print("[yellow]Using the built-in seed list. It only contains names that survived to "
                      "today, so any backtest on it is survivorship-biased. "
                      "`abg stocks universe --index sp500` loads the real list.[/]")

    t = Table(title="Bar store", box=None)
    for k in ("symbols", "bars", "first_day", "last_day", "errors", "stale_symbols",
              "unadjusted_symbols", "membership_rows"):
        t.add_row(k, str(stats.get(k)))
    console.print(t)
    console.print(f"Sweep: {sw['page']} symbols every {sw['every_minutes']} min -> "
                  f"{sw['passes_per_cycle']} passes ({sw['cycle_hours']}h) for a full cycle of "
                  f"{sw['wide']} names. Last: {sw['last_sweep'] or 'never'}")

    if short:
        console.print(f"\n[yellow]{len(short)} symbol(s) short of {need} bars[/] "
                      f"- run `abg stocks backfill`")
        if missing:
            t = Table(box=None)
            t.add_column("symbol"); t.add_column("bars", justify="right"); t.add_column("last error")
            for s, n, err in short[:60]:
                t.add_row(s, str(n), (err or "")[:60])
            console.print(t)
    elif not u["seed"] or stats["symbols"]:
        console.print("\n[green]every followed symbol has enough history[/]")


@stocks_app.command("backfill")
def backfill(period: str = typer.Option(None, "--period", help="how much history, e.g. 10y"),
             symbols: str = typer.Option(None, "--symbols", help="comma list; default = everything missing"),
             all_: bool = typer.Option(False, "--all", help="refetch even symbols that already have history")):
    """Fill in daily history. Slow by design - this is the only step that needs years of bars."""
    syms = [x.strip().upper() for x in symbols.split(",")] if symbols else None

    async def go(d):
        return await d.sweeper.backfill(syms, period, only_missing=not all_)
    rep = _run(lambda d: go(d))
    r = rep.to_dict() if hasattr(rep, "to_dict") else rep
    console.print(f"[green]{r['ok']} ok[/], {r['failed']} failed, {r['skipped']} skipped - "
                  f"{r['bars']:,} bars in {r['seconds']}s")
    for e in r.get("errors", [])[:5]:
        console.print(f"  [red]{e['symbol']}[/]: {e['error']}")
    if r.get("parked"):
        console.print(f"  parked after repeated failures: {', '.join(r['parked'][:10])}")


@stocks_app.command("sweep")
def sweep(page: int = typer.Option(None, "--page", help="symbols this pass")):
    """One paged refresh pass over the wide universe."""
    r = _run(lambda d: d.sweep(page))
    console.print(f"[green]{r['ok']} ok[/], {r['failed']} failed, {r['skipped']} skipped - "
                  f"{r['bars']:,} bars in {r['seconds']}s; cursor now {r['cursor']}"
                  + (" (wrapped)" if r.get("wrapped") else ""))


@stocks_app.command("market")
def market(max_calls: int = typer.Option(None, "--max-calls", help="Polygon calls this run (5 a minute)"),
           force: bool = typer.Option(False, "--force", help="rebuild the tier even if no new session arrived")):
    """Every US common stock: sync whole-market bars from Polygon (one call per session) and rebuild
    the market tier - the liquid names outside the S&P 500 that the daily pass also scans."""
    r = _run(lambda d: d.market_refresh(max_calls=max_calls, force=force))
    if r.get("skipped") or r.get("error"):
        console.print(f"[yellow]{r.get('skipped') or r.get('error')}[/]")
        return
    sy = r.get("sync") or {}
    console.print(f"sessions stored {r.get('sessions_stored')} of {sy.get('wanted')} wanted; fetched "
                  f"{sy.get('fetched')} this run in {sy.get('seconds')}s"
                  + (f" - stopped: {sy['error']}" if sy.get("error") else ""))
    if r.get("reloaded"):
        console.print(f"[green]{r['market_tier']} liquid US common stocks[/] outside the S&P 500 in the market tier "
                      f"({r['common_stocks']} common stocks listed, {r['liquid']} pass the books' floor); "
                      f"{r['bars_loaded']:,} bars loaded")
    else:
        console.print(f"no new session - the market tier stays at {r.get('market_tier')} names")


# --------------------------------------------------------------------------- signals
@stocks_app.command("signals")
def signals(post: bool = typer.Option(False, "--post", help="send the pitches to Discord"),
            enrich: int = typer.Option(None, "--enrich", help="how many names get fundamentals + news"),
            full: str = typer.Option(None, "--full", help="also print the whole write-up for this symbol")):
    """Today's candidates from every enabled book, with the thesis for each."""
    async def go(d):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out = await d.daily(post=post, enrich=enrich)
        return out, d.today(), d

    out, today, d = _run(go)
    st = today.get("state") or {}
    console.print(f"\n[bold]{out['as_of']}[/] · regime [bold]{out['regime']}[/] · "
                  f"stress {out['stress']:.2f}" + ("  [red]CRISIS MODE[/]" if out["crisis"] else ""))
    if st.get("weights"):
        console.print("capital by book: " + ", ".join(f"{k} {_pct(v, 0).lstrip('+')}"
                                                      for k, v in st["weights"].items()))
    cands = today.get("candidates") or []
    if not cands:
        console.print("\n[yellow]Nothing to do.[/] No book's entry conditions were met and nothing "
                      "pitched hit an exit. Standing pat is the signal.")
    else:
        t = Table(box=None)
        for c, j in (("action", "left"), ("symbol", "left"), ("book", "left"), ("close", "right"),
                     ("stop", "right"), ("size", "right"), ("rank", "right"), ("risk", "right")):
            t.add_column(c, justify=j)
        for c in cands:
            t.add_row(c["action"], c["symbol"], BOOK_SHORT.get(c["book"], c["book"]),
                      _px(c.get("price")), _px(c.get("stop")),
                      f"{c['shares']:,.0f}" if c.get("shares") else "-",
                      f"{c['rank']}/{c['of']}" if c.get("rank") else "-",
                      _pct(c.get("risk_pct"), 2) if c.get("risk_pct") else "-")
        console.print(t)
        console.print(f"\nwrote {out['candidates']} ledger row(s); "
                      f"{out['opens']} to open, {out['closes']} to close; posted: {out['posted']}")
        for c in cands:
            if c["action"] in ("BUY", "REBALANCE"):
                console.print(f"\n[bold]{c['symbol']}[/] — {c['what_happened']}")
                break
    if full:
        sym = full.strip().upper()
        hit = next((c for c in cands if c["symbol"] == sym), None)
        if not hit:
            console.print(f"[yellow]{sym} is not in today's candidates[/]")
        else:
            md = d.memo(hit["id"])
            console.print("\n" + (md or "(no write-up: Book A rebalances get a digest line only)"))


@stocks_app.command("memo")
def memo(pitch_id: str = typer.Argument(..., help="e.g. 2026-11-07-trend_pullback-AAPL")):
    """Print one pitch's full write-up."""
    md = _run(lambda d: _ident(d.memo(pitch_id)))
    if md is None:
        console.print(f"[red]no write-up for {pitch_id}[/]")
        raise typer.Exit(1)
    console.print(md)


async def _ident(x):
    return x


@stocks_app.command("score")
def score():
    """Mark every pitch against the benchmark: the desk's track record."""
    sc = _run(lambda d: _ident(d.scorecard()))
    a = sc["all"]
    console.print(f"\n[bold]{a['n']}[/] pitch(es) marked · avg return {_pct(a.get('avg_ret'))} · "
                  f"avg vs benchmark {_pct(a.get('avg_excess'))} · "
                  f"beat it {_pct(a.get('beat_spy'), 0).lstrip('+') if a.get('beat_spy') is not None else '-'} "
                  f"of the time")
    if sc["by_book"]:
        t = Table(title="By book", box=None)
        for c in ("book", "n", "avg return", "vs benchmark", "beat it"):
            t.add_column(c, justify="right" if c != "book" else "left")
        for b, v in sc["by_book"].items():
            t.add_row(b, str(v["n"]), _pct(v.get("avg_ret")), _pct(v.get("avg_excess")),
                      _pct(v.get("beat_spy"), 0).lstrip("+") if v.get("beat_spy") is not None else "-")
        console.print(t)
    t = Table(title="Latest", box=None)
    for c in ("date", "symbol", "book", "entry", "last", "return", "vs bench"):
        t.add_column(c, justify="right" if c not in ("symbol", "book") else "left")
    for r in sc["pitches"][:20]:
        t.add_row(r["as_of"], r["symbol"], r["book"], _px(r.get("price")), _px(r.get("last")),
                  _pct(r.get("ret")), _pct(r.get("excess")))
    console.print(t)


@stocks_app.command("rules")
def rules():
    """The active rule set, from configs/stocks.yaml plus any .env overrides."""
    from .config import config_path, describe
    cfg, path = _run(lambda d: _ident((d.cfg, config_path(d.s))))
    r = describe(cfg)
    console.print(f"\nrule file: [bold]{path}[/]")
    t = Table(box=None)
    for k, v in r.items():
        t.add_row(k, str(v))
    console.print(t)
    t = Table(title="Books", box=None)
    for c in ("book", "on", "regimes", "key parameters"):
        t.add_column(c)
    for name, p in (cfg.get("books") or {}).items():
        keys = [k for k in ("top_k", "top_n", "rsi_pullback", "rsi_entry", "rsi_exit", "stop_atr",
                            "trail_atr", "max_hold", "risk_per_trade", "max_positions", "budget")
                if k in p]
        t.add_row(name, "yes" if p.get("enabled", True) else "no",
                  ", ".join(f"{k}:{v:g}" for k, v in (p.get("regimes") or {}).items()),
                  ", ".join(f"{k}={p[k]:g}" if isinstance(p[k], (int, float)) else f"{k}={p[k]}"
                            for k in keys))
    console.print(t)


@stocks_app.command("backtest")
def backtest(years: int = typer.Option(10, "--years"),
             out: str = typer.Option(None, "--out", help="write summary.md and the CSVs here")):
    """Replay the books over the stored history. In-sample unless you walk it forward - read docs/21."""
    import pandas as pd
    from .live import LiveSignals

    def go(d):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ls = LiveSignals(d.cfg, d.store, d.u).build(warn=False)
            end = pd.Timestamp(d.store.stats()["last_day"])
            res = ls.engine.run(start=str((end - pd.DateOffset(years=years)).date()))
        return res, d.cfg

    res, cfg = _run(lambda x: _ident(go(x)))
    from abg.tradebook.stocks.report import summarize
    s = summarize(res, out, cfg.get("events_file")) if out else None
    eq = res["equity"]
    tr = res["trades"]
    n = len(eq)
    total = eq["equity"].iloc[-1] / eq["equity"].iloc[0] - 1
    bench = eq["benchmark"].iloc[-1] / eq["benchmark"].iloc[0] - 1
    console.print(f"\n{eq.index[0].date()} -> {eq.index[-1].date()} ({n} sessions)")
    console.print(f"strategy {_pct(total)} · benchmark {_pct(bench)} · "
                  f"max drawdown {_pct(eq['drawdown'].min())}")
    if len(tr):
        closed = tr.dropna(subset=["R"])
        console.print(f"{len(tr)} swing trade(s)" + (
            f" · avg {closed['R'].mean():+.3f}R · win rate "
            f"{(closed['R'] > 0).mean() * 100:.0f}% · total {closed['R'].sum():+.1f}R" if len(closed) else ""))
    console.print("[yellow]These parameters were chosen on history that includes this window, so read "
                  "this as in-sample. docs/21 has the walk-forward protocol.[/]")
    if s:
        console.print(f"wrote {out}/summary.md")
