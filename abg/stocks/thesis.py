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
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import pitch_plan as P
from . import rank as RK
from .live import BOOK_EDGE, BOOK_LABEL, BOOK_SHORT

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

VERDICT_COLOR = {"BUY": 0x2E9B5B, "ADD": 0x2E9B5B, "REBALANCE": 0x3F7FBF, "HOLD": 0x3F7FBF, "TRIM": 0xC98A2B,
                 "SELL": 0xB3403A, "WATCH": 0x8A8F98, "AVOID": 0x8A8F98}


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


def usd_s(x) -> str:
    return "—" if x is None else f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def _money(x) -> str:
    return "—" if x is None else f"${x:,.0f}"


def _regime_words(regime: str) -> str:
    return {"bull_trend": "a trending market", "bull_chop": "a choppy but rising market",
            "risk_off": "a risk-off market", "crisis": "crisis mode",
            "warmup": "not enough history yet"}.get(regime, regime)


# --------------------------------------------------------------------------- per-book narrative
def _exit_words(c) -> str:
    return (c.reason or "exit rule").replace("_", " ")


def _what_happened(c, f: dict) -> str:
    """One or two sentences naming the rule and the numbers behind it."""
    s = c.symbol
    if not c.is_entry and c.action == "SELL":
        return (f"The {BOOK_LABEL.get(c.book, c.book).lower()} book's exit fired on {s}: {_exit_words(c)}, "
                f"at the close of {_px(f.get('close') or c.price)}. The rule closes the position at the next "
                f"open; this is the trade ending on its own terms, not a new view on the company.")
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
    out = ["Research only. The desk pitches ideas; nothing here places an order."]
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
               news: list[dict] | None = None, provider: str | None = None, ctx: dict | None = None) -> dict:
    """Everything a memo, an embed or a deck needs for one candidate. Pure: no I/O, no secrets.

    ``ctx`` (optional, from the desk): ``trades`` (the replay's trades, for base rates), ``period``,
    ``bars`` (this symbol's daily bars), ``store``, ``open_pitches`` (the desk's own open pitches,
    ``{symbol: {book: row}}``), ``fund_holdings`` (``{symbol: weight}`` or None), ``sectors``,
    ``earnings`` (next report date, ISO), ``ml_threshold``.
    """
    ctx = ctx or {}
    pc = P.pitch_cfg(cfg)
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
    as_of = date.fromisoformat(state.date)
    exits = _exits(c, cfg)
    pack = {
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
        "price": c.price, "stop": c.stop,
        # retired with the paper book (docs/22.1): kept as null so old readers do not break
        "shares": None, "notional": None, "weight": None, "risk_pct": None, "r_dollars": None,
        # the historical replay's own order, for the ledger a ranker learns from; not a position
        "replay": {"shares": c.shares, "notional": c.notional, "weight": c.weight, "risk_pct": c.risk_pct},
        "atr": c.atr, "stop_atr": c.stop_atr,
        "max_hold": bp.get("max_hold"),
        "what_happened": _what_happened(c, f),
        "facts": f, "gates": c.gates,
        "exits": exits,
        "risks": _risks(c, state, cfg, val, fund),
        "caveats": _caveats(state, cfg),
        "valuation": val,
        "peers": [{k: x.get(k) for k in ("symbol", "pe", "forward_pe", "eps")} for x in (peers or [])
                  if isinstance(x, dict)],
        "charts": {},
        "news": [{"title": (x.get("title") or "")[:160], "url": x.get("url"),
                  "published": x.get("published") or x.get("datetime")} for x in (news or [])[:4]],
        "context": {"regime": state.regime, "regime_words": _regime_words(state.regime),
                    "crisis": state.crisis, "stress": state.stress,
                    "flags_on": [k for k, v in (state.flags or {}).items() if v],
                    "book_weight": (state.weights or {}).get(c.book),
                    "book_budget": None,
                    "size_mult": (bp.get("regimes") or {}).get(state.regime),
                    "vol_scale": state.vol_scale, "equity": None, "drawdown": None},
        "provider": provider,
        "reason": c.reason,
        # research context from the whole-market screen and the news store (docs/22.2-22.3); all optional
        "grades": ctx.get("grades"), "sentiment": ctx.get("sentiment"), "flags": ctx.get("flags") or [],
        "themes": ctx.get("themes") or [],
    }
    pack.update(_plan(c, state, cfg, pc, fund, val, as_of, exits, ctx))
    return pack


