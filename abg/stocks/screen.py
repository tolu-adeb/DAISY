"""The whole-market screen (docs/22.2): one row per US-listed common stock and ADR, from the last close.

Research coverage and pitch eligibility are two different things:

    coverage      every name with bars - scanned, ranked, graded and searchable on the dashboard
    eligibility   a price and liquidity floor (``pitch.eligibility``); only eligible names are pitched,
                  and a name that is not eligible says why

The indicators are ``PanelIndicators`` - the same class the rule books read - so a number here is the
number the rule would use. ``setups`` reads each book's own thresholds from the rule file and says how far
a name is from that book's trigger. Nothing here is a new strategy and nothing here fires a trade.

A name with fewer than ``too_new_sessions`` bars is marked "too new for the tested rules": the momentum
and trend rules need about a year of history, so a fresh IPO is watched and researched, not rule-pitched.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pandas as pd

ELIGIBILITY = {"min_price": 5.0, "min_adv_usd": 20e6, "min_sessions": 60, "too_new_sessions": 252}
LAST = ("close", "ema50", "ema200", "sma200", "sma5", "sma50", "rsi14", "rsi2", "min_rsi14_5", "mom_12_1",
        "mom126", "vol252", "atr14", "prev_high", "macd_hist", "macd_hist_prev", "adv20")
STOCK_BOOKS = ("trend_pullback", "mean_reversion", "xs_momentum")


def eligibility_cfg(cfg: dict | None) -> dict:
    """``pitch.eligibility`` over the defaults; the floor never goes below what the books themselves use."""
    out = dict(ELIGIBILITY)
    out.update(((cfg or {}).get("pitch") or {}).get("eligibility") or {})
    return out


def wide_from_long(long: pd.DataFrame, symbols: set | None = None) -> SimpleNamespace:
    """Polygon's day files (date, T, o, h, l, c, v) as date x ticker frames."""
    if symbols is not None:
        long = long[long["T"].isin(symbols)]
    long = long.drop_duplicates(["date", "T"], keep="last")
    piv = {k: long.pivot(index="date", columns="T", values=k).sort_index() for k in ("o", "h", "l", "c", "v")}
    return SimpleNamespace(open=piv["o"], high=piv["h"], low=piv["l"], close=piv["c"], volume=piv["v"])


def _n(x, d=4):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, d) if math.isfinite(v) else None


def build(panel, cfg: dict, names: dict | None = None, sectors: dict | None = None,
          non_stock: set | None = None, ind=None) -> pd.DataFrame:
    """The screen: one row per ticker in ``panel`` (anything with close/high/low/volume frames)."""
    from abg.tradebook.stocks.books import PanelIndicators
    I = ind if ind is not None else PanelIndicators(panel)
    c = panel.close
    if c.empty:
        return pd.DataFrame()
    ec = eligibility_cfg(cfg)
    df = pd.DataFrame({k: getattr(I, k).iloc[-1] for k in LAST})
    df = df.rename(columns={"adv20": "adv20_usd"})
    df["high"] = panel.high.iloc[-1]
    df["prev_close"] = c.shift(1).iloc[-1]
    df["min_rsi14_4"] = I.rsi14.rolling(4, min_periods=1).min().iloc[-1]
    df["hi252"] = c.rolling(252, min_periods=60).max().iloc[-1]
    df["lo252"] = c.rolling(252, min_periods=60).min().iloc[-1]
    df["sessions"] = c.notna().sum()
    df["first_day"] = c.apply(lambda s: s.first_valid_index()).map(lambda d: str(d.date()) if d is not None and d == d else None)
    df = df[df["close"].notna()].copy()
    df["chg_1d"] = df["close"] / df["prev_close"] - 1
    df["chg_5d"] = (c.iloc[-1] / c.shift(5).iloc[-1] - 1).reindex(df.index)
    df["chg_21d"] = (c.iloc[-1] / c.shift(21).iloc[-1] - 1).reindex(df.index)
    df["dist_200"] = df["close"] / df["sma200"] - 1
    df["mom_score"] = df["mom_12_1"] / df["vol252"].replace(0, np.nan)
    df["day"] = str(c.index[-1].date())
    ns = set(non_stock or ())
    df["is_stock"] = [t not in ns and not str(t).startswith("^") for t in df.index]
    liquid = (df["close"] >= ec["min_price"]) & (df["adv20_usd"] >= ec["min_adv_usd"]) & (df["sessions"] >= ec["min_sessions"])
    df["eligible"] = liquid & df["is_stock"]
    df["too_new"] = df["sessions"] < ec["too_new_sessions"]

    def why(r):
        if not r["is_stock"]:
            return "an ETF or a gauge, not a single stock"
        w = []
        if not (r["close"] >= ec["min_price"]):
            w.append(f"price under ${ec['min_price']:g}")
        if not (r["adv20_usd"] >= ec["min_adv_usd"]):
            w.append(f"trades under ${ec['min_adv_usd'] / 1e6:g}M a day (20-day average)")
        if r["sessions"] < ec["min_sessions"]:
            w.append(f"only {int(r['sessions'])} sessions of history")
        return "; ".join(w) or None
    wn = df.apply(why, axis=1).astype(object)
    df["why_not"] = wn.where(wn.notna(), None)
    el = df[df["eligible"]]
    df["mom_pct"] = el["mom_score"].rank(pct=True)
    df["trend_pct"] = el["dist_200"].rank(pct=True)
    df["mom_rank"] = el["mom_score"].rank(ascending=False, method="first")
    df["name"] = [(names or {}).get(t) for t in df.index]
    df["sector"] = [(sectors or {}).get(t) for t in df.index]
    return df


