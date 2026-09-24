"""Rich terminal rendering of engine output (kept separate from CLI wiring)."""
from __future__ import annotations

from typing import Any

from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


# --------------------------------------------------------------------------- formatting
def num(x: Any, nd: int = 2, suffix: str = "") -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):,.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return str(x)


def big(x: Any) -> str:
    if x is None:
        return "—"
    x = float(x)
    for d, s in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= d:
            return f"{x / d:,.2f}{s}"
    return f"{x:,.0f}"


def signed(x: Any, nd: int = 2, suffix: str = "") -> Text:
    if x is None:
        return Text("—", style="dim")
    x = float(x)
    return Text(f"{x:+,.{nd}f}{suffix}", style="green" if x > 0 else "red" if x < 0 else "white")


def score_bar(score: float, width: int = 30) -> Text:
    """-100..100 bar centred on zero."""
    half = width // 2
    n = int(round(abs(score) / 100 * half))
    t = Text()
    if score >= 0:
        t.append(" " * half + "│")
        t.append("█" * n, style="green")
        t.append(" " * (half - n))
    else:
        t.append(" " * (half - n))
        t.append("█" * n, style="red")
        t.append("│" + " " * half)
    return t


def risk_style(level: str | None) -> str:
    return {"Low": "green", "Moderate": "yellow", "Elevated": "dark_orange", "High": "red",
            "Extreme": "bold red"}.get(level or "", "white")


