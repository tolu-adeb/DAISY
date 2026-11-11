"""Stock desk API.

    GET  /api/stocks                 status, today's candidates, scorecard
    GET  /api/stocks/today           the last daily pass (cheap: read straight from disk)
    POST /api/stocks/daily           run the daily pass now  {post?: false}
    GET  /api/stocks/memo/{id}       one pitch's full write-up (markdown)
    GET  /api/stocks/chart/{name}    a pitch chart (PNG next to the memo)
    GET  /api/stocks/deck/{id}       the pitch as the AIBG 5-slide PowerPoint (needs python-pptx)
    POST /api/stocks/score           mark every pitch against the benchmark now
    POST /api/stocks/sweep           run one paged refresh pass  {page?: int}
    POST /api/stocks/backfill        fill missing history  {symbols?: [..], period?: "10y"}
    GET  /api/stocks/universe        which symbols are followed, and store coverage
    POST /api/stocks/universe        {index: "sp500"} from FMP, or {symbols: [...]} / {extra: [...]}
    GET  /api/stocks/ledger          the last N candidates, taken or not
    GET  /api/stocks/rules           the active rule set (no secrets)
    GET  /api/stocks/research        the latest weekly study; POST starts one ({scope: quick|full})
    POST /api/stocks/market          whole-market bars from Polygon -> the market tier  {max_calls?, force?}

The pitching desk and its research day (docs/22):

    GET  /api/stocks/desk            everything the Today page needs in one call
    GET  /api/stocks/watchlist       the morning watchlist and what changed; POST .../scan runs it now
    GET  /api/stocks/pitches         the pitch book (?status=open|closed)
    GET  /api/stocks/pitch/{key}     one pitch: pack, memo, lifecycle history, notes and votes
    GET  /api/stocks/scorecard       the pitch track record, the watchlist score, the untested flags
    GET  /api/stocks/explore?q=      search every researched name; GET /api/stocks/name/{symbol} for one
    GET  /api/stocks/news            stored headlines and filings (?symbol=)
    GET  /api/stocks/live            the live layer's heartbeat and recent alerts; POST .../tick runs one
    GET/POST /api/stocks/notes/{id}  team notes and votes (a viewer may post these)
    GET/POST /api/stocks/holdings    the fund's real holdings, for the fit section (kept out of the repo)
    GET  /api/stocks/glossary        the shared glossary and status sentences
    GET  /api/stocks/settings        feature switches, schedule and which keys are present (yes/no only)
    GET  /api/stocks/recap           the weekly recap; POST builds it now ({post?: false})

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


@router.get("/chart/{name}")
async def chart(request: Request, name: str):
    """A pitch chart PNG saved next to its memo (names only, no paths)."""
    import re
    from fastapi.responses import FileResponse
    if not re.fullmatch(r"[\w.-]+\.png", name) or ".." in name:
        raise HTTPException(400, "bad chart name")
    path = _desk(request).dir / "memos" / name
    if not path.is_file():
        raise HTTPException(404, "no such chart")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@router.get("/deck/{key}")
async def deck(request: Request, key: str):
    """Build (or rebuild) the AIBG 5-slide deck for a saved pitch and download it."""
    import asyncio
    import re
    from fastapi.responses import FileResponse
    if not re.fullmatch(r"[\w.-]+", key):
        raise HTTPException(400, "bad pitch id")
    try:
        path = await asyncio.to_thread(_desk(request).deck, key)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(501, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return FileResponse(path, filename=path.name,
                        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


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


@router.post("/market")
async def market(request: Request, body: dict = Body(default={})):
    """Sync whole-market bars from Polygon and rebuild the market tier.  {max_calls?: int, force?: bool}.
    The first fill is up to ABG_STOCKS_MARKET_SESSIONS calls at 5 a minute - pass max_calls to keep a
    click short; it carries on from where it stopped next time."""
    d = _desk(request)
    mc = body.get("max_calls")
    if mc is not None:
        try:
            mc = max(0, min(int(mc), 1000))
        except (TypeError, ValueError):
            raise HTTPException(400, "max_calls must be a number")
    return jsonable(await d.market_refresh(max_calls=mc, force=bool(body.get("force"))))


# --------------------------------------------------------------------------- the pitching desk (docs/22)
def _slim(c: dict) -> dict:
    """A candidate for a list: what a card shows, without the long text."""
    pl, rec = c.get("plan") or {}, c.get("rec") or {}
    return {"id": c["id"], "symbol": c["symbol"], "name": c.get("name"), "sector": c.get("sector"), "book": c["book"],
            "book_label": c.get("book_label"), "action": c.get("action"), "price": c.get("price"), "stop": c.get("stop"),
            "rank": c.get("rank"), "of": c.get("of"), "score": c.get("score"), "reason": c.get("reason"),
            "rating": rec.get("rating"), "daisy_rank": rec.get("daisy_rank"), "one_liner": rec.get("one_liner"),
            "conviction": rec.get("conviction"), "open_pitch": rec.get("open_pitch"),
            "entry": pl.get("entry"), "targets": {k: (pl.get("targets") or {}).get(k) for k in ("t1", "t2", "review_date")},
            "risk": {k: (pl.get("risk") or {}).get(k) for k in ("rr", "pct_of_fund", "risk_pct_of_fund", "stop_pct", "bound", "shares")},
            "grades": c.get("grades"), "flags": c.get("flags"), "gates": c.get("gates"), "facts": c.get("facts"),
            "charts": c.get("charts"), "what_happened": c.get("what_happened")}


@router.get("/desk")
async def desk_today(request: Request):
    d = _desk(request)
    t = d.today()
    wm = d.watch_meta()
    return jsonable({
        "state": t.get("state"), "pitch_book": d.book.counts((t.get("state") or {}).get("date")),
        "candidates": [_slim(c) for c in t.get("candidates") or []],
        "board": d.board(), "last_daily": d.state().get("last_daily"),
        "watch": {"meta": wm, "rows": d.watch_rows()},
        "mood": wm.get("mood") or d.market_mood(), "shocks": d.last_shocks(),
        "heat": d.heat(), "calendar": d.calendar(), "live": {"heartbeat": d.live, "feed": d.live_feed()[:40]},
        "screen": d.screen_meta(), "tag": (getattr(d.s, "stocks_tag", "") or "") or None,
    })


@router.get("/watchlist")
async def watchlist(request: Request):
    d = _desk(request)
    return jsonable({"meta": d.watch_meta(), "rows": d.watch_rows(), "score": d.watch_score()})


@router.post("/watchlist/scan")
async def watchlist_scan(request: Request, body: dict = Body(default={})):
    d = _desk(request)
    post = bool(body.get("post", False))
    if post and not d._webhook():
        raise HTTPException(400, "no Discord webhook configured (ABG_STOCKS_WEBHOOK_URL)")
    return jsonable(await d.morning(post=post))


@router.get("/watchlist/{symbol}/history")
async def watch_history(request: Request, symbol: str):
    return jsonable(_desk(request).db.history("watch", symbol.strip().upper()[:12]))


@router.get("/pitches")
async def pitches(request: Request, status: str | None = None):
    d = _desk(request)
    rows = await _thread(d.mark_pitches)
    if status == "open":
        rows = [r for r in rows if r.get("status") in ("new", "active")]
    elif status == "closed":
        rows = [r for r in rows if r.get("status") == "closed"]
    return jsonable({"counts": d.book.counts(), "rows": sorted(rows, key=lambda r: r["as_of"], reverse=True)[:300]})


@router.get("/pitch/{key}")
async def pitch(request: Request, key: str):
    import re
    if not re.fullmatch(r"[\w.^-]+", key):
        raise HTTPException(400, "bad pitch id")
    d = _desk(request)
    pack = d.pack(key)
    row = d.book.get(key) or (d.book.get(pack["id"]) if pack else None)
    if pack is None and row is None:
        raise HTTPException(404, "no such pitch")
    pid = (pack or row)["id"]
    from .desk import _public
    return jsonable({"id": pid, "pack": _public(pack) if pack else None, "pack_json": pack, "memo": d.memo(pid),
                     "row": row, "history": d.book.history(pid), "notes": d.notes(pid), "votes": d.votes(pid),
                     "writeup": (pack or {}).get("writeup")})


@router.get("/scorecard")
async def scorecard(request: Request):
    d = _desk(request)
    sc = await _thread(d.scorecard)
    return jsonable({**sc, "watchlist": d.watch_score(), "forming": d.forming_stats()})


@router.get("/explore")
async def explore(request: Request, q: str = "", limit: int = 25, only: str | None = None):
    d = _desk(request)
    if len(q) > 40:
        raise HTTPException(400, "query too long")
    return jsonable({"as_of": d.screen_meta().get("day"), "coverage": d.screen_meta(), "q": q,
                     "rows": d.explore(q, max(1, min(100, limit)), only if only in ("eligible", "new") else None),
                     "listings": d.new_listings()})


@router.get("/name/{symbol}")
async def name(request: Request, symbol: str):
    import re
    if not re.fullmatch(r"[A-Za-z0-9.^-]{1,12}", symbol):
        raise HTTPException(400, "bad symbol")
    card = _desk(request).name_card(symbol)
    if card is None:
        d = _desk(request)
        if not d.screen_meta().get("day"):
            raise HTTPException(404, "The research universe has not been built yet. It is built by the daily pass (16:20 ET) "
                                     "and refreshed by the morning pass; search will work after the next one.")
        raise HTTPException(404, f"{symbol.upper()} is not in the research universe. DAISY covers US-listed common stocks and "
                                 "ADRs with recent prices: check the ticker, or it may be an ETF, a fund or not US-listed.")
    return jsonable(card)


@router.get("/bars/{symbol}")
async def bars(request: Request, symbol: str, sessions: int = 520):
    """Daily bars, EMAs and plan levels for the Explore chart."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9.^-]{1,12}", symbol):
        raise HTTPException(400, "bad symbol")
    out = await _desk(request).bars_for(symbol, max(30, min(1300, sessions)))
    if out is None:
        raise HTTPException(404, "no price history for that ticker")
    return jsonable(out)


