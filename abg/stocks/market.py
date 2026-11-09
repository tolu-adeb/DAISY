"""The whole US market for the stock desk: every common stock, one API call per trading day.

Asking providers for one symbol at a time can't cover the market on a free plan - 5,000 stocks is
5,000 calls a day.  Polygon's *grouped daily* endpoint returns every US stock's bar for one session in a
single call, so the daily cost is one call, and a year of history is ~250 calls (about 50 minutes at the
free plan's 5 a minute, once).

    sync()         fetch any missing sessions (newest first) into <data_dir>/swing/wide/bars/
                   - the same day files the old wide scanner kept, so its history is reused
    common()       Polygon's list of US common stocks and ADRs (types CS, ADRC), cached for a week:
                   ETFs, warrants, units and preferreds are not stocks and never enter a stock book
    liquid()       common stocks that clear the books' own floor ($5, 20-day dollar volume) - the
                   names that could ever be selected, so the panel stays a manageable size
    load()         copy those names' bars into the bar store the books read

The S&P 500 core keeps its ten years of per-symbol history and dated membership; the market tier is
everything else that is liquid today, on the history Polygon's plan allows (two years on the free one).

Honest limits: the common-stock list is today's active list, so a stock that has since been delisted is
not in the market tier - the same survivorship caveat as before, for the non-S&P names.  The Polygon key
travels as a request parameter only and is never logged.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

KV_MARKET = "universe_market"
COMMON_TTL_DAYS = 7


@dataclass
class SyncReport:
    started: float
    wanted: int = 0
    have: int = 0
    fetched: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {"at": datetime.fromtimestamp(self.started, NY).isoformat(timespec="seconds"),
                "wanted": self.wanted, "have": self.have, "fetched": len(self.fetched),
                "newest_fetched": max(self.fetched) if self.fetched else None,
                "not_published": self.empty[:5], "error": self.error,
                "seconds": round(time.time() - self.started, 1)}


class MarketFeed:
    def __init__(self, settings, http, store, universe):
        from abg.swing.wide import Sources, Store as DayFiles
        self.s = settings
        self.store = store
        self.u = universe
        self.src = Sources(settings, http)
        self.days = DayFiles(Path(settings.data_dir).expanduser() / "swing")

    # ------------------------------------------------------------------ availability
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.s, "stocks_market", True)) and bool(self.src.polygon_key)

    def sessions_wanted(self) -> list[date]:
        from abg.swing.wide import last_session, sessions_back
        n = max(60, int(getattr(self.s, "stocks_market_sessions", 300) or 300))
        return sessions_back(last_session(), n)

    # ------------------------------------------------------------------ bars
    async def sync(self, max_calls: int | None = None) -> SyncReport:
        """Fetch missing sessions, newest first, so a cut-off run still leaves the recent bars in place."""
        rep = SyncReport(time.time())
        want = self.sessions_wanted()
        rep.wanted = len(want)
        missing = sorted((d for d in want if not self.days.has(d)), reverse=True)
        rep.have = rep.wanted - len(missing)
        if max_calls is not None:
            missing = missing[:max(0, max_calls)]
        for d in missing:
            try:
                rows = await self.src.grouped(d)
            except asyncio.CancelledError:
                raise
            except Exception as e:                          # never echo a URL: it carries the key
                rep.error = f"{type(e).__name__} on {d}"
                log.warning("market sync stopped at %s (%s)", d, type(e).__name__)
                break
            if not rows:
                rep.empty.append(d.isoformat())
                continue
            self.days.write_day(d, rows)
            rep.fetched.append(d.isoformat())
            rep.have += 1
        return rep

    # ------------------------------------------------------------------ which tickers are stocks
    def _common_cache(self) -> Path:
        return Path(self.s.data_dir).expanduser() / "stocks" / "common_stocks.json"

    async def common(self, refresh: bool = False) -> set[str]:
        p = self._common_cache()
        try:
            d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except (OSError, ValueError):
            d = None
        fresh = d and (time.time() - float(d.get("at", 0))) < COMMON_TTL_DAYS * 86400
        if fresh and not refresh:
            return set(d["symbols"])
        try:
            # CS = common stock; ADRC = American depositary receipts (TSM, ASML, BABA, NVO, ARM...) -
            # leaving them out would silently drop some of the most traded names in the market
            rows = await self.src.common_stocks("CS,ADRC")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if d:                                           # a stale list beats none
                log.warning("market: common-stock list refresh failed (%s); using the cached one", type(e).__name__)
                return set(d["symbols"])
            raise
        syms = sorted({_norm(r["ticker"]) for r in rows if r.get("ticker")})
        if len(syms) < 1000:
            raise ValueError(f"Polygon returned only {len(syms)} common stocks - refusing to use that list")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"at": time.time(), "symbols": syms}), encoding="utf-8")
        tmp.replace(p)
        return set(syms)

    # ------------------------------------------------------------------ liquidity and loading
    def frame(self, sessions: int | None = None) -> pd.DataFrame:
        """Long table (date, T, o, h, l, c, v) of the stored sessions, newest ``sessions`` of them."""
        files = sorted(self.days.bars.glob("*.csv.gz"))
        if sessions:
            files = files[-sessions:]
        parts = []
        for f in files:
            try:
                df = pd.read_csv(f, dtype={"T": str})
            except Exception:                               # a half-written file must not stop the pass
                continue
            df["date"] = pd.Timestamp(f.name[:10])
            parts.append(df)
        if not parts:
            return pd.DataFrame(columns=["date", "T", "o", "h", "l", "c", "v"])
        out = pd.concat(parts, ignore_index=True)
        out["T"] = out["T"].map(_norm)
        return out.dropna(subset=["c"])

    def liquid(self, long: pd.DataFrame, common: set[str], min_price: float, min_dv: float,
               window: int = 20, min_sessions: int = 60) -> list[str]:
        """Common stocks whose last close is >= ``min_price`` and whose median dollar volume over the
        last ``window`` sessions is >= ``min_dv``, with enough history to matter. The books apply the
        same floor day by day, so a name that fails it here could not be picked anyway."""
        if long.empty:
            return []
        days = sorted(long["date"].unique())
        recent = long[long["date"].isin(days[-window:])]
        g = recent.assign(dv=recent["c"] * recent["v"]).groupby("T")
        med_dv = g["dv"].median()
        last_c = g["c"].last()
        counts = long.groupby("T")["date"].count()
        ok = (med_dv >= min_dv) & (last_c >= min_price)
        names = [t for t in ok[ok].index if t in common and counts.get(t, 0) >= min_sessions]
        return sorted(names)

    def load(self, long: pd.DataFrame, symbols: list[str]) -> int:
        """Write the chosen names' bars into the bar store (the books' panel reads it from there)."""
        if not symbols or long.empty:
            return 0
        sub = long[long["T"].isin(set(symbols))]
        n = 0
        for t, g in sub.groupby("T", sort=False):
            df = g.set_index("date").rename(columns={"o": "open", "h": "high", "l": "low", "c": "close",
                                                     "v": "volume"})[["open", "high", "low", "close", "volume"]]
            n += self.store.upsert(t, df.sort_index(), provider="polygon-grouped", adjusted=True)
        return n

    # ------------------------------------------------------------------ the whole step
    async def refresh(self, max_calls: int | None = None, min_price: float = 5.0,
                      min_dv: float = 20e6, force: bool = False) -> dict:
        """Sync bars, pick the liquid common stocks outside the core, load them, save the tier.
        ``min_price`` / ``min_dv``: the loosest floor any stock book uses (from the rule file)."""
        if not self.enabled:
            return {"skipped": "no Polygon key (ABG_POLYGON_API_KEY) or ABG_STOCKS_MARKET=false"}
        rep = await self.sync(max_calls)
        if not rep.fetched and self.u.market and not force:
            # nothing new arrived and the tier is already loaded: re-reading ~300 day files and
            # re-writing thousands of names would change nothing
            return {"sync": rep.to_dict(), "market_tier": len(self.u.market), "reloaded": False,
                    "sessions_stored": len(self.days.days())}
        common = await self.common()
        long = await asyncio.to_thread(self.frame, int(getattr(self.s, "stocks_market_sessions", 300) or 300))
        names = await asyncio.to_thread(self.liquid, long, common, min_price, min_dv)
        core = set(self.u.core())
        tier = [t for t in names if t not in core]
        cap = int(getattr(self.s, "stocks_market_max", 3000) or 0)
        if cap and len(tier) > cap:                         # keep the most liquid if a cap is set
            recent = long[long["date"] >= long["date"].max() - pd.Timedelta(days=40)]
            dv = (recent["c"] * recent["v"]).groupby(recent["T"]).median()
            tier = sorted(tier, key=lambda t: -float(dv.get(t, 0)))[:cap]
            tier.sort()
        bars = await asyncio.to_thread(self.load, long, tier)
        self.u.save_market(self.store, tier)
        return {"sync": rep.to_dict(), "common_stocks": len(common), "liquid": len(names),
                "market_tier": len(tier), "bars_loaded": bars, "reloaded": True,
                "sessions_stored": len(self.days.days())}

    def status(self) -> dict:
        ds = self.days.days()
        return {"enabled": self.enabled, "sessions_stored": len(ds),
                "first": ds[0].isoformat() if ds else None, "last": ds[-1].isoformat() if ds else None,
                "wanted": int(getattr(self.s, "stocks_market_sessions", 300) or 300),
                "tier": len(self.u.market)}


def _norm(t) -> str:
    return str(t).strip().upper().replace(".", "-")
