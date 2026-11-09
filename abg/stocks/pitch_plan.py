"""The decision layer of a pitch: recommendation, entry, targets, risk, size, fit and questions.

Every number a pitch shows is computed here, in Python, from the candidate the rule produced and the
data in the pack - never by a language model, and never changing what the rule decided:

* the **rating** is a translation of the rule's action (BUY/ADD/TRIM/SELL), not a second opinion;
* the **entry** is the tested one (the next open); anything else is labelled an *untested refinement*;
* **trade targets** come from the book's own replayed trades (in R), or from its actual exit level;
* **12-month targets** come from the valuation, with each line's arithmetic shown and re-checked;
* **conviction** is a transparent sum: every input that moved it is listed with its points;
* **size** follows from risk to the stop, scaled by conviction and capped - and says which bound it.

Missing data is written as ``None`` and shown as "not in data". Nothing here guesses.
Tunables live under ``pitch:`` in configs/stocks.yaml (defaults below, same names).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

NOT_IN_DATA = "not in data"

DEFAULTS: dict = {
    "entry_zone_atr": 0.5,          # entry zone = last close +/- this x ATR
    "chase_atr": 1.0,               # untested: skip a long that opens more than this x ATR above the close
    "earnings_days": 10,            # earnings within this many calendar days of entry = earnings-aware option
    "min_rr": 1.5,                  # reward:risk below this is flagged
    "risk_per_trade": 0.005,        # share of the fund risked to the stop, before the conviction scale
    "max_position_pct": 0.10,       # cap on one position as a share of the fund (analysts propose 5-12%)
    "fund_equity": None,            # the fund's size in $; null = the desk's paper book equity
    "size_scale": {"High": 1.0, "Medium": 0.75, "Low": 0.5},
    "scenario_weights": {"bear": 0.25, "base": 0.50, "bull": 0.25},
    "valuation_agree_upside": 0.15, # 12-mo upside at/above this = valuation agrees with a buy
    "horizon_months": 12,
    "conviction": {                 # points added to a start of 50, then clamped to 0..100
        "start": 50, "gates_all": 10, "gates_missing": -15, "ml_above": 10, "ml_below": -10,
        "valuation_agrees": 10, "valuation_disagrees": -8, "horizons_disagree": -7,
        "regime": {"bull_trend": 5, "bull_chop": 0, "risk_off": -10, "crisis": -15, "warmup": -5},
        "flag_each": -3, "flags_max": -12, "earnings_in_hold": -10, "no_fundamentals": -5,
        "small_sample": -5,
    },
    "buckets": {"High": 70, "Medium": 45},
    "min_base_trades": 30,          # fewer replayed trades than this = small-sample warning
    "ai_infrastructure": [          # the fund's AI focus; edit freely - membership is a tag, not a signal
        "NVDA", "AVGO", "AMD", "TSM", "ASML", "MU", "ARM", "MRVL", "ANET", "AMAT", "LRCX", "KLAC", "CDNS",
        "SNPS", "SMCI", "DELL", "HPE", "VRT", "ETN", "PWR", "GEV", "CEG", "VST", "NEE", "TLN", "EQIX",
        "DLR", "ORCL", "MSFT", "AMZN", "GOOGL", "GOOG", "META", "CRWV", "NBIS", "COHR", "CIEN", "GLW",
    ],
    "glossary": True,
}

BOOK_STYLE = {
    "trend_pullback": "swing trade, held days to a few weeks (at most {max_hold} trading days)",
    "mean_reversion": "short swing, held a few days (at most {max_hold} trading days)",
    "xs_momentum": "monthly-rebalanced position, reviewed at each month-end",
    "dual_momentum": "monthly ETF rotation, reviewed at each month-end",
    "crisis_alpha": "crisis hedge, held while crisis mode lasts",
}
ACTION_LINE = {"BUY": "Initiate position", "ADD": "Add to position", "HOLD": "Hold position",
               "TRIM": "Trim position", "SELL": "Close position", "WATCH": "Watch - setup forming, not triggered",
               "AVOID": "Do not initiate"}
VERDICT_TO_RATING = {"LONG": "BUY", "WATCH": "WATCH", "PASS": "AVOID"}


def pitch_cfg(cfg: dict | None) -> dict:
    """``DEFAULTS`` overlaid with ``cfg['pitch']`` (one level of nesting merged)."""
    out = {k: (dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v))
           for k, v in DEFAULTS.items()}
    for k, v in ((cfg or {}).get("pitch") or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            for kk, vv in v.items():
                merged[kk] = {**merged[kk], **vv} if isinstance(vv, dict) and isinstance(merged.get(kk), dict) else vv
            out[k] = merged
        else:
            out[k] = v
    return out


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _r(x, d=4):
    v = _f(x)
    return None if v is None else round(v, d)


# --------------------------------------------------------------------------- calendar
def next_trading_day(d: date) -> date:
    """The next NYSE session after ``d`` (weekends and exchange holidays skipped)."""
    from abg.live.market_hours import is_trading_day
    x = d + timedelta(days=1)
    for _ in range(15):
        if is_trading_day(x):
            return x
        x += timedelta(days=1)
    return x                                                   # pragma: no cover


def next_month_end_session(d: date) -> date:
    """The last trading day of the month after ``d``'s session (the momentum books' next review)."""
    from abg.live.market_hours import is_trading_day
    y, m = (d.year, d.month)
    # if d is already the month's last session, the review is next month's
    probe = d + timedelta(days=1)
    while not is_trading_day(probe):
        probe += timedelta(days=1)
    if probe.month != d.month:
        y, m = probe.year, probe.month
    last = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    while not is_trading_day(last):
        last -= timedelta(days=1)
    return last


# --------------------------------------------------------------------------- rating
def rating(action: str, shares: float | None = None, book_held: float | None = None,
           fund_held: float | None = None) -> str:
    """The rule's action in the fund's vocabulary.

    Books keep separate sleeves, so the *direction* comes from the order against the same book's
    holding (a REBALANCE to 1 share in a book that holds none is a purchase, even if another book
    already owns the name), and BUY vs ADD comes from what the fund holds in total.
    """
    a = (action or "").upper()
    fund_held = fund_held or 0.0
    book_held = book_held or 0.0
    if a == "BUY":
        return "ADD" if fund_held > 0 else "BUY"
    if a == "SELL":
        return "SELL" if fund_held - book_held <= 0 else "TRIM"
    if a == "REBALANCE":
        if shares is None:
            return "HOLD"
        if shares > book_held:
            return "ADD" if fund_held > 0 else "BUY"
        if shares < book_held:
            return "TRIM" if shares > 0 or fund_held - book_held > 0 else "SELL"
        return "HOLD"
    return VERDICT_TO_RATING.get(a, "WATCH")


# --------------------------------------------------------------------------- base rates
def base_rates(trades: pd.DataFrame | None, book: str, period: tuple[str, str] | None,
               source: str = "replay", min_n: int = 30) -> dict:
    """Win rate, average win / loss and expectancy in R, median hold, from the book's own trades."""
    out = {"book": book, "source": source, "period": list(period) if period else None,
           "n": 0, "small_sample": True}
    if trades is None or not len(trades) or "R" not in trades:
        out["note"] = NOT_IN_DATA if book in ("trend_pullback", "mean_reversion") else \
            "monthly-rebalanced book: no per-trade stop, so no per-trade R record"
        return out
    t = trades[(trades["book"] == book) & trades["R"].notna()]
    if not len(t):
        out["note"] = NOT_IN_DATA
        return out
    r = t["R"].astype(float)
    wins, losses = r[r > 0], r[r <= 0]
    out.update({
        "n": int(len(r)), "win_rate": _r(len(wins) / len(r)),
        "avg_win_R": _r(wins.mean()) if len(wins) else None,
        "avg_loss_R": _r(losses.mean()) if len(losses) else None,
        "expectancy_R": _r(r.mean()),
        "median_win_R": _r(wins.median()) if len(wins) else None,
        "p75_win_R": _r(wins.quantile(0.75)) if len(wins) else None,
        "median_hold_days": _r(t["days"].median(), 1) if "days" in t else None,
        "small_sample": len(r) < min_n,
    })
    return out


