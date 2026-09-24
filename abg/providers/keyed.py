"""Adapters for key-based vendors: Polygon (Massive), Alpha Vantage, Finnhub, Tiingo,
Financial Modeling Prep and Twelve Data.

Each adapter is only *active* when its API key is set (``ABG_<VENDOR>_API_KEY``).  Free
tiers differ a lot; capabilities below reflect what the free plans generally offer
at the time of writing, and anything the plan rejects surfaces as ``AuthError`` so the
router's circuit breaker parks that vendor and moves on.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd

from ..errors import AuthError, NoDataError, ProviderError, RateLimited
from ..models import Capability, Fundamentals, NewsItem, PriceHistory, Quote
from ..utils import to_float
from .base import Provider, register_provider


def _parse_dt(s) -> datetime | None:
    if not s:
        return None
    try:
        ts = pd.Timestamp(s)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.to_pydatetime()
    except Exception:
        return None


def _mentions(n: NewsItem, symbol: str, name: str | None) -> bool:
    """True if the article text names the ticker (as a word) or the company's first name token."""
    import re
    text = f"{n.title} {n.summary or ''}"
    if re.search(rf"(?<![A-Za-z]){re.escape(symbol)}(?![A-Za-z])", text):
        return True
    if name:
        core = re.sub(r"[,.]|\b(inc|corp|corporation|co|ltd|plc|holdings|group|class [ab])\b", "", name, flags=re.I).strip()
        first = core.split()[0] if core.split() else ""
        if len(first) >= 3 and re.search(rf"\b{re.escape(first)}\b", text, re.I):
            return True
    return False


# =========================================================================== Polygon
@register_provider
class PolygonProvider(Provider):
    name = "polygon"
    label = "Polygon.io / Massive"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTE, Capability.NEWS, Capability.FUNDAMENTALS})
    requires_key = True
    key_setting = "polygon_api_key"
    rate_limit = (5, 60.0)          # free tier: 5 calls/min
    intraday = True
    notes = "Free tier: 5 req/min, end-of-day data; news includes per-ticker sentiment insights."
    _TS = {"1d": (1, "day"), "1wk": (1, "week"), "1mo": (1, "month"), "1h": (1, "hour"),
           "30m": (30, "minute"), "15m": (15, "minute"), "5m": (5, "minute"), "1m": (1, "minute")}

    @property
    def base(self) -> str:
        return self.settings.polygon_base_url.rstrip("/")

    async def _get(self, path: str, params: dict | None = None) -> dict:
        data = await self.http.get_json(f"{self.base}{path}", provider=self.name,
                                        params={**(params or {}), "apiKey": self.api_key})
        status = str(data.get("status", "")).upper() if isinstance(data, dict) else ""
        if status == "ERROR":
            msg = data.get("error") or data.get("message") or "error"
            if "exceeded" in msg.lower():
                raise RateLimited(msg, provider=self.name, retry_after=60)
            if "not authorized" in msg.lower() or "entitle" in msg.lower():
                raise AuthError(msg, provider=self.name)
            raise ProviderError(msg, provider=self.name)
        return data

    async def get_history(self, symbol, start, end, interval="1d"):
        mult, span = self._TS[interval]
        d = await self._get(f"/v2/aggs/ticker/{symbol}/range/{mult}/{span}/{start.isoformat()}/{end.isoformat()}",
                            {"adjusted": "true", "sort": "asc", "limit": 50000})
        res = d.get("results") or []
        if not res:
            raise NoDataError("no aggregates", provider=self.name)
        df = pd.DataFrame(res).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
        if interval in ("1d", "1wk", "1mo"):
            df.index = df.index.tz_convert("America/New_York").normalize().tz_localize(None)
        return PriceHistory.from_frame(symbol, df, self.name, interval, currency="USD")

    async def get_quote(self, symbol):
        d = await self._get(f"/v2/aggs/ticker/{symbol}/prev")
        res = (d.get("results") or [])
        if not res:
            raise NoDataError("no prev bar", provider=self.name)
        r = res[0]
        # Free plan has no real-time snapshot: the latest bar is the last session.
        return Quote(symbol=symbol, price=to_float(r.get("c")), source=self.name, open=to_float(r.get("o")),
                     day_high=to_float(r.get("h")), day_low=to_float(r.get("l")), volume=to_float(r.get("v")),
                     prev_close=None, currency="USD",
                     timestamp=datetime.fromtimestamp(r["t"] / 1000, timezone.utc) if r.get("t") else None)

    async def get_news(self, symbol, limit=20):
        d = await self._get("/v2/reference/news", {"ticker": symbol, "limit": min(limit, 50), "order": "desc"})
        out = []
        smap = {"positive": 0.6, "negative": -0.6, "neutral": 0.0}
        for n in d.get("results") or []:
            ps = None
            for ins in n.get("insights") or []:
                if ins.get("ticker") == symbol and ins.get("sentiment") in smap:
                    ps = smap[ins["sentiment"]]
            out.append(NewsItem(title=n.get("title", ""), source=self.name, url=n.get("article_url"),
                                publisher=(n.get("publisher") or {}).get("name"), summary=n.get("description"),
                                published_at=_parse_dt(n.get("published_utc")), provider_sentiment=ps,
                                tickers=n.get("tickers") or []))
        if not out:
            raise NoDataError("no news", provider=self.name)
        return out

    async def get_fundamentals(self, symbol):
        d = await self._get(f"/v3/reference/tickers/{symbol}")
        r = d.get("results") or {}
        if not r:
            raise NoDataError("no ticker details", provider=self.name)
        return Fundamentals(symbol=symbol, source=self.name, name=r.get("name"), industry=r.get("sic_description"),
                            description=r.get("description"), market_cap=to_float(r.get("market_cap")),
                            shares_outstanding=to_float(r.get("share_class_shares_outstanding")))


