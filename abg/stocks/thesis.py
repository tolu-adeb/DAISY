"""Turning a candidate into a pitch a person can act on or argue with.

A pitch has to answer four questions, in this order, or it is not a thesis:

1. **What happened** - the rule that fired, stated with the numbers that fired it.
2. **Why that should pay** - the documented behaviour the book is harvesting, named plainly.
3. **What we are risking** - entry, stop, size, the loss if the stop is hit, and when we get out.
4. **What would make this wrong** - the exits restated as invalidation, plus the known weaknesses of
   the data and of the book itself.

Everything printed here is read off the same indicator frames the book read, so the pitch cannot drift
from the decision.  Valuation is added for single stocks when fundamentals are available; it informs
the write-up but it never overrides the rule, because the rule is what was tested.

No secret is ever written into a pack, a memo or an embed.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .live import BOOK_EDGE, BOOK_LABEL, BOOK_SHORT

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

VERDICT_COLOR = {"BUY": 0x2E9B5B, "REBALANCE": 0x3F7FBF, "SELL": 0xB3403A, "WATCH": 0x8A8F98}


# --------------------------------------------------------------------------- formatting
def _px(x) -> str:
    if x is None:
        return "—"
    return f"${x:,.2f}" if abs(x) < 1000 else f"${x:,.0f}"


def _pct(x, d=1) -> str:
    return "—" if x is None else f"{x * 100:+.{d}f}%"


def _pct0(x, d=0) -> str:
    return "—" if x is None else f"{x * 100:.{d}f}%"


def _n(x, d=1) -> str:
    return "—" if x is None else f"{x:,.{d}f}"


def _money(x) -> str:
    return "—" if x is None else f"${x:,.0f}"


def _regime_words(regime: str) -> str:
    return {"bull_trend": "a trending market", "bull_chop": "a choppy but rising market",
            "risk_off": "a risk-off market", "crisis": "crisis mode",
            "warmup": "not enough history yet"}.get(regime, regime)


# --------------------------------------------------------------------------- per-book narrative
def _what_happened(c, f: dict) -> str:
    """One or two sentences naming the rule and the numbers behind it."""
    s = c.symbol
    if c.book == "trend_pullback":
        return (f"{s} is in an established uptrend — the close of {_px(f.get('close'))} is above its "
                f"200-day EMA of {_px(f.get('ema200'))}, and the 50-day EMA ({_px(f.get('ema50'))}) is "
                f"above the 200-day. It then pulled back: RSI-14 got as low as "
                f"{_n(f.get('min_rsi14_5d'))} in the last five sessions. Today the pullback ended — the "
                f"close is above yesterday's high of {_px(f.get('prev_high'))} and the MACD histogram "
                f"turned up ({_n(f.get('macd_hist'), 2)} against {_n(f.get('macd_hist_prev'), 2)} "
                f"yesterday).")
    if c.book == "mean_reversion":
        rsi = next((v for k, v in f.items() if k.startswith("rsi") and k != "rsi14"), None)
        return (f"{s} is still in a long-term uptrend — the close of {_px(f.get('close'))} is above its "
                f"200-day average of {_px(f.get('sma200'))} — but it has just been sold hard: the "
                f"2-day RSI is {_n(rsi)}, which is about as stretched as this measure gets. The book "
                f"buys that weakness and sells into the first bounce, usually within a week.")
    if c.book == "xs_momentum":
        return (f"{s} has one of the strongest risk-adjusted 12-month trends in the universe: "
                f"{_pct(f.get('mom_12_1'))} over the year to one month ago, divided by its annualised "
                f"volatility of {_pct0(f.get('vol252'))}, giving a score of {_n(c.score, 2)}. The most "
                f"recent month is deliberately skipped, because short-term moves tend to reverse.")
    if c.book == "dual_momentum":
        return (f"{s} is one of the strongest things on the board — {_pct(f.get('mom126'))} over six "
                f"months and {_pct(f.get('mom252'))} over twelve, averaging to a score of "
                f"{_n(c.score, 3)} — and it is above its own 200-day average "
                f"({_px(f.get('sma200'))}). The broad market is above its 200-day average too, so the "
                f"book is risk-on and holding the leaders rather than hiding in Treasuries.")
    if c.book == "crisis_alpha":
        return (f"Crisis mode is on, so this book has the capital. {s} is part of the defensive sleeve "
                f"or the index hedge: it is held while it stays above its 50-day average "
                f"({_px(f.get('sma50'))}) with a positive 63-day return ({_pct(f.get('mom63'))}).")
    return f"{s}: {c.reason}."


def _exits(c, cfg: dict) -> list[str]:
    """What closes this position, in the book's own terms."""
    p = (cfg.get("books") or {}).get(c.book, {})
    if c.book == "trend_pullback":
        out = [f"RSI-14 goes above {p.get('rsi_exit', 70)} — the bounce has been paid",
               "the close falls below the 50-day EMA — the trend we were buying into is gone",
               f"held {p.get('max_hold', 30)} days without resolving",
               f"the initial stop at {_px(c.stop)} is touched "
               f"({_n(p.get('stop_atr', 2.0))}× ATR below the fill)",
               f"a trailing stop {_n(p.get('trail_atr', 3.0))}× ATR under the highest close since entry, "
               f"which only ever ratchets up",
               "the market turns risk-off, or crisis mode starts"]
        if p.get("sentiment_floor") is not None:
            out.append(f"the daily news score drops below {p['sentiment_floor']:g}")
        return out
    if c.book == "mean_reversion":
        return [f"the close gets back above the 5-day average — the bounce has come, take it",
                f"RSI goes above {p.get('rsi_exit', 70)}",
                f"held {p.get('max_hold', 7)} days — the setup had its chance",
                f"the catastrophe stop at {_px(c.stop)} ({_n(p.get('stop_atr', 2.5))}× ATR below the "
                f"fill); there is no trailing stop here, the hold is too short for one",
                "the market turns risk-off, or crisis mode starts"]
    if c.book == "xs_momentum":
        return [f"at the next month-end it is no longer in the top {p.get('top_n', 20)} by score",
                "its score turns negative",
                "the market turns risk-off or crisis mode starts — the whole book goes to cash"]
    if c.book == "dual_momentum":
        return [f"at the next month-end it is no longer in the top {p.get('top_k', 3)} by score",
                "it drops below its own 200-day average",
                "the benchmark drops below its 200-day average — the book rotates into the best "
                "defensive asset instead"]
    if c.book == "crisis_alpha":
        return ["crisis mode ends — every position in this book is closed at the next open",
                "it falls below its 50-day average at the next 5-day rebalance"]
    return ["see the rule file"]


