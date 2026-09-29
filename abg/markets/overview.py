"""Markets overview (futures, rates & bonds, FX, commodities, crypto, volatility) and the
market-regime filter used to grade trade ideas.

Regime = how friendly the tape is for new longs: S&P 500 / Nasdaq-100 trend (price vs 50- and
200-day), VIX level and 1-day change, and the 10-year yield's 1-month move.  Cached for
``ttl`` seconds so dozens of tracked ideas cost two history calls, not dozens.
"""
from __future__ import annotations

import asyncio
import time

from ..errors import ABGError
from ..portfolio.valuation import fetch_quotes
from .instruments import spec_for

GROUPS: list[tuple[str, list[str]]] = [
    ("Equity index futures", ["ES=F", "NQ=F", "YM=F", "RTY=F"]),
    ("Rates: Treasury futures", ["ZT=F", "ZF=F", "ZN=F", "ZB=F"]),
    ("Rates: yields", ["^IRX", "^FVX", "^TNX", "^TYX"]),
    ("Bond ETFs", ["SHY", "IEF", "TLT", "TIP", "LQD", "HYG"]),
    ("Energy", ["CL=F", "NG=F"]),
    ("Metals", ["GC=F", "SI=F", "HG=F"]),
    ("FX", ["DX-Y.NYB", "EURUSD=X", "USDJPY=X", "6E=F"]),
    ("Crypto", ["BTC-USD", "ETH-USD", "SOL-USD"]),
    ("Volatility", ["^VIX"]),
]


_CACHE: dict = {}


async def overview(engine, groups: list[tuple[str, list[str]]] | None = None, max_age: float = 60) -> dict:
    """Cached for ``max_age`` seconds so repeated page loads don't re-quote ~30 instruments."""
    key = id(engine)
    hit = _CACHE.get(key)
    if groups is None and hit and time.time() - hit[0] < max_age:
        return hit[1]
    out = await _overview(engine, groups or GROUPS)
    if groups is None:
        _CACHE[key] = (time.time(), out)
    return out


async def _overview(engine, groups: list[tuple[str, list[str]]]) -> dict:
    syms = [s for _, g in groups for s in g]
    quotes = await fetch_quotes(engine, syms, use_cache=True)
    out = []
    for name, g in groups:
        rows = []
        for s in g:
            q, sp = quotes.get(s) or {}, spec_for(s)
            rows.append({"symbol": s, "name": sp.name, "asset_class": sp.asset_class, "price": q.get("price"),
                         "change_pct": q.get("change_pct"), "change": q.get("change"), "error": q.get("error"),
                         "multiplier": sp.multiplier, "tick": sp.tick, "tick_value": sp.tick_value,
                         "session": sp.session, "provider": q.get("provider")})
        out.append({"group": name, "rows": rows})
    y = {s: (quotes.get(s) or {}).get("price") for s in ("^IRX", "^FVX", "^TNX", "^TYX")}
    curve = {"3m": y["^IRX"], "5y": y["^FVX"], "10y": y["^TNX"], "30y": y["^TYX"]}
    spreads = {}
    if y["^TNX"] is not None and y["^IRX"] is not None:
        spreads["10y-3m"] = y["^TNX"] - y["^IRX"]
    if y["^TYX"] is not None and y["^FVX"] is not None:
        spreads["30y-5y"] = y["^TYX"] - y["^FVX"]
    return {"groups": out, "curve": curve, "spreads": spreads, "inverted": (spreads.get("10y-3m") or 0) < 0,
            "as_of": time.time()}


class RegimeCache:
    """Shared market regime, refreshed at most every ``ttl`` seconds; never raises."""

    def __init__(self, engine, ttl: float = 900):
        self.engine, self.ttl = engine, ttl
        self._val: dict | None = None
        self._ts = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> dict | None:
        if self._val is not None and time.time() - self._ts < self.ttl:
            return self._val
        async with self._lock:
            if self._val is not None and time.time() - self._ts < self.ttl:
                return self._val
            try:
                self._val = await market_regime(self.engine)
            except Exception:                      # regime is context, never a blocker
                self._val = self._val or None
            self._ts = time.time()
            return self._val


def _trend(close) -> dict:
    c = close.dropna()
    last = float(c.iloc[-1])
    s50 = float(c.tail(50).mean()) if len(c) >= 50 else None
    s200 = float(c.tail(200).mean()) if len(c) >= 200 else None
    return {"price": last, "sma50": s50, "sma200": s200,
            "above50": s50 is not None and last > s50, "above200": s200 is not None and last > s200,
            "ret_1m": float(last / c.iloc[-22] - 1) * 100 if len(c) > 22 else None}


async def market_regime(engine) -> dict:
    async def hist(sym):
        try:
            return (await engine.history(sym, "1y", "1d", ttl=3600)).value.df["close"]
        except ABGError:
            return None
    spy, qqq, vix, tnx = await asyncio.gather(hist("SPY"), hist("QQQ"), hist("^VIX"), hist("^TNX"))
    return regime_from_series(spy, qqq, vix, tnx)


def regime_from_series(spy, qqq, vix, tnx) -> dict:
    """Regime from daily closes (also used by the backtester with series cut at the signal date)."""
    out: dict = {"indexes": {}, "notes": []}
    score = 0.0
    for name, c in (("SPY", spy), ("QQQ", qqq)):
        if c is None or len(c) < 60:
            continue
        t = _trend(c)
        out["indexes"][name] = t
        score += (0.5 if t["above50"] else -0.5) + (0.5 if t["above200"] else -0.5)
    if vix is not None and len(vix) > 2:
        v, v1 = float(vix.iloc[-1]), float(vix.iloc[-2])
        chg = (v / v1 - 1) * 100 if v1 else 0.0
        out["vix"] = {"level": v, "change_pct": chg}
        if v >= 30:
            score -= 1.0
            out["notes"].append(f"VIX {v:.1f}: stressed market")
        elif v >= 22:
            score -= 0.5
            out["notes"].append(f"VIX {v:.1f}: elevated fear")
        elif v <= 15:
            score += 0.25
        if chg >= 15:
            score -= 0.5
            out["notes"].append(f"VIX jumped {chg:+.0f}% today")
    if tnx is not None and len(tnx) > 22:
        d = float(tnx.iloc[-1] - tnx.iloc[-22])
        out["ten_year"] = {"level": float(tnx.iloc[-1]), "change_1m": d}
        if d >= 0.35:
            out["notes"].append(f"10-year yield up {d:.2f} pts in a month (pressure on growth stocks)")
    out["score"] = score
    out["label"] = ("risk-on" if score >= 1.25 else "constructive" if score >= 0.25 else
                    "mixed" if score > -0.75 else "risk-off")
    parts = []
    for n, t in out["indexes"].items():
        parts.append(f"{n} {'above' if t['above50'] else 'below'} 50-day, {'above' if t['above200'] else 'below'} 200-day")
    if "vix" in out:
        parts.append(f"VIX {out['vix']['level']:.1f} ({out['vix']['change_pct']:+.0f}%)")
    out["summary"] = f"{out['label']}: " + "; ".join(parts) if parts else out["label"]
    return out