# =========================================================================== Alpha Vantage
@register_provider
class AlphaVantageProvider(Provider):
    name = "alphavantage"
    label = "Alpha Vantage"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTE, Capability.NEWS, Capability.FUNDAMENTALS})
    requires_key = True
    key_setting = "alphavantage_api_key"
    rate_limit = (5, 60.0)
    intraday = True
    notes = ("Free tier is 25 requests/day; daily bars are NOT split-adjusted (adjusted series is premium). "
             "NEWS_SENTIMENT supplies per-ticker sentiment scores.")
    URL = "https://www.alphavantage.co/query"

    async def _q(self, **params) -> dict:
        d = await self.http.get_json(self.URL, provider=self.name, params={**params, "apikey": self.api_key})
        if not isinstance(d, dict):
            raise ProviderError("unexpected payload", provider=self.name)
        if "Error Message" in d:
            raise NoDataError(d["Error Message"][:160], provider=self.name)
        msg = d.get("Note") or d.get("Information")
        if msg and len(d) <= 2:
            low = msg.lower()
            if "premium" in low:
                raise AuthError(msg[:160], provider=self.name)
            raise RateLimited(msg[:160], provider=self.name, retry_after=60)
        return d

    async def get_history(self, symbol, start, end, interval="1d"):
        if interval in ("1d", "1wk", "1mo"):
            fn = {"1d": "TIME_SERIES_DAILY", "1wk": "TIME_SERIES_WEEKLY", "1mo": "TIME_SERIES_MONTHLY"}[interval]
            params = {"function": fn, "symbol": symbol}
            if interval == "1d":
                params["outputsize"] = "full" if (end - start).days > 140 else "compact"
            try:
                d = await self._q(**params)
            except AuthError:
                if params.get("outputsize") != "full":
                    raise
                params["outputsize"] = "compact"          # 'full' is premium on some plans
                d = await self._q(**params)
        else:
            iv = {"1h": "60min", "30m": "30min", "15m": "15min", "5m": "5min", "1m": "1min"}[interval]
            d = await self._q(function="TIME_SERIES_INTRADAY", symbol=symbol, interval=iv, outputsize="full")
        key = next((k for k in d if "Time Series" in k), None)
        if not key or not d[key]:
            raise NoDataError("no time series", provider=self.name)
        df = pd.DataFrame.from_dict(d[key], orient="index")
        df.columns = [c.split(". ", 1)[-1] for c in df.columns]
        df.index = pd.to_datetime(df.index)
        df = df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end) + pd.Timedelta(days=1))]
        return PriceHistory.from_frame(symbol, df, self.name, interval, currency="USD")

    async def get_quote(self, symbol):
        d = await self._q(function="GLOBAL_QUOTE", symbol=symbol)
        g = d.get("Global Quote") or {}
        if not g:
            raise NoDataError("empty quote", provider=self.name)
        return Quote(symbol=symbol, price=to_float(g.get("05. price")), source=self.name,
                     prev_close=to_float(g.get("08. previous close")), change=to_float(g.get("09. change")),
                     change_pct=to_float(g.get("10. change percent")), open=to_float(g.get("02. open")),
                     day_high=to_float(g.get("03. high")), day_low=to_float(g.get("04. low")),
                     volume=to_float(g.get("06. volume")), currency="USD")

    async def get_news(self, symbol, limit=20):
        d = await self._q(function="NEWS_SENTIMENT", tickers=symbol, limit=max(limit, 50), sort="LATEST")
        out = []
        for n in (d.get("feed") or [])[:limit]:
            ps = to_float(n.get("overall_sentiment_score"))
            for ts in n.get("ticker_sentiment") or []:
                if ts.get("ticker") == symbol:
                    ps = to_float(ts.get("ticker_sentiment_score"))
            pub = None
            if n.get("time_published"):
                try:
                    pub = datetime.strptime(n["time_published"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
            out.append(NewsItem(title=n.get("title", ""), source=self.name, url=n.get("url"), publisher=n.get("source"),
                                summary=n.get("summary"), published_at=pub, provider_sentiment=ps))
        if not out:
            raise NoDataError("no news", provider=self.name)
        return out

    async def get_fundamentals(self, symbol):
        d = await self._q(function="OVERVIEW", symbol=symbol)
        if not d.get("Symbol"):
            raise NoDataError("no overview", provider=self.name)
        return Fundamentals(symbol=symbol, source=self.name, name=d.get("Name"), sector=d.get("Sector"),
                            industry=d.get("Industry"), description=d.get("Description"),
                            market_cap=to_float(d.get("MarketCapitalization")), pe=to_float(d.get("PERatio")),
                            forward_pe=to_float(d.get("ForwardPE")), eps=to_float(d.get("EPS")), beta=to_float(d.get("Beta")),
                            dividend_yield=to_float(d.get("DividendYield")), week52_high=to_float(d.get("52WeekHigh")),
                            week52_low=to_float(d.get("52WeekLow")), shares_outstanding=to_float(d.get("SharesOutstanding")),
                            profit_margin=to_float(d.get("ProfitMargin")),
                            extra={"target_mean_price": to_float(d.get("AnalystTargetPrice"))})


# =========================================================================== Finnhub
@register_provider
class FinnhubProvider(Provider):
    name = "finnhub"
    label = "Finnhub"
    capabilities = frozenset({Capability.QUOTE, Capability.NEWS, Capability.FUNDAMENTALS})
    requires_key = True
    key_setting = "finnhub_api_key"
    rate_limit = (55, 60.0)          # free plan: 60 calls/min (kept slightly under)
    notes = "Real-time quotes, company news and fundamentals on the free tier (candles are premium)."
    URL = "https://finnhub.io/api/v1"

    def __init__(self, settings, http):
        super().__init__(settings, http)
        self._names: dict[str, str] = {}    # symbol -> company name, for news relevance ranking

    async def _company_name(self, symbol: str) -> str | None:
        if symbol not in self._names:
            try:
                prof = await self._get("/stock/profile2", symbol=symbol)
                self._names[symbol] = (prof or {}).get("name") or ""
            except ProviderError:
                return None
        return self._names[symbol] or None

    async def _get(self, path: str, **params):
        return await self.http.get_json(f"{self.URL}{path}", provider=self.name, params=params,
                                        headers={"X-Finnhub-Token": self.api_key or ""})

    async def get_quote(self, symbol):
        d = await self._get("/quote", symbol=symbol)
        if not d or not to_float(d.get("c")):
            raise NoDataError("empty quote", provider=self.name)
        return Quote(symbol=symbol, price=to_float(d["c"]), source=self.name, prev_close=to_float(d.get("pc")),
                     change=to_float(d.get("d")), change_pct=to_float(d.get("dp")), open=to_float(d.get("o")),
                     day_high=to_float(d.get("h")), day_low=to_float(d.get("l")),
                     timestamp=datetime.fromtimestamp(d["t"], timezone.utc) if d.get("t") else None)

    async def get_news(self, symbol, limit=20):
        today = date.today()
        d = await self._get("/company-news", symbol=symbol, **{"from": (today - timedelta(days=14)).isoformat(),
                                                               "to": today.isoformat()})
        out = [NewsItem(title=n.get("headline", ""), source=self.name, url=n.get("url"), publisher=n.get("source"),
                        summary=n.get("summary"), tickers=[t for t in str(n.get("related") or "").split(",") if t],
                        published_at=datetime.fromtimestamp(n["datetime"], timezone.utc) if n.get("datetime") else None)
               for n in (d or []) if n.get("headline")]
        if not out:
            raise NoDataError("no news", provider=self.name)
        # Finnhub tags loosely (a week of AAPL news includes Meta product stories), so put
        # articles that actually mention the ticker or company name first, newest first within each group.
        name = await self._company_name(symbol)
        out.sort(key=lambda n: (not _mentions(n, symbol, name),
                                -(n.published_at.timestamp() if n.published_at else 0)))
        return out[:limit]

    async def get_fundamentals(self, symbol):
        prof = await self._get("/stock/profile2", symbol=symbol)
        if not prof:
            raise NoDataError("no profile", provider=self.name)
        try:
            met = (await self._get("/stock/metric", symbol=symbol, metric="all")).get("metric") or {}
        except ProviderError:
            met = {}
        mc = to_float(prof.get("marketCapitalization"))
        so = to_float(prof.get("shareOutstanding"))
        dy = to_float(met.get("dividendYieldIndicatedAnnual"))
        return Fundamentals(symbol=symbol, source=self.name, name=prof.get("name"), industry=prof.get("finnhubIndustry"),
                            market_cap=mc * 1e6 if mc else None, shares_outstanding=so * 1e6 if so else None,
                            pe=to_float(met.get("peTTM") or met.get("peBasicExclExtraTTM")), eps=to_float(met.get("epsTTM")),
                            forward_pe=to_float(met.get("forwardPE")),
                            beta=to_float(met.get("beta")), dividend_yield=dy / 100 if dy else None,
                            week52_high=to_float(met.get("52WeekHigh")), week52_low=to_float(met.get("52WeekLow")),
                            profit_margin=(to_float(met.get("netProfitMarginTTM")) or 0) / 100 or None,
                            extra={"exchange": prof.get("exchange"), "country": prof.get("country"),
                                   "gross_margin": (to_float(met.get("grossMarginTTM")) or 0) / 100 or None,
                                   "operating_margin": (to_float(met.get("operatingMarginTTM")) or 0) / 100 or None,
                                   "eps_growth_ttm_yoy_pct": to_float(met.get("epsGrowthTTMYoy")),
                                   "peg": to_float(met.get("pegTTM"))})


# =========================================================================== Tiingo
@register_provider
class TiingoProvider(Provider):
    name = "tiingo"
    label = "Tiingo"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTE, Capability.FUNDAMENTALS, Capability.NEWS})
    requires_key = True
    key_setting = "tiingo_api_key"
    rate_limit = (50, 3600.0)        # free plan: 50 requests/hour, 1000/day
    notes = ("Split- and dividend-adjusted EOD history back decades; IEX quotes. "
             "News needs the paid News API add-on (enable with ABG_TIINGO_NEWS_ENABLED=true).")
    URL = "https://api.tiingo.com"

    def supports(self, cap, interval="1d"):
        if cap == Capability.NEWS and not self.settings.tiingo_news_enabled:
            return False          # free keys get HTTP 403 on /tiingo/news; don't waste calls
        return super().supports(cap, interval)

    def map_symbol(self, symbol):
        return symbol.replace(".", "-").lower()

    async def _get(self, path: str, **params):
        return await self.http.get_json(f"{self.URL}{path}", provider=self.name, params=params,
                                        headers={"Authorization": f"Token {self.api_key}"})

    async def get_history(self, symbol, start, end, interval="1d"):
        freq = {"1d": "daily", "1wk": "weekly", "1mo": "monthly"}[interval]
        d = await self._get(f"/tiingo/daily/{self.map_symbol(symbol)}/prices", startDate=start.isoformat(),
                            endDate=end.isoformat(), resampleFreq=freq)
        if not d:
            raise NoDataError("no prices", provider=self.name)
        df = pd.DataFrame(d)
        df.index = pd.to_datetime(df["date"], utc=True).dt.normalize()
        # Tiingo's open/high/low/close are RAW (not split-adjusted), so a 4:1 split would look like a
        # -75% crash to every indicator. Use the adjusted series when present.
        if {"adjOpen", "adjHigh", "adjLow", "adjClose"} <= set(df.columns):
            df = pd.DataFrame({"open": df["adjOpen"], "high": df["adjHigh"], "low": df["adjLow"],
                               "close": df["adjClose"], "volume": df.get("adjVolume", df.get("volume")),
                               "adj_close": df["adjClose"]}, index=df.index)
        return PriceHistory.from_frame(symbol, df, self.name, interval, currency="USD", meta={"adjusted": True})

    async def get_quote(self, symbol):
        d = await self._get(f"/iex/{self.map_symbol(symbol)}")
        if not d:
            raise NoDataError("no quote", provider=self.name)
        r = d[0]
        # "last" is null outside IEX trading / on the free feed; tngoLast is Tiingo's composite last price
        return Quote(symbol=symbol, price=to_float(r.get("last") or r.get("tngoLast") or r.get("mid")), source=self.name,
                     prev_close=to_float(r.get("prevClose")), open=to_float(r.get("open")), day_high=to_float(r.get("high")),
                     day_low=to_float(r.get("low")), volume=to_float(r.get("volume")), currency="USD",
                     timestamp=_parse_dt(r.get("timestamp")))

    async def get_fundamentals(self, symbol):
        d = await self._get(f"/tiingo/daily/{self.map_symbol(symbol)}")
        if not d or not d.get("ticker"):
            raise NoDataError("no metadata", provider=self.name)
        return Fundamentals(symbol=symbol, source=self.name, name=d.get("name"), description=d.get("description"),
                            extra={"exchange": d.get("exchangeCode"), "listed_since": d.get("startDate")})

    async def get_news(self, symbol, limit=20):
        d = await self._get("/tiingo/news", tickers=self.map_symbol(symbol), limit=limit)
        out = [NewsItem(title=n.get("title", ""), source=self.name, url=n.get("url"), publisher=n.get("source"),
                        summary=n.get("description"), published_at=_parse_dt(n.get("publishedDate")),
                        tickers=[t.upper() for t in n.get("tickers") or []]) for n in (d or []) if n.get("title")]
        if not out:
            raise NoDataError("no news", provider=self.name)
        return out