def _risks(c, state, cfg: dict, val: dict | None, pack_fund: dict | None) -> list[str]:
    out: list[str] = []
    if c.book in ("trend_pullback", "mean_reversion"):
        out.append("This is a short hold bought into weakness: the single biggest risk is that the "
                   "weakness is news, not noise. A pullback that is actually the start of a "
                   "re-rating keeps going, and the stop pays for it.")
    if c.book == "xs_momentum":
        out.append("Momentum crashes when beaten-down stocks rebound sharply after a bear market — "
                   "2009 is the standard example. The risk-off multiplier and crisis mode are the "
                   "defence, and they are late by construction.")
    if c.book == "dual_momentum":
        out.append("A 200-day switch sells late and buys back late. In a sharp V-shaped reversal this "
                   "book gives up the first leg of the recovery.")
    if state.crisis:
        out.append("Crisis mode is on, so position sizes are cut and the other books are standing "
                   "down. Signals in this state have the widest outcomes.")
    on = [k for k, v in (state.flags or {}).items() if v]
    if on and not state.crisis:
        out.append(f"{len(on)} early-warning flag(s) are already on ({', '.join(on[:4])}"
                   f"{', …' if len(on) > 4 else ''}) — stress score {state.stress:.2f}, crisis starts "
                   f"at {(cfg.get('stress') or {}).get('enter', 0.5):g}.")
    if val and val.get("upside") is not None and val["upside"] < 0:
        out.append(f"Valuation disagrees with the rule: the blended fair value is "
                   f"{_pct(val['upside'])} against the current price. The rule is what was tested, so "
                   f"the trade stands, but this is a reason to keep it small.")
    if pack_fund is None and c.book in ("trend_pullback", "mean_reversion", "xs_momentum"):
        out.append("No fundamentals were available for this name, so there is no valuation cross-check "
                   "— the pitch rests on price behaviour alone.")
    if c.ml_prob is not None:
        out.append(f"The walk-forward take/skip model put this at {_pct0(c.ml_prob)} to reach its "
                   f"profit target before its stop. That model is a filter, not a forecast.")
    return out


