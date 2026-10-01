"""CLI: ``abg alpha ...`` - the MNQ signal bot's backtests, research and status (docs/14)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.table import Table

from .cli import _run, _state, app, console
from .config import Settings

alpha_app = typer.Typer(help="MNQ signal bot: backtests, walk-forward, journal analysis, status.", no_args_is_help=True)
app.add_typer(alpha_app, name="alpha")


def _settings() -> Settings:
    return Settings(**_state["overrides"])


def _stats_table(title: str, st: dict) -> Table:
    t = Table(title=title, box=box.SIMPLE, title_justify="left")
    t.add_column("Metric")
    t.add_column("Value", justify="right")
    if not st or not st.get("trades"):
        t.add_row("trades", "0")
        return t
    rows = [("trades / days traded", f"{st['trades']} / {st['days_traded']} of {st['days']}"),
            ("win rate (W/L/BE)", f"{st['win_rate']:.0%} ({st['wins']}/{st['losses']}/{st['breakeven']})"),
            ("net points / avg per trade", f"{st['net_pts']:+,.1f} / {st['avg_pts']:+.1f}"),
            ("avg R / net R", f"{st['avg_r']:+.2f} / {st['net_r']:+.1f}"),
            ("profit factor", f"{st['profit_factor']}" if st.get("profit_factor") else "n/a"),
            ("max drawdown (pts)", f"{st['max_dd_pts']:,.1f}"),
            ("net $ per micro after costs", f"{st['net_usd_per_micro']:+,.0f}"),
            ("max drawdown $ per micro", f"{st['max_dd_usd_per_micro']:,.0f}"),
            ("worst / best day $", f"{st['worst_day_usd']:+,.0f} / {st['best_day_usd']:+,.0f}"),
            ("Sharpe (daily, annualised)", f"{st['sharpe_daily']}" if st.get("sharpe_daily") is not None else "n/a")]
    for a, b in rows:
        t.add_row(a, b)
    return t


def _groups(st: dict) -> None:
    for key, label in (("by_setup", "setup"), ("by_time", "entry time"), ("by_regime", "regime"), ("by_exit", "exit")):
        g = st.get(key) or {}
        if not g:
            continue
        t = Table(title=f"by {label}", box=box.SIMPLE, title_justify="left")
        for c in (label, "n", "win", "net pts", "avg"):
            t.add_column(c, justify="right" if c != label else "left")
        for k, v in g.items():
            t.add_row(k, str(v["n"]), f"{v['win_rate']:.0%}", f"{v['net_pts']:+.1f}", f"{v['avg_pts']:+.1f}")
        console.print(t)


@alpha_app.command("backtest")
def backtest_cmd(csv: Optional[Path] = typer.Option(None, help="Intraday bars CSV (TradingView / NinjaTrader / generic)."),
                 tz: Optional[str] = typer.Option(None, help="Timezone of naive timestamps in the CSV (default ET)."),
                 symbol: str = typer.Option("MNQ=F", help="Download bars for this symbol when no CSV is given."),
                 interval: str = typer.Option("5m", help="1m (last ~7 days) or 5m (last ~60 days) when downloading."),
                 period: str = typer.Option("60d"),
                 walk: bool = typer.Option(False, "--walk-forward", help="Pick settings on 2/3 of the days, test on the rest."),
                 save: bool = typer.Option(False, help="Save walk-forward settings for the live bot (only if they beat the defaults)."),
                 seed_learner: bool = typer.Option(False, help="Seed the live bot's adaptive book from this backtest."),
                 out: Optional[Path] = typer.Option(None, "--json", help="Write the full result to this file."),
                 trades: bool = typer.Option(False, help="List every trade.")):
    """Backtest the MNQ strategy on intraday bars (the same code the live bot runs)."""
    from .alpha.backtest import run_backtest, walk_forward
    from .alpha.context import calendar_events
    from .alpha.data import fetch_intraday, load_intraday_csv
    from .alpha.learn import AdaptiveBook
    from .alpha.strategy import AlphaParams

    s = _settings()
    if csv:
        bars = load_intraday_csv(csv, tz)
    else:
        bars = _run(lambda eng: fetch_intraday(eng, symbol, interval, period))
    console.print(f"[dim]{len(bars):,} bars · {bars.index[0]:%Y-%m-%d %H:%M} → {bars.index[-1]:%Y-%m-%d %H:%M} ET[/dim]")
    ev = lambda d: calendar_events(d, s)  # noqa: E731
    params_path = Path(s.data_dir).expanduser() / "alpha_params.json"
    base = AlphaParams()
    book = AdaptiveBook()
    res = run_backtest(bars, base, learner=book, events_for=ev, commission_rt=s.alpha_commission_rt)
    console.print(_stats_table("MNQ strategy - default settings, adaptive filter on", res.stats))
    _groups(res.stats)
    full = {"backtest": res.to_dict()}
    if walk:
        wf = walk_forward(bars, base, events_for=ev, commission_rt=s.alpha_commission_rt, min_win_rate=s.alpha_min_win_rate,
                          min_trades=10)
        console.print(f"\n[bold]Walk-forward[/bold]: fit on {wf['train_days'][0]}…{wf['train_days'][1]} ({wf['train_days'][2]} days), "
                      f"test on {wf['test_days'][0]}…{wf['test_days'][1]} ({wf['test_days'][2]} days)")
        console.print(f"best settings on the fit window: {wf['best']}")
        console.print(_stats_table("out-of-sample with the fitted settings", wf["test_stats"]))
        console.print(_stats_table("out-of-sample with the defaults", wf["default_test_stats"]))
        full["walk_forward"] = wf
        ts, ds = wf["test_stats"], wf["default_test_stats"]
        if save:
            from .alpha.backtest import worth_adopting
            if worth_adopting(ts, ds):
                params_path.parent.mkdir(parents=True, exist_ok=True)
                params_path.write_text(json.dumps({"params": wf["params"], "fitted": str(date.today()),
                                                   "test_stats": {k: ts.get(k) for k in ("trades", "avg_r", "net_pts")}}, indent=1))
                console.print(f"[green]saved → {params_path}[/green]")
            else:
                console.print("[yellow]not saved: out of sample the fitted settings need 15+ trades, a positive result after "
                              "costs, and to beat the defaults[/yellow]")
    if seed_learner:
        path = Path(s.data_dir).expanduser() / "alpha_learner.json"
        book.save(path)
        console.print(f"[green]adaptive book seeded from {res.stats.get('trades', 0)} backtest trades → {path}[/green]")
    if trades:
        t = Table(box=box.SIMPLE)
        for c in ("date", "time", "side", "setup", "entry", "stop", "T1", "final", "exit", "reason", "pts", "R"):
            t.add_column(c)
        for x in res.trades:
            t.add_row(x["date"], x["opened"][11:16], x["side_label"], x["setup"], f"{x['entry']:,.2f}", f"{x['stop0']:,.2f}",
                      f"{x['t1']:,.2f}", f"{x['final']:,.2f}", f"{x['exit']:,.2f}", x["exit_reason"], f"{x['pts']:+.2f}", f"{x['r']:+.2f}")
        console.print(t)
    target = out or Path(s.data_dir).expanduser() / "alpha_backtest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    full["bars"] = {"n": len(bars), "start": str(bars.index[0]), "end": str(bars.index[-1]), "source": str(csv or f"{symbol} {interval}")}
    target.write_text(json.dumps(full, default=str))
    console.print(f"[dim]full result → {target}[/dim]")


@alpha_app.command("daily")
def daily_cmd(path: Path, offset: Optional[float] = typer.Option(None, help="Manual roll offset in points."),
              carry: float = typer.Option(0.033, help="Annual carry used to estimate the roll offset."),
              journal: Optional[Path] = typer.Option(None, help="Another service's journal (DiscordKit JSON or CSV) to line up by day.")):
    """What a daily CSV can tell you: roll fixes, ranges, and whether the day-bias call works."""
    import numpy as np
    from .alpha.backtest import daily_layer
    from .alpha.data import back_adjust, load_daily_csv
    raw = load_daily_csv(path)
    df, rolls = back_adjust(raw, carry, offset)
    console.print(f"{len(raw)} daily bars {raw.index[0].date()} → {raw.index[-1].date()}")
    for r in rolls:
        console.print(f"[yellow]roll {r.expiry}: dropped stale rows {', '.join(r.stale)}; earlier prices +{r.offset:g} pts ({r.method})[/yellow]")
    rep = daily_layer(df)
    console.print(f"days tested {rep['days']} · trend call made on {rep['bias_calls']} · right {rep['bias_hit_rate']:.0%} · "
                  f"'same as yesterday' right {rep['follow_yesterday_hit_rate']:.0%} · up days {rep['base_rate_up']:.0%}")
    console.print(f"average daily range {rep['avg_range_pts']} pts · average ATR {rep['avg_atr_pts']} pts")
    if journal:
        import pandas as pd
        from .alpha.journal import load_trades
        tr, flat = load_trades(journal)
        daily = tr.groupby("date")["pts"].sum()
        for d in flat:
            daily.loc[d] = 0.0
        rows = pd.DataFrame(rep["rows"]).set_index("date")
        j = rows.join(daily.rename("pnl"), how="inner")
        if len(j):
            for col, lab in (("range", "that day's range"), ("atr", "prior ATR")):
                q = j[col].median()
                lo, hi = j[j[col] <= q]["pnl"], j[j[col] > q]["pnl"]
                console.print(f"journal P&L by {lab}: low half avg {lo.mean():+.1f} pts/day (n={len(lo)}), high half "
                              f"avg {hi.mean():+.1f} (n={len(hi)})")
            same = j[(j.bias != 0)]
            if len(same):
                console.print(f"journal P&L on days the bias was called: {same.pnl.mean():+.1f} vs uncalled "
                              f"{j[j.bias == 0].pnl.mean():+.1f} pts/day")
            _ = np


@alpha_app.command("journal")
def journal_cmd(path: Path, split: float = typer.Option(0.67, help="Fit on this share of the dates, check on the rest.")):
    """Test day rules (stop after a loss, time cut-offs, max trades) on another service's published trades."""
    from .alpha.journal import load_trades, rules_report
    tr, flat = load_trades(path)
    if not len(tr):
        console.print("[red]no trades found[/red]")
        raise typer.Exit(1)
    rep = rules_report(tr, split)
    console.print(f"{len(tr)} trades over {tr['date'].nunique()} trading days (+{len(flat)} flat days) · "
                  f"rules fitted before {rep['split_date']}, checked from it")
    t = Table(box=box.SIMPLE)
    for c in ("rule", "fit: n / net / max DD / PF", "check: n / net / max DD / PF", "all: net / max DD"):
        t.add_column(c)
    fmt = lambda s: f"{s['n']} / {s.get('net_pts', 0):+,.0f} / {s.get('max_dd_pts', 0):,.0f} / {s.get('profit_factor')}"  # noqa: E731
    for name, r in rep["rules"].items():
        t.add_row(name, fmt(r["fit"]), fmt(r["check"]), f"{r['all'].get('net_pts', 0):+,.0f} / {r['all'].get('max_dd_pts', 0):,.0f}")
    console.print(t)
    for key in ("by_time", "by_trade_number", "after_a_loss", "by_side"):
        t = Table(title=key.replace("_", " "), box=box.SIMPLE, title_justify="left")
        for c in ("", "n", "win", "net", "avg"):
            t.add_column(c)
        for k, s in rep[key].items():
            t.add_row(k, str(s["n"]), f"{s.get('win_rate', 0):.0%}", f"{s.get('net_pts', 0):+,.0f}", f"{s.get('avg_pts', 0):+.1f}")
        console.print(t)