@router.get("/news")
async def news_list(request: Request, symbol: str | None = None, limit: int = 40):
    from . import news as N
    d = _desk(request)
    rows = N.recent(d.db, symbol.strip().upper() if symbol else None, limit=max(1, min(200, limit)))
    return jsonable({"rows": [{k: a.get(k) for k in ("at", "symbol", "title", "url", "source", "kind", "sent", "rel")} for a in rows],
                     "last": d.last_news, "shocks": d.last_shocks()})


@router.post("/news/refresh")
async def news_refresh(request: Request, body: dict = Body(default={})):
    d = _desk(request)
    out = await d.news_refresh(_symbols(body)[:40] if body.get("symbols") else None)
    return jsonable({k: v for k, v in out.items() if k != "material"})


@router.get("/live")
async def live(request: Request):
    d = _desk(request)
    return jsonable({"heartbeat": d.live, "feed": d.live_feed(), "forming": d.forming_stats()})


@router.post("/live/tick")
async def live_tick(request: Request):
    """One look at the watched names now. Never posts from the API: a click cannot fire a Discord alert."""
    out = await _desk(request).live_tick(post=False)
    return jsonable(out)


@router.get("/notes/{pid}")
async def notes(request: Request, pid: str):
    d = _desk(request)
    return jsonable({"notes": d.notes(pid), "votes": d.votes(pid)})