def _caveats(state, cfg: dict) -> list[str]:
    out = ["Paper only. Nothing here places an order, and the desk holds no real position."]
    uni = state.universe or {}
    if uni.get("seed"):
        out.append("The single-stock universe is still the built-in seed list, which only contains "
                   "names that survived to today. Load a real point-in-time index list before trusting "
                   "any backtest number from it.")
    for w in (state.warnings or [])[:2]:
        out.append(w.split(" `abg")[0].strip())
    return out


# --------------------------------------------------------------------------- pack
def build_pack(c, state, cfg: dict, fund: dict | None = None, peers: list[dict] | None = None,
               news: list[dict] | None = None, provider: str | None = None) -> dict:
    """Everything a memo or an embed needs for one candidate. Pure: no I/O, no secrets."""
    f = c.facts or {}
    val = None
    if fund and c.price and c.book != "dual_momentum" and not c.symbol.startswith("^"):
        try:
            from abg.pitch.valuation import value
            val = value(fund, c.price, peers or [],
                        rf=float(cfg.get("pitch_risk_free", 0.042)),
                        erp=float(cfg.get("pitch_equity_premium", 0.05)),
                        tg=float(cfg.get("pitch_terminal_growth", 0.025)))
        except Exception as e:                      # a valuation failure must not lose the signal
            log.warning("valuation failed for %s: %s", c.symbol, e)
    bp = (cfg.get("books") or {}).get(c.book, {})
    return {
        "id": f"{state.date}-{c.book}-{c.symbol}",
        "as_of": state.date,
        "written_at": datetime.now(NY).isoformat(timespec="seconds"),
        "symbol": c.symbol,
        "name": (fund or {}).get("name") or c.symbol,
        "sector": (fund or {}).get("sector"),
        "action": c.action,
        "book": c.book,
        "book_label": BOOK_LABEL.get(c.book, c.book),
        "edge": BOOK_EDGE.get(c.book, ""),
        "rank": c.rank, "of": c.of, "score": c.score, "ml_prob": c.ml_prob,
        "price": c.price, "stop": c.stop, "shares": c.shares, "notional": c.notional,
        "weight": c.weight, "risk_pct": c.risk_pct, "r_dollars": c.r_dollars,
        "atr": c.atr, "stop_atr": c.stop_atr,
        "max_hold": bp.get("max_hold"),
        "what_happened": _what_happened(c, f),
        "facts": f, "gates": c.gates,
        "exits": _exits(c, cfg),
        "risks": _risks(c, state, cfg, val, fund),
        "caveats": _caveats(state, cfg),
        "valuation": val,
        "news": [{"title": (x.get("title") or "")[:160], "url": x.get("url"),
                  "published": x.get("published") or x.get("datetime")} for x in (news or [])[:4]],
        "context": {"regime": state.regime, "regime_words": _regime_words(state.regime),
                    "crisis": state.crisis, "stress": state.stress,
                    "flags_on": [k for k, v in (state.flags or {}).items() if v],
                    "book_weight": (state.weights or {}).get(c.book),
                    "book_budget": (state.budgets or {}).get(c.book),
                    "size_mult": (bp.get("regimes") or {}).get(state.regime),
                    "vol_scale": state.vol_scale, "equity": state.equity,
                    "drawdown": state.drawdown},
        "provider": provider,
        "reason": c.reason,
    }


