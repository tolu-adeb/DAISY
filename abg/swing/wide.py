"""Wide scan: the swing desk's own setup across the whole US market, ranked (docs/19 section 19.8).

The core scanner checks the 85 names in ``swingdesk/config/universe.txt``.  The wide scan adds every liquid US common
stock, using one Polygon call per day for the whole market's daily bars:

    whole market (Polygon grouped daily, ~10k tickers)
      -> common stocks only (Polygon reference, type CS), not in the core list
      -> liquid: last close >= $5, median 20-day dollar volume >= $20M, >= 60 sessions of history
      -> the same setup as the core scanner (``swingdesk.scanner.find_setups``) on the latest session
      -> the same gate (``swingdesk.strategy.grade``); earnings inside the hold window = ``binary_event`` (half size)
      -> ranked 0-100 on strength vs SPY (20 and 60 days), the sector ETF vs SPY, volume, and news
      -> news for the top few: Finnhub headlines read by Claude Haiku (red flags such as an offering, a lawsuit or a
         downgrade keep an idea off Discord); without an Anthropic key a keyword check does the red flags
      -> every taken setup is tracked (author "wide") on its own page; the best ``ABG_SWING_WIDE_MAX_POSTS`` a day go
         to Discord only with ``ABG_SWING_WIDE_POST=true`` (default: shadow, dashboard only)

Market context: breadth (share of the liquid universe above its 50-day average).  Below
``ABG_SWING_WIDE_MIN_BREADTH`` nothing is posted (ideas are still tracked).

Nothing here changes a rule: take / skip, size, stop and take-profit come from ``swingdesk.strategy.grade``.
API keys are sent as request parameters/headers only and never logged.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import math
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
AUTHOR = "wide"
POSTED_TIME = "16:31"            # posted_et for wide ideas ("D 16:31"); the core scanner uses 16:30

# Finnhub industry (finnhubIndustry) -> SPDR sector ETF.  First matching keyword wins.
SECTOR_RULES = [
    ("XLK", ("semiconductor", "technology", "software", "it services", "electronic", "communications equipment",
             "computer", "internet")),
    ("XLC", ("media", "telecommunication", "entertainment", "communication")),
    ("XLV", ("pharma", "biotech", "health", "medical", "life sciences")),
    ("XLF", ("bank", "financial", "insurance", "capital markets", "consumer finance")),
    ("XLE", ("energy", "oil", "gas", "coal")),
    ("XLU", ("utilit",)),
    ("XLRE", ("real estate", "reit")),
    ("XLP", ("food", "beverage", "tobacco", "household", "personal products", "staples")),
    ("XLY", ("retail", "hotel", "restaurant", "leisure", "auto", "textile", "apparel", "consumer", "distributor",
             "diversified consumer", "homebuilding", "education")),
    ("XLI", ("industrial", "machinery", "aerospace", "defense", "airline", "logistics", "transport", "construction",
             "electrical", "building", "professional services", "marine", "road", "trading companies")),
    ("XLB", ("chemical", "metal", "mining", "packaging", "paper", "materials", "steel")),
]
SECTOR_ETFS = sorted({e for e, _ in SECTOR_RULES})

RED_FLAG_WORDS = {
    "offering": r"\b(public|secondary|stock|share|equity|at-the-market|registered direct) offering\b|\bprices? .{0,30}offering",
    "lawsuit / investigation": r"\b(class action|lawsuit|investigation|subpoena|probe|sec charges|fraud)\b",
    "downgrade": r"\bdowngrade[sd]?\b|\bcut to (sell|underperform|neutral|hold)\b",
    "guidance cut": r"\b(cuts?|lowers?|slashes|withdraws?) (its |full-year |annual )?(guidance|outlook|forecast)\b",
    "going concern / bankruptcy": r"\b(going concern|bankruptcy|chapter 11|delist)",
    "regulatory setback": r"\b(complete response letter|fda (rejects|declines)|clinical hold|recall)\b",
    "executive exit": r"\b(ceo|cfo|chief executive|chief financial) (resigns|steps down|departs|exits|to step down)\b",
}


# ====================================================================== helpers
def sector_etf(industry: str | None) -> str | None:
    t = (industry or "").lower()
    for etf, words in SECTOR_RULES:
        if any(w in t for w in words):
            return etf
    return None


def sessions_back(end: date, n: int) -> list[date]:
    """The last ``n`` trading days up to and including ``end`` (oldest first)."""
    from abg.live.market_hours import is_trading_day
    out, d = [], end
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def sessions_between(start: date, end: date) -> list[date]:
    from abg.live.market_hours import is_trading_day
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def last_session(now: datetime | None = None) -> date:
    from abg.live.market_hours import is_trading_day
    now = now or datetime.now(NY)
    d = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def _clip(x, lo=-1.0, hi=1.0):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    return max(lo, min(hi, float(x)))


def _ret(px: pd.DataFrame, n: int) -> float | None:
    c = px["close"]
    if len(c) <= n or c.iloc[-n - 1] <= 0:
        return None
    return round(100 * (float(c.iloc[-1]) / float(c.iloc[-n - 1]) - 1), 2)


def keyword_flags(texts: list[str]) -> list[str]:
    blob = " \n ".join(t for t in texts if t).lower()
    return [name for name, pat in RED_FLAG_WORDS.items() if re.search(pat, blob)]


# ====================================================================== API clients (async, rate limited)
class Sources:
    """Polygon (whole-market daily bars, common-stock list), Finnhub (earnings, headlines, sector), Claude (news read).

    ``http`` is the bot's ``mnq.live.feeds.Http`` (retries, typed errors).  Keys travel as parameters/headers only."""

    def __init__(self, settings, http):
        self.s, self.http = settings, http
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -------------------------------------------------- availability
    @property
    def polygon_key(self) -> str | None:
        return getattr(self.s, "polygon_api_key", None) or None

    @property
    def finnhub_key(self) -> str | None:
        return getattr(self.s, "finnhub_api_key", None) or None

    @property
    def anthropic_key(self) -> str | None:
        return getattr(self.s, "anthropic_api_key", None) or None

    def configured(self) -> dict:
        return {"polygon": bool(self.polygon_key), "finnhub": bool(self.finnhub_key), "claude": bool(self.anthropic_key)}

    async def _pace(self, provider: str, per_min: float) -> None:
        lock = self._locks.setdefault(provider, asyncio.Lock())
        async with lock:
            gap = 60.0 / max(0.1, per_min)
            wait = self._last.get(provider, 0) + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[provider] = time.monotonic()

    async def _get(self, provider: str, per_min: float, url: str, params: dict, timeout: float = 30.0):
        from abg.errors import RateLimited
        for attempt in range(3):
            await self._pace(provider, per_min)
            try:
                return await self.http.get_json(url, provider=provider, params=params, timeout=timeout)
            except RateLimited:
                if attempt == 2:
                    raise
                await asyncio.sleep(15 * (attempt + 1))
        return None                                                  # pragma: no cover

    # -------------------------------------------------- Polygon
    def _poly(self, path: str) -> str:
        return (getattr(self.s, "polygon_base_url", None) or "https://api.polygon.io").rstrip("/") + path

    async def grouped(self, day: date) -> list[dict] | None:
        """Every US stock's bar for ``day`` (None = not published yet / not allowed on this plan yet)."""
        from abg.errors import AuthError, NoDataError
        try:
            j = await self._get("polygon", self.s.swing_wide_polygon_per_min,
                                self._poly(f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}"),
                                {"adjusted": "true", "apiKey": self.polygon_key})
        except (AuthError, NoDataError):
            if day >= last_session():        # today's bar before the free plan publishes it
                return None
            raise
        rows = (j or {}).get("results") or []
        return rows or None

    async def common_stocks(self, types: str = "CS") -> list[dict]:
        out = []
        for t in [x.strip() for x in types.split(",") if x.strip()]:
            url, params = self._poly("/v3/reference/tickers"), {"market": "stocks", "type": t, "active": "true",
                                                                "limit": 1000, "apiKey": self.polygon_key}
            for _ in range(30):
                j = await self._get("polygon", self.s.swing_wide_polygon_per_min, url, params)
                out += [{"ticker": r.get("ticker"), "name": r.get("name"), "exchange": r.get("primary_exchange"),
                         "type": r.get("type")} for r in (j or {}).get("results") or [] if r.get("ticker")]
                nxt = (j or {}).get("next_url")
                if not nxt:
                    break
                # next_url carries the cursor, not the key. Pass the cursor as a parameter: httpx (0.28+) drops
                # a URL's own query string when params are given, which silently returned page one of an
                # unfiltered list and cut the market to about 1,400 names.
                from urllib.parse import parse_qs, urlparse
                cur = (parse_qs(urlparse(nxt).query).get("cursor") or [None])[0]
                if not cur:
                    break
                params = {"cursor": cur, "apiKey": self.polygon_key}
        return out

    # -------------------------------------------------- Finnhub
    async def earnings(self, frm: date, to: date) -> dict[str, list[str]]:
        """symbol -> sorted report dates in [frm, to] (chunks of 30 days)."""
        out: dict[str, set] = {}
        d = frm
        while d <= to:
            e = min(to, d + timedelta(days=29))
            j = await self._get("finnhub", self.s.swing_wide_finnhub_per_min, "https://finnhub.io/api/v1/calendar/earnings",
                                {"from": d.isoformat(), "to": e.isoformat(), "token": self.finnhub_key})
            for r in (j or {}).get("earningsCalendar") or []:
                s, dt = r.get("symbol"), r.get("date")
                if s and dt:
                    out.setdefault(s, set()).add(dt)
            d = e + timedelta(days=1)
        return {k: sorted(v) for k, v in out.items()}

    async def profile(self, sym: str) -> dict:
        j = await self._get("finnhub", self.s.swing_wide_finnhub_per_min, "https://finnhub.io/api/v1/stock/profile2",
                            {"symbol": sym, "token": self.finnhub_key})
        j = j or {}
        return {"name": j.get("name"), "industry": j.get("finnhubIndustry"), "mcap_m": j.get("marketCapitalization"),
                "sector_etf": sector_etf(j.get("finnhubIndustry"))}

    async def news(self, sym: str, frm: date, to: date, limit: int = 12) -> list[dict]:
        j = await self._get("finnhub", self.s.swing_wide_finnhub_per_min, "https://finnhub.io/api/v1/company-news",
                            {"symbol": sym, "from": frm.isoformat(), "to": to.isoformat(), "token": self.finnhub_key})
        rows = sorted([r for r in (j or []) if isinstance(r, dict) and r.get("headline")],
                      key=lambda r: -(r.get("datetime") or 0))[:limit]
        return [{"headline": r["headline"][:220], "summary": (r.get("summary") or "")[:300], "source": r.get("source"),
                 "at": datetime.fromtimestamp(r["datetime"], NY).strftime("%Y-%m-%d") if r.get("datetime") else None,
                 "url": r.get("url")} for r in rows]

    # -------------------------------------------------- Claude
    async def read_news(self, items: dict[str, list[dict]]) -> dict[str, dict]:
        """One Haiku call for all candidates -> {sym: {score -2..2, red_flags: [...], why}}."""
        if not items:
            return {}
        import os
        model = os.getenv("SWINGDESK_FAST_MODEL", "claude-haiku-5-5")
        lines = []
        for sym, news in items.items():
            lines.append(f"## {sym}")
            lines += [f"- {n['at'] or ''} {n['headline']}" + (f" — {n['summary'][:160]}" if n.get("summary") else "")
                      for n in news[:10]] or ["- (no headlines in the last 10 days)"]
        prompt = (
            "You screen news for a swing-trading desk.  Each stock below just broke above a recent swing high and "
            "will be bought on a pullback and held for days to a few weeks.  For each ticker, read only the headlines "
            "given (do not use outside knowledge) and return a JSON array of objects: "
            '{"symbol": str, "score": int from -2 (clearly bad for the next few weeks) to 2 (clearly good), '
            '"red_flags": [short strings; only for: share offering/dilution, lawsuit or investigation, analyst '
            "downgrade, guidance cut, going concern/bankruptcy/delisting, regulatory setback, CEO/CFO exit, pending "
            'acquisition at a fixed price], "why": one sentence of at most 20 words}.  Routine or unrelated '
            "headlines score 0.  Reply with the JSON array only.\n\n" + "\n".join(lines))
        resp = await self.http.request(
            "POST", (getattr(self.s, "anthropic_base_url", None) or "https://api.anthropic.com").rstrip("/") + "/v1/messages",
            provider="anthropic", timeout=90,
            headers={"x-api-key": self.anthropic_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]})
        text = "".join(b.get("text", "") for b in (resp.json().get("content") or []) if b.get("type") == "text")
        m = re.search(r"\[.*\]", text, re.S)
        out = {}
        for r in json.loads(m.group(0)) if m else []:
            s = str(r.get("symbol") or "").upper()
            if s in items:
                out[s] = {"score": int(max(-2, min(2, int(r.get("score") or 0)))),
                          "red_flags": [str(x)[:60] for x in (r.get("red_flags") or [])][:4],
                          "why": str(r.get("why") or "")[:200], "by": "claude"}
        return out