def _plan(c, state, cfg: dict, pc: dict, fund: dict | None, val: dict | None, as_of: date,
          exits: list[str], ctx: dict) -> dict:
    """The AIBG pitch fields (docs/21.6): recommendation, both horizons, entry, risk, size, fit, questions."""
    mine = (ctx.get("open_pitches") or {}).get(c.symbol) or {}
    rating = P.rating(c.action, c.shares, open_same=c.book in mine, open_other=any(b != c.book for b in mine))
    base = P.base_rates(ctx.get("trades"), c.book, ctx.get("period"), min_n=int(pc["min_base_trades"]))
    try:
        tgt = P.trade_targets(c, base, ctx.get("bars"), as_of) if c.is_entry else \
            {"book": c.book, "entry_ref": c.price, "stop": None, "t1": None, "t2": None, "t1_kind": None,
             "t2_kind": None, "t1_basis": None, "t2_basis": None, "review_date": None, "structural": {},
             "notes": []}
    except ValueError as e:                     # levels out of order: a data fault, so no targets, said so
        log.warning("targets for %s failed their check (%s)", c.symbol, e)
        tgt = {"book": c.book, "entry_ref": c.price, "stop": c.stop, "t1": None, "t2": None, "t1_kind": None,
               "t2_kind": None, "t1_basis": None, "t2_basis": None, "review_date": None, "structural": {},
               "notes": [f"No targets: the levels failed the order check ({e})."], "error": str(e)}
    try:
        inv = P.investment_view(val, c.price, fund, as_of, pc)
    except ValueError as e:                     # a units mismatch or a case that doesn't recompute
        log.warning("valuation view for %s failed its check (%s)", c.symbol, e)
        inv = P.investment_view(None, c.price, fund, as_of, pc)
        inv["reason"] = f"not shown: the valuation failed its check ({e})"
        inv["error"] = str(e)
    earnings = ctx.get("earnings")
    hold_days = c.book in ("trend_pullback", "mean_reversion") and (cfg.get("books") or {}).get(c.book, {}).get("max_hold")
    earnings_in_hold = bool(earnings and hold_days and
                            date.fromisoformat(earnings) <= as_of + timedelta(days=int(hold_days * 7 / 5) + 1))
    entry = P.entry_plan(c, as_of, earnings if earnings_in_hold or (earnings and c.is_entry) else None, pc)
    buyish = rating in ("BUY", "ADD")
    pw_up = inv.get("prob_upside")
    horizons_disagree = bool(buyish and pw_up is not None and pw_up < 0) or \
        bool(rating in ("SELL", "TRIM") and pw_up is not None and pw_up >= pc["valuation_agree_upside"])
    conv = P.conviction(gates=c.gates or {}, ml_prob=c.ml_prob, ml_threshold=ctx.get("ml_threshold"),
                        upside=inv.get("upside") if buyish else None, regime=state.regime,
                        flags_on=sum(1 for v in (state.flags or {}).values() if v),
                        earnings_in_hold=earnings_in_hold, has_fundamentals=bool(fund),
                        horizons_disagree=horizons_disagree,
                        small_sample=bool(base.get("n")) and base.get("small_sample", True), pcfg=pc,
                        news=ctx.get("conviction_news"))
    # the weight the book's own rule gives the name (replay order / replay capital): a ratio, not a position
    rule_w = (float(c.notional) / float(state.equity)) if c.notional and getattr(state, "equity", None) else None
    risk = P.risk_and_size(c, c.price, c.stop, tgt.get("t1"), pc.get("fund_equity"), conv["bucket"], pc,
                           rule_weight=rule_w)
    fit = None
    if ctx.get("store") is not None and c.is_entry:
        try:
            fit = P.fund_fit(c.symbol, (fund or {}).get("sector") or (ctx.get("sectors") or {}).get(c.symbol),
                             ctx["store"], ctx.get("fund_holdings"), ctx.get("sectors") or {},
                             risk.get("pct_of_fund"), pc, cfg.get("benchmark", "SPY"))
        except Exception as e:                     # fit is context; never lose the pitch over it
            log.info("fund fit failed for %s: %s", c.symbol, e)
    style = P.BOOK_STYLE.get(c.book, "").format(max_hold=(cfg.get("books") or {}).get(c.book, {}).get("max_hold", "?"))
    name = (fund or {}).get("name") or c.symbol
    hold_txt = (f"about {max(1, round((base.get('median_hold_days') or 0) / 5))} week(s)"
                if base.get("median_hold_days") else None)
    nxt = date.fromisoformat(entry["date"])
    if rating in ("BUY", "ADD") and c.price:
        one = (f"{'Buy' if rating == 'BUY' else 'Add to'} {c.symbol} at {nxt:%A}'s open near ${c.price:,.2f}. "
               f"{_why_short(c)}")
        if c.stop and tgt.get("t1"):
            one += (f" Risk ${c.price - c.stop:,.2f}/share to a ${tgt['t1']:,.2f} first target"
                    + (f" in {hold_txt}" if hold_txt else "") + ".")
        elif inv.get("prob_weighted"):
            one += f" 12-month probability-weighted target ${inv['prob_weighted']:,.2f}."
    elif rating in ("SELL", "TRIM"):
        one = f"{'Sell' if rating == 'SELL' else 'Trim'} {c.symbol} at {nxt:%A}'s open: {c.reason.replace('_', ' ')}."
    else:
        one = f"{rating.title()} {c.symbol}."
    if rating in ("SELL", "TRIM"):
        core = (f"{name}: {_why_short(c)} The book is built to {BOOK_EDGE.get(c.book, '')}; the trade it opened "
                f"is over by its own rules. This is the exit, not a view on the company.")
    else:
        core = (f"{name}: {_why_short(c)} The {BOOK_LABEL.get(c.book, c.book).lower()} book is built to "
                f"{BOOK_EDGE.get(c.book, '')}; this is that setup, not a forecast about the company.")
    conflict = None
    if horizons_disagree:
        conflict = (f"The rule says {rating}, while the 12-month probability-weighted target is {pw_up:+.1%} "
                    f"from here. Conviction is cut for it; treat this as a trade, sized as one.")
    exit_rev = P.exit_review(c, mine.get(c.book), base) if rating in ("SELL", "TRIM") else None
    if rating in ("SELL", "TRIM"):
        qs = P.exit_questions(c, exit_rev, base)
        wrong = P.exit_wrong_if(c)
    else:
        qs = P.questions(inv, tgt, conv, earnings if earnings_in_hold else None, fund, horizons_disagree, rating)
        wrong = P.wrong_if(c, exits)
    overview = {
        "market_cap": (fund or {}).get("market_cap"), "sector": (fund or {}).get("sector"),
        "industry": (fund or {}).get("industry"), "revenue_ttm": None,
        "pe_ntm": (fund or {}).get("forward_pe"), "pe_ltm": (fund or {}).get("pe"),
        "description": ((fund or {}).get("description") or "")[:1200] or None,
        "metrics": {k: v for k, v in {
            "EPS growth (TTM, YoY)": ((fund or {}).get("extra") or {}).get("eps_growth_ttm_yoy_pct"),
            "Gross margin": ((fund or {}).get("extra") or {}).get("gross_margin"),
            "Operating margin": ((fund or {}).get("extra") or {}).get("operating_margin"),
            "Net margin": (fund or {}).get("profit_margin"),
            "Dividend yield": (fund or {}).get("dividend_yield"),
            "Beta": (fund or {}).get("beta"),
            "52-week range": ([(fund or {}).get("week52_low"), (fund or {}).get("week52_high")]
                              if (fund or {}).get("week52_high") else None),
        }.items() if v is not None},
        "source": ctx.get("fund_provider"), "as_of": as_of.isoformat(),
    }
    return {
        "rec": {"rating": rating, "action_line": P.ACTION_LINE.get(rating, ""), "core_thesis": core,
                "open_pitch": (mine.get(c.book) or next(iter(mine.values()), {})).get("id"),
                "daisy_rank": RK.daisy_rank(rating, conv.get("bucket")),
                "one_liner": one, "conviction": conv, "conflict": conflict,
                "method": {"book": c.book, "label": BOOK_LABEL.get(c.book, c.book),
                           "edge": BOOK_EDGE.get(c.book, ""), "style": style}},
        "plan": {"entry": entry, "targets": tgt, "investment": inv, "risk": risk, "fit": fit,
                 "wrong_if": wrong, "questions": qs, "base_rates": base,
                 "earnings_in_hold": earnings_in_hold, "horizons_disagree": horizons_disagree,
                 "exit_review": exit_rev,
                 "max_hold": (cfg.get("books") or {}).get(c.book, {}).get("max_hold"),
                 "settings": {k: pc[k] for k in ("entry_zone_atr", "min_rr", "glossary", "risk_per_trade",
                                                 "max_position_pct", "scenario_weights")}},
        "overview": overview,
        "writeup": None,
    }


