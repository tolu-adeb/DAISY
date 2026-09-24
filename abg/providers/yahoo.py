"""Yahoo Finance (direct JSON endpoints, no key).

Endpoints used
--------------
chart        https://query1.finance.yahoo.com/v8/finance/chart/{sym}      history + quote (no crumb)
search       https://query1.finance.yahoo.com/v1/finance/search            news (no crumb)
options      https://query2.finance.yahoo.com/v7/finance/options/{sym}     needs cookie + crumb
quoteSummary https://query2.finance.yahoo.com/v10/finance/quoteSummary/{s} needs cookie + crumb

These are unofficial and Yahoo changes them without notice; that's exactly why the
router can fall back to ``yfinance`` (maintained against those changes) and to keyed
vendors.  The crumb is fetched lazily once and refreshed on auth failure.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from ..errors import AuthError, NoDataError, ProviderError
from ..models import Capability, Fundamentals, NewsItem, OptionChain, OptionContract, PriceHistory, Quote
from ..utils import to_float
from .base import Provider, register_provider

Q1 = "https://query1.finance.yahoo.com"
Q2 = "https://query2.finance.yahoo.com"
_INTERVAL = {"1d": "1d", "1wk": "1wk", "1mo": "1mo", "1h": "60m", "30m": "30m", "15m": "15m", "5m": "5m", "1m": "1m"}
_INTRADAY_MAX_DAYS = {"60m": 729, "30m": 59, "15m": 59, "5m": 59, "1m": 7}


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


@register_provider
class YahooProvider(Provider):
    name = "yahoo"
    label = "Yahoo Finance (direct)"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTE, Capability.NEWS,
                              Capability.OPTIONS, Capability.FUNDAMENTALS})
    rate_limit = (8, 1.0)
    intraday = True
    notes = "Unofficial API; options/fundamentals need a session crumb (handled automatically)."

    def __init__(self, settings, http):
        super().__init__(settings, http)
        self._crumb: str | None = None
        self._crumb_at = 0.0
        self._crumb_lock = asyncio.Lock()

    def map_symbol(self, symbol: str) -> str:
        return symbol.replace(".", "-") if "." in symbol and not symbol.startswith("^") else symbol

    # ------------------------------------------------------------------ crumb
    async def _get_crumb(self, force: bool = False) -> str:
        if self._crumb and not force and time.time() - self._crumb_at < 3600:
            return self._crumb
        async with self._crumb_lock:
            if self._crumb and not force and time.time() - self._crumb_at < 3600:
                return self._crumb
            try:  # sets the A1/A3 consent cookies in the shared client's jar; status irrelevant
                await self.http.raw.get("https://fc.yahoo.com", timeout=5.0)
            except Exception:
                pass
            text = await self.http.get_text(f"{Q1}/v1/test/getcrumb", provider=self.name)
            crumb = text.strip()
            if not crumb or "<" in crumb or len(crumb) > 64:
                raise AuthError("could not obtain Yahoo crumb", provider=self.name)
            self._crumb, self._crumb_at = crumb, time.time()
            return crumb

    async def _crumbed_json(self, url: str, params: dict) -> dict:
        for attempt in (0, 1):
            crumb = await self._get_crumb(force=attempt == 1)
            try:
                return await self.http.get_json(url, provider=self.name, params={**params, "crumb": crumb})
            except AuthError:
                if attempt == 1:
                    raise
        raise ProviderError("unreachable", provider=self.name)  # pragma: no cover

    # ------------------------------------------------------------------ chart
    async def _chart(self, symbol: str, params: dict) -> dict:
        data = await self.http.get_json(f"{Q1}/v8/finance/chart/{self.map_symbol(symbol)}", provider=self.name,
                                        params={"includeAdjustedClose": "true", "events": "div,split", **params})
        chart = (data or {}).get("chart") or {}
        if chart.get("error"):
            raise NoDataError(str(chart["error"].get("description", chart["error"])), provider=self.name)
        result = chart.get("result") or []
        if not result:
            raise NoDataError("empty chart result", provider=self.name)
        return result[0]

    async def get_history(self, symbol, start, end, interval="1d"):
        yi = _INTERVAL.get(interval)
        if yi is None:
            raise NoDataError(f"interval {interval} unsupported", provider=self.name)
        if yi in _INTRADAY_MAX_DAYS:
            start = max(start, end - timedelta(days=_INTRADAY_MAX_DAYS[yi]))
        r = await self._chart(symbol, {"period1": _epoch(start), "period2": _epoch(end + timedelta(days=1)),
                                       "interval": yi})
        ts = r.get("timestamp") or []
        q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
        if not ts or not q:
            raise NoDataError("no bars in range", provider=self.name)
        df = pd.DataFrame({k: q.get(k) for k in ("open", "high", "low", "close", "volume")},
                          index=pd.to_datetime(ts, unit="s", utc=True))
        adj = ((r.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
        if adj and len(adj) == len(ts):
            df["adj_close"] = adj
        if yi in ("1d", "1wk", "1mo"):
            df.index = df.index.normalize()
        meta = r.get("meta") or {}
        return PriceHistory.from_frame(symbol, df, self.name, interval, currency=meta.get("currency"),
                                       meta={"exchange": meta.get("exchangeName"), "name": meta.get("longName")})

    async def get_quote(self, symbol):
        r = await self._chart(symbol, {"range": "5d", "interval": "1d"})
        m = r.get("meta") or {}
        price = to_float(m.get("regularMarketPrice"))
        closes = [c for c in (((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []) if c]
        prev = to_float(m.get("previousClose")) or (closes[-2] if len(closes) >= 2 else to_float(m.get("chartPreviousClose")))
        ts = m.get("regularMarketTime")
        return Quote(symbol=symbol, price=price, source=self.name, prev_close=prev,
                     day_high=to_float(m.get("regularMarketDayHigh")), day_low=to_float(m.get("regularMarketDayLow")),
                     volume=to_float(m.get("regularMarketVolume")), currency=m.get("currency"),
                     name=m.get("longName") or m.get("shortName"),
                     timestamp=datetime.fromtimestamp(ts, timezone.utc) if ts else None)

    # ------------------------------------------------------------------ news
    async def get_news(self, symbol, limit=20):
        data = await self.http.get_json(f"{Q1}/v1/finance/search", provider=self.name,
                                        params={"q": self.map_symbol(symbol), "newsCount": limit, "quotesCount": 0,
                                                "enableFuzzyQuery": "false"})
        items = []
        for n in (data or {}).get("news") or []:
            if not n.get("title"):
                continue
            ts = n.get("providerPublishTime")
            items.append(NewsItem(title=n["title"], source=self.name, url=n.get("link"), publisher=n.get("publisher"),
                                  published_at=datetime.fromtimestamp(ts, timezone.utc) if ts else None,
                                  tickers=n.get("relatedTickers") or []))
        if not items:
            raise NoDataError("no news", provider=self.name)
        return items[:limit]

    # ------------------------------------------------------------------ options
    async def get_options(self, symbol, expiry=None):
        params = {}
        if expiry:
            params["date"] = _epoch(expiry)
        data = await self._crumbed_json(f"{Q2}/v7/finance/options/{self.map_symbol(symbol)}", params)
        res = ((data or {}).get("optionChain") or {}).get("result") or []
        if not res:
            raise NoDataError("no option chain", provider=self.name)
        res = res[0]
        spot = to_float((res.get("quote") or {}).get("regularMarketPrice"))
        exps = [datetime.fromtimestamp(e, timezone.utc).date() for e in res.get("expirationDates") or []]
        contracts: list[OptionContract] = []
        for block in res.get("options") or []:
            exp = datetime.fromtimestamp(block.get("expirationDate"), timezone.utc).date()
            for kind in ("calls", "puts"):
                for c in block.get(kind) or []:
                    strike = to_float(c.get("strike"))
                    if not strike:
                        continue
                    contracts.append(OptionContract(
                        expiry=exp, strike=strike, kind=kind[:-1], bid=to_float(c.get("bid")), ask=to_float(c.get("ask")),
                        last=to_float(c.get("lastPrice")), iv=to_float(c.get("impliedVolatility")),
                        volume=to_float(c.get("volume")), open_interest=to_float(c.get("openInterest")),
                        symbol=c.get("contractSymbol")))
        if not spot or not contracts:
            raise NoDataError("empty option chain", provider=self.name)
        return OptionChain(symbol=symbol, underlying_price=spot, source=self.name, expirations=exps, contracts=contracts)

    # ------------------------------------------------------------------ fundamentals
    async def get_fundamentals(self, symbol):
        data = await self._crumbed_json(
            f"{Q2}/v10/finance/quoteSummary/{self.map_symbol(symbol)}",
            {"modules": "price,summaryDetail,defaultKeyStatistics,assetProfile,financialData"})
        res = ((data or {}).get("quoteSummary") or {}).get("result") or []
        if not res:
            raise NoDataError("no quoteSummary", provider=self.name)
        r = res[0]
        price, sd, ks, ap, fd = (r.get(k) or {} for k in
                                 ("price", "summaryDetail", "defaultKeyStatistics", "assetProfile", "financialData"))
        return Fundamentals(
            symbol=symbol, source=self.name, name=price.get("longName") or price.get("shortName"),
            sector=ap.get("sector"), industry=ap.get("industry"), description=ap.get("longBusinessSummary"),
            market_cap=to_float(price.get("marketCap") or sd.get("marketCap")), pe=to_float(sd.get("trailingPE")),
            forward_pe=to_float(sd.get("forwardPE") or ks.get("forwardPE")), eps=to_float(ks.get("trailingEps")),
            beta=to_float(sd.get("beta") or ks.get("beta")), dividend_yield=to_float(sd.get("dividendYield")),
            week52_high=to_float(sd.get("fiftyTwoWeekHigh")), week52_low=to_float(sd.get("fiftyTwoWeekLow")),
            shares_outstanding=to_float(ks.get("sharesOutstanding")), profit_margin=to_float(fd.get("profitMargins")),
            extra={"target_mean_price": to_float(fd.get("targetMeanPrice")),
                   "recommendation": fd.get("recommendationKey"),
                   "short_percent_float": to_float(ks.get("shortPercentOfFloat"))})