def breadth(df: pd.DataFrame) -> dict:
    """Market breadth from the screen: the share of eligible names above their 50- and 200-day averages,
    and 52-week highs against lows."""
    el = df[df["eligible"]] if len(df) else df
    if not len(el):
        return {"n": 0, "above_50": None, "above_200": None, "new_highs": None, "new_lows": None}
    a50 = el[el["sma50"].notna()]
    a200 = el[el["sma200"].notna()]
    hl = el[el["hi252"].notna()]
    return {"n": int(len(el)),
            "above_50": _n((a50["close"] > a50["sma50"]).mean()) if len(a50) else None,
            "above_200": _n((a200["close"] > a200["sma200"]).mean()) if len(a200) else None,
            "new_highs": int((hl["close"] >= hl["hi252"] * 0.999).sum()),
            "new_lows": int((hl["close"] <= hl["lo252"] * 1.001).sum())}


def rsi_trigger_price(closes: pd.Series, period: int, threshold: float) -> float | None:
    """The close below which RSI(period) would sit under ``threshold`` tomorrow (bisection on the same
    RSI the rules use). ``None`` if no close within 25% of the last one does it."""
    from abg.tradebook.stocks import books as B
    s = closes.dropna().iloc[-(period * 15 + 30):]
    if len(s) < period + 5:
        return None
    last = float(s.iloc[-1])
    nxt = s.index[-1] + pd.Timedelta(days=1)

    def rsi_at(px):
        return float(B.ind.rsi(pd.concat([s, pd.Series([px], index=[nxt])]), period).iloc[-1])
    lo, hi = last * 0.75, last * 1.05
    if not rsi_at(lo) < threshold:
        return None
    if rsi_at(hi) < threshold:
        return round(hi, 2)
    for _ in range(24):
        mid = (lo + hi) / 2
        if rsi_at(mid) < threshold:
            lo = mid
        else:
            hi = mid
    return round(lo, 2)


def setups(r: dict, cfg: dict, closes: pd.Series | None = None) -> list[dict]:
    """How far one screen row is from each single-stock book's trigger at the next close: the gates
    that already pass, the ones still missing, and the exact condition still needed."""
    out = []
    if not r.get("eligible"):
        return out
    books = cfg.get("books") or {}
    c, atr = r.get("close"), r.get("atr14")

    def ok(*xs):
        return all(x is not None and x == x for x in xs)

    p = books.get("trend_pullback") or {}
    if p.get("enabled", True) and not r.get("too_new") and ok(c, r.get("ema200"), r.get("ema50")):
        thr = float(p.get("rsi_pullback", 40))
        up = c > r["ema200"] and r["ema50"] > r["ema200"]
        pulled = ok(r.get("min_rsi14_4")) and r["min_rsi14_4"] <= thr      # still inside tomorrow's 5-day window
        rising = ok(r.get("macd_hist"), r.get("macd_hist_prev")) and r["macd_hist"] > r["macd_hist_prev"]
        near = ok(r.get("rsi14")) and r["rsi14"] <= thr + 5
        if up and (pulled or near):
            missing = []
            if not pulled:
                missing.append(f"RSI-14 at or below {thr:g} (now {r['rsi14']:.0f})")
            missing.append(f"a close above ${r['high']:,.2f} (today's high)")
            if not rising:
                missing.append("the MACD histogram turning up")
            stop_atr = float(p.get("stop_atr", 2.0))
            out.append({"book": "trend_pullback", "steps": len(missing), "missing": missing,
                        "passing": ["close above the 200-day EMA", "50-day EMA above the 200-day"]
                        + (["a pullback is on record (RSI-14 touched the threshold)"] if pulled else []),
                        "trigger": " and ".join(missing),
                        "level": _n(r["high"], 2), "level_kind": "above",
                        "stop_note": (f"stop {stop_atr:g} ATR (${stop_atr * atr:,.2f}) under the close that triggers"
                                      if ok(atr) else None),
                        "max_hold": p.get("max_hold")})
    p = books.get("mean_reversion") or {}
    if p.get("enabled", True) and ok(c, r.get("sma200"), r.get("rsi2")) and r.get("sessions", 0) >= 200:
        ent, rp = float(p.get("rsi_entry", 10)), int(p.get("rsi_period", 2))
        if c > r["sma200"] and ent <= r["rsi2"] < ent + 15:
            lvl = rsi_trigger_price(closes, rp, ent) if closes is not None else None
            miss = f"RSI({rp}) below {ent:g} at the close (now {r['rsi2']:.0f})" + (f": a close under about ${lvl:,.2f}" if lvl else "")
            out.append({"book": "mean_reversion", "steps": 1, "missing": [miss],
                        "passing": ["close above the 200-day average"], "trigger": miss,
                        "level": lvl, "level_kind": "below",
                        "stop_note": "the book sells into the first bounce (close above the 5-day average)",
                        "max_hold": p.get("max_hold")})
    p = books.get("xs_momentum") or {}
    if p.get("enabled", True) and not r.get("too_new") and ok(r.get("mom_rank"), r.get("mom_score")):
        top = int(p.get("top_n", 20))
        if r["mom_rank"] <= 2 * top and r["mom_score"] > float(p.get("min_score", 0.0)):
            inside = r["mom_rank"] <= top
            miss = (f"still ranked in the top {top} at the month-end rebalance (now #{int(r['mom_rank'])})" if inside else
                    f"a rank inside the top {top} by 12-1 momentum over volatility at month-end (now #{int(r['mom_rank'])})")
            out.append({"book": "xs_momentum", "steps": 1 if inside else 2, "missing": [miss],
                        "passing": ["12-1 momentum is positive after dividing by volatility"], "trigger": miss,
                        "level": None, "level_kind": None, "stop_note": "no stop: the book re-ranks every month-end",
                        "max_hold": None})
    return sorted(out, key=lambda s: s["steps"])