def _why_short(c) -> str:
    f = c.facts or {}
    if c.action == "SELL":
        return f"The {BOOK_LABEL.get(c.book, c.book).lower()} book's exit fired ({_exit_words(c)})."
    return {
        "trend_pullback": "A pullback in an established uptrend just ended.",
        "mean_reversion": "A sharp sell-off in a long-term uptrend has it deeply oversold.",
        "xs_momentum": "It ranks among the strongest risk-adjusted 12-month trends in the market.",
        "dual_momentum": "It is one of the strongest sectors or markets while the market switch is risk-on.",
        "crisis_alpha": "Crisis mode is on and this is the defensive sleeve.",
    }.get(c.book, (c.reason or "").replace("_", " ") + ".") if f is not None else ""


# --------------------------------------------------------------------------- markdown
def _v(x, fmt=_px) -> str:
    return P.NOT_IN_DATA if x is None else fmt(x)


def _cap(x) -> str:
    if x is None:
        return P.NOT_IN_DATA
    return f"${x / 1e12:,.2f} T" if x >= 1e12 else f"${x / 1e9:,.2f} B" if x >= 1e9 else f"${x / 1e6:,.0f} M"


from .glossary import memo_terms as _memo_terms  # noqa: E402

GLOSSARY = _memo_terms()          # one glossary, shared with the dashboard (abg/stocks/glossary.py)