# =========================================================================== FMP
@register_provider
class FMPProvider(Provider):
    name = "fmp"
    label = "Financial Modeling Prep"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTE, Capability.NEWS, Capability.FUNDAMENTALS})
    requires_key = True
    key_setting = "fmp_api_key"
    rate_limit = (4, 1.0)
    notes = "Uses the /stable API (250 req/day free); rich fundamentals."
    URL = "https://financialmodelingprep.com/stable"

    async def _get(self, path: str, **params):
        d = await self.http.get_json(f"{self.URL}{path}", provider=self.name, params={**params, "apikey": self.api_key})
        if isinstance(d, dict) and d.get("Error Message"):
            msg = d["Error Message"]
            if "limit" in msg.lower():
                raise RateLimited(msg[:160], provider=self.name, retry_after=60)
            raise AuthError(msg[:160], provider=self.name)
        return d

    async def get_history(self, symbol, start, end, interval="1d"):
        if interval != "1d":
            raise NoDataError("only daily supported", provider=self.name)
        d = await self._get("/historical-price-eod/full", symbol=symbol, **{"from": start.isoformat(), "to": end.isoformat()})
        rows = d.get("historical") if isinstance(d, dict) else d      # legacy v3 shape vs stable list
        if not rows:
            raise NoDataError("no prices", provider=self.name)
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["date"])
        if "adjClose" in df.columns:
            df = df.rename(columns={"adjClose": "adj_close"})
        return PriceHistory.from_frame(symbol, df, self.name, interval, currency="USD")

    async def get_quote(self, symbol):
        d = await self._get("/quote", symbol=symbol)
        if not d:
            raise NoDataError("no quote", provider=self.name)
        r = d[0]
        return Quote(symbol=symbol, price=to_float(r.get("price")), source=self.name,
                     prev_close=to_float(r.get("previousClose")), change=to_float(r.get("change")),
                     change_pct=to_float(r.get("changePercentage") or r.get("changesPercentage")),
                     open=to_float(r.get("open")), day_high=to_float(r.get("dayHigh")), day_low=to_float(r.get("dayLow")),
                     volume=to_float(r.get("volume")), market_cap=to_float(r.get("marketCap")), name=r.get("name"),
                     timestamp=datetime.fromtimestamp(r["timestamp"], timezone.utc) if r.get("timestamp") else None)

    async def get_news(self, symbol, limit=20):
        d = await self._get("/news/stock", symbols=symbol, limit=limit)
        out = [NewsItem(title=n.get("title", ""), source=self.name, url=n.get("url"),
                        publisher=n.get("publisher") or n.get("site"), summary=(n.get("text") or "")[:500] or None,
                        published_at=_parse_dt(n.get("publishedDate"))) for n in (d or []) if n.get("title")]
        if not out:
            raise NoDataError("no news", provider=self.name)
        return out[:limit]

    async def get_fundamentals(self, symbol):
        d = await self._get("/profile", symbol=symbol)
        if not d:
            raise NoDataError("no profile", provider=self.name)
        r = d[0]
        rng = str(r.get("range") or "").split("-")
        price = to_float(r.get("price"))
        div = to_float(r.get("lastDividend") or r.get("lastDiv"))
        return Fundamentals(symbol=symbol, source=self.name, name=r.get("companyName"), sector=r.get("sector"),
                            industry=r.get("industry"), description=r.get("description"),
                            market_cap=to_float(r.get("marketCap") or r.get("mktCap")), beta=to_float(r.get("beta")),
                            dividend_yield=(div / price) if div and price else None,
                            week52_low=to_float(rng[0]) if len(rng) == 2 else None,
                            week52_high=to_float(rng[1]) if len(rng) == 2 else None)