# --------------------------------------------------------------------------- trade view
def trade_targets(c, base: dict, bars: pd.DataFrame | None, as_of: date) -> dict:
    """Rule-based levels for the book that fired. Every level says whether it is a rule exit or only
    a reference; partial profit-taking is never presented as tested."""
    entry, stop = _f(c.price), _f(c.stop)
    f = c.facts or {}
    out: dict = {"book": c.book, "entry_ref": entry, "stop": stop, "t1": None, "t2": None,
                 "t1_kind": None, "t2_kind": None, "t1_basis": None, "t2_basis": None,
                 "review_date": None, "structural": {}, "notes": []}
    r_unit = (entry - stop) if entry and stop and entry > stop else None
    if c.book == "trend_pullback":
        for k, q, label in (("t1", "median_win_R", "median"), ("t2", "p75_win_R", "75th-percentile")):
            m = _f(base.get(q))
            if r_unit and m:
                out[k] = round(entry + m * r_unit, 4)
                out[f"{k}_kind"] = "reference level"
                out[f"{k}_basis"] = (f"{label} winning exit of this book's replayed trades = {m:.2f}R; "
                                     f"{m:.2f} x ${r_unit:.2f} risk + ${entry:.2f} entry")
        out["notes"].append("The book has no fixed profit target: it exits on RSI-14 > 70, a close below the "
                            "50-day EMA, the trailing stop or time. T1/T2 are where its past winners exited.")
    elif c.book == "mean_reversion":
        sma5 = _f(f.get("sma5"))
        if sma5:
            out["t1"], out["t1_kind"] = round(sma5, 4), "rule exit"
            out["t1_basis"] = f"the book exits when the close gets back above the 5-day average, today ${sma5:.2f}"
            if entry and sma5 <= entry:
                out["notes"].append("The 5-day average is already at or below the entry, so the rule exit "
                                    "could trigger on the first close.")
        m = _f(base.get("p75_win_R"))
        if r_unit and m:
            out["t2"], out["t2_kind"] = round(entry + m * r_unit, 4), "reference level"
            out["t2_basis"] = (f"75th-percentile winning exit of this book's replayed trades = {m:.2f}R; "
                               f"{m:.2f} x ${r_unit:.2f} + ${entry:.2f}")
    elif c.book in ("xs_momentum", "dual_momentum"):
        out["review_date"] = next_month_end_session(as_of).isoformat()
        out["notes"].append("A monthly-rebalanced book has no price trigger: it holds while the name stays in "
                            "the top of the ranking. The 12-month targets below are reference levels only.")
    elif c.book == "crisis_alpha":
        out["notes"].append("No price target: the exit is the regime (crisis mode ending).")
    if bars is not None and len(bars):
        hi20 = _f(bars["high"].iloc[-20:].max()) if len(bars) >= 5 else None
        hi52 = _f(bars["high"].iloc[-252:].max())
        out["structural"] = {"high_20d": _r(hi20, 2), "high_52w": _r(hi52, 2)}
        if out["t1"] and hi20 and entry and entry < hi20 < out["t1"]:
            out["notes"].append(f"The 20-day high (${hi20:.2f}) sits below T1: expect resistance before it.")
        elif out["t1"] and hi52 and entry and entry < hi52 < out["t1"]:
            out["notes"].append(f"The 52-week high (${hi52:.2f}) sits below T1: expect resistance before it.")
    if entry and stop and out["t1"] and not (out["t1"] > entry > stop):
        raise ValueError(f"{c.symbol}: levels out of order for a long (stop {stop}, entry {entry}, T1 {out['t1']})")
    return out