def render_md(pack: dict, note: str | None = None) -> str:
    """The AIBG pitch layout: cover line, TL;DR, 01 Overview, 02 Thesis, 03 Data, 04 Recommendation."""
    p = pack
    rec, pl, ov = p.get("rec") or {}, p.get("plan") or {}, p.get("overview") or {}
    ctx = p["context"]
    inv, tgt, risk, ent = pl.get("investment") or {}, pl.get("targets") or {}, pl.get("risk") or {}, pl.get("entry") or {}
    conv = rec.get("conviction") or {}
    rating = rec.get("rating") or p["action"]
    name = p.get("name") or p["symbol"]
    w = p.get("writeup") or {}
    cfgp = pl.get("settings") or {}

    head = f"{rating} {p['symbol']}" + (f" — {name}" if name != p["symbol"] else "")
    bits = [head, rec.get("action_line") or ""]
    if conv and rating not in ("SELL", "TRIM"):
        bits.append(f"{conv['bucket']} conviction ({conv['score']})")
    elif rating in ("SELL", "TRIM"):
        bits.append("rule exit")
    if inv.get("prob_weighted") is not None:
        bits.append(f"12-mo target {_px(inv['prob_weighted'])} ({_pct(inv['prob_upside'])}"
                    + (f", total return {_pct(inv['total_return'])}" if inv.get("total_return") is not None else "") + ")")
    if tgt.get("t1") or p.get("stop"):
        bits.append("Trade " + ", ".join(x for x in (
            f"T1 {_px(tgt['t1'])}" if tgt.get("t1") else "",
            f"stop {_px(p['stop'])}" if p.get("stop") else "",
            f"R:R {risk['rr']:.1f}" if risk.get("rr") else "") if x))
    bits.append(f"Price {_px(p['price'])} as of {p['as_of']}")
    L = [f"# {head}", "", "**" + " | ".join(b for b in bits[1:] if b) + "**", "",
         f"*{p['book_label']}" + (f" · ranked {p['rank']} of {p['of']} that qualified today" if p.get("rank") else "")
         + " · signal from the close, to be acted on at the next open*", ""]
    if rec.get("conflict"):
        L += [f"> **Views disagree.** {rec['conflict']}", ""]

    # TL;DR
    L += ["**TL;DR**", "", f"- **Do:** {rec.get('one_liner') or p['what_happened']}",
          f"- **Why now:** {p['what_happened']}"]
    if risk.get("risk_pct_of_fund") is not None and tgt.get("t1"):
        at_risk = (f"{_money(risk['risk_dollars'])} at risk ({_pct0(risk['risk_pct_of_fund'], 2)} of the fund)"
                   if risk.get("risk_dollars") is not None else f"{_pct0(risk['risk_pct_of_fund'], 2)} of the fund at risk")
        L.append(f"- **Risk vs expect:** {at_risk} to the stop, for a first target {_pct((tgt['t1'] / p['price'] - 1) if p.get('price') else None)} away"
                 + (f" (R:R {risk['rr']:.1f})" if risk.get("rr") else "") + ".")
    elif inv.get("prob_upside") is not None:
        L.append(f"- **Risk vs expect:** {_pct(inv['prob_upside'])} to the 12-month probability-weighted target; "
                 f"bear case {_pct((inv.get('cases') or {}).get('bear', {}).get('upside'))}.")
    L.append("")

    # 01 overview
    L += ["## 01 Company Overview", "",
          "| Market cap | Sector | Revenue (TTM) | P/E (NTM) | P/E (LTM) |", "|---|---|---|---|---|",
          f"| {_cap(ov.get('market_cap'))} | {ov.get('sector') or P.NOT_IN_DATA} | {P.NOT_IN_DATA} | "
          f"{_v(ov.get('pe_ntm'), lambda x: f'{x:.1f}x')} | {_v(ov.get('pe_ltm'), lambda x: f'{x:.1f}x')} |", ""]
    L += ["**What they do**", "", w.get("what_they_do") or (ov.get("description") or P.NOT_IN_DATA), ""]
    sentences = [x.strip().rstrip(".") + "." for x in re.split(r"(?<=\.)\s+(?=[A-Z])", p["what_happened"]) if x.strip()]
    why = w.get("why_now") or sentences[:3] + [f"Headline: {n['title']}" for n in (p.get("news") or [])[:1]]
    L += ["**Why now**", ""] + [f"- {x}" for x in (why if isinstance(why, list) else [why])] + [""]
    L += ["**Competitive moat**", "", w.get("moat") or P.NOT_IN_DATA, ""]
    mets = ov.get("metrics") or {}
    L += ["**Key financial metrics**", ""]
    if mets:
        for k, v in mets.items():
            if k == "52-week range":
                L.append(f"- {k}: {_px(v[0])} – {_px(v[1])}")
            elif k == "Beta":
                L.append(f"- {k}: {v:.2f}")
            elif k == "EPS growth (TTM, YoY)":
                L.append(f"- {k}: {v:+.1f}%")
            else:
                L.append(f"- {k}: {_pct0(v, 1)}")
    else:
        L.append(f"- {P.NOT_IN_DATA}")
    L += [f"- Revenue, free cash flow, net debt/EBITDA: {P.NOT_IN_DATA}", ""]

    # 02 thesis
    L += ["## 02 Investment Thesis", "", "### The thesis — core statement", "",
          w.get("core_thesis") or rec.get("core_thesis") or p["what_happened"], "",
          f"The book is harvesting a documented behaviour: {p['edge']}. "
          f"That is the whole reason this trade exists — it is not a forecast about {p['symbol']}.", ""]
    cases = inv.get("cases") or {}
    bull = w.get("bull_case") or (f"Bull target {_px(cases['bull']['value'])} ({_pct(cases['bull']['upside'])}): "
                                  f"earnings grow {cases['bull']['growth'] * 100:.1f}% a year." if "bull" in cases else
                                  (f"Trade target T2 {_px(tgt['t2'])}." if tgt.get("t2") else P.NOT_IN_DATA))
    bear = w.get("bear_case") or (f"Bear value {_px(cases['bear']['value'])} ({_pct(cases['bear']['upside'])}); "
                                  f"the trade's stop at {_px(p.get('stop'))} limits the loss well before that."
                                  if "bear" in cases else
                                  (f"The stop at {_px(p['stop'])} ({_pct(risk.get('stop_pct'))}) caps the loss."
                                   if p.get("stop") else P.NOT_IN_DATA))
    L += ["**Bull case**", "", bull, "", "**Bear case**", "", bear, ""]
    if inv.get("available"):
        L += ["### Valuation cross-check and price targets", "",
              f"12-month view, horizon {inv['horizon']}. Each line shows its arithmetic:", ""]
        L += [f"- {x}" for x in inv.get("lines") or []]
        if cases:
            L += ["", "| Case | Value | vs price |", "|---|---|---|"]
            for k in ("bear", "base", "bull"):
                if k in cases:
                    L.append(f"| {k.title()} | {_px(cases[k]['value'])} | {_pct(cases[k]['upside'])} |")
        extra = []
        if inv.get("dividend_yield") is not None:
            extra.append(f"dividend yield {_pct0(inv['dividend_yield'], 1)}")
        if inv.get("total_return") is not None:
            extra.append(f"total return to the probability-weighted target {_pct(inv['total_return'])}")
        if inv.get("street_target"):
            extra.append(f"street mean target {_px(inv['street_target'])} ({_pct(inv['street_upside'])}) — reference only")
        if inv.get("implied_growth") is not None:
            extra.append(f"the price implies {_pct(inv['implied_growth'])} a year of earnings growth"
                         + (f" against our {_pct(inv['growth'])}" if inv.get("growth") is not None else ""))
        if extra:
            L += ["", "; ".join(extra).capitalize() + "."]
        if w.get("valuation_view"):
            L += ["", w["valuation_view"]]
        L += ["", "*Valuation is context. The rule above is what was tested and what decides the trade.*", ""]
    else:
        why = inv.get("reason") if inv.get("error") else f"{P.NOT_IN_DATA} (no fundamentals for this name)"
        L += ["### Valuation / price target", "", f"12-month targets: {why}.", ""]
    L += ["### Current events and risks", ""]
    risks_w = w.get("risks")
    for r in (risks_w if isinstance(risks_w, list) and risks_w else p["risks"]):
        L.append(f"- {r}")
    if p.get("flags"):
        L += ["", "Flags from the news and filings (each with its source):", ""]
        for fl in p["flags"]:
            L.append(f"- ⚑ {fl['text']}" + (f" ([source]({fl['url']}))" if fl.get("url") else ""))
    sn = p.get("sentiment") or {}
    if sn.get("name") or sn.get("market"):
        nm = sn.get("name") or {}
        bits = []
        if nm.get("level") is not None:
            bits.append(f"news sentiment {nm['level']:+.2f} over {nm.get('n', 0)} article(s) in 3 days"
                        + (f", {nm['change']:+.2f} against its 20-day baseline" if nm.get("change") is not None else ""))
        if sn.get("market"):
            bits.append(f"market mood {sn['market']}")
        line = "; ".join(bits)
        L += ["", "*" + line[:1].upper() + line[1:] + ". Scores are computed from stored headlines; they inform conviction and never gate the rule.*"]
    if p.get("news"):
        L += ["", "Recent headlines:", ""]
        for n in p["news"]:
            L.append(f"- [{n['title']}]({n['url']})" if n.get("url") else f"- {n['title']}")
    L.append("")

    # 03 data
    L += ["## 03 Data & Supporting Analysis", ""]
    charts = p.get("charts") or {}
    alt = {"price": "Price with the 50/200-day EMAs, the entry zone, stop, T1/T2 and the 12-month targets",
           "valuation": "Valuation football field: 52-week range, DCF bear-bull, peer comps, street, weighted",
           "peers": "Peer comps table"}
    if charts:
        for k, path in charts.items():
            L += [f"![{alt.get(k, k)}]({path})", ""]
    else:
        L.append("Charts: none for this pitch (no bars, or matplotlib is not installed: `pip install .[charts]`).")
    base = pl.get("base_rates") or {}
    if base.get("n"):
        L += ["", f"**How this book has done** ({base['source']}, {base['period'][0]} to {base['period'][1]}, "
                  f"{base['n']} trades{' — small sample' if base.get('small_sample') else ''}): win rate "
                  f"{_pct0(base.get('win_rate'))}, average win {_n(base.get('avg_win_R'), 2)}R, average loss "
                  f"{_n(base.get('avg_loss_R'), 2)}R, expectancy {_n(base.get('expectancy_R'), 3)}R per trade, "
                  f"median hold {('under a day' if (base.get('median_hold_days') or 0) < 1 else _n(base.get('median_hold_days'), 0) + ' days')}. In-sample: the rules were not fitted on "
                  f"this window, but it is not a walk-forward record either."]
    elif base.get("note"):
        L += ["", f"Base rates: {base['note']}."]
    L.append("")

    # 04 recommendation
    L += ["## 04 Recommendation", "", f"### {rating} — {rec.get('action_line', '')}"
          + (f" · {conv['bucket']} conviction ({conv['score']}/100)" if conv and rating not in ("SELL", "TRIM")
             else " · rule exit" if rating in ("SELL", "TRIM") else ""), "",
          f"**Method:** {rec.get('method', {}).get('label', p['book_label'])} — {rec.get('method', {}).get('style', '')}.", "",
          "### The trade plan", "", "| | |", "|---|---|",
          f"| Action | **{rating}** {ent.get('tested', 'at the next open')} "
          f"({'tested exit' if rating in ('SELL', 'TRIM') else 'tested entry'}) |",
          f"| Reference price | {_px(p['price'])} ({ent.get('reference_kind', 'last close')}) |"]
    if ent.get("zone"):
        L.append(f"| Entry zone | {_px(ent['zone'][0])} – {_px(ent['zone'][1])} (close ± {cfgp.get('entry_zone_atr', 0.5):g} ATR) |")
    if p.get("stop") is not None:
        L.append(f"| Initial stop | {_px(p['stop'])} ({_pct(risk.get('stop_pct'))}, {_n(p['stop_atr'])}× ATR of {_px(p['atr'])}) |")
    for k in ("t1", "t2"):
        if tgt.get(k):
            L.append(f"| {k.upper()} | {_px(tgt[k])} ({_pct(tgt[k] / p['price'] - 1)}) — {tgt[f'{k}_kind']}: {tgt[f'{k}_basis']} |")
    if tgt.get("review_date"):
        L.append(f"| Next review | {tgt['review_date']} (month-end rebalance) |")
    if inv.get("prob_weighted") is not None:
        L.append(f"| 12-month targets | bear {_px(cases.get('bear', {}).get('value'))} · base {_px(cases.get('base', {}).get('value'))} · "
                 f"bull {_px(cases.get('bull', {}).get('value'))} · weighted {_px(inv['prob_weighted'])} (reference, by {inv['horizon']}) |")
    if risk.get("rr"):
        L.append(f"| Reward:risk | {risk['rr']:.2f}{' — below the ' + format(cfgp.get('min_rr', 1.5), 'g') + ' minimum' if risk.get('rr_flag') else ''} |")
    if risk.get("pct_of_fund") and rating not in ("SELL",):
        label = "Suggested add" if rating == "ADD" else "Suggested size"
        if risk.get("shares"):
            L.append(f"| {label} | {_pct0(risk['pct_of_fund'], 1)} of the fund = {risk['shares']:,} "
                     f"share{'s' if risk['shares'] != 1 else ''}, {_money(risk['notional'])} ({risk['bound']}) |")
        else:
            L.append(f"| {label} | {_pct0(risk['pct_of_fund'], 1)} of the fund ({risk['bound']}) |")
    if risk.get("risk_pct_of_fund") is not None:
        L.append(f"| At risk | {_pct0(risk['risk_pct_of_fund'], 2)} of the fund"
                 + (f" = {_money(risk['risk_dollars'])}" if risk.get("risk_dollars") is not None else "") + " |")
    if (risk.get("small_account") or {}).get("shares") is not None:
        L.append(f"| $10k account risking 1% | {risk['small_account']['shares']} shares |")
    if pl.get("max_hold"):
        L.append(f"| Max hold | {pl['max_hold']} trading days |")
    if p.get("ml_prob") is not None:
        L.append(f"| Model's odds of reaching target before stop | {_pct0(p['ml_prob'])} |")
    L.append("")
    for t in tgt.get("notes") or []:
        L.append(f"*{t}*")
    if ent.get("refinements"):
        L += ["", "**Conditional entries — untested refinements** (the scorecard tracks them apart from the tested entry):", ""]
        L += [f"- {r['rule'].capitalize()}: {r['text']}" for r in ent["refinements"]]
    L.append("")
    ex = pl.get("exit_review")
    if rating in ("SELL", "TRIM"):
        L += ["### Why we're exiting", "", f"The rule's exit fired: {p['reason'].replace('_', ' ')}.", ""]
        if ex:
            L += [f"- Pitched {ex.get('pitched')}, tested entry {_px(ex['entry'])} at the {ex['entry_date']} open, "
                  f"now {_px(ex['price'])}: {_pct(ex['return'])}"
                  + (f", {ex['r']:+.2f}R" if ex.get("r") is not None else "")
                  + (f", held {ex['days_held']} sessions" if ex.get("days_held") is not None else "")]
            if ex.get("lesson"):
                L.append(f"- What it tells us: {ex['lesson']}")
        else:
            L.append(f"- Result: {P.NOT_IN_DATA} (we have no open pitch on this name; the exit is the rule's, from its historical replay)")
        L.append("")
    L += ["### What would make this wrong", "", "We're wrong if:", ""] + [f"- {x}" for x in pl.get("wrong_if") or p["exits"]]
    L.append("")
    fit = pl.get("fit")
    L += ["### Why this fits our fund", ""]
    if w.get("why_it_fits"):
        L += [w["why_it_fits"], ""]
    if fit:
        if fit.get("adds"):
            L += [f"It adds {fit['adds']}.", ""]
        prov = fit.get("holdings_provided")
        L += [f"- Sector ({fit['sector']}): {_pct0(fit['sector_before'], 1)} of the fund before, "
              f"{_pct0(fit['sector_after'], 1)} after" if fit.get("sector_before") is not None else f"- Sector: {fit['sector']}",
              (f"- 60-day correlation: {_n(fit['corr_holdings'], 2)} to the fund's holdings, " if prov else "- 60-day correlation: ")
              + f"{_n(fit['corr_benchmark'], 2)} to the benchmark",
              f"- Beta: {_n(fit['beta'], 2)}" + (f" ({fit['beta_source']})" if fit.get("beta_source") else ""),
              (f"- Already held by the fund: {'yes' if fit['already_held'] else 'no'} · AI-infrastructure name: "
               f"{'yes' if fit['ai_infrastructure'] else 'no'} (the fund holds {len(fit['ai_names_held'] or [])} today)"
               if prov else
               f"- Fund holdings: holdings not provided (add them in Settings) · AI-infrastructure name: "
               f"{'yes' if fit['ai_infrastructure'] else 'no'}")]
    elif rating in ("SELL", "TRIM"):
        L.append("An exit frees the capital; fit is a question for the next entry, not this one.")
    else:
        L.append(P.NOT_IN_DATA)
    L += ["", "### Questions for the team", ""]
    for i, q in enumerate(w.get("questions") or pl.get("questions") or [], 1):
        L.append(f"{i}. {q}")
    if rating not in ("SELL", "TRIM"):
        L += ["", f"**Conviction {conv.get('score', '—')}** = " + " ".join(
            f"{'+' if b['points'] >= 0 and i else ''}{b['points']:g} ({b['input']})"
            for i, b in enumerate(conv.get("breakdown") or [])), ""]
    else:
        L.append("")

    # bottom matter
    bullets = [
        f"- Regime: **{ctx['regime']}** ({ctx['regime_words']})"
        + (f", book size ×{ctx['size_mult']:g}" if ctx.get("size_mult") is not None else ""),
        f"- Stress score {ctx['stress']:.2f}"
        + (f", {len(ctx['flags_on'])} early-warning flag(s) on" if ctx["flags_on"] else ", no early-warning flags on")
        + (" — **crisis mode**" if ctx["crisis"] else ""),
    ]
    if ctx.get("book_weight") is not None:
        bullets.append(f"- This book currently has {_pct0(ctx['book_weight'])} of capital "
                       f"({_money(ctx['book_budget'])}), set by the Hedge allocator from how the books have been doing lately")
    L += ["## Market context", ""] + bullets + [""]
    if p["gates"]:
        L += ["## Every check the rule made", ""] + [f"- [{'x' if ok else ' '}] {n}" for n, ok in p["gates"].items()] + [""]
    L += ["## Sources", "",
          f"- Prices: {p.get('provider') or 'bar store'}, daily bars to the {p['as_of']} close",
          f"- Fundamentals: {ov.get('source') or (P.NOT_IN_DATA if not ov.get('market_cap') else 'provider')}, as of {ov.get('as_of')}",
          f"- Base rates: {base.get('source', P.NOT_IN_DATA)}" + (f", {base['period'][0]} to {base['period'][1]}" if base.get('period') else ""),
          "- Rules: configs/stocks.yaml", ""]
    if cfgp.get("glossary", True):
        L += ["## Glossary", ""] + [f"- **{k}** — {v}" for k, v in GLOSSARY] + [""]
    L += ["---", ""] + [f"*{cv}*" for cv in p["caveats"]]
    if note:
        L += ["", note]
    L += ["", "*Research, not advice.*"]
    return "\n".join(L)