@router.post("/notes/{pid}")
async def add_note(request: Request, pid: str, body: dict = Body(...)):
    d = _desk(request)
    try:
        n = d.add_note(pid, str(body.get("name") or ""), body.get("vote"), body.get("text"))
    except KeyError:
        raise HTTPException(404, "no such pitch")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return jsonable({"note": n, "votes": d.votes(pid)})


@router.get("/holdings")
async def holdings(request: Request):
    h = _desk(request).fund_holdings()
    return {"provided": bool(h), "holdings": [{"symbol": k, "weight": v} for k, v in sorted((h or {}).items())]}


@router.post("/holdings")
async def set_holdings(request: Request, body: dict = Body(...)):
    rows = body.get("holdings")
    if not isinstance(rows, (list, dict)) or len(rows) > 300:
        raise HTTPException(400, "holdings must be a list of {symbol, weight} (at most 300)")
    h = _desk(request).set_fund_holdings(rows)
    return {"provided": bool(h), "holdings": [{"symbol": k, "weight": v} for k, v in sorted((h or {}).items())]}


@router.get("/portfolio")
async def portfolio(request: Request):
    """The fund's recorded portfolio. Anyone signed in may read it; changes need the admin token."""
    d = _desk(request)
    v = await _thread(d.portfolio_view)
    return jsonable({**v, "log": d.portfolio.log()})


