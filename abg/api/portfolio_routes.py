"""REST + Server-Sent-Events endpoints for the saved portfolio, alerts, signals and the monitor."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..errors import ABGError
from ..live.monitor import MonitorBusy
from ..portfolio import RULE_KINDS, snapshot
from ..utils import jsonable

router = APIRouter(prefix="/api")


def _pf(request: Request, name: str | None) -> str:
    return request.app.state.store.ensure(name or request.app.state.engine.settings.default_portfolio)


def _poke(request: Request, analysis: bool = True) -> None:
    mon = request.app.state.monitor
    if mon.running:
        mon.poke(analysis=analysis)


# --------------------------------------------------------------------------- portfolio
@router.get("/portfolio")
async def get_portfolio(request: Request, portfolio: str | None = None, risk: bool = True):
    st, mon = request.app.state, request.app.state.monitor
    name = _pf(request, portfolio)
    fresh = mon.running and mon.pf == name and mon.last_quote_sweep and time.time() - mon.last_quote_sweep < 120
    snap = await asyncio.wait_for(snapshot(st.engine, st.store, name, quotes=mon.quotes if fresh else None,
                                           analysis=mon.analysis if mon.pf == name else None, with_risk=risk), 60)
    snap["monitor"] = mon.status()
    return snap


class TxIn(BaseModel):
    symbol: str = Field(..., max_length=15)
    side: str = Field(..., pattern="^(?i:buy|sell)$")
    shares: float = Field(..., gt=0)
    price: float | None = Field(None, ge=0)
    fees: float = Field(0.0, ge=0)
    date: str | None = None
    notes: str | None = Field(None, max_length=200)


@router.post("/portfolio/transactions")
async def add_tx(request: Request, body: TxIn, portfolio: str | None = None):
    name = _pf(request, portfolio)
    price = body.price
    if price is None:
        price = (await request.app.state.engine.quote(body.symbol)).value.price
    ts = datetime.fromisoformat(body.date).replace(hour=16).timestamp() if body.date else None
    t = request.app.state.store.add_transaction(name, body.symbol, body.side, body.shares, price, body.fees, ts, body.notes)
    _poke(request)
    return t.to_dict()


@router.get("/portfolio/transactions")
async def list_tx(request: Request, portfolio: str | None = None, symbol: str | None = None):
    return [t.to_dict() for t in request.app.state.store.transactions(_pf(request, portfolio), symbol.upper() if symbol else None)]


@router.delete("/portfolio/transactions/{tx_id}")
async def del_tx(request: Request, tx_id: int, portfolio: str | None = None):
    ok = request.app.state.store.delete_transaction(_pf(request, portfolio), tx_id)
    _poke(request, analysis=False)
    return {"deleted": ok}


class WatchIn(BaseModel):
    symbol: str = Field(..., max_length=15)
    notes: str | None = Field(None, max_length=200)


@router.post("/portfolio/watchlist")
async def add_watch(request: Request, body: WatchIn, portfolio: str | None = None):
    sym = request.app.state.store.watch(_pf(request, portfolio), body.symbol, body.notes)
    _poke(request)
    return {"symbol": sym}


@router.delete("/portfolio/watchlist/{symbol}")
async def del_watch(request: Request, symbol: str, portfolio: str | None = None):
    return {"removed": request.app.state.store.unwatch(_pf(request, portfolio), symbol)}


class MetaIn(BaseModel):
    stop_loss: float | None = Field(None, gt=0)
    take_profit: float | None = Field(None, gt=0)
    notes: str | None = Field(None, max_length=200)
    clear: bool = False


@router.put("/portfolio/positions/{symbol}")
async def set_meta(request: Request, symbol: str, body: MetaIn, portfolio: str | None = None):
    request.app.state.store.set_position_meta(_pf(request, portfolio), symbol, body.stop_loss, body.take_profit,
                                              body.notes, body.clear)
    _poke(request, analysis=False)
    return {"ok": True}


@router.get("/portfolio/export")
async def export(request: Request, portfolio: str | None = None):
    return request.app.state.store.export(_pf(request, portfolio))


@router.post("/portfolio/import")
async def import_(request: Request, data: dict, portfolio: str | None = None, replace: bool = False):
    res = request.app.state.store.import_(data, portfolio, replace)
    _poke(request)
    return res


# --------------------------------------------------------------------------- alert rules
class RuleIn(BaseModel):
    symbol: str = Field(..., max_length=15)
    kind: str
    value: float
    one_shot: bool = True
    note: str | None = Field(None, max_length=200)


@router.get("/alerts")
async def list_rules(request: Request, portfolio: str | None = None):
    return {"kinds": RULE_KINDS, "rules": [r.to_dict() for r in request.app.state.store.rules(_pf(request, portfolio))]}


@router.post("/alerts")
async def add_rule(request: Request, body: RuleIn, portfolio: str | None = None):
    r = request.app.state.store.add_rule(_pf(request, portfolio), body.symbol, body.kind, body.value, body.one_shot, body.note)
    _poke(request, analysis=body.kind.startswith(("rsi", "score")))
    return r.to_dict()


@router.delete("/alerts/{rule_id}")
async def del_rule(request: Request, rule_id: int, portfolio: str | None = None):
    return {"deleted": request.app.state.store.delete_rule(_pf(request, portfolio), rule_id)}


# --------------------------------------------------------------------------- signals
@router.get("/signals")
async def list_signals(request: Request, portfolio: str | None = None, limit: int = Query(100, le=1000),
                       symbol: str | None = None, since: float | None = None):
    return request.app.state.store.signals(_pf(request, portfolio), limit, symbol.upper() if symbol else None, since)


@router.post("/signals/ack")
async def ack(request: Request, id: int | None = None, portfolio: str | None = None):
    return {"acknowledged": request.app.state.store.acknowledge(_pf(request, portfolio), id)}


@router.get("/signals/stream")
async def stream(request: Request):
    """Server-Sent Events: `signal`, `snapshot` and `status` events pushed by the in-process monitor."""
    hub = request.app.state.hub
    q = hub.subscribe()

    async def gen():
        try:
            yield "retry: 5000\n\n"
            yield f"event: status\ndata: {json.dumps(jsonable(request.app.state.monitor.status()))}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: {msg['event']}\ndata: {json.dumps(jsonable(msg['data']))}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- monitor / notifications
@router.get("/monitor")
async def monitor_status(request: Request):
    st = request.app.state.monitor.status()
    st["note"] = getattr(request.app.state, "monitor_note", None)
    return st


@router.post("/monitor/start")
async def monitor_start(request: Request):
    start_monitor(request.app)
    await asyncio.sleep(0.2)
    return await monitor_status(request)


@router.post("/monitor/stop")
async def monitor_stop(request: Request):
    mon = request.app.state.monitor
    mon.stop()
    t = getattr(request.app.state, "monitor_task", None)
    if t:
        try:
            await asyncio.wait_for(t, 10)
        except Exception:
            pass
    return mon.status()


@router.post("/monitor/refresh")
async def monitor_refresh(request: Request):
    mon = request.app.state.monitor
    if mon.running:
        mon.poke(analysis=True)
        return {"ok": True, "mode": "poked running monitor"}
    await mon.analysis_sweep()
    return {"ok": True, "mode": "ran one sweep", "status": mon.status()}


@router.post("/notify/test")
async def notify_test(request: Request, channel: str = "all"):
    return await request.app.state.hub.test(channel)


def start_monitor(app) -> None:
    mon = app.state.monitor
    if mon.running:
        return
    holder = mon.lock.holder()
    if holder and (holder.get("pid"), holder.get("host")) != (mon.lock.me["pid"], mon.lock.me["host"]):
        app.state.monitor_note = (f"Another monitor is running (pid {holder.get('pid')}); this server shows its "
                                  f"saved signals but won't send duplicate alerts.")
        return

    async def runner():
        try:
            await mon.run()
        except MonitorBusy as e:
            app.state.monitor_note = e.message
        except ABGError as e:  # pragma: no cover
            app.state.monitor_note = e.message

    app.state.monitor_note = None
    app.state.monitor_task = asyncio.ensure_future(runner())