# --------------------------------------------------------------------------- Discord
DISCORD = {"title": 256, "description": 4096, "fields": 25, "field_name": 256, "field_value": 1024,
           "footer": 2048, "total": 6000}


def _fit_embed(e: dict) -> dict:
    """Trim an embed to Discord's limits, total included."""
    e["title"] = (e.get("title") or "")[:DISCORD["title"]]
    e["description"] = (e.get("description") or "")[:DISCORD["description"]]
    e["fields"] = [{**f, "name": f["name"][:DISCORD["field_name"]], "value": (f["value"] or "—")[:DISCORD["field_value"]]}
                   for f in (e.get("fields") or [])[:DISCORD["fields"]]]
    if e.get("footer"):
        e["footer"] = {"text": e["footer"]["text"][:DISCORD["footer"]]}

    def total(x):
        return len(x["title"]) + len(x["description"]) + sum(len(f["name"]) + len(f["value"]) for f in x["fields"]) \
            + len((x.get("footer") or {}).get("text", ""))
    while total(e) > DISCORD["total"] and e["fields"]:
        e["fields"].pop()
    if total(e) > DISCORD["total"]:
        e["description"] = e["description"][:max(0, DISCORD["total"] - total({**e, "description": ""}))]
    return e


def embed(pack: dict, note: str = "Full write-up on the dashboard (Stocks tab).") -> dict:
    """One pitch as a Discord embed: TL;DR up top, then Plan, 12-mo view, Why, We're wrong if, Timing."""
    p = pack
    rec, pl = p.get("rec") or {}, p.get("plan") or {}
    inv, tgt, risk, ent = pl.get("investment") or {}, pl.get("targets") or {}, pl.get("risk") or {}, pl.get("entry") or {}
    conv = rec.get("conviction") or {}
    rating = rec.get("rating") or p["action"]
    title = f"{rating} {p['symbol']} — {BOOK_SHORT.get(p['book'], p['book_label'])}"
    if conv and rating not in ("SELL", "TRIM"):
        title += f" · {conv['bucket']} conviction"
    desc = rec.get("one_liner") or p["what_happened"]
    if rec.get("conflict"):
        desc += f"\n\n⚠️ {rec['conflict']}"
    plan = [f"**Entry** {ent.get('tested', 'next open')} · ref {_px(p['price'])}"]
    if p.get("stop") is not None:
        plan.append(f"**Stop** {_px(p['stop'])} ({_pct(risk.get('stop_pct'))})")
    t = " · ".join(f"**{k.upper()}** {_px(tgt[k])}" for k in ("t1", "t2") if tgt.get(k))
    if t:
        plan.append(t + (f" · **R:R** {risk['rr']:.1f}" if risk.get("rr") else ""))
    if risk.get("pct_of_fund") is not None:
        plan.append(f"**Size** {_pct0(risk['pct_of_fund'], 1)} of fund ("
                    + (f"{risk['shares']:,} sh, " if risk.get("shares") else "") + f"{risk.get('bound')})")
    fields = [{"name": "Plan", "value": "\n".join(plan), "inline": False}]
    cases = inv.get("cases") or {}
    if cases or inv.get("comps"):
        fields.append({"name": "12-mo view", "inline": True, "value": "\n".join(x for x in (
            " · ".join(f"{k} {_px(cases[k]['value'])}" for k in ("bear", "base", "bull") if k in cases),
            f"weighted {_px(inv['prob_weighted'])} ({_pct(inv['prob_upside'])})" if inv.get("prob_weighted") else "",
            f"total return {_pct(inv['total_return'])}" if inv.get("total_return") is not None else "",
        ) if x) or P.NOT_IN_DATA})
    why = [p["what_happened"].split(". ")[0].rstrip(".") + "."]
    if conv:
        why.append(f"Conviction {conv['score']}/100")
    fields.append({"name": "Why", "inline": True, "value": "\n".join(f"• {x}" for x in why[:3])})
    fields.append({"name": "We're wrong if", "inline": False,
                   "value": "\n".join(f"• {x}" for x in (pl.get("wrong_if") or p["exits"])[:4])})
    timing = [f"Entry {ent.get('date', '—')}"]
    if tgt.get("review_date"):
        timing.append(f"review {tgt['review_date']}")
    if pl.get("max_hold"):
        timing.append(f"max hold {pl['max_hold']} trading days")
    if pl.get("earnings_in_hold") and ent.get("earnings"):
        timing.append(f"⚠️ earnings {ent['earnings']} inside the hold")
    fields.append({"name": "Timing", "inline": False, "value": " · ".join(timing)})
    e = {"title": title, "description": desc,
         "color": VERDICT_COLOR.get(rating, VERDICT_COLOR["WATCH"]), "fields": fields,
         "footer": {"text": f"{p['caveats'][0]} {note}"}, "timestamp": p["written_at"]}
    return _fit_embed(e)


