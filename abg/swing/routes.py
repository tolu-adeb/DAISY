"""Swing desk API for the dashboard (docs/19).

    GET  /api/swing                  status, gate, scoreboard, every idea (book), recent alerts
    GET  /api/swing/record           track record: by author / tier / month, cumulative R curve
    GET  /api/swing/research         the rules file, proposals, R&D reports
    GET  /api/swing/variants         variants of the rules replayed on your data (cached until the data changes)
    GET  /api/swing/alerts           the alert log (what was posted, kept on the dashboard, or summarised)
    POST /api/swing/parse            {text} or {fields} -> parsed levels + the gate's verdict (nothing is saved)
    POST /api/swing/ideas            {fields, author} -> track it (graded immediately)
    POST /api/swing/ideas/cancel     {key} -> cancel a working order
    POST /api/swing/run              run a pass now (fresh prices, fills, exits)
    POST /api/swing/digest           {period, post} build the digest (and post it when a webhook is set)
    POST /api/swing/research/run     {deep} run the R&D agent once (needs an API key; costs tokens)
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from abg.utils import jsonable

router = APIRouter(prefix="/api/swing")


def _desk(request: Request):
    m = getattr(request.app.state, "monitor", None)
    d = getattr(m, "swing", None)
    if d is None:
        raise HTTPException(404, "swing desk is off (ABG_SWING_ENABLED=false)")
    return d


def _live_desk(request: Request):
    """Actions that change the book or post to Discord run only where the monitor runs (never twice)."""
    d = _desk(request)
    m = request.app.state.monitor
    if not getattr(m, "running", False):
        raise HTTPException(409, getattr(request.app.state, "monitor_note", None) or
                            "the monitor isn't running in this window - swing actions are read-only here")
    return d


@router.get("")
async def overview(request: Request):
    d = _desk(request)
    ideas = d.ideas()
    from swingdesk.monitor import scoreboard
    return jsonable({"status": d.status(), "gate": d.gate(), "scoreboard": scoreboard(d.main_book()), "ideas": ideas,
                     "alerts": d.alerts(40)})


@router.get("/record")
async def record(request: Request):
    return jsonable(_desk(request).record())


@router.get("/alerts")
async def alerts(request: Request, limit: int = 200, history: bool = False):
    return jsonable({"alerts": _desk(request).alerts(max(1, min(limit, 2000)), include_history=history)})


@router.get("/research")
async def research(request: Request):
    d = _desk(request)
    return jsonable({"config": d.config(), **d.research_files()})


@router.get("/variants")
async def variants(request: Request):
    try:
        return jsonable({"variants": await _desk(request).variants()})
    except Exception as e:
        return jsonable({"variants": [], "error": f"{type(e).__name__}: {e}"[:300]})


@router.post("/parse")
async def parse(request: Request, body: dict = Body(...)):
    d = _desk(request)
    text, fields = body.get("text"), body.get("fields")
    if not text and not fields:
        raise HTTPException(400, "send {text} or {fields}")
    return jsonable(await d.preview(text=text, fields=fields))


@router.post("/ideas")
async def add(request: Request, body: dict = Body(...)):
    d = _live_desk(request)
    if not body.get("fields"):
        raise HTTPException(400, "send {fields: {...}}")
    try:
        return jsonable(await d.add(body["fields"], body.get("author"), body.get("posted_et")))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/ideas/cancel")
async def cancel(request: Request, body: dict = Body(...)):
    try:
        return jsonable(await _live_desk(request).cancel(str(body.get("key") or "")))
    except KeyError:
        raise HTTPException(404, "no such idea")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/run")
async def run(request: Request):
    d = _live_desk(request)
    if d.busy:
        raise HTTPException(409, f"busy: {d.busy}")
    r = await d.run_pass("manual")
    return jsonable({k: r[k] for k in ("alerts", "seed", "signals", "live", "symbols", "delivered")})


@router.post("/digest")
async def digest(request: Request, body: dict = Body(default={})):
    period = body.get("period") or "daily"
    if period not in ("daily", "weekly", "monthly"):
        raise HTTPException(400, "period: daily | weekly | monthly")
    post = bool(body.get("post"))
    d = _live_desk(request) if post else _desk(request)      # a preview is fine anywhere; posting only where the monitor runs
    return jsonable(await d.digest(period, post=post))


@router.post("/research/run")
async def research_run(request: Request, body: dict = Body(default={})):
    d = _live_desk(request)
    if d.busy:
        raise HTTPException(409, f"busy: {d.busy}")
    try:
        return jsonable(await d.research(bool(body.get("deep"))))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- wide scan (docs/19.8)
@router.get("/wide")
async def wide(request: Request):
    return jsonable(_desk(request).wide_view())


@router.post("/wide/run")
async def wide_run(request: Request):
    d = _live_desk(request)
    if not d.wide_ready():
        raise HTTPException(400, "set ABG_POLYGON_API_KEY in .env to use the wide scan")
    if d.wide_busy:
        raise HTTPException(409, f"busy: {d.wide_busy}")
    from abg.swing.wide import last_session
    day = last_session()
    d.wide_start(day)
    return {"started": True, "day": str(day)}


@router.post("/wide/backtest")
async def wide_backtest(request: Request, body: dict = Body(default={})):
    d = _live_desk(request)
    if not d.wide_ready():
        raise HTTPException(400, "set ABG_POLYGON_API_KEY in .env to use the wide scan")
    t = getattr(d, "_wide_bt_task", None)
    if t is not None and not t.done():
        raise HTTPException(409, "a backtest is already running")
    from datetime import date
    days = d.wide_store.days()
    dflt = str(days[min(60, len(days) - 1)]) if len(days) > 60 else "2026-07-01"     # all stored history after a warm-up
    try:
        start = date.fromisoformat(body.get("start") or dflt)
    except ValueError:
        raise HTTPException(400, "start: YYYY-MM-DD")
    d.wide_backtest_start(start)
    return {"started": True, "start": str(start)}
