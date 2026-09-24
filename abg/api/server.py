"""FastAPI server: REST API + static web dashboard.

Run:  ``abg serve``  or  ``uvicorn abg.api.server:app``
Docs: http://127.0.0.1:8000/docs (OpenAPI, auto-generated)

One ``AnalysisEngine`` is shared by all requests, so the connection pool, cache,
circuit breakers and in-flight request coalescing are shared too: ten browser tabs
asking for AAPL at once cause one upstream fetch.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..config import Settings
from ..engine import AnalysisEngine, AnalyzeOptions
from ..errors import (ABGError, AllProvidersFailed, ConfigError, DataValidationError, InvalidSymbolError)
from ..risk import features as feat
from ..utils import PERIOD_CHOICES, jsonable

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
REQUEST_TIMEOUT = 60.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    from ..live import Monitor, NotificationHub
    from ..portfolio import PortfolioStore
    from .portfolio_routes import start_monitor

    s = Settings()
    app.state.engine = AnalysisEngine(s)
    app.state.store = PortfolioStore.from_settings(s)
    app.state.hub = NotificationHub(s, app.state.store, app.state.engine.http)
    app.state.monitor = Monitor(app.state.engine, app.state.store, app.state.hub)
    app.state.monitor_note = None
    if s.monitor_on_serve:
        start_monitor(app)
    try:
        yield
    finally:
        app.state.monitor.stop()
        task = getattr(app.state, "monitor_task", None)
        if task is not None:
            try:
                await asyncio.wait_for(task, 10)
            except Exception:
                task.cancel()
        await app.state.hub.aclose()
        app.state.store.close()
        await app.state.engine.aclose()


app = FastAPI(title="ABG Intelligence Terminal API", version=__version__, lifespan=lifespan,
              description="Multi-source stock analytics for AI Business Group. Educational use - not investment advice.")
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["*"])


def eng(request: Request) -> AnalysisEngine:
    return request.app.state.engine


@app.exception_handler(ABGError)
async def abg_error(_: Request, e: ABGError):
    from ..portfolio.store import PortfolioError
    status = 400 if isinstance(e, (InvalidSymbolError, ConfigError, PortfolioError)) else \
        502 if isinstance(e, AllProvidersFailed) else 422 if isinstance(e, DataValidationError) else 500
    return JSONResponse(status_code=status, content={"error": e.to_dict()})


@app.exception_handler(ValueError)
async def value_error(_: Request, e: ValueError):
    return JSONResponse(status_code=400, content={"error": {"code": "bad_request", "message": str(e)}})


@app.exception_handler(asyncio.TimeoutError)
async def timeout_error(_: Request, e):
    return JSONResponse(status_code=504, content={"error": {"code": "timeout", "message": "analysis timed out"}})


@app.exception_handler(Exception)
async def unhandled(_: Request, e: Exception):  # last line of defence: log, never crash the worker
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={"error": {"code": "internal", "message": f"{type(e).__name__}: {e}"}})


async def _t(coro):
    return await asyncio.wait_for(coro, REQUEST_TIMEOUT)


# --------------------------------------------------------------------------- endpoints
@app.get("/api/health")
async def health(request: Request):
    return {"status": "ok", "version": __version__}


@app.get("/api/status")
async def status(request: Request):
    return jsonable(eng(request).status())


@app.get("/api/analyze/{symbol}")
async def analyze(request: Request, symbol: str, period: str = "1y", interval: str = "1d",
                  source: str | None = None, ai: bool = True, news: bool = True, options: bool = True,
                  expiry: date | None = None, series: bool = True, refresh: bool = False):
    o = AnalyzeOptions(period=period, interval=interval, source=source or None, ai=ai, news=news, options=options,
                       expiry=expiry, include_series=series, use_cache=not refresh)
    return await _t(eng(request).analyze(symbol, o))


@app.get("/api/quote/{symbol}")
async def quote(request: Request, symbol: str, source: str | None = None):
    f = await _t(eng(request).quote(symbol, source))
    return jsonable({"quote": f.value, "provenance": f.provenance})


@app.get("/api/history/{symbol}")
async def history(request: Request, symbol: str, period: str = "1y", interval: str = "1d", source: str | None = None):
    f = await _t(eng(request).history(symbol, period, interval, source, warmup=False))
    return jsonable({"symbol": f.value.symbol, "interval": f.value.interval, "bars": f.value.to_records(),
                     "provenance": f.provenance})


@app.get("/api/news/{symbol}")
async def news(request: Request, symbol: str, limit: int = Query(20, le=100)):
    from ..analysis.sentiment import analyze_news
    f = await _t(eng(request).news(symbol, limit))
    return jsonable({"sentiment": analyze_news(f.value), "news": f.value, "provenance": f.provenance})


@app.get("/api/options/{symbol}")
async def options(request: Request, symbol: str, expiry: date | None = None):
    r = await _t(eng(request).analyze(symbol, AnalyzeOptions(ai=False, news=False, fundamentals=False, benchmark=False,
                                                             risk=False, expiry=expiry)))
    return {"symbol": r["symbol"], "options": r["options"], "warnings": r["warnings"], "provenance": r["provenance"]}


@app.get("/api/risk/{symbol}")
async def risk(request: Request, symbol: str, period: str = "1y"):
    r = await _t(eng(request).analyze(symbol, AnalyzeOptions(period=period, ai=False, options=True)))
    return {"symbol": r["symbol"], "risk": r["risk"], "features": r["features"], "warnings": r["warnings"]}


@app.get("/api/compare")
async def compare(request: Request, symbols: str = Query(..., description="Comma-separated tickers"), period: str = "1y"):
    syms = [s for s in symbols.split(",") if s.strip()][:12]
    res = await _t(eng(request).compare(syms, period))
    res.pop("reports", None)
    return res


@app.get("/api/features/{symbol}")
async def features(request: Request, symbol: str, period: str = "2y", labels: bool = False, tail: int = Query(300, le=5000)):
    df = await _t(eng(request).feature_frame(symbol, period, labels))
    df = df.tail(tail)
    return jsonable({"schema_version": feat.FEATURE_SCHEMA_VERSION, "columns": list(df.columns),
                     "index": [d.isoformat() for d in df.index], "rows": df.to_numpy().tolist()})


@app.get("/api/schema")
async def schema():
    return feat.schema()


class CSVUpload(BaseModel):
    symbol: str = Field("CSV", max_length=15)
    csv: str = Field(..., max_length=20_000_000)
    period: str = "max"


@app.post("/api/analyze-csv")
async def analyze_csv(request: Request, body: CSVUpload):
    o = AnalyzeOptions(period=body.period, news=False, options=False, fundamentals=False, benchmark=False, ai=False,
                       include_series=True)
    return await _t(eng(request).analyze_csv(body.csv, body.symbol or "CSV", o))


@app.get("/api/meta")
async def meta(request: Request):
    e = eng(request)
    return {"periods": PERIOD_CHOICES, "intervals": ["1d", "1wk", "1mo", "1h", "30m", "15m", "5m"],
            "providers": [p["name"] for p in e.router.status() if p["configured"]],
            "demo": e.settings.allow_synthetic, "ai": bool(e.settings.anthropic_api_key and e.settings.ai_enabled),
            "version": __version__}


# --------------------------------------------------------------------------- portfolio / live
from .portfolio_routes import router as portfolio_router  # noqa: E402

app.include_router(portfolio_router)

# --------------------------------------------------------------------------- dashboard
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")