# --------------------------------------------------------------------------- markdown
def render_md(pack: dict, note: str | None = None) -> str:
    p = pack
    ctx = p["context"]
    head = f"# {p['action']} {p['symbol']}"
    if p.get("name") and p["name"] != p["symbol"]:
        head += f" — {p['name']}"
    L = [head, "", f"**{p['book_label']}**"
         + (f" · ranked {p['rank']} of {p['of']} that qualified today" if p.get("rank") else "")
         + f" · signal from the {p['as_of']} close, to be acted on at the next open", ""]
    if p.get("sector"):
        L.append(f"*{p['sector']}*")
        L.append("")

    L += ["## The thesis", "", p["what_happened"], "",
          f"The book is harvesting a documented behaviour: {p['edge']}. "
          f"That is the whole reason this trade exists — it is not a forecast about {p['symbol']}.", ""]

    bullets = [
        f"- Regime: **{ctx['regime']}** ({ctx['regime_words']})"
        + (f", book size ×{ctx['size_mult']:g}" if ctx.get("size_mult") is not None else ""),
        f"- Stress score {ctx['stress']:.2f}"
        + (f", {len(ctx['flags_on'])} early-warning flag(s) on" if ctx["flags_on"]
           else ", no early-warning flags on")
        + (" — **crisis mode**" if ctx["crisis"] else ""),
    ]
    if ctx.get("book_weight") is not None:
        bullets.append(f"- This book currently has {_pct0(ctx['book_weight'])} of capital "
                       f"({_money(ctx['book_budget'])}), set by the Hedge allocator from how the books "
                       f"have been doing lately")
    if ctx.get("vol_scale") is not None and abs(ctx["vol_scale"] - 1.0) > 1e-9:
        bullets.append(f"- Portfolio size scaled ×{_n(ctx['vol_scale'], 2)} by the volatility target")
    L += ["## Market context", ""] + bullets + [""]

    if p["gates"]:
        L += ["## Every check the rule made", ""]
        for name, ok in p["gates"].items():
            L.append(f"- [{'x' if ok else ' '}] {name}")
        L.append("")

    L += ["## The trade", "",
          "| | |", "|---|---|",
          f"| Action | **{p['action']}** at the next open |",
          f"| Last close | {_px(p['price'])} |"]
    if p.get("stop") is not None:
        L.append(f"| Initial stop | {_px(p['stop'])} ({_n(p['stop_atr'])}× ATR of {_n(p['atr'])}) |")
    if p.get("shares"):
        L.append(f"| Size | {p['shares']:,.0f} shares, {_money(p['notional'])}"
                 + (f" — {_pct0(p['weight'])} of the book" if p.get("weight") else "") + " |")
    if p.get("r_dollars"):
        L.append(f"| Risked to the stop | {_money(p['r_dollars'])}"
                 + (f" — {_pct(p['risk_pct'], 2)} of the book" if p.get("risk_pct") else "") + " |")
    if p.get("max_hold"):
        L.append(f"| Max hold | {p['max_hold']} trading days |")
    if p.get("ml_prob") is not None:
        L.append(f"| Model's odds of reaching target before stop | {_pct0(p['ml_prob'])} |")
    L.append("")

    v = p.get("valuation")
    if v:
        L += ["## Valuation cross-check", "",
              f"Blended fair value **{_px(v.get('fair_value'))}**, "
              f"{_pct(v.get('upside'))} against the close. "
              f"The market is pricing in about {_pct(v.get('implied_growth'))} of growth."]
        sc = v.get("scenarios") or {}
        if sc:
            L += ["", "| Case | Value | vs price |", "|---|---|---|"]
            for k in ("bear", "base", "bull"):
                if k in sc:
                    val_ = sc[k].get("value")
                    L.append(f"| {k.title()} | {_px(val_)} | "
                             f"{_pct((val_ / p['price'] - 1) if val_ and p.get('price') else None)} |")
        cm = v.get("comps") or {}
        if cm.get("value"):
            n_peers = max(cm.get("n_pe") or 0, cm.get("n_forward_pe") or 0)
            pe = cm.get("median_pe") or cm.get("median_forward_pe")
            L.append("")
            L.append(f"Peer comps put it at {_px(cm.get('value'))}"
                     + (f" (median P/E {_n(pe)} across {n_peers} peers)" if pe else "") + ".")
        L += ["", f"*Method: {v.get('method')}.* Valuation is context. The rule above is what was "
                  f"tested and what decides the trade.", ""]

    L += ["## What would make this wrong", ""]
    L.append("The position closes when any of these happens:")
    L.append("")
    for e in p["exits"]:
        L.append(f"- {e}")
    if p["risks"]:
        L += ["", "Known risks in this setup:", ""]
        for r in p["risks"]:
            L.append(f"- {r}")
    L.append("")

    if p.get("news"):
        L += ["## Recent headlines", ""]
        for n in p["news"]:
            L.append(f"- [{n['title']}]({n['url']})" if n.get("url") else f"- {n['title']}")
        L.append("")

    L += ["---", ""]
    for cv in p["caveats"]:
        L.append(f"*{cv}*")
    if p.get("provider"):
        L.append(f"*Prices from {p['provider']}, bars to the {p['as_of']} close.*")
    if note:
        L += ["", note]
    L.append("")
    L.append("*Research, not advice.*")
    return "\n".join(L)


