"""Stock desk API.

    GET  /api/stocks                 status, today's candidates, scorecard
    GET  /api/stocks/today           the last daily pass (cheap: read straight from disk)
    POST /api/stocks/daily           run the daily pass now  {post?: false}
    GET  /api/stocks/memo/{id}       one pitch's full write-up (markdown)
    POST /api/stocks/score           mark every pitch against the benchmark now
    POST /api/stocks/sweep           run one paged refresh pass  {page?: int}
    POST /api/stocks/backfill        fill missing history  {symbols?: [..], period?: "10y"}
    GET  /api/stocks/universe        which symbols are followed, and store coverage
    POST /api/stocks/universe        {index: "sp500"} from FMP, or {symbols: [...]} / {extra: [...]}
    GET  /api/stocks/ledger          the last N candidates, taken or not
    GET  /api/stocks/rules           the active rule set (no secrets)
    GET  /api/stocks/research        the latest weekly study; POST starts one ({scope: quick|full})

Everything works on demand even with ABG_STOCKS_ENABLED=false; the switch controls the schedule and
Discord posting.  ``POST /daily`` defaults to post=false from the API so a page click can never fire
a Discord post by accident.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from abg.utils import jsonable

router = APIRouter(prefix="/api/stocks")
MAX_SYMBOLS = 2000


def _desk(request: Request):
    m = getattr(request.app.state, "monitor", None)
    d = getattr(m, "stocks", None)
    if d is None:
        d = getattr(request.app.state, "stock_desk", None)
        if d is None:
            from .desk import StockDesk
            d = StockDesk(request.app.state.settings, request.app.state.engine, None,
                          symbols_fn=(lambda: getattr(m, "symbols", []) or []) if m is not None else None)
            request.app.state.stock_desk = d
    return d


def _symbols(body: dict, key: str = "symbols") -> list[str]:
    raw = body.get(key) or []
    if isinstance(raw, str):
        raw = [x for x in raw.replace("\n", ",").split(",")]
    if not isinstance(raw, list):
        raise HTTPException(400, f"{key} must be a list or a comma-separated string")
    out, seen = [], set()
    for x in raw:
        s = str(x).strip().upper()
        if not s or s in seen:
            continue
        if len(s) > 12 or not all(ch.isalnum() or ch in ".-^" for ch in s):
            raise HTTPException(400, f"{s!r} does not look like a symbol")
        seen.add(s)
        out.append(s)
    if len(out) > MAX_SYMBOLS:
        raise HTTPException(400, f"at most {MAX_SYMBOLS} symbols at a time")
    return out


@router.get("")
async def overview(request: Request):
    d = _desk(request)
    return jsonable({"status": d.status(), "today": d.today(), "scorecard": d.scorecard()})


@router.get("/today")
async def today(request: Request):
    return jsonable(_desk(request).today())


@router.get("/rules")
async def rules(request: Request):
    from .config import describe
    d = _desk(request)
    return jsonable({"rules": describe(d.cfg), "books": d.cfg.get("books"),
                     "regime": d.cfg.get("regime"), "stress": d.cfg.get("stress"),
                     "allocation": d.cfg.get("allocation"), "risk": d.cfg.get("risk"),
                     "costs": d.cfg.get("costs")})


@router.post("/daily")
async def daily(request: Request, body: dict = Body(default={})):
    d = _desk(request)
    post = bool(body.get("post", False))
    if post and not d._webhook():
        raise HTTPException(400, "no Discord webhook configured (ABG_STOCKS_WEBHOOK_URL)")
    try:
        return jsonable(await d.daily(post=post, enrich=body.get("enrich")))
    except FileNotFoundError as e:
        raise HTTPException(409, str(e))


@router.get("/memo/{pid}")
async def memo(request: Request, pid: str):
    md = _desk(request).memo(pid)
    if md is None:
        raise HTTPException(404, "no such write-up")
    return {"id": pid, "markdown": md}


@router.post("/score")
async def score(request: Request):
    return jsonable(_desk(request).scorecard())


@router.post("/sweep")
async def sweep(request: Request, body: dict = Body(default={})):
    page = body.get("page")
    if page is not None:
        try:
            page = max(1, min(int(page), MAX_SYMBOLS))
        except (TypeError, ValueError):
            raise HTTPException(400, "page must be a number")
    return jsonable(await _desk(request).sweep(page))


@router.post("/backfill")
async def backfill(request: Request, body: dict = Body(default={})):
    d = _desk(request)
    syms = _symbols(body) or None
    period = str(body.get("period") or "").strip() or None
    return jsonable(await d.backfill(syms, period))


@router.get("/universe")
async def universe(request: Request):
    d = _desk(request)
    cov = d.store.coverage()
    core = d.u.core()
    rows = [{"symbol": s, "bars": (c.bars if (c := cov.get(d.u.stored(s))) else 0),
             "last_day": c.last_day if c else None, "age_h": round(c.age_h, 1) if c and c.age_h else None,
             "provider": c.provider if c else None, "error": c.error if c else None,
             "delisted": bool(c.delisted) if c else False}
            for s in core]
    missing = [r["symbol"] for r in rows if r["bars"] < int(getattr(d.s, "stocks_min_bars", 250))]
    return jsonable({"universe": d.u.describe(), "store": d.store.stats(), "sweep": d.sweeper.status(),
                     "symbols": rows, "missing": missing})


@router.post("/universe")
async def set_universe(request: Request, body: dict = Body(...)):
    d = _desk(request)
    if body.get("index"):
        name = str(body["index"]).strip().lower()
        try:
            out = await d.sweeper.refresh_index(name)
        except (RuntimeError, ValueError) as e:
            raise HTTPException(400, str(e))
        from .config import build_config
        d.cfg = build_config(d.s, d.u)          # Book A rotates whatever the new list brought in
        return jsonable({**out, "universe": d.u.describe()})
    if "symbols" in body:
        syms = _symbols(body)
        try:
            n = d.u.save_index(d.store, str(body.get("name") or "custom"), syms, source="api")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return jsonable({"symbols": n, "universe": d.u.describe()})
    if "extra" in body:
        n = d.u.save_extra(d.store, _symbols(body, "extra"))
        return jsonable({"extra": n, "universe": d.u.describe()})
    raise HTTPException(400, "send {index: 'sp500'}, {symbols: [...]} or {extra: [...]}")


@router.get("/ledger")
async def ledger(request: Request, limit: int = 200):
    limit = max(1, min(int(limit), 5000))
    return jsonable({"rows": _desk(request).ledger_tail(limit)})


@router.get("/research")
async def research_latest(request: Request):
    """The newest saved study (report + markdown), and whether one is running."""
    d = _desk(request)
    return jsonable({"busy": d.research_busy, "report": d.last_research(), "markdown": d.research_md(),
                     "schedule": getattr(d.s, "stocks_research", "weekly")})


@router.post("/research")
async def research_run(request: Request, body: dict = Body(default={})):
    """Start a study in the background (it takes minutes). Never posts: the R&D desk does that on Saturdays.

    {scope: "quick"} = ablations only over 5 years; "full" (default) = ablations, walk-forward and
    early-warning over ABG_STOCKS_RESEARCH_YEARS.
    """
    import asyncio
    d = _desk(request)
    if d.research_busy:
        raise HTTPException(409, f"a study is already running ({d.research_busy})")
    quick = str(body.get("scope") or "full").lower() == "quick"
    kw = {"years": 5, "do_wf": False, "do_ew": False} if quick else {}

    async def go():
        try:
            await d.research(post_url=None, **kw)
        except Exception:                      # recorded on the desk's error list by research()
            pass
    d._research_task = asyncio.create_task(go())   # keep a reference so it isn't garbage-collected
    return {"started": True, "scope": "quick" if quick else "full"}