# --------------------------------------------------------------------------- investment view
def check_units(fund: dict | None, price: float | None) -> list[str]:
    """Catch the classic unit mistakes before any target is printed. Raises on a clear error."""
    if not fund or not price:
        return []
    problems = []
    pe, fpe, eps = _f(fund.get("pe")), _f(fund.get("forward_pe")), _f(fund.get("eps"))
    dy, mc, sh = _f(fund.get("dividend_yield")), _f(fund.get("market_cap")), _f(fund.get("shares_outstanding"))
    if dy is not None and not 0 <= dy < 0.25:
        raise ValueError(f"dividend yield {dy} is not a decimal fraction (percent passed as a fraction?)")
    if eps and pe and pe > 0 and eps > 0 and not 0.5 <= (price / eps) / pe <= 2.0:
        raise ValueError(f"price/EPS = {price / eps:.1f} disagrees with the stated P/E {pe:.1f} - EPS in the wrong units?")
    if mc and sh and not 0.5 <= mc / (price * sh) <= 2.0:
        raise ValueError(f"market cap ${mc:,.0f} vs price x shares ${price * sh:,.0f} - millions vs billions?")
    if fpe is not None and fpe > 1000:
        problems.append(f"forward P/E {fpe:.0f} looks wrong; ignored")
    return problems