# ====================================================================== the bar store
class Store:
    """``data/swing/wide/``: one gzip CSV of the whole market per session, plus small JSON caches."""

    def __init__(self, home: Path):
        self.root = Path(home) / "wide"
        self.bars = self.root / "bars"
        self.bars.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------- daily bars
    def day_path(self, d: date) -> Path:
        return self.bars / f"{d.isoformat()}.csv.gz"

    def has(self, d: date) -> bool:
        return self.day_path(d).exists()

    def days(self) -> list[date]:
        return sorted(date.fromisoformat(p.name[:10]) for p in self.bars.glob("*.csv.gz"))

    def write_day(self, d: date, rows: list[dict]) -> int:
        df = pd.DataFrame([{"T": r.get("T"), "o": r.get("o"), "h": r.get("h"), "l": r.get("l"), "c": r.get("c"),
                            "v": r.get("v")} for r in rows if r.get("T") and r.get("c")])
        tmp = self.day_path(d).with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8", newline="") as fh:
            df.to_csv(fh, index=False)
        tmp.replace(self.day_path(d))
        return len(df)

    def panel(self, end: date, n: int, symbols: set | None = None) -> dict[str, pd.DataFrame]:
        """symbol -> daily frame in the desk's format (open/high/low/close, volume in thousands), last ``n`` stored
        sessions up to ``end``."""
        files = [p for p in sorted(self.bars.glob("*.csv.gz")) if date.fromisoformat(p.name[:10]) <= end][-n:]
        if not files:
            return {}
        frames = []
        for p in files:
            df = pd.read_csv(p, dtype={"T": str})
            if symbols is not None:
                df = df[df["T"].isin(symbols)]
            df["date"] = pd.Timestamp(p.name[:10])
            frames.append(df)
        big = pd.concat(frames, ignore_index=True).dropna(subset=["c"])
        big = big.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close"})
        big["volume"] = big["v"].fillna(0) / 1000.0
        out = {}
        for sym, g in big.groupby("T", sort=False):
            out[sym] = g.set_index("date")[["open", "high", "low", "close", "volume"]].sort_index()
        return out

    # -------------------------------------------------- JSON caches
    def _jpath(self, name: str) -> Path:
        return self.root / f"{name}.json"

    def load(self, name: str, default=None):
        try:
            return json.loads(self._jpath(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def save(self, name: str, obj) -> None:
        p = self._jpath(name)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, default=str), encoding="utf-8")
        tmp.replace(p)

    def run_path(self, d: date) -> Path:
        return self.root / "runs" / f"{d.isoformat()}.json"

    def latest_run(self) -> dict | None:
        runs = sorted((self.root / "runs").glob("*.json"))
        for p in reversed(runs):
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        return None


