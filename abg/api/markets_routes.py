"""REST endpoints: markets overview (futures, rates, FX, crypto, vol), regime, calendar, risk book,
trained models and backtests."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from ..markets.instruments import canonical, spec_for
from ..markets.overview import overview
from ..utils import jsonable

router = APIRouter(prefix="/api", tags=["markets"])


def _tr(request: Request):
    tr = getattr(request.app.state.monitor, "ext", None)
    if tr is None:
        raise HTTPException(400, "external signals are disabled")
    return tr


@router.get("/markets")
async def markets(request: Request):
    tr = getattr(request.app.state.monitor, "ext", None)
    ov = await asyncio.wait_for(overview(request.app.state.engine), 60)
    ov["regime"] = await tr.regime.get() if tr else None
    return jsonable(ov)


@router.get("/instrument/{symbol}")
async def instrument(symbol: str, futures: bool = False):
    return spec_for(canonical(symbol, futures), futures).to_dict()


@router.get("/calendar")
async def calendar(request: Request, days: int = Query(14, ge=1, le=90)):
    tr = _tr(request)
    evs = await asyncio.wait_for(tr.calendar.upcoming(tr.symbols(), days), 60)
    return {"events": [e.to_dict() for e in evs],
            "blackout": {"before_min": tr.s.ext_event_blackout_before_min, "after_min": tr.s.ext_event_blackout_after_min}}


@router.get("/ext/risk")
async def risk_book(request: Request):
    tr = _tr(request)
    s = tr.s
    eq = tr.gate.equity()
    active = tr.store.ideas({"active"}, limit=200)
    return jsonable({"equity": eq, "positions": [{"id": i.id, "symbol": i.symbol, "direction": i.direction,
                                                  "risk": i.open_risk(), "open_pnl": i.open_pnl(i.last_price),
                                                  "units": i.shares * i.remaining,
                                                  "contract": (i.meta.get("instrument") or {}).get("contract")}
                                                 for i in active],
                     "limits": {"max_open": s.ext_max_open_positions, "max_heat_pct": s.ext_max_heat_pct,
                                "max_correlated": s.ext_max_correlated, "max_sector": s.ext_max_sector,
                                "prop_enabled": s.prop_enabled, "prop_daily_loss_limit": s.prop_daily_loss_limit,
                                "prop_max_drawdown": s.prop_max_drawdown, "prop_max_contracts": s.prop_max_contracts,
                                "prop_drawdown_mode": s.prop_drawdown_mode}})


@router.get("/models")
async def models(request: Request):
    s = request.app.state.engine.settings
    d = Path(s.data_dir).expanduser() / "models"
    out = {}
    for name in ("grade_model", "risk_model"):
        p = d / f"{name}.json"
        if p.exists():
            m = json.loads(p.read_text())
            out[name] = {"trained_at": m.get("trained_at"), "n": m.get("n"), "metrics": m.get("metrics"),
                         "sources": m.get("sources"), "importance": (m.get("importance") or [])[:8]}
    return out


@router.get("/ext/backtests")
async def backtests(request: Request):
    s = request.app.state.engine.settings
    d = Path(s.data_dir).expanduser() / "backtests"
    out = []
    for p in sorted(d.glob("*.json"), reverse=True)[:30] if d.exists() else []:
        try:
            rep = json.loads(p.read_text())
        except ValueError:
            continue
        out.append({"name": p.stem, "overall": rep.get("overall"), "by": rep.get("by"), "grade_note": rep.get("grade_note"),
                    "equity_curve": rep.get("equity_curve")})
    return {"backtests": out}


# --------------------------------------------------------------------------- MNQ alpha bot (docs/14)
@router.get("/alpha")
async def alpha_status(request: Request):
    s = request.app.state.engine.settings
    bot = getattr(request.app.state.monitor, "alpha", None)
    dd = Path(s.data_dir).expanduser()
    out = {"status": bot.status() if bot else {"enabled": False, "hint": "set ABG_ALPHA_ENABLED=true and restart"}}
    try:
        from ..alpha.bot import AlphaStore
        out["trades"] = AlphaStore(dd / "alpha.sqlite3").trades(100)
    except Exception:
        out["trades"] = []
    p = dd / "alpha_backtest.json"
    if p.exists():
        try:
            bt = json.loads(p.read_text())
            out["backtest"] = {"stats": bt["backtest"]["stats"], "bars": bt.get("bars"), "at": p.stat().st_mtime,
                               "walk_forward": {k: bt["walk_forward"].get(k) for k in ("best", "test_stats", "default_test_stats",
                                                                                         "train_days", "test_days")}
                               if bt.get("walk_forward") else None}
        except Exception:
            out["backtest"] = None
    if bot is None:
        from ..alpha.learn import AdaptiveBook
        out["learner"] = AdaptiveBook.load(dd / "alpha_learner.json").summary()
    return jsonable(out)