def investment_view(val: dict | None, price: float | None, fund: dict | None, as_of: date,
                    pcfg: dict) -> dict:
    """Bear / base / bull with the arithmetic on one line each, recomputed and checked."""
    from abg.pitch.valuation import comps as _comps, dcf
    horizon = (pd.Timestamp(as_of) + pd.DateOffset(months=int(pcfg["horizon_months"]))).date().isoformat()
    out: dict = {"available": False, "horizon": horizon, "cases": {}, "lines": [], "comps": None,
                 "prob_weighted": None, "prob_upside": None, "dividend_yield": None,
                 "total_return": None, "street_target": None, "street_upside": None, "fair_value": None,
                 "upside": None, "implied_growth": None, "growth": None, "methods": 0, "warnings": []}
    if not val or not price:
        out["reason"] = NOT_IN_DATA
        return out
    out["warnings"] = check_units(fund, price)
    inp = val.get("inputs") or {}
    sc = val.get("scenarios") or {}
    cf0 = inp.get("eps") if (inp.get("eps") or 0) > 0 else None
    if cf0 is None and inp.get("forward_eps") and inp.get("growth") is not None:
        cf0 = inp["forward_eps"] / (1 + inp["growth"])
    tg = float(inp.get("terminal_growth", 0.025))
    for k in ("bear", "base", "bull"):
        s = sc.get(k)
        if not s or s.get("value") is None or cf0 is None:
            continue
        again = dcf(cf0, s["growth"], s["rate"], tg)
        if again is None or abs(again - s["value"]) > max(0.01, 1e-6 * abs(s["value"])):
            raise ValueError(f"{k} case does not recompute: {s['value']} vs {again}")
        v = float(s["value"])
        out["cases"][k] = {"value": round(v, 2), "upside": round(v / price - 1, 4), "growth": s["growth"],
                           "rate": s["rate"]}
        out["lines"].append(f"{k.title()}: 10-year earnings DCF - ${cf0:.2f} EPS growing {s['growth'] * 100:.1f}% "
                            f"a year, fading to {tg * 100:.1f}%, discounted at {s['rate'] * 100:.1f}% = ${v:,.2f}")
    cm = val.get("comps") or {}
    if cm.get("value"):
        fe, e = inp.get("forward_eps"), inp.get("eps")
        parts, chk = [], []
        if cm.get("value_forward_pe") and fe:
            parts.append(f"{cm['median_forward_pe']:.1f}x peer forward P/E x ${fe:.2f} forward EPS = ${cm['value_forward_pe']:,.2f}")
            chk.append(cm["median_forward_pe"] * fe)
        if cm.get("value_pe") and e:
            parts.append(f"{cm['median_pe']:.1f}x peer P/E x ${e:.2f} EPS = ${cm['value_pe']:,.2f}")
            chk.append(cm["median_pe"] * e)
        if chk and abs(sum(chk) / len(chk) - cm["value"]) > 0.01:
            raise ValueError("comps value does not recompute from its multiples")
        out["comps"] = {"value": round(cm["value"], 2), "upside": round(cm["value"] / price - 1, 4),
                        "peers": [p for p in cm.get("peers") or [] if p and p != "?"],
                        "n": max(cm.get("n_pe") or 0, cm.get("n_forward_pe") or 0)}
        out["lines"].append("Comps: " + "; ".join(parts) + (f" (mean ${cm['value']:,.2f})" if len(parts) > 1 else ""))
    w = pcfg["scenario_weights"]
    if all(k in out["cases"] for k in ("bear", "base", "bull")):
        pw = sum(w[k] * out["cases"][k]["value"] for k in ("bear", "base", "bull")) / sum(w.values())
        out["prob_weighted"] = round(pw, 2)
        out["prob_upside"] = round(pw / price - 1, 4)
        out["lines"].append(f"Probability-weighted: {w['bear']:.0%} bear + {w['base']:.0%} base + {w['bull']:.0%} bull "
                            f"= ${pw:,.2f}")
    dy = _f((fund or {}).get("dividend_yield"))
    out["dividend_yield"] = dy
    if out["prob_upside"] is not None:
        out["total_return"] = round(out["prob_upside"] + (dy or 0.0), 4)
    st = _f(((fund or {}).get("extra") or {}).get("target_mean_price"))
    if st:
        out["street_target"], out["street_upside"] = round(st, 2), round(st / price - 1, 4)
    out["fair_value"] = _r(val.get("fair_value"), 2)
    out["upside"] = _r(val.get("upside"))
    out["implied_growth"] = _r(val.get("implied_growth"))
    out["growth"] = _r(inp.get("growth"))
    out["growth_source"] = inp.get("growth_source")
    out["methods"] = int(bool(out["cases"])) + int(bool(out["comps"]))
    out["available"] = bool(out["cases"] or out["comps"])
    return out