# ====================================================================== pure computations (worker thread)
def liquid(panel: dict, syms, min_price: float, min_dv_m: float, min_bars: int = 60) -> list[str]:
    out = []
    for s in syms:
        px = panel.get(s)
        if px is None or len(px) < min_bars:
            continue
        last = float(px["close"].iloc[-1])
        if last < min_price:
            continue
        dv = float((px["close"] * px["volume"] * 1000).tail(20).median()) / 1e6
        if dv >= min_dv_m:
            out.append(s)
    return out


def breadth(panel: dict, syms) -> dict:
    above50 = above20 = up = n = 0
    for s in syms:
        c = panel[s]["close"]
        if len(c) < 51:
            continue
        n += 1
        above50 += c.iloc[-1] > c.tail(50).mean()
        above20 += c.iloc[-1] > c.tail(20).mean()
        up += c.iloc[-1] > c.iloc[-2]
    pct = (lambda k: round(100 * k / n, 1) if n else None)
    return {"stocks": n, "above_50d_pct": pct(above50), "above_20d_pct": pct(above20), "up_today_pct": pct(up)}


def features(px: pd.DataFrame, spy: pd.DataFrame, etf: pd.DataFrame | None) -> dict:
    r20, r60 = _ret(px, 20), _ret(px, 60)
    s20, s60 = _ret(spy, 20), _ret(spy, 60)
    e20 = _ret(etf, 20) if etf is not None and len(etf) > 21 else None
    v = px["volume"]
    surge = round(float(v.iloc[-1] / v.tail(21).head(20).mean()), 2) if len(v) > 21 and v.tail(21).head(20).mean() > 0 else None
    return {"ret20": r20, "rs20": None if r20 is None or s20 is None else round(r20 - s20, 2),
            "rs60": None if r60 is None or s60 is None else round(r60 - s60, 2),
            "sector_rs20": None if e20 is None or s20 is None else round(e20 - s20, 2), "vol_surge": surge}


