"""Live valuation of a saved portfolio: prices, P&L, weights and portfolio-level risk."""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pandas as pd

from ..errors import ABGError
from ..risk.portfolio import portfolio_risk
from ..utils import jsonable, period_to_start
from .store import PortfolioStore

if TYPE_CHECKING:
    from ..engine import AnalysisEngine


async def fetch_quotes(engine: "AnalysisEngine", symbols: list[str], use_cache: bool = True) -> dict[str, dict]:
    """Quotes for many symbols concurrently; failures become {'error': ...} (never raise)."""
    async def one(s):
        try:
            f = await engine.quote(s, use_cache=use_cache)
            return s, {**f.value.to_dict(), "provider": f.provenance.provider, "cache": f.provenance.cache}
        except ABGError as e:
            return s, {"symbol": s, "error": e.message}
    return dict(await asyncio.gather(*(one(s) for s in symbols)))


async def snapshot(engine: "AnalysisEngine", store: PortfolioStore, portfolio: str,
                   quotes: dict[str, dict] | None = None, analysis: dict[str, dict] | None = None,
                   with_risk: bool = True, risk_period: str = "1y") -> dict:
    pf = store.ensure(portfolio)
    positions = store.positions(pf)
    watch = store.watchlist(pf)
    syms = sorted({p.symbol for p in positions} | {w["symbol"] for w in watch})
    if quotes is None:
        quotes = await fetch_quotes(engine, syms)
    else:
        missing = [s for s in syms if s not in quotes]
        if missing:
            quotes = {**quotes, **await fetch_quotes(engine, missing)}
    analysis = analysis or {}

    rows, total_val, total_cost, day_pnl, prev_val = [], 0.0, 0.0, 0.0, 0.0
    for p in positions:
        q = quotes.get(p.symbol) or {}
        price = q.get("price")
        prev = q.get("prev_close") or price
        mv = p.shares * price if price else None
        row = {**p.to_dict(), "price": price, "prev_close": prev, "change_pct": q.get("change_pct"),
               "market_value": mv, "unrealized_pnl": (mv - p.cost_basis) if mv is not None else None,
               "unrealized_pct": ((price / p.avg_cost - 1) * 100) if price and p.avg_cost else None,
               "day_pnl": p.shares * (price - prev) if price and prev else None,
               "quote_error": q.get("error"), "quote_provider": q.get("provider"),
               **_analysis_summary(analysis.get(p.symbol))}
        if mv is not None:
            total_val += mv
            total_cost += p.cost_basis
            day_pnl += row["day_pnl"] or 0.0
            prev_val += p.shares * prev
        rows.append(row)
    for r in rows:
        r["weight_pct"] = (r["market_value"] / total_val * 100) if (r["market_value"] and total_val) else None

    watch_rows = []
    for w in watch:
        q = quotes.get(w["symbol"]) or {}
        watch_rows.append({"symbol": w["symbol"], "notes": w.get("notes"), "added_at": w["added_at"],
                           "price": q.get("price"), "change_pct": q.get("change_pct"), "quote_error": q.get("error"),
                           **_analysis_summary(analysis.get(w["symbol"]))})

    out = {
        "portfolio": pf, "as_of": time.time(),
        "totals": {"market_value": total_val, "cost_basis": total_cost,
                   "unrealized_pnl": total_val - total_cost if positions else 0.0,
                   "unrealized_pct": ((total_val / total_cost - 1) * 100) if total_cost else None,
                   "day_pnl": day_pnl, "day_pct": (day_pnl / prev_val * 100) if prev_val else None,
                   "realized_pnl": store.realized_pnl(pf), "positions": len(positions), "watching": len(watch)},
        "holdings": rows, "watchlist": watch_rows,
        "rules": [r.to_dict() for r in store.rules(pf)],
        "risk": {"available": False},
    }
    if with_risk and len(positions) >= 1:
        out["risk"] = await _risk(engine, rows, risk_period)
    return jsonable(out)


def _analysis_summary(a: dict | None) -> dict:
    if not a:
        return {}
    return {"signal_score": a.get("signal_score"), "signal_label": a.get("signal_label"), "trend": a.get("trend"),
            "rsi": a.get("rsi"), "risk_level": a.get("risk_level"), "risk_score": a.get("risk_score"),
            "top_setup": a.get("top_setup"), "analyzed_at": a.get("analyzed_at"),
            "recommendation": a.get("recommendation"), "forecast_confidence": a.get("forecast_confidence"),
            "prob_up": a.get("prob_up")}


async def _risk(engine: "AnalysisEngine", rows: list[dict], period: str) -> dict:
    weights = {r["symbol"]: r["market_value"] for r in rows if r.get("market_value")}
    if not weights:
        return {"available": False, "reason": "no priced holdings"}
    closes = {}

    async def one(sym):
        try:
            h = (await engine.history(sym, period)).value
            closes[sym] = h.df["close"][h.df.index >= pd.Timestamp(period_to_start(period))]
        except ABGError:
            pass
    await asyncio.gather(*(one(s) for s in weights))
    if not closes:
        return {"available": False, "reason": "no price history"}
    res = portfolio_risk(pd.DataFrame(closes), {k: v for k, v in weights.items() if k in closes})
    if res.get("available"):
        total = sum(weights.values())
        res["var_95_1d_usd"] = total * res["var_95_1d_pct"] / 100
        res["cvar_95_1d_usd"] = total * res["cvar_95_1d_pct"] / 100
    return res
