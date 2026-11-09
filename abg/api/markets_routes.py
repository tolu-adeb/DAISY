"""REST endpoints: markets overview (futures, rates, FX, crypto, vol), regime, calendar, risk book,
trained models and backtests."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query, Request

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


# --------------------------------------------------------------------------- MNQ Bot tab (docs/20)
def _alpha_store(request: Request):
    from ..alpha.bot import AlphaStore
    bot = getattr(request.app.state.monitor, "alpha", None)
    if bot is not None:
        return bot.store
    return AlphaStore(Path(request.app.state.engine.settings.data_dir).expanduser() / "alpha.sqlite3")


@router.post("/alpha/pause")
async def alpha_pause(request: Request, body: dict = Body(...)):
    """Hold (or resume) the MNQ bot's Discord posts.  Trades are still tracked and logged while paused."""
    bot = getattr(request.app.state.monitor, "alpha", None)
    if bot is None or not getattr(request.app.state.monitor, "running", False):
        raise HTTPException(409, "the signal bot isn't running in this window")
    return {"paused": bot.set_posting_paused(bool(body.get("on")), str(body.get("by") or "dashboard")[:40])}


@router.get("/alpha/events")
async def alpha_events(request: Request, day: str | None = None, limit: int = Query(200, ge=1, le=1000)):
    """The bot's event log (brief, ideas, signals, management, day close) with what reached Discord."""
    from ..alpha.messages import Renderer
    s = request.app.state.engine.settings
    bot = getattr(request.app.state.monitor, "alpha", None)
    rd = bot.renderer if bot is not None else Renderer(s.alpha_tag, None)
    try:
        evs = await asyncio.to_thread(_alpha_store(request).events, day, limit)
    except Exception as e:
        return {"events": [], "error": type(e).__name__}
    out = []
    for e in evs:
        try:
            r = rd.render(e["data"])
            title = r[1].get("title") if r else None
        except Exception:
            title = None
        d = e["data"]
        out.append({"at": e["at"], "type": e["type"], "delivered": e["delivered"], "title": title,
                    "ts": d.get("ts"), "day": (d.get("ctx") or {}).get("day") or (str(d.get("ts") or "")[:10] or None)})
    return jsonable({"events": out})


def paper_stats(trades: list[dict], commission_rt: float = 1.24, point_value: float = 2.0) -> dict:
    """Live paper record: win rate, net, drawdown, by month (same formulas as the backtest)."""
    import numpy as np
    tr = sorted(trades, key=lambda t: (t.get("date", ""), t.get("opened", "")))
    if not tr:
        return {"trades": 0}
    pts = np.array([float(t.get("pts") or 0) for t in tr])
    usd = pts * point_value - commission_rt
    by_day: dict[str, float] = {}
    by_month: dict[str, list[float]] = {}
    for t, u in zip(tr, usd):
        by_day[t["date"]] = by_day.get(t["date"], 0.0) + u
        by_month.setdefault(t["date"][:7], []).append(u)
    cum = np.cumsum([by_day[d] for d in sorted(by_day)])
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:] - cum)) if len(cum) else 0.0
    wins, losses = pts[pts > 0.5], pts[pts < -0.5]
    return {"trades": len(tr), "wins": int(len(wins)), "losses": int(len(losses)), "win_rate": float(len(wins) / len(tr)),
            "net_pts": round(float(pts.sum()), 2), "net_usd": round(float(usd.sum()), 2), "max_dd_usd": round(dd, 2),
            "avg_r": round(float(np.mean([float(t.get("r") or 0) for t in tr])), 3),
            "first": tr[0]["date"], "last": tr[-1]["date"],
            "months": [{"month": k, "trades": len(v), "net_usd": round(sum(v), 2)} for k, v in sorted(by_month.items())]}


@router.get("/alpha/paper")
async def alpha_paper(request: Request, since: str | None = None):
    s = request.app.state.engine.settings
    try:
        trades = await asyncio.to_thread(_alpha_store(request).trades, 5000)
    except Exception:
        trades = []
    if since:
        trades = [t for t in trades if str(t.get("date") or "") >= since]
    return jsonable({"stats": paper_stats(trades, s.alpha_commission_rt), "trades": trades[:50]})


@router.get("/system")
async def system(request: Request):
    """System tab: monitor, jobs, errors, data freshness and which features are on (no secret values)."""
    import time as _t
    s = request.app.state.engine.settings
    m = request.app.state.monitor
    dd = Path(s.data_dir).expanduser()
    files = {}
    for name in ("alpha.sqlite3", "mnq_proxy_5m.csv", "qqq_daily.csv", "lab/lab.sqlite3", "swing/state/book.json",
                 "pitch/pitches.json", "stocks.sqlite3", "stocks/today.json", "stocks/ledger.jsonl",
                 "health.json", "uptime.json"):
        f = dd / name
        files[name] = {"exists": f.exists(), "age_h": round((_t.time() - f.stat().st_mtime) / 3600, 1) if f.exists() else None,
                       "kb": round(f.stat().st_size / 1024) if f.exists() else None}
    alpha_retired = bool(getattr(s, "alpha_retired", True))
    stocks_on = bool(getattr(s, "stocks_enabled", False))
    feats = {"stock desk": stocks_on,
             "MNQ bot" + (" (retired)" if alpha_retired else ""): bool(s.alpha_enabled and not alpha_retired),
             "R&D desk": s.rd_enabled, "forward lab": s.fwd_enabled,
             "swing desk (retired)": bool(s.swing_enabled and not stocks_on),
             "pitch desk": getattr(s, "pitch_enabled", False), "external signals": s.ext_enabled}
    keys = {"Finnhub (real-time)": bool(s.finnhub_api_key), "Polygon": bool(s.polygon_api_key),
            "Twelve Data": bool(s.twelvedata_api_key), "FMP": bool(s.fmp_api_key), "Anthropic (agents)": bool(s.anthropic_api_key),
            "MNQ Discord": bool(s.alpha_webhook_url or s.alpha_channel_id), "R&D Discord": bool(s.rd_webhook_url),
            "Stocks Discord": bool(getattr(s, "stocks_webhook_url", None)),
            "Swing Discord": bool(s.swing_webhook_url), "Pitch Discord": bool(getattr(s, "pitch_webhook_url", None)),
            "Healthcheck": bool(s.healthcheck_url)}
    st = m.status()
    return jsonable({"monitor": {k: st.get(k) for k in ("running", "started_at", "elsewhere", "market", "errors", "uptime")},
                     "note": getattr(request.app.state, "monitor_note", None), "features": feats, "keys": keys,
                     "files": files, "data_dir": str(dd),
                     "jobs": {"scheduler": ((st.get("external") or {}).get("scheduler")), "rd": st.get("rd"),
                              "forward": {k: (st.get("forward") or {}).get(k) for k in ("enabled", "busy", "last", "errors")},
                              "swing": {k: (st.get("swing") or {}).get(k) for k in ("enabled", "busy", "last_pass", "errors")},
                              "pitch": st.get("pitch")}})