def entry_dist(sig: dict, px: pd.DataFrame) -> float | None:
    """How far the last close is from the order (the zone's near edge), in daily ATRs; small = likely to fill soon.
    In the backtest (Jun-Oct 2026) the third of setups nearest their entry filled 26% of the time vs 10% for the
    farthest, and ranking by it beat the strength score for what would have posted (docs/19.9)."""
    try:
        from swingdesk.backtest import atr as _atr
        a = float(_atr(px).iloc[-1])
        last = float(px["close"].iloc[-1])
    except Exception:
        return None
    if not a or sig.get("entry_type") != "zone":
        return None
    long = sig.get("direction") == "long"
    edge = float(sig["entry_hi"] if long else sig["entry_lo"])
    return round((last - edge) / a * (1 if long else -1), 2)


# The sessions used to choose the entry-distance ranking and the chase rule (docs/19.9).  Trades inside them are
# in-sample for those choices, so the posting gate counts only trades outside this window.
DESIGN_WINDOW = ("2026-06-08", "2026-10-22")


def rank_key(x: dict, mode: str) -> float:
    """Sort key (ascending) for taken setups: "entry" = nearest to its entry first, "score" = highest score first."""
    if (mode or "entry") == "score":
        return -(x.get("score") or 0)
    d = x.get("dist_atr")
    return 99.0 if d is None else d


