"""REST endpoints for external signals (docs/11): interpret, ingest, list, act on tracked ideas."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..extsignals.lifecycle import FINAL_STATES, OPEN_STATES
from ..extsignals.parser import looks_like_signal, parse
from ..utils import jsonable

router = APIRouter(prefix="/api/ext", tags=["external signals"])


def _tr(request: Request):
    tr = getattr(request.app.state.monitor, "ext", None)
    if tr is None:
        raise HTTPException(400, "external signals are disabled (ABG_EXT_ENABLED=false)")
    return tr


def _idea(tr, idea_id: int):
    idea = tr.store.get(idea_id)
    if idea is None:
        raise HTTPException(404, f"idea #{idea_id} not found")
    return idea


class TextIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    author: str | None = Field(None, max_length=80)
    source: str = Field("manual", max_length=40)


@router.post("/parse")
async def parse_text(body: TextIn):
    """Preview how a message is interpreted, without tracking it."""
    p = parse(body.text)
    return {"parsed": p.to_dict(), "trackable": p.trackable, "looks_like_signal": looks_like_signal(body.text)}


@router.post("/ingest")
async def ingest(request: Request, body: TextIn):
    tr = _tr(request)
    res = await asyncio.wait_for(tr.ingest(body.text, source=body.source, author=body.author), 90)
    return jsonable(res)


@router.get("/ideas")
async def ideas(request: Request, status: str = Query("all", pattern="^(all|open|closed|pending|active|final)$"),
                symbol: str | None = None, limit: int = Query(200, ge=1, le=2000)):
    tr = _tr(request)
    st = {"all": None, "open": OPEN_STATES, "final": FINAL_STATES}.get(status, {status})
    return {"ideas": [tr.view(i) for i in tr.store.ideas(st, symbol.upper() if symbol else None, limit)],
            "stats": tr.stats(), "status": tr.status()}


@router.get("/ideas/{idea_id}")
async def idea(request: Request, idea_id: int):
    tr = _tr(request)
    return {"idea": tr.view(_idea(tr, idea_id)), "events": jsonable(tr.store.events(idea_id, limit=500))}


class CloseIn(BaseModel):
    price: float | None = Field(None, gt=0)
    fraction: float | None = Field(None, gt=0, le=1)


@router.post("/ideas/{idea_id}/close")
async def close(request: Request, idea_id: int, body: CloseIn):
    tr = _tr(request)
    if _idea(tr, idea_id).status != "active":
        raise HTTPException(400, "only an active (entered) idea can be closed; cancel a pending one")
    return {"idea": tr.view(await tr.close(idea_id, body.price, body.fraction))}


@router.post("/ideas/{idea_id}/cancel")
async def cancel(request: Request, idea_id: int):
    tr = _tr(request)
    if _idea(tr, idea_id).status not in OPEN_STATES:
        raise HTTPException(400, "idea is already finished")
    return {"idea": tr.view(await tr.cancel(idea_id))}


class EditIn(BaseModel):
    stop: float | None = Field(None, gt=0)
    targets: list[float] | None = None
    entry_low: float | None = Field(None, gt=0)
    entry_high: float | None = Field(None, gt=0)
    stop_basis: str | None = Field(None, pattern="^(touch|close)$")


@router.post("/ideas/{idea_id}/edit")
async def edit(request: Request, idea_id: int, body: EditIn):
    tr = _tr(request)
    if _idea(tr, idea_id).status not in OPEN_STATES:
        raise HTTPException(400, "idea is already finished")
    return {"idea": tr.view(await tr.edit(idea_id, **body.model_dump()))}


@router.get("/events")
async def events(request: Request, limit: int = Query(100, ge=1, le=1000), since: float | None = None):
    return {"events": jsonable(_tr(request).store.events(None, limit, since))}


@router.get("/stats")
async def stats(request: Request):
    return _tr(request).stats()


@router.get("/status")
async def status(request: Request):
    mon = request.app.state.monitor
    tr = _tr(request)
    return {**tr.status(), "monitor_running": mon.running,
            "discord": mon.poller.describe() if mon.poller else None,
            "messages": jsonable(tr.store.messages(20))}


@router.get("/discord/check")
async def discord_check(request: Request):
    mon = request.app.state.monitor
    if mon.poller is None:
        raise HTTPException(400, "external signals are disabled")
    return await asyncio.wait_for(mon.poller.check(), 60)