# --------------------------------------------------------------------------- entry plan
def entry_plan(c, as_of: date, earnings: str | None, pcfg: dict) -> dict:
    nxt = next_trading_day(as_of)
    price, atr = _f(c.price), _f(c.atr)
    zone = None
    if price and atr:
        z = float(pcfg["entry_zone_atr"]) * atr
        zone = [round(price - z, 2), round(price + z, 2)]
    out = {"tested": f"at the open on {nxt:%A %b %d, %Y}", "date": nxt.isoformat(), "reference": price,
           "reference_kind": "last close", "zone": zone, "refinements": [], "earnings": earnings}
    if c.is_entry and price and atr:
        out["refinements"].append({
            "kind": "untested refinement", "rule": "don't chase",
            "text": f"if it opens above ${price + float(pcfg['chase_atr']) * atr:,.2f} "
                    f"(close + {pcfg['chase_atr']:g} ATR), skip and re-check at the next close"})
    if c.is_entry and c.stop:
        out["refinements"].append({
            "kind": "untested refinement", "rule": "gap below the stop",
            "text": f"if it opens below the stop (${c.stop:,.2f}), skip it"})
    if c.is_entry and earnings:
        out["refinements"].append({
            "kind": "untested refinement", "rule": "earnings-aware",
            "text": f"earnings on {earnings}: alternatively wait for the print and enter only if it closes "
                    f"above ${(c.stop or price or 0):,.2f} the session after. The tested entry is the default."})
    return out