WEIGHTS = {"rs20": 0.35, "rs60": 0.20, "sector": 0.15, "volume": 0.10, "news": 0.20}


def score(f: dict, news: dict | None = None, earnings_soon: bool = False) -> dict:
    """0-100 (50 = neutral).  Each part is clipped to [-1, 1] before weighting, so no single input dominates."""
    parts = {"rs20": _clip((f.get("rs20") or 0) / 10), "rs60": _clip((f.get("rs60") or 0) / 20),
             "sector": _clip((f.get("sector_rs20") or 0) / 5),
             "volume": _clip(((f.get("vol_surge") or 1) - 1) / 2),
             "news": _clip((news or {}).get("score", 0) / 2)}
    raw = sum(WEIGHTS[k] * v for k, v in parts.items()) - (0.15 if earnings_soon else 0)
    return {"score": round(50 * (1 + max(-1.0, min(1.0, raw))), 1), "parts": {k: round(v, 2) for k, v in parts.items()}}


def scan(panel: dict, syms: list[str], spy: pd.DataFrame, day: date, cfg: dict | None = None) -> list[dict]:
    """The core scanner's setup on ``day`` for each symbol, graded.  -> [{sig, verdict, features}]"""
    from swingdesk.scanner import find_setups
    from swingdesk.strategy import grade, load_config
    cfg = cfg or load_config()
    ts, out = pd.Timestamp(day), []
    start = str((ts - pd.Timedelta(days=45)).date())     # enough for the scanner's same-leg de-duplication
    for s in syms:
        px = panel[s]
        px = px[px.index <= ts]
        if len(px) < 60 or px.index[-1] != ts:
            continue
        for sig in find_setups(s, px.tail(100), start=start):
            if not sig["posted_et"].startswith(day.isoformat()):
                continue
            sig.update(author=AUTHOR, posted_et=f"{day.isoformat()} {POSTED_TIME}", levels_source="wide scan")
            v = grade(sig, px, spy, cfg, asof=ts)
            out.append({"sig": sig, "verdict": v.as_dict(), "dist_atr": entry_dist(sig, px)})
    return out


