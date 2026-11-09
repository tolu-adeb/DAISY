"""Keeping the bar store current.

Two jobs, both rate-limit aware, because a universe of hundreds of names cannot be refreshed in one
burst on a free data tier:

``backfill``   first fill: a long history per symbol, run once (or after adding names).  Slow and
               deliberate - it is the only thing that needs years of bars.
``pass_once``  the paged sweep: each call refreshes the next ``page`` symbols from the wide universe
               and moves a stored cursor on, so a full cycle completes over several passes.  Called
               every ``stocks_sweep_minutes`` (default 30) while the market is open, plus once after
               the close.

Why paged and not "all stocks every 30 minutes": the books are daily-bar strategies.  They emit one
signal set per session, after the close, so sweeping the whole tape every half hour buys no extra
signal - it only spends rate limit.  What the intraday passes are for is keeping the newest bar fresh,
watching open positions against their stops, and flagging a name that crosses an entry condition
during the session so it can be looked at before the close.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

KV_CURSOR = "sweep_cursor"
KV_LAST = "sweep_last"
MAX_ERRORS = 6          # consecutive failures before a symbol is parked as probably delisted


@dataclass
class SweepReport:
    started: float
    kind: str                      # 'backfill' | 'pass'
    asked: int = 0
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    bars: int = 0
    parked: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    cursor: int = 0
    wrapped: bool = False

    @property
    def seconds(self) -> float:
        return round(time.time() - self.started, 1)

    def to_dict(self) -> dict:
        return {"at": datetime.fromtimestamp(self.started, NY).isoformat(timespec="seconds"),
                "kind": self.kind, "asked": self.asked, "ok": self.ok, "skipped": self.skipped,
                "failed": self.failed, "bars": self.bars, "seconds": self.seconds,
                "parked": self.parked[:20], "errors": self.errors[:5], "cursor": self.cursor,
                "wrapped": self.wrapped}


class Sweeper:
    def __init__(self, settings, engine, store, universe):
        self.s = settings
        self.engine = engine
        self.store = store
        self.u = universe

    # ------------------------------------------------------------------ one symbol
    async def refresh(self, symbol: str, period: str, use_cache: bool = False) -> int:
        """Fetch ``symbol`` and write its bars.  Returns rows written; raises on a provider failure."""
        res = await self.engine.history(symbol, period=period, interval="1d", use_cache=use_cache)
        ph = res.value
        df = ph.df
        if df is None or len(df) == 0:
            raise ValueError("no bars returned")
        # Prefer an explicitly adjusted close: every momentum and trend signal in the book needs one.
        adjusted = True
        if "adj_close" in df.columns and df["adj_close"].notna().sum() > len(df) * 0.9:
            df = df.copy()
            ratio = df["adj_close"] / df["close"].replace(0, float("nan"))
            for c in ("open", "high", "low"):
                if c in df.columns:
                    df[c] = df[c] * ratio
            df["close"] = df["adj_close"]
        else:
            adjusted = bool(getattr(ph, "adjusted", False)) or symbol.startswith("^")
        key = self.u_alias(symbol)
        return self.store.upsert(key, df, provider=getattr(res.provenance, "provider", None),
                                 adjusted=adjusted)

    def u_alias(self, symbol: str) -> str:
        """Store the gauges under the plain names the config uses (^VIX -> VIX)."""
        from .universe import GAUGE_ALIAS
        return GAUGE_ALIAS.get(symbol, symbol.upper().strip())

    # ------------------------------------------------------------------ bulk
    async def _run_many(self, symbols: list[str], period: str, kind: str,
                        concurrency: int, pause: float) -> SweepReport:
        rep = SweepReport(time.time(), kind, asked=len(symbols), symbols=list(symbols))
        if not symbols:
            return rep
        sem = asyncio.Semaphore(max(1, concurrency))
        cov = self.store.coverage()

        async def one(sym: str) -> None:
            store_key = self.u_alias(sym)
            c = cov.get(store_key)
            if c is not None and c.delisted:
                rep.skipped += 1
                return
            async with sem:
                try:
                    n = await self.refresh(sym, period)
                    rep.ok += 1
                    rep.bars += n
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    rep.failed += 1
                    msg = f"{type(e).__name__}: {e}"[:200]
                    rep.errors.append({"symbol": sym, "error": msg})
                    self.store.mark_error(store_key, msg)
                    errs = (c.errors if c else 0) + 1
                    if errs >= MAX_ERRORS:
                        self.store.set_delisted(store_key, True)
                        rep.parked.append(sym)
                if pause:
                    await asyncio.sleep(pause)

        await asyncio.gather(*(one(s) for s in symbols))
        self.store.kv_set(KV_LAST, str(int(time.time())))
        return rep

    async def backfill(self, symbols: list[str] | None = None, period: str | None = None,
                       concurrency: int | None = None, only_missing: bool = True) -> SweepReport:
        """First fill: a long history for every symbol that needs one."""
        syms = list(symbols) if symbols is not None else self.u.core()
        period = period or getattr(self.s, "stocks_backfill_period", "10y")
        min_bars = int(getattr(self.s, "stocks_min_bars", 250))
        if only_missing:
            cov = self.store.coverage()
            syms = [s for s in syms
                    if (c := cov.get(self.u_alias(s))) is None or c.bars < min_bars and not c.delisted]
        return await self._run_many(syms, period, "backfill",
                                    concurrency if concurrency is not None else int(getattr(self.s, "stocks_backfill_concurrency", 3)),
                                    float(getattr(self.s, "stocks_sweep_pause", 0.0)))

    async def pass_once(self, page: int | None = None, tier: str = "wide") -> SweepReport:
        """Refresh the next page of the universe and advance the cursor."""
        all_syms = self.u.wide() if tier == "wide" else self.u.core()
        if not all_syms:
            return SweepReport(time.time(), "pass")
        page = int(page if page is not None else getattr(self.s, "stocks_sweep_page", 60))
        page = max(1, min(page, len(all_syms)))
        try:
            cur = int(self.store.kv_get(KV_CURSOR) or 0)
        except (TypeError, ValueError):
            cur = 0
        cur %= len(all_syms)
        end = cur + page
        syms = all_syms[cur:end]
        wrapped = False
        if end > len(all_syms):                   # wrap round to the front
            syms += all_syms[: end - len(all_syms)]
            wrapped = True
        nxt = end % len(all_syms)
        rep = await self._run_many(syms, getattr(self.s, "stocks_sweep_period", "6mo"), "pass",
                                   int(getattr(self.s, "stocks_sweep_concurrency", 4)),
                                   float(getattr(self.s, "stocks_sweep_pause", 0.0)))
        rep.cursor, rep.wrapped = nxt, wrapped
        self.store.kv_set(KV_CURSOR, str(nxt))
        return rep

    # ------------------------------------------------------------------ index list
    async def refresh_index(self, name: str = "sp500") -> dict:
        """Load the real constituent list from FMP (needs an FMP key) into the store."""
        path = {"sp500": "sp500_constituent", "nasdaq100": "nasdaq_constituent",
                "dowjones": "dowjones_constituent"}.get(name)
        if path is None:
            raise ValueError(f"unknown index {name!r} (sp500 | nasdaq100 | dowjones)")
        key = getattr(self.s, "fmp_api_key", None)
        if not key:
            raise RuntimeError("an FMP API key is needed to refresh the index list "
                               "(set ABG_FMP_API_KEY in .env), or pass a symbol file instead")
        url = f"https://financialmodelingprep.com/api/v3/{path}"
        r = await self.engine.http.get(url, params={"apikey": key})
        rows = r.json() if hasattr(r, "json") else r
        syms = [str(x.get("symbol", "")).upper().strip().replace(".", "-") for x in rows if x.get("symbol")]
        sectors = {str(x["symbol"]).upper().strip().replace(".", "-"): x.get("sector") or ""
                   for x in rows if x.get("symbol")}
        n = self.u.save_index(self.store, name, syms, source="fmp",
                              as_of=datetime.now(NY).date().isoformat())
        if sectors:
            merged = {**self.store.sectors(), **{k: v for k, v in sectors.items() if v}}
            self.store.set_sectors(merged)
        return {"index": name, "symbols": n, "sectors": len(sectors)}

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        last = self.store.kv_get(KV_LAST)
        try:
            cur = int(self.store.kv_get(KV_CURSOR) or 0)
        except (TypeError, ValueError):
            cur = 0
        wide = self.u.wide()
        page = int(getattr(self.s, "stocks_sweep_page", 60))
        every = int(getattr(self.s, "stocks_sweep_minutes", 30))
        passes = max(1, -(-len(wide) // max(page, 1)))
        return {"wide": len(wide), "core": len(self.u.core()), "cursor": cur, "page": page,
                "every_minutes": every, "passes_per_cycle": passes,
                "cycle_hours": round(passes * every / 60.0, 1),
                "last_sweep": None if not last else datetime.fromtimestamp(int(last), NY).isoformat(timespec="seconds"),
                "next_symbols": wide[cur:cur + min(page, 8)]}