# --------------------------------------------------------------------------- Discord
def embed(pack: dict, note: str = "Full write-up on the dashboard (Stocks tab).") -> dict:
    """One candidate as a Discord embed. Compact, but it still carries the thesis and the stop."""
    p = pack
    ctx = p["context"]
    title = f"{p['action']} {p['symbol']}"
    if p.get("name") and p["name"] != p["symbol"]:
        title += f" — {p['name'][:60]}"
    sub = p["book_label"]
    if p.get("rank"):
        sub += f" · {p['rank']} of {p['of']}"

    fields = []
    plan = [f"**Last close** {_px(p['price'])}"]
    if p.get("stop") is not None:
        plan.append(f"**Stop** {_px(p['stop'])}")
    if p.get("shares"):
        plan.append(f"**Size** {p['shares']:,.0f} sh ({_pct0(p.get('weight'))} of book)")
    if p.get("r_dollars"):
        plan.append(f"**At risk** {_money(p['r_dollars'])}")
    fields.append({"name": "The trade", "value": "\n".join(plan), "inline": True})

    c2 = [f"**Regime** {ctx['regime']}", f"**Stress** {ctx['stress']:.2f}"]
    if ctx.get("size_mult") is not None:
        c2.append(f"**Size mult** ×{ctx['size_mult']:g}")
    if p.get("ml_prob") is not None:
        c2.append(f"**Model** {_pct0(p['ml_prob'])}")
    fields.append({"name": "Context", "value": "\n".join(c2), "inline": True})

    v = p.get("valuation")
    if v and v.get("fair_value"):
        fields.append({"name": "Valuation", "inline": False,
                       "value": f"Fair value {_px(v['fair_value'])} ({_pct(v.get('upside'))}); "
                                f"market prices in {_pct(v.get('implied_growth'))} growth"})

    fields.append({"name": "Exits — any one of these", "inline": False,
                   "value": "\n".join(f"• {e}" for e in p["exits"][:4])[:1020]})
    if p["risks"]:
        fields.append({"name": "Main risk", "value": p["risks"][0][:1020], "inline": False})

    return {"title": title[:250], "description": f"*{sub}*\n\n{p['what_happened']}"[:4000],
            "color": VERDICT_COLOR.get(p["action"], VERDICT_COLOR["WATCH"]),
            "fields": fields[:6],
            "footer": {"text": f"{p['caveats'][0]} {note}"[:2040]},
            "timestamp": p["written_at"]}


def digest_embed(packs: list[dict], state, cfg: dict) -> dict:
    """The one embed that leads a daily post: what the desk sees and what it wants to do."""
    buys = [p for p in packs if p["action"] in ("BUY", "REBALANCE")]
    sells = [p for p in packs if p["action"] == "SELL"]
    lines = [f"**{state.date}** · regime **{state.regime}** ({_regime_words(state.regime)}) · "
             f"stress {state.stress:.2f}" + (" · **CRISIS MODE**" if state.crisis else "")]
    on = [k for k, v in (state.flags or {}).items() if v]
    if on:
        lines.append(f"Early-warning flags on: {', '.join(on)}")
    if state.weights:
        w = ", ".join(f"{BOOK_SHORT.get(k, k)} {_pct0(v)}"
                      for k, v in sorted(state.weights.items(), key=lambda kv: -(kv[1] or 0)))
        lines.append(f"Capital by book: {w}")

    fields = []
    if buys:
        fields.append({"name": f"{len(buys)} to open or add", "inline": False,
                       "value": "\n".join(
                           f"**{p['symbol']}** {_px(p['price'])} · {BOOK_SHORT.get(p['book'], p['book'])}"
                           + (f" · stop {_px(p['stop'])}" if p.get("stop") else "")
                           + (f" · rank {p['rank']}/{p['of']}" if p.get("rank") else "")
                           for p in buys[:12])[:1020]})
    if sells:
        fields.append({"name": f"{len(sells)} to close", "inline": False,
                       "value": "\n".join(f"**{p['symbol']}** — {p['reason']}" for p in sells[:12])[:1020]})
    if not buys and not sells:
        fields.append({"name": "Nothing to do", "inline": False,
                       "value": "No book's entry conditions were met at today's close, and nothing "
                                "held hit an exit. Standing pat is the signal."})
    held = sum(len(v) for v in (state.positions or {}).values())
    fields.append({"name": "Book", "inline": False,
                   "value": f"{held} position(s) open · equity {_money(state.equity)} "
                            f"({_pct(state.drawdown)} from peak) · gross {_pct0(state.gross)} of equity"})

    return {"title": "Stock desk — daily signals",
            "description": "\n".join(lines)[:4000],
            "color": 0xB3403A if state.crisis else 0x2E9B5B if buys else 0x8A8F98,
            "fields": fields[:5],
            "footer": {"text": "Paper only, research not advice. Rules in configs/stocks.yaml."},
            "timestamp": datetime.now(NY).isoformat(timespec="seconds")}