def sessions_after(day: date, n: int) -> list[date]:
    from abg.live.market_hours import is_trading_day
    out, d = [], day
    while len(out) < n:
        d += timedelta(days=1)
        if is_trading_day(d):
            out.append(d)
    return out


def earnings_soon(sym: str, cal: dict, day: date, sessions: int) -> str | None:
    """The first report date after ``day`` and within the next ``sessions`` sessions (the hold window), if any."""
    dates = cal.get(sym) or []
    if isinstance(dates, str):
        dates = [dates]
    if not dates or sessions <= 0:
        return None
    end = sessions_after(day, sessions)[-1]
    for d in sorted(dates):
        try:
            e = date.fromisoformat(d)
        except ValueError:
            continue
        if day < e <= end:
            return d
    return None


# ====================================================================== backtest (no news: headlines can't be replayed)
def backtest(panel: dict, syms: list[str], spy: pd.DataFrame, start: date, end: date, profiles: dict,
             cal: dict | None, min_price: float, min_dv_m: float, earn_sessions: int, top_n: int,
             core: set, chase_atr: float = 0.0, rank: str = "entry") -> dict:
    """Walk every session from ``start``: liquid-at-the-time filter, setup, gate, v1 exits (+ the wide scan's chase
    entry when ``chase_atr`` > 0).  -> stats + trades."""
    from dataclasses import replace as _replace

    from swingdesk.engine import run
    from swingdesk.scanner import find_setups
    from swingdesk.strategy import grade, load_config, plan_from_config
    cfg = load_config()
    plan = _replace(plan_from_config(cfg), chase_atr=float(chase_atr or 0))
    rows = []
    t0 = pd.Timestamp(start)
    for s in syms:
        px = panel[s]
        if len(px) < 80:
            continue
        for sig in find_setups(s, px, start=str(start)):
            d = pd.Timestamp(sig["posted_et"][:10])
            if d < t0 or d > pd.Timestamp(end):
                continue
            hist = px[px.index <= d]
            if len(hist) < 60 or float(hist["close"].iloc[-1]) < min_price or \
                    float((hist["close"] * hist["volume"] * 1000).tail(20).median()) / 1e6 < min_dv_m:
                continue
            day = d.date()
            ed = earnings_soon(s, cal or {}, day, earn_sessions)
            flags = ["binary_event"] if ed else []
            v = grade(sig, px, spy, cfg, asof=d, flags=flags)
            prof = profiles.get(s) or {}
            etf = panel.get(prof.get("sector_etf") or "")
            f = features(hist, spy[spy.index <= d], etf[etf.index <= d] if etf is not None else None)
            sc = score(f, None, bool(ed))
            sig["posted"] = datetime.strptime(sig["posted_et"], "%Y-%m-%d %H:%M")
            res = run(sig, px, plan) if v.take else {"status": "SKIPPED"}
            rows.append({"date": str(day), "symbol": s, "core": s in core, "take": v.take, "tier": v.tier,
                         "earnings": ed, "score": sc["score"], **{k: f[k] for k in ("rs20", "rs60", "sector_rs20", "vol_surge")},
                         "dist_atr": entry_dist(sig, hist),
                         "sector_etf": prof.get("sector_etf"), "status": res.get("status"), "r": res.get("r"),
                         "chased": bool(res.get("chased")), "fill_date": res.get("fill_date"), "exit_date": res.get("exit_date")})
    df = pd.DataFrame(rows)
    return {"trades": df, "summary": summarize_bt(df, top_n, rank)}


def rescore(df: pd.DataFrame, panel: dict, spy: pd.DataFrame, profiles: dict, top_n: int, rank: str = "entry") -> dict:
    """Add the sector part (needs each stock's sector, fetched after the first pass) and re-rank; no re-scan."""
    if not len(df):
        return {"trades": df, "summary": summarize_bt(df, top_n, rank)}
    df = df.copy()
    secs, scores = [], []
    for r in df.to_dict("records"):
        etf_sym = (profiles.get(r["symbol"]) or {}).get("sector_etf")
        etf = panel.get(etf_sym or "")
        d = pd.Timestamp(r["date"])
        e20 = _ret(etf[etf.index <= d], 20) if etf is not None else None
        s20 = _ret(spy[spy.index <= d], 20)
        srs = None if e20 is None or s20 is None else round(e20 - s20, 2)
        secs.append((etf_sym, srs))
        f = {"rs20": r["rs20"], "rs60": r["rs60"], "sector_rs20": srs, "vol_surge": r["vol_surge"]}
        scores.append(score(f, None, isinstance(r["earnings"], str))["score"])
    df["sector_etf"], df["sector_rs20"], df["score"] = [a for a, _ in secs], [b for _, b in secs], scores
    return {"trades": df, "summary": summarize_bt(df, top_n, rank)}