# --------------------------------------------------------------------------- report
def render_report(r: dict, show_chain: bool = True, show_news: int = 6) -> None:
    q = r.get("quote") or {}
    dq = r.get("data_quality") or {}
    if dq.get("synthetic"):
        console.print(Panel("[bold]DEMO MODE — prices are SIMULATED.[/bold] Not real market data.",
                            style="bold white on red", box=box.HEAVY))
    title = Text()
    title.append(f" {r['symbol']} ", style="bold black on cyan")
    title.append(f"  {r.get('name') or ''}", style="bold")
    head = Table.grid(padding=(0, 3))
    head.add_row(Text(f"{num(q.get('price'))} {q.get('currency') or ''}", style="bold white"),
                 signed(q.get("change"), 2), signed(q.get("change_pct"), 2, "%"),
                 Text(f"as of {str(r.get('as_of'))[:10]} · {r.get('period')} / {r.get('interval')} · "
                      f"history: {dq.get('history_source')}", style="dim"))
    console.print(Panel(Group(title, head), box=box.ROUNDED, border_style="cyan"))

    sig, reg = r.get("signal") or {}, r.get("regime") or {}
    risk = next((x for x in r.get("risk") or [] if "error" not in x), {})
    sp = Table.grid()
    sp.add_row(Text(f"{sig.get('label', '—')}  {num(sig.get('score'), 0)}/100", style="bold"))
    sp.add_row(score_bar(sig.get("score") or 0))
    rp = Table.grid()
    rp.add_row(Text(f"{reg.get('trend', '—')}", style="bold"))
    rp.add_row(f"strength: {reg.get('trend_strength') or '—'} (ADX {num(reg.get('adx'), 1)})")
    rp.add_row(f"volatility: {reg.get('volatility_regime') or '—'}")
    kp = Table.grid()
    kp.add_row(Text(f"{risk.get('level', '—')}  {num(risk.get('score'), 0)}/100", style="bold " + risk_style(risk.get("level"))))
    m = risk.get("metrics") or {}
    kp.add_row(f"VaR95 1d {num(m.get('var_95_1d_pct'))}% · CVaR {num(m.get('cvar_95_1d_pct'))}%")
    kp.add_row(f"top driver: {(risk.get('drivers') or [{}])[0].get('factor', '—')}")
    console.print(Columns([Panel(sp, title="Composite signal", border_style="blue"),
                           Panel(rp, title="Regime", border_style="blue"),
                           Panel(kp, title=f"Risk ({risk.get('model', 'baseline')})", border_style="blue")], expand=True))

    # plays
    pt = Table(title="Trade setups (rule-based)", box=box.SIMPLE_HEAVY, expand=True)
    for c in ("Setup", "Dir", "Conf.", "Entry", "Stop", "T1", "T2", "Horizon"):
        pt.add_column(c)
    for p in r.get("plays") or []:
        lv = p.get("levels") or {}
        pt.add_row(p["name"], p["direction"], f"{p['confidence']:.0%}", num(lv.get("entry")), num(lv.get("stop")),
                   num(lv.get("target_1")), num(lv.get("target_2")), p.get("horizon", ""))
    console.print(pt)

    # indicators + stats side by side
    ind = r.get("indicators") or {}
    it = Table(title="Indicators", box=box.SIMPLE, show_header=False)
    it.add_column(style="dim")
    it.add_column(justify="right")
    for k, lbl in (("rsi_14", "RSI 14"), ("macd_hist", "MACD hist"), ("adx", "ADX"), ("bb_pctb", "Bollinger %B"),
                   ("atr_pct", "ATR %"), ("williams_r", "Williams %R"), ("cci_20", "CCI 20"), ("mfi_14", "MFI 14"),
                   ("stoch_k", "Stoch %K"), ("vwap_20", "VWAP 20"), ("sma_50", "SMA 50"), ("sma_200", "SMA 200"),
                   ("rel_volume", "Rel. volume")):
        it.add_row(lbl, num(ind.get(k)))
    st = r.get("statistics") or {}
    stt = Table(title="Statistics (view period)", box=box.SIMPLE, show_header=False)
    stt.add_column(style="dim")
    stt.add_column(justify="right")
    for k, lbl, sfx in (("total_return_pct", "Total return", "%"), ("cagr_pct", "CAGR", "%"),
                        ("ann_volatility_pct", "Ann. volatility", "%"), ("vol_20d_pct", "Vol 20d", "%"),
                        ("sharpe", "Sharpe", ""), ("sortino", "Sortino", ""), ("max_drawdown_pct", "Max drawdown", "%"),
                        ("current_drawdown_pct", "Current drawdown", "%"), ("beta", "Beta", ""),
                        ("relative_return_pct", "vs benchmark", "%"), ("worst_day_pct", "Worst day", "%")):
        stt.add_row(lbl, num(st.get(k), 2, sfx))
    lv = r.get("levels") or {}
    lt = Table(title="Key levels", box=box.SIMPLE, show_header=False)
    lt.add_column(style="dim")
    lt.add_column(justify="right")
    for x in reversed(lv.get("resistance") or []):
        lt.add_row("Resistance", Text(num(x), style="red"))
    lt.add_row("Pivot", num((lv.get("pivots") or {}).get("pivot")))
    for x in lv.get("support") or []:
        lt.add_row("Support", Text(num(x), style="green"))
    console.print(Columns([it, stt, lt], expand=True))

    # options
    o = r.get("options") or {}
    if o.get("available"):
        s = o.get("summary") or {}
        hdr = (f"exp {str(o.get('selected_expiry'))[:10]} ({o.get('days_to_expiry')}d) · ATM IV {num((s.get('atm_iv') or 0) * 100, 1)}% · "
               f"expected move ±{num(s.get('expected_move'))} ({num(s.get('expected_move_pct'), 1)}%) · "
               f"P/C OI {num(s.get('put_call_oi_ratio'))} · max pain {num(s.get('max_pain'))}")
        if o.get("model_generated"):
            hdr += "  [yellow](MODEL chain — no market quotes)[/yellow]"
        console.print(Panel(hdr, title=f"Options · {o.get('source')}", border_style="magenta"))
        if show_chain:
            render_chain(o, rows=5)

    # sentiment + news
    se = r.get("sentiment") or {}
    news = r.get("news") or []
    if news:
        nt = Table(title=f"News sentiment: {se.get('label')} ({num(se.get('score'), 2)}) · {se.get('articles')} articles",
                   box=box.SIMPLE, expand=True)
        nt.add_column("Sent.", width=6)
        nt.add_column("Headline")
        nt.add_column("Publisher", style="dim", width=18)
        for n in news[:show_news]:
            nt.add_row(signed(n.get("sentiment"), 2), n.get("title", ""), (n.get("publisher") or "")[:18])
        console.print(nt)

    render_forecast(r.get("forecast") or {}, compact=True)

    ai = r.get("ai_insight") or {}
    if ai:
        body = Text(ai.get("summary") or "", style="bold")
        g = [body]
        for lbl, key, sty in (("Bull case", "bull_case", "green"), ("Bear case", "bear_case", "red"),
                              ("Risks", "risks_to_watch", "yellow")):
            for x in ai.get(key) or []:
                g.append(Text(f"  {lbl}: {x}", style=sty))
        if ai.get("note"):
            g.append(Text(ai["note"], style="dim italic"))
        console.print(Panel(Group(*g), title=f"Insight · {ai.get('engine')} · stance: {ai.get('stance')}",
                            border_style="green"))

    for w in r.get("warnings") or []:
        console.print(f"[yellow]⚠ {w}[/yellow]")
    render_provenance(r)
    console.print(f"[dim]{r.get('disclaimer', '')}[/dim]")