@router.post("/portfolio")
async def portfolio_add(request: Request, body: dict = Body(...)):
    d = _desk(request)
    try:
        if "cash" in body and "symbol" not in body:
            return {"cash": d.portfolio.set_cash(body.get("cash"))}
        return jsonable({"row": d.portfolio.add(body)})
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/portfolio/{hid}")
async def portfolio_change(request: Request, hid: int, body: dict = Body(...)):
    """{action: "update" | "close" | "reopen" | "delete", ...fields}"""
    d = _desk(request)
    act = str(body.get("action") or "update")
    try:
        if act == "close":
            return jsonable({"row": d.portfolio.close(hid, body.get("exit_price"), body.get("closed"))})
        if act == "reopen":
            return jsonable({"row": d.portfolio.reopen(hid)})
        if act == "delete":
            return {"deleted": d.portfolio.delete(hid)}
        if act == "update":
            return jsonable({"row": d.portfolio.update(hid, {k: v for k, v in body.items() if k != "action"})})
        raise HTTPException(400, "action must be update, close, reopen or delete")
    except KeyError:
        raise HTTPException(404, "no such position")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/glossary")
async def glossary():
    from . import glossary as G
    from . import rank as RK
    return {"terms": G.TERMS, "status": G.STATUS_WORDS, "ranks": RK.RANK_WORDS, "grades": RK.GRADE_LABEL}


@router.get("/settings")
async def settings_view(request: Request):
    """What is switched on, when things run, and which keys are present. Never a value of a key."""
    d = _desk(request)
    st = d.status()
    s = d.s
    return jsonable({
        "features": st.get("features"), "keys": st.get("keys"), "webhook": st.get("webhook"), "tag": st.get("tag"),
        "enabled": st.get("enabled"), "running": st.get("running"),
        "schedule": {"daily": st.get("daily_time"), "board": getattr(s, "stocks_board_times", None),
                     "watch_scan": getattr(s, "stocks_watch_scan_time", None), "watch_post": getattr(s, "stocks_watch_post_time", None),
                     "live_seconds": getattr(s, "stocks_live_seconds", None), "news_minutes": getattr(s, "stocks_news_minutes", None),
                     "weekly_recap": getattr(s, "stocks_weekly_recap_time", None)},
        "universe": st.get("universe"), "screen": st.get("screen"), "market": st.get("market"),
        "fund_holdings": st.get("fund_holdings"), "fund_equity_set": st.get("fund_equity_set"),
        "pitch_book": st.get("pitch_book"), "errors": st.get("errors"), "last_daily": st.get("last_daily"),
        "live": st.get("live"), "news": st.get("news"),
    })


@router.get("/recap")
async def recap(request: Request):
    d = _desk(request)
    return jsonable(d.db.kv_get("recap_last") or await _thread(d.recap))


@router.post("/recap")
async def recap_now(request: Request, body: dict = Body(default={})):
    d = _desk(request)
    if body.get("post"):
        if not d._webhook():
            raise HTTPException(400, "no Discord webhook configured (ABG_STOCKS_WEBHOOK_URL)")
        return {"posted": await d.recap_post()}
    rec = await _thread(d.recap)
    d.db.kv_set("recap_last", rec)
    return jsonable(rec)


async def _thread(fn, *a):
    import asyncio
    return await asyncio.to_thread(fn, *a)