# --------------------------------------------------------------------------- conviction
def conviction(*, gates: dict, ml_prob: float | None, ml_threshold: float | None, upside: float | None,
               regime: str, flags_on: int, earnings_in_hold: bool, has_fundamentals: bool,
               horizons_disagree: bool, small_sample: bool, pcfg: dict) -> dict:
    w = pcfg["conviction"]
    score, parts = float(w["start"]), [("start", float(w["start"]))]

    def add(name, pts):
        nonlocal score
        if pts:
            score += pts
            parts.append((name, float(pts)))
    if gates:
        add("all of the rule's checks passed" if all(gates.values()) else "not every check passed",
            w["gates_all"] if all(gates.values()) else w["gates_missing"])
    if ml_prob is not None and ml_threshold is not None:
        add(f"model odds {ml_prob:.0%} vs {ml_threshold:.0%} threshold",
            w["ml_above"] if ml_prob >= ml_threshold else w["ml_below"])
    if upside is not None:
        if upside >= pcfg["valuation_agree_upside"]:
            add(f"valuation agrees ({upside:+.0%} upside)", w["valuation_agrees"])
        elif upside < 0:
            add(f"valuation disagrees ({upside:+.0%})", w["valuation_disagrees"])
    add(f"regime: {regime}", (w["regime"] or {}).get(regime, 0))
    if flags_on:
        add(f"{flags_on} early-warning flag(s) on", max(w["flags_max"], w["flag_each"] * flags_on))
    if earnings_in_hold:
        add("earnings inside the hold window", w["earnings_in_hold"])
    if not has_fundamentals:
        add("no fundamentals to cross-check", w["no_fundamentals"])
    if horizons_disagree:
        add("trade view and 12-month view disagree", w["horizons_disagree"])
    if small_sample:
        add("base rates rest on a small sample", w["small_sample"])
    s = int(round(min(100.0, max(0.0, score))))
    b = pcfg["buckets"]
    bucket = "High" if s >= b["High"] else "Medium" if s >= b["Medium"] else "Low"
    return {"score": s, "bucket": bucket, "breakdown": [{"input": n, "points": p} for n, p in parts]}


# --------------------------------------------------------------------------- risk and size
def risk_and_size(c, entry: float | None, stop: float | None, t1: float | None, equity: float,
                  bucket: str, pcfg: dict) -> dict:
    out: dict = {"equity": round(equity, 2), "stop": stop, "stop_pct": None, "stop_atr": _f(c.stop_atr),
                 "rr": None, "rr_flag": False, "shares": None, "notional": None, "pct_of_fund": None,
                 "risk_dollars": None, "risk_pct_of_fund": None, "bound": None, "small_account": None}
    if entry and stop and entry > stop:
        out["stop_pct"] = round(stop / entry - 1, 4)
        if t1 and t1 > entry:
            out["rr"] = round((t1 - entry) / (entry - stop), 2)
            out["rr_flag"] = out["rr"] < float(pcfg["min_rr"])
        per = entry - stop
        scale = float((pcfg["size_scale"] or {}).get(bucket, 0.5))
        risk_cash = float(pcfg["risk_per_trade"]) * scale * equity
        by_risk = math.floor(risk_cash / per)
        by_cap = math.floor(float(pcfg["max_position_pct"]) * equity / entry)
        sh = max(0, min(by_risk, by_cap))
        out.update({"shares": sh, "notional": round(sh * entry, 2), "pct_of_fund": round(sh * entry / equity, 4),
                    "risk_dollars": round(sh * per, 2), "risk_pct_of_fund": round(sh * per / equity, 5),
                    "bound": "risk-based" if by_risk <= by_cap else "capped",
                    "risk_per_trade": float(pcfg["risk_per_trade"]) * scale})
        out["small_account"] = {"account": 10_000, "risk_pct": 0.01,
                                "shares": int(min(math.floor(100 / per),
                                                  math.floor(float(pcfg["max_position_pct"]) * 10_000 / entry)))}
    elif entry and c.weight is not None and c.notional:
        # momentum books carry no stop: they are sized by the book's own target weight
        pct = min(float(c.notional) / equity, float(pcfg["max_position_pct"]))
        sh = math.floor(pct * equity / entry)
        out.update({"shares": sh, "notional": round(sh * entry, 2), "pct_of_fund": round(sh * entry / equity, 4),
                    "bound": "book weight" if float(c.notional) / equity <= float(pcfg["max_position_pct"]) else "capped"})
    if out["pct_of_fund"] is not None and out["pct_of_fund"] > float(pcfg["max_position_pct"]) + 1e-9:
        raise AssertionError("position above max_position_pct")                         # pragma: no cover
    return out