def render_provenance(r: dict) -> None:
    parts = []
    for p in r.get("provenance") or []:
        c = p.get("cache")
        tag = p.get('capability') + (f":{p.get('symbol')}" if p.get('symbol') != r.get('symbol') else "")
        parts.append(f"{tag}←{p.get('provider')} ({'cache ' + c if c not in ('miss', None) else str(p.get('latency_ms')) + 'ms'})")
    t = r.get("timings_ms") or {}
    console.print(f"[dim]sources: {' · '.join(parts)}[/dim]")
    console.print(f"[dim]timings: " + " · ".join(f"{k} {v:.0f}ms" for k, v in t.items()) + "[/dim]")


def render_chain(o: dict, rows: int = 8) -> None:
    rows_ = o.get("contracts") or []
    strikes = sorted({c["strike"] for c in rows_})
    spot = o.get("underlying_price") or 0
    if not strikes:
        return
    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    keep = set(strikes[max(0, atm_i - rows): atm_i + rows + 1])
    by = {(c["strike"], c["kind"]): c for c in rows_ if c["strike"] in keep}
    wide = console.width >= 130
    t = Table(box=box.SIMPLE, title="Chain (calls | strike | puts)")
    ccols = ["Δ", "Γ", "Θ/day", "Vega", "IV", "Mid"] if wide else ["Δ", "IV %", "Mid"]
    for c in ccols:
        t.add_column(c, justify="right", style="green", no_wrap=True)
    t.add_column("Strike", justify="center", style="bold", no_wrap=True)
    for c in reversed(ccols):
        t.add_column(c, justify="right", style="red", no_wrap=True)
    pr = lambda d: d.get("mid") if d.get("mid") is not None else d.get("theo")  # noqa: E731
    for k in sorted(keep):
        c, p = by.get((k, "call"), {}), by.get((k, "put"), {})
        iv = lambda d: num((d.get("iv_used") or 0) * 100, 1)  # noqa: E731
        strike = Text(num(k), style="bold reverse" if k == strikes[atm_i] else "bold")
        if wide:
            t.add_row(num(c.get("delta"), 2), num(c.get("gamma"), 3), num(c.get("theta"), 3), num(c.get("vega"), 3),
                      iv(c), num(pr(c)), strike, num(pr(p)), iv(p), num(p.get("vega"), 3), num(p.get("theta"), 3),
                      num(p.get("gamma"), 3), num(p.get("delta"), 2))
        else:
            t.add_row(num(c.get("delta"), 2), iv(c), num(pr(c)), strike, num(pr(p)), iv(p), num(p.get("delta"), 2))
    console.print(t)


