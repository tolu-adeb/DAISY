"""Pitch desk API.

    GET  /api/pitch               status, last screen, scorecard
    POST /api/pitch/screen        run the screen now
    POST /api/pitch/make          {symbol, ai?} -> memo (dashboard only; Discord posts come from the weekly batch)
    GET  /api/pitch/memo/{id}     the memo markdown
    POST /api/pitch/score         mark every pitch against SPY now

Screens and memos work on demand even with ABG_PITCH_ENABLED=false; the switch controls the weekly batch and Discord.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from abg.utils import jsonable

router = APIRouter(prefix="/api/pitch")


def _desk(request: Request):
    m = getattr(request.app.state, "monitor", None)
    d = getattr(m, "pitch", None)
    if d is None:
        d = getattr(request.app.state, "pitch_desk", None)
        if d is None:
            from .desk import PitchDesk
            store = getattr(request.app.state, "store", None)
            d = PitchDesk(request.app.state.settings, request.app.state.engine, None,
                          symbols_fn=(lambda: getattr(m, "symbols", []) or []) if m is not None else None)
            request.app.state.pitch_desk = d
    return d


@router.get("")
async def overview(request: Request):
    d = _desk(request)
    return jsonable({"status": d.status(), "screen": d.last_screen(), "scorecard": d.scorecard()})


@router.post("/screen")
async def run_screen(request: Request):
    return jsonable(await _desk(request).screen())


@router.post("/make")
async def make(request: Request, body: dict = Body(...)):
    sym = str(body.get("symbol") or "").strip().upper()
    if not sym or len(sym) > 12:
        raise HTTPException(400, "symbol required")
    try:
        r = await _desk(request).make(sym, post=False, ai=bool(body.get("ai")) if "ai" in body else None)
    except ValueError as e:
        raise HTTPException(404, str(e))
    r.pop("pack", None)
    return jsonable(r)


@router.get("/memo/{pid}")
async def memo(request: Request, pid: str):
    md = _desk(request).memo(pid)
    if md is None:
        raise HTTPException(404, "no such memo")
    return {"id": pid, "markdown": md}


@router.post("/score")
async def score(request: Request):
    return jsonable(await _desk(request).score())