# =========================================================================== Twelve Data
@register_provider
class TwelveDataProvider(Provider):
    name = "twelvedata"
    label = "Twelve Data"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTE})
    requires_key = True
    key_setting = "twelvedata_api_key"
    rate_limit = (7, 60.0)           # free "basic" plan: 8 credits/min, 800/day (kept one under)
    intraday = True
    notes = "8 req/min, 800/day free; split-adjusted history; good intraday coverage."
    URL = "https://api.twelvedata.com"
    _IV = {"1d": "1day", "1wk": "1week", "1mo": "1month", "1h": "1h", "30m": "30min", "15m": "15min", "5m": "5min", "1m": "1min"}

    async def _get(self, path: str, **params):
        d = await self.http.get_json(f"{self.URL}{path}", provider=self.name, params={**params, "apikey": self.api_key})
        if isinstance(d, dict) and d.get("status") == "error":
            code, msg = d.get("code"), str(d.get("message", "error"))[:160]
            if code == 429:
                raise RateLimited(msg, provider=self.name, retry_after=60)
            if code in (401, 403):
                raise AuthError(msg, provider=self.name)
            if code == 404 or code == 400:
                raise NoDataError(msg, provider=self.name)
            raise ProviderError(msg, provider=self.name)
        return d

    async def get_history(self, symbol, start, end, interval="1d"):
        d = await self._get("/time_series", symbol=symbol, interval=self._IV[interval], outputsize=5000,
                            start_date=start.isoformat(), end_date=(end + timedelta(days=1)).isoformat(), order="ASC")
        vals = d.get("values") or []
        if not vals:
            raise NoDataError("no values", provider=self.name)
        meta = d.get("meta") or {}
        df = pd.DataFrame(vals)
        idx = pd.to_datetime(df["datetime"])
        if interval not in ("1d", "1wk", "1mo"):
            # intraday timestamps are exchange-local wall-clock times -> convert to UTC
            idx = idx.dt.tz_localize(meta.get("exchange_timezone") or "America/New_York",
                                     ambiguous="NaT", nonexistent="shift_forward")
        df.index = idx
        return PriceHistory.from_frame(symbol, df, self.name, interval, currency=meta.get("currency"),
                                       meta={"exchange": meta.get("exchange")})

    async def get_quote(self, symbol):
        d = await self._get("/quote", symbol=symbol)
        return Quote(symbol=symbol, price=to_float(d.get("close")), source=self.name,
                     prev_close=to_float(d.get("previous_close")), change=to_float(d.get("change")),
                     change_pct=to_float(d.get("percent_change")), open=to_float(d.get("open")),
                     day_high=to_float(d.get("high")), day_low=to_float(d.get("low")), volume=to_float(d.get("volume")),
                     currency=d.get("currency"), name=d.get("name"),
                     timestamp=datetime.fromtimestamp(d["last_quote_at"], timezone.utc) if d.get("last_quote_at") else None)