def render_compare(res: dict) -> None:
    t = Table(title=f"Comparison · {res['period']}", box=box.SIMPLE_HEAVY, expand=True)
    for c in ("Symbol", "Price", "Chg %", "Signal", "Trend", "RSI", "Return %", "Vol %", "Sharpe", "MaxDD %",
              "Risk", "Sent.", "Top setup"):
        t.add_column(c)
    for r in res["rows"]:
        if "error" in r:
            t.add_row(r["symbol"], Text(r["error"][:80], style="red"))
            continue
        t.add_row(r["symbol"], num(r["price"]), signed(r.get("change_pct")), f"{num(r['signal'], 0)} {r['label']}",
                  r["trend"], num(r["rsi"], 1), signed(r["return_pct"], 1), num(r["vol_pct"], 1), num(r["sharpe"]),
                  num(r["max_dd_pct"], 1), Text(f"{num(r['risk_score'], 0)} {r['risk_level'] or ''}", style=risk_style(r["risk_level"])),
                  signed(r.get("sentiment")), r.get("top_play") or "")
    console.print(t)
    p = res.get("portfolio") or {}
    if p.get("available"):
        console.print(Panel(
            f"Equal-weight portfolio · vol {num(p['ann_volatility_pct'], 1)}% · VaR95 1d {num(p['var_95_1d_pct'])}% · "
            f"CVaR {num(p['cvar_95_1d_pct'])}% · diversification ratio {num(p['diversification_ratio'])}\n"
            "Risk contribution: " + ", ".join(f"{k} {v:.1f}%" for k, v in p["risk_contribution_pct"].items()),
            title="Portfolio risk", border_style="blue"))
        syms = p["symbols"]
        ct = Table(title="Correlation (daily returns)", box=box.SIMPLE)
        ct.add_column("")
        for s in syms:
            ct.add_column(s, justify="right")
        for a in syms:
            ct.add_row(a, *[num(p["correlation"][a][b], 2) for b in syms])
        console.print(ct)


def render_providers(status: list[dict]) -> None:
    wide = console.width >= 130
    t = Table(title="Data providers (priority order)", box=box.SIMPLE_HEAVY, expand=True)
    cols = ["#", "Provider", "Ready", "Capabilities", "Circuit", "Success", "Latency"] + (["Notes"] if wide else [])
    for c in cols:
        t.add_column(c, overflow="fold")
    for p in status:
        if p["configured"] and p["rank"] is None:
            ready = Text("not in order", style="dim")
        elif p["configured"]:
            ready = Text("yes", style="green")
        else:
            ready = Text(f"set {p['key_env']}" if p["requires_key"] else "no", style="dim")
        b, h = p["breaker"], p["health"]
        row = [str(p["rank"] + 1) if p["rank"] is not None else "–", p["label"], ready, ", ".join(p["capabilities"]),
               Text(b["state"], style="green" if b["state"] == "closed" else "red"),
               f"{h['success_rate']:.0%} ({h['calls']})" if h["calls"] else "—",
               f"{h['ewma_latency_ms']:.0f}ms" if h["ewma_latency_ms"] else "—"]
        t.add_row(*row, *([p["notes"]] if wide else []))
    console.print(t)


REC_STYLE = {"Strong Buy": "bold black on green", "Buy": "bold green", "Hold": "bold white", "Reduce": "bold red",
             "Sell": "bold white on red"}