def _stats(d: pd.DataFrame) -> dict:
    if d is None or not len(d):
        return {"n": 0}
    closed = d[d.status.isin(["WIN", "LOSS", "SCRATCH"])]
    wins = closed[closed.status == "WIN"]
    return {"n": int(len(d)), "filled": int(d.status.isin(["WIN", "LOSS", "SCRATCH", "OPEN"]).sum()),
            "never_filled": int(d.status.isin(["NOFILL_RAN", "NOFILL_INVALID", "PENDING"]).sum()),
            "closed": int(len(closed)), "wins": int(len(wins)),
            "win_rate": round(100 * len(wins) / len(closed), 1) if len(closed) else None,
            "avg_r": round(float(closed.r.mean()), 3) if len(closed) else None,
            "sum_r": round(float(closed.r.sum()), 2) if len(closed) else 0.0, "open": int((d.status == "OPEN").sum())}


def top_per_day(taken: pd.DataFrame, top_n: int, rank: str) -> pd.DataFrame:
    if (rank or "entry") == "score" or "dist_atr" not in taken:
        return taken.sort_values(["date", "score"], ascending=[True, False]).groupby("date").head(top_n)
    t = taken.assign(_k=taken["dist_atr"].fillna(99.0))
    return t.sort_values(["date", "_k"], ascending=[True, True]).groupby("date").head(top_n).drop(columns="_k")


def summarize_bt(df: pd.DataFrame, top_n: int, rank: str = "entry") -> dict:
    if not len(df):
        return {"setups": 0}
    taken = df[df["take"]]
    out = {"setups": int(len(df)), "taken": int(len(taken)), "symbols": int(df.symbol.nunique()),
           "all_taken": _stats(taken), "by_tier": {t: _stats(taken[taken.tier == t]) for t in ("A", "B")},
           "earnings_in_window": _stats(taken[taken.earnings.notna()]),
           "no_earnings": _stats(taken[taken.earnings.isna()]),
           "core_list_only": _stats(taken[taken.core]), "outside_core": _stats(taken[~taken.core])}
    if len(taken) >= 3:
        q = taken.score.quantile([1 / 3, 2 / 3]).tolist()
        out["by_score"] = {"top third": _stats(taken[taken.score > q[1]]),
                           "middle third": _stats(taken[(taken.score > q[0]) & (taken.score <= q[1])]),
                           "bottom third": _stats(taken[taken.score <= q[0]]), "cuts": [round(x, 1) for x in q]}
    top = top_per_day(taken, top_n, rank)
    out[f"top_{top_n}_per_day"] = _stats(top)               # what would post, ranked the configured way
    out["posting"] = {"rank": rank, "top_n": top_n, **_stats(top)}
    lo, hi = DESIGN_WINDOW
    out["posting_unseen"] = {"rank": rank, "top_n": top_n, "excludes": [lo, hi],
                             **_stats(top[(top.date < lo) | (top.date > hi)])}
    out["rank_compare"] = {m: _stats(top_per_day(taken, top_n, m)) for m in ("entry", "score")}
    if "chased" in taken:
        out["chased"] = _stats(taken[taken["chased"].fillna(False).astype(bool)])
    if "dist_atr" in taken and taken["dist_atr"].notna().sum() >= 3:
        q = taken["dist_atr"].quantile([1 / 3, 2 / 3]).tolist()
        out["by_entry_distance"] = {"nearest third": _stats(taken[taken.dist_atr <= q[0]]),
                                    "middle third": _stats(taken[(taken.dist_atr > q[0]) & (taken.dist_atr <= q[1])]),
                                    "farthest third": _stats(taken[taken.dist_atr > q[1]]), "cuts": [round(x, 2) for x in q]}
    taken = taken.assign(month=taken.date.str[:7])
    out["by_month"] = {m: _stats(g) for m, g in taken.groupby("month")}
    return out