@alpha_app.command("sanity")
def sanity_cmd(days: int = typer.Option(120)):
    """Run the strategy on structureless random-walk bars: a sound backtester shows no edge there."""
    from .alpha.backtest import random_walk_check
    r = random_walk_check(days)
    console.print(r)


@alpha_app.command("replay")
def replay_cmd(csv: Path, day: str = typer.Option(..., "--date", help="YYYY-MM-DD"), tz: Optional[str] = None):
    """Print the exact Discord messages the bot would have sent on one day of a bars CSV."""
    from .alpha.backtest import run_backtest
    from .alpha.data import load_intraday_csv
    from .alpha.messages import Renderer
    s = _settings()
    bars = load_intraday_csv(csv, tz)
    res = run_backtest(bars, collect_events=True, days=[date.fromisoformat(day)])
    rd = Renderer(s.alpha_tag, None)
    for ev in res.events:
        r = rd.render(ev)
        if not r:
            continue
        _, emb = r
        when = str(ev.get("ts") or "")[11:16] or "09:25"
        console.rule(f"[bold]{when} ET  {emb['title']}[/bold]")
        if emb.get("description"):
            console.print(emb["description"])
        for fld in emb.get("fields") or []:
            console.print(f"[bold]{fld['name']}[/bold]: {fld['value']}")