def gate_report(r: dict, cfg: dict) -> list[dict]:
    """Rule by rule, for Explore: every single-stock book's checks on this name and whether each passes."""
    books = cfg.get("books") or {}
    out = []

    def g(name, ok):
        return {"check": name, "pass": None if ok is None else bool(ok)}

    def cmp(a, b, fn):
        return None if a is None or b is None or a != a or b != b else fn(a, b)
    base = [g("eligible: clears the price and liquidity floor", r.get("eligible"))]
    p = books.get("trend_pullback") or {}
    thr = p.get("rsi_pullback", 40)
    out.append({"book": "trend_pullback", "checks": base + [
        g("close above the 200-day EMA", cmp(r.get("close"), r.get("ema200"), lambda a, b: a > b)),
        g("50-day EMA above the 200-day", cmp(r.get("ema50"), r.get("ema200"), lambda a, b: a > b)),
        g(f"lowest RSI-14 of the last 5 days at or below {thr}", cmp(r.get("min_rsi14_5"), thr, lambda a, b: a <= b)),
        g("close above yesterday's high", cmp(r.get("close"), r.get("prev_high"), lambda a, b: a > b)),
        g("MACD histogram rising", cmp(r.get("macd_hist"), r.get("macd_hist_prev"), lambda a, b: a > b))]})
    p = books.get("mean_reversion") or {}
    out.append({"book": "mean_reversion", "checks": base + [
        g("close above the 200-day average", cmp(r.get("close"), r.get("sma200"), lambda a, b: a > b)),
        g(f"RSI({p.get('rsi_period', 2)}) below {p.get('rsi_entry', 10)}",
          cmp(r.get("rsi2"), p.get("rsi_entry", 10), lambda a, b: a < b))]})
    p = books.get("xs_momentum") or {}
    out.append({"book": "xs_momentum", "checks": base + [
        g("12-1 momentum positive after dividing by volatility", cmp(r.get("mom_score"), p.get("min_score", 0.0), lambda a, b: a > b)),
        g(f"in the top {p.get('top_n', 20)} by that score", cmp(r.get("mom_rank"), p.get("top_n", 20), lambda a, b: a <= b))]})
    for b in out:
        b["passes"] = all(x["pass"] for x in b["checks"])
    return out


def rows(df: pd.DataFrame) -> list[tuple[str, str, dict]]:
    """``(symbol, day, data)`` for the desk database: plain floats, NaN as null."""
    out = []
    for sym, r in df.iterrows():
        d = {}
        for k, v in r.items():
            if isinstance(v, (bool, np.bool_)):
                d[k] = bool(v)
            elif isinstance(v, (int, float, np.integer, np.floating)):
                d[k] = _n(v, 6)
            else:
                d[k] = v if v is None or isinstance(v, str) else str(v)
        out.append((str(sym), d.get("day") or "", d))
    return out