# --------------------------------------------------------------------------- fund fit
def fund_fit(symbol: str, sector: str | None, store, holdings: list[str], sectors: dict,
             equity: float, notional: float | None, held_value: dict, pcfg: dict, bench: str = "SPY") -> dict:
    """Sector weight before/after, 60-day correlation to holdings and to the benchmark, beta, overlap,
    AI-infrastructure exposure - and one computed sentence on what the position adds."""
    out: dict = {"sector": sector or NOT_IN_DATA, "sector_before": None, "sector_after": None,
                 "corr_holdings": None, "corr_benchmark": None, "beta": None, "beta_source": None,
                 "already_held": symbol in holdings, "ai_infrastructure": symbol in set(pcfg["ai_infrastructure"]),
                 "ai_names_held": sorted(set(holdings) & set(pcfg["ai_infrastructure"])), "adds": None}
    if sector and equity:
        before = sum(v for t, v in held_value.items() if sectors.get(t) == sector)
        out["sector_before"] = round(before / equity, 4)
        out["sector_after"] = round((before + (notional or 0)) / equity, 4)

    def rets(t, n=61):
        f = store.frame(t)
        return f["close"].pct_change().iloc[-n:] if len(f) > 2 else pd.Series(dtype=float)
    me = rets(symbol)
    if len(me) >= 40:
        b = rets(bench)
        j = pd.concat([me, b], axis=1, join="inner").dropna()
        if len(j) >= 40:
            out["corr_benchmark"] = _r(j.iloc[:, 0].corr(j.iloc[:, 1]), 3)
            out["beta"] = _r(j.iloc[:, 0].cov(j.iloc[:, 1]) / j.iloc[:, 1].var(), 3)
            out["beta_source"] = f"60-day vs {bench}, from the bar store"
        cs = []
        for h in holdings:
            if h == symbol:
                continue
            j = pd.concat([me, rets(h)], axis=1, join="inner").dropna()
            if len(j) >= 40:
                cs.append(j.iloc[:, 0].corr(j.iloc[:, 1]))
        if cs:
            out["corr_holdings"] = _r(float(np.mean(cs)), 3)
    ch, beta = out["corr_holdings"], out["beta"]
    if out["already_held"]:
        out["adds"] = "doubling down: the paper book already holds it"
    elif ch is not None and ch < 0.3:
        out["adds"] = f"a diversifier (average 60-day correlation to holdings {ch:.2f})"
    elif beta is not None and beta < 0.7:
        out["adds"] = f"defensive ballast (beta {beta:.2f})"
    elif ch is not None and ch > 0.7:
        out["adds"] = f"a concentration warning: it moves with what we hold (correlation {ch:.2f})"
    elif out["sector_after"] is not None and out["sector_after"] > 0.30:
        out["adds"] = f"a concentration warning: {sector} would be {out['sector_after']:.0%} of the fund"
    else:
        out["adds"] = "a momentum position alongside the book" if beta is None or beta >= 1 else \
            "a steady addition with moderate market sensitivity"
    return out