def digest_embed(packs: list[dict], state, cfg: dict, book: dict | None = None) -> dict:
    """The one embed that leads a daily post: what the desk sees and what it wants to do."""
    def rating(p):
        return (p.get("rec") or {}).get("rating") or p["action"]
    buys = [p for p in packs if rating(p) in ("BUY", "ADD")]
    trims = [p for p in packs if rating(p) in ("SELL", "TRIM")]
    buys.sort(key=lambda p: -((p.get("rec") or {}).get("conviction") or {}).get("score", 0))
    lines = [f"**{state.date}** · regime **{state.regime}** ({_regime_words(state.regime)}) · "
             f"stress {state.stress:.2f}" + (" · **CRISIS MODE**" if state.crisis else "")]
    on = [k for k, v in (state.flags or {}).items() if v]
    if on:
        lines.append(f"Early-warning flags on: {', '.join(on)}")
    if state.weights:
        w = ", ".join(f"{BOOK_SHORT.get(k, k)} {_pct0(v)}"
                      for k, v in sorted(state.weights.items(), key=lambda kv: -(kv[1] or 0)))
        lines.append(f"Rule weights by book: {w}")

    def buy_line(p):
        pl = p.get("plan") or {}
        tg, rk, cv = pl.get("targets") or {}, pl.get("risk") or {}, (p.get("rec") or {}).get("conviction") or {}
        s = f"**{rating(p)} {p['symbol']}** {_px(p['price'])} · {BOOK_SHORT.get(p['book'], p['book'])}"
        if p.get("stop"):
            s += f" · stop {_px(p['stop'])}"
        if tg.get("t1"):
            s += f" · T1 {_px(tg['t1'])}"
        if rk.get("rr"):
            s += f" · R:R {rk['rr']:.1f}"
        if cv:
            s += f" · {cv['bucket'].lower()} ({cv['score']})"
        return s
    fields = []
    if buys:
        fields.append({"name": f"{len(buys)} to open or add (by conviction)", "inline": False,
                       "value": "\n".join(buy_line(p) for p in buys[:12])})
    if trims:
        fields.append({"name": f"{len(trims)} to close or trim", "inline": False,
                       "value": "\n".join(f"**{rating(p)} {p['symbol']}** — {p['reason'].replace('_', ' ')}"
                                          for p in trims[:12])})
    if not buys and not trims:
        fields.append({"name": "Nothing to do", "inline": False,
                       "value": "No book's entry conditions were met at today's close, and nothing "
                                "pitched hit an exit. Standing pat is the signal."})
    if book:
        fields.append({"name": "Pitch book", "inline": False,
                       "value": f"{book.get('active', 0)} active · {book.get('new', 0)} new, entering at the next open"
                                f" · {book.get('closed_today', 0)} closed today · {book.get('closed', 0)} closed in all"})
    return _fit_embed({"title": "Stock desk — daily signals", "description": "\n".join(lines),
                       "color": 0xB3403A if state.crisis else 0x2E9B5B if buys else 0x8A8F98,
                       "fields": fields[:5],
                       "footer": {"text": "Research, not advice. Rules in configs/stocks.yaml."},
                       "timestamp": datetime.now(NY).isoformat(timespec="seconds")})