@alpha_app.command("status")
def status_cmd():
    """Trades and settings recorded by the live bot."""
    from .alpha.bot import AlphaStore
    from .alpha.learn import AdaptiveBook
    s = _settings()
    dd = Path(s.data_dir).expanduser()
    st = AlphaStore(dd / "alpha.sqlite3")
    tr = st.trades(50)
    console.print(f"bot enabled: {s.alpha_enabled} · feed {s.alpha_feed} · destination "
                  f"{'bot channel' if s.discord_bot_token and s.alpha_channel_id else 'webhook' if s.alpha_webhook_url else 'none (dashboard only)'}")
    if tr:
        net = sum(t["pts"] for t in tr)
        console.print(f"last {len(tr)} trades: net {net:+.1f} pts, wins {sum(t['pts'] > 0.5 for t in tr)}")
    for r in AdaptiveBook.load(dd / "alpha_learner.json").summary():
        console.print(f"  {r['setup']:6} {r['regime']:6} n={r['n']:3d} exp {r['expectancy']:+.2f}R {'on' if r['active'] else 'PAUSED'}")
    p = dd / "alpha_params.json"
    console.print(f"settings: {p if p.exists() else 'defaults'}")


@alpha_app.command("optimize")
def optimize_cmd(save: bool = typer.Option(True)):
    """Walk-forward re-fit on the last ~60 days of 5-min bars (what the Sunday job does)."""
    from .alpha.bot import AlphaBot

    async def fn(eng):
        bot = AlphaBot(eng, eng.settings)
        return await bot.optimize(save)
    res = _run(fn)
    console.print(_stats_table("out of sample (fitted)", res["test_stats"]))
    console.print(f"best {res['best']} · adopted: {res['adopted']}")


@alpha_app.command("bars")
def bars_cmd(out: Path = typer.Option(Path("data/mnq_5m.csv"), help="Where to write the CSV."),
             symbol: str = typer.Option("MNQ=F"), interval: str = typer.Option("5m", help="1m (7 days) or 5m (60 days)"),
             period: str = typer.Option("60d")):
    """Download intraday bars once and save them (ET timestamps) - for repeatable backtests and research."""
    from .alpha.data import fetch_intraday
    bars = _run(lambda eng: fetch_intraday(eng, symbol, interval, period))
    out.parent.mkdir(parents=True, exist_ok=True)
    df = bars.copy()
    df.index = df.index.strftime("%Y-%m-%d %H:%M:%S")
    df.index.name = "datetime"
    if out.exists():                        # append to an earlier download instead of losing older bars
        old = pd_read(out)
        df = old.combine_first(df) if old is not None else df
    df.to_csv(out)
    console.print(f"[green]{len(df):,} bars → {out}[/green] ({df.index[0]} → {df.index[-1]} ET)")


def pd_read(path: Path):
    import pandas as pd
    try:
        return pd.read_csv(path, index_col=0)
    except Exception:
        return None