# --------------------------------------------------------------------------- questions
def questions(inv: dict, tgt: dict, conv: dict, earnings: str | None, fund: dict | None,
              horizons_disagree: bool, rating_: str) -> list[str]:
    q: list[str] = []
    if horizons_disagree and inv.get("prob_upside") is not None:
        q.append(f"The rule says {rating_}, but the 12-month probability-weighted target is "
                 f"{inv['prob_upside']:+.0%} from here. Do we size this as a trade only, or not at all?")
    ig, g = inv.get("implied_growth"), inv.get("growth")
    if ig is not None and g is not None and ig > g + 0.02:
        q.append(f"The price implies {ig:.1%} a year of earnings growth against our {g:.1%} estimate. "
                 f"What would have to go right for the market to be correct?")
    if earnings:
        q.append(f"Earnings land on {earnings}, inside the hold window. Take the tested entry, or wait for the print?")
    if inv.get("methods") == 1:
        q.append("The 12-month target rests on one valuation method only. Is there a second way to cross-check it?")
    if not fund:
        q.append("No fundamentals came back for this name, so the case is price behaviour alone. "
                 "Does the team want a fundamental view before acting?")
    s = (tgt or {}).get("structural") or {}
    if len(q) < 2 and s.get("high_52w") and tgt.get("entry_ref"):
        gap = s["high_52w"] / tgt["entry_ref"] - 1
        q.append(f"The stock is {gap:.0%} below its 52-week high of ${s['high_52w']:,.2f}. "
                 f"Is that room to run, or overhead supply?")
    if len(q) < 2:
        q.append(f"Conviction is {conv['score']} ({conv['bucket']}). Which single input would change the team's mind?")
    return q[:3]


def wrong_if(c, exits: list[str]) -> list[str]:
    """Exits rewritten as conditions, led by the thesis-level one."""
    f = c.facts or {}
    lead = {
        "trend_pullback": (f"the trend breaks: a close below the 50-day EMA (${f['ema50']:,.2f})"
                           if f.get("ema50") else "the trend breaks: a close below the 50-day EMA"),
        "mean_reversion": (f"the long-term uptrend fails: a close below the 200-day average (${f['sma200']:,.2f})"
                           if f.get("sma200") else "the long-term uptrend fails"),
        "xs_momentum": "it drops out of the top of the momentum ranking at the month-end review",
        "dual_momentum": (f"the market switch flips: the benchmark closes below its 200-day average"
                          + (f" (${f['benchmark_sma200']:,.2f})" if f.get("benchmark_sma200") else "")),
        "crisis_alpha": "crisis mode ends",
    }.get(c.book)
    # exits that restate the lead line (which carries the price) are dropped
    anchors = {"trend_pullback": ("50-day EMA",), "mean_reversion": ("200-day average",),
               "xs_momentum": ("next month-end",), "dual_momentum": ("benchmark drops below",)}.get(c.book, ())
    out = [lead[0].upper() + lead[1:]] if lead else []
    for e in exits:
        if not e or any(a in e and ("below" in e or "month-end" in a) for a in anchors):
            continue
        e2 = e[0].upper() + e[1:]
        if e2 not in out:
            out.append(e2)
    return out


def exit_review(c, positions: dict, base: dict) -> dict | None:
    """For a SELL / TRIM: what the position did, in $ and in R, and how that compares with the book."""
    pos = (positions or {}).get(c.book, {}).get(c.symbol)
    if not pos:
        return None
    entry, price, sh = _f(pos.get("entry")), _f(pos.get("price")), _f(pos.get("shares"))
    if not entry or not price:
        return None
    pnl = (price - entry) * (sh or 0)
    r = _f(pos.get("open_r"))
    out = {"entry": entry, "entry_date": pos.get("entry_date"), "price": price, "shares": sh,
           "days_held": pos.get("days_held"), "return": round(price / entry - 1, 4),
           "pnl": round(pnl, 2), "r": _r(r, 2), "reason": (c.reason or "").replace("_", " ")}
    if r is not None and base.get("n"):
        ref = base.get("avg_win_R") if r > 0 else base.get("avg_loss_R")
        if ref is not None:
            kind = "average win" if r > 0 else "average loss"
            out["lesson"] = (f"{r:+.2f}R against the book's {kind} of {ref:+.2f}R over {base['n']} replayed trades: "
                             + ("a normal outcome for this book." if abs(r - ref) <= max(0.5, abs(ref)) else
                                "an outlier for this book - worth a look at what was different."))
    return out