def render_forecast(fc: dict, compact: bool = False) -> None:
    if not fc.get("available"):
        if fc.get("reason") and fc.get("reason") != "disabled":
            console.print(f"[dim]Prediction unavailable: {fc['reason']}[/dim]")
        return
    rec, th, conf = fc.get("recommendation") or {}, fc.get("thesis") or {}, fc["confidence"]
    head = Table.grid(padding=(0, 3))
    head.add_row(Text(f" {rec.get('action', 'n/a')} ", style=REC_STYLE.get(rec.get("action"), "bold")),
                 Text(f"confidence {conf['rating']} ({conf['score']:.0f}/100)", style="bold"),
                 Text(f"horizon {rec.get('horizon_label')} · P(up) {rec.get('prob_up', 0):.0%} · "
                      f"expected {rec.get('expected_return_pct', 0):+.1f}%", style="dim"))
    body = [head, Text(th.get("headline", ""), style="bold"), Text(th.get("context", ""))]
    for lbl, key, sty in (("+", "bull_points", "green"), ("-", "bear_points", "red")):
        for x in (th.get(key) or [])[: (3 if compact else 5)]:
            body.append(Text(f"  {lbl} {x}", style=sty))
    if th.get("setup"):
        body.append(Text(f"  > {th['setup']}", style="cyan"))
    for x in th.get("invalidation") or []:
        body.append(Text(f"  ! {x}", style="yellow"))
    for n in rec.get("notes") or []:
        body.append(Text(f"  note: {n}", style="dim italic"))
    console.print(Panel(Group(*body), title=f"Prediction · {fc['paths']:,} simulated paths", border_style="magenta"))

    t = Table(box=box.SIMPLE, expand=not compact, title=None if compact else "Simulated outcomes")
    for c in ("Horizon", "P5", "P25", "Median", "P75", "P95", "Mean", "P(up)", "P(>+10%)", "P(<-10%)", "VaR95"):
        t.add_column(c, justify="right")
    for h in fc["horizons"]:
        q = h["return_pct"]
        t.add_row(h["label"], signed(q["p5"], 1, "%"), signed(q["p25"], 1, "%"), signed(q["p50"], 1, "%"),
                  signed(q["p75"], 1, "%"), signed(q["p95"], 1, "%"), signed(h["expected_return_pct"], 1, "%"),
                  f"{h['prob_up']:.0%}", f"{h['prob_up_10']:.0%}", f"{h['prob_down_10']:.0%}", f"{h['var_95_pct']:.1f}%")
    console.print(t)
    if compact:
        return
    st = Table(box=box.SIMPLE, title=f"Scenarios · {fc['primary_horizon']} trading days")
    for c in ("Scenario", "Probability", "Avg return", "Price", "Range"):
        st.add_column(c)
    for sc in fc["scenarios"]:
        st.add_row(sc["name"], f"{sc['probability']:.0%}", signed(sc["return_pct"], 1, "%"), num(sc["price"]),
                   f"{sc['range_pct'][0]:+.1f}% to {sc['range_pct'][1]:+.1f}%")
    console.print(st)
    for b in fc.get("barriers") or []:
        console.print(f"[cyan]{b['setup']}[/cyan]: target {num(b['target_1'])} first {b['prob_target_first']:.0%} · stop "
                      f"{num(b['stop'])} first {b['prob_stop_first']:.0%} · neither {b['prob_neither']:.0%} · "
                      f"expected {b['expected_r_multiple']:+.2f}R")
    cal = fc.get("calibration") or {}
    v, d = fc["volatility"], fc["drift"]
    console.print(f"[dim]Volatility {v['now_annual_pct']:.0f}% now → {v['long_run_annual_pct']:.0f}% long-run · drift "
                  f"{d['total_annual'] * 100:+.1f}%/yr (rf {d['risk_free'] * 100:.1f} + beta×ERP {d['equity_premium'] * 100:.1f} "
                  f"+ signal {d['signal_tilt'] * 100:+.1f} + news {d['sentiment_tilt'] * 100:+.1f})"
                  + (f" · calibration: {cal['verdict']} ({cal['coverage_90']:.0%} of past 90% bands hit)" if cal.get("available") else "")
                  + "[/dim]")
    ct = Table(box=None, title="Confidence components")
    for c in ("Component", "Score", "Weight", "Detail"):
        ct.add_column(c)
    for k, c in conf["components"].items():
        ct.add_row(k.replace("_", " "), f"{c['score']:.2f}", f"{c['weight']:.2f}", c["detail"])
    console.print(ct)
    console.print(f"[dim]{th.get('method', '')} {th.get('disclaimer', '')}[/dim]")
