"""Key-less sources: yfinance (library wrapper around Yahoo) and Stooq (CSV)."""
from __future__ import annotations

import asyncio
import io
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from ..errors import NoDataError, ProviderError, RateLimited
from ..models import Capability, Fundamentals, NewsItem, OptionChain, OptionContract, PriceHistory, Quote
from ..utils import to_float
from .base import Provider, register_provider

import logging

try:  # optional dependency
    import yfinance as _yf  # type: ignore
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)   # it logs every failure to stderr
except Exception:  # pragma: no cover - depends on environment
    _yf = None


# =========================================================================== yfinance
@register_provider
class YFinanceProvider(Provider):
    """Wraps the ``yfinance`` package (pip install abg-terminal[yfinance]).

    yfinance is synchronous, so calls run in a worker thread.  It is slower than the
    direct Yahoo adapter but is actively maintained against Yahoo's auth changes, which
    makes it an excellent *second* line of defence for options and fundamentals.
    """

    name = "yfinance"
    label = "yfinance (library)"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTE, Capability.NEWS,
                              Capability.OPTIONS, Capability.FUNDAMENTALS})
    rate_limit = (4, 1.0)
    intraday = True
    notes = "Optional dependency; install with `pip install yfinance`."
    _IV = {"1d": "1d", "1wk": "1wk", "1mo": "1mo", "1h": "60m", "30m": "30m", "15m": "15m", "5m": "5m", "1m": "1m"}

    def is_configured(self) -> bool:
        return _yf is not None

    def map_symbol(self, symbol):
        return symbol.replace(".", "-") if "." in symbol and not symbol.startswith("^") else symbol

    async def _run(self, fn, *a, **kw):
        try:
            return await asyncio.to_thread(fn, *a, **kw)
        except ProviderError:
            raise
        except Exception as e:
            name = type(e).__name__
            if "RateLimit" in name or "Too Many" in str(e):
                raise RateLimited(str(e)[:160], provider=self.name, retry_after=30) from None
            raise ProviderError(f"{name}: {str(e)[:160]}", provider=self.name) from None

    def _ticker(self, symbol):
        return _yf.Ticker(self.map_symbol(symbol))

    async def get_history(self, symbol, start, end, interval="1d"):
        def _h():
            return self._ticker(symbol).history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                                                interval=self._IV[interval], auto_adjust=False, actions=False)
        df = await self._run(_h)
        if df is None or df.empty:
            raise NoDataError("empty history", provider=self.name)
        df = df.rename(columns={"Adj Close": "adj_close"})
        return PriceHistory.from_frame(symbol, df, self.name, interval)

    async def get_quote(self, symbol):
        def _q():
            fi = self._ticker(symbol).fast_info
            return {k: fi.get(k) if hasattr(fi, "get") else getattr(fi, k, None)
                    for k in ("last_price", "previous_close", "open", "day_high", "day_low", "last_volume",
                              "market_cap", "currency")}
        f = await self._run(_q)
        return Quote(symbol=symbol, price=to_float(f["last_price"]), source=self.name,
                     prev_close=to_float(f["previous_close"]), open=to_float(f["open"]), day_high=to_float(f["day_high"]),
                     day_low=to_float(f["day_low"]), volume=to_float(f["last_volume"]),
                     market_cap=to_float(f["market_cap"]), currency=f["currency"])

    async def get_news(self, symbol, limit=20):
        raw = await self._run(lambda: self._ticker(symbol).news or [])
        out = []
        for n in raw:
            c = n.get("content") or n          # yfinance >=0.2.50 nests under "content"
            title = c.get("title")
            if not title:
                continue
            pub = c.get("pubDate") or c.get("providerPublishTime")
            if isinstance(pub, (int, float)):
                pub = datetime.fromtimestamp(pub, timezone.utc)
            elif pub:
                pub = pd.Timestamp(pub).to_pydatetime()
            url = (c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else c.get("link")
            pubr = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher")
            out.append(NewsItem(title=title, source=self.name, url=url, publisher=pubr, summary=c.get("summary"),
                                published_at=pub))
        if not out:
            raise NoDataError("no news", provider=self.name)
        return out[:limit]

    async def get_options(self, symbol, expiry=None):
        def _o():
            t = self._ticker(symbol)
            exps = list(t.options or [])
            if not exps:
                return None
            target = expiry.isoformat() if expiry and expiry.isoformat() in exps else exps[0]
            ch = t.option_chain(target)
            spot = t.fast_info.get("last_price") if hasattr(t.fast_info, "get") else t.fast_info.last_price
            return exps, target, ch.calls, ch.puts, spot
        r = await self._run(_o)
        if not r:
            raise NoDataError("no expirations", provider=self.name)
        exps, target, calls, puts, spot = r
        exp_d = date.fromisoformat(target)
        contracts = []
        for kind, frame in (("call", calls), ("put", puts)):
            for row in frame.to_dict(orient="records"):
                contracts.append(OptionContract(expiry=exp_d, strike=float(row["strike"]), kind=kind,
                                                bid=to_float(row.get("bid")), ask=to_float(row.get("ask")),
                                                last=to_float(row.get("lastPrice")), iv=to_float(row.get("impliedVolatility")),
                                                volume=to_float(row.get("volume")), open_interest=to_float(row.get("openInterest")),
                                                symbol=row.get("contractSymbol")))
        return OptionChain(symbol=symbol, underlying_price=float(spot), source=self.name,
                           expirations=[date.fromisoformat(e) for e in exps], contracts=contracts)

    async def get_fundamentals(self, symbol):
        info = await self._run(lambda: self._ticker(symbol).info or {})
        if not info:
            raise NoDataError("no info", provider=self.name)
        return Fundamentals(symbol=symbol, source=self.name, name=info.get("longName") or info.get("shortName"),
                            sector=info.get("sector"), industry=info.get("industry"),
                            description=info.get("longBusinessSummary"), market_cap=to_float(info.get("marketCap")),
                            pe=to_float(info.get("trailingPE")), forward_pe=to_float(info.get("forwardPE")),
                            eps=to_float(info.get("trailingEps")), beta=to_float(info.get("beta")),
                            # yfinance >=0.2.54 reports dividendYield in percent (0.44) instead of decimal
                            dividend_yield=(lambda d: d / 100 if d and d > 0.2 else d)(to_float(info.get("dividendYield"))),
                            week52_high=to_float(info.get("fiftyTwoWeekHigh")), week52_low=to_float(info.get("fiftyTwoWeekLow")),
                            shares_outstanding=to_float(info.get("sharesOutstanding")),
                            profit_margin=to_float(info.get("profitMargins")),
                            extra={"target_mean_price": to_float(info.get("targetMeanPrice")),
                                   "recommendation": info.get("recommendationKey")})


# =========================================================================== Stooq
@register_provider
class StooqProvider(Provider):
    name = "stooq"
    label = "Stooq"
    capabilities = frozenset({Capability.HISTORY})
    rate_limit = (2, 1.0)
    notes = "Free daily/weekly/monthly CSV history; US tickers mapped to '<sym>.us'."
    URL = "https://stooq.com/q/d/l/"

    def map_symbol(self, symbol):
        s = symbol.lower().replace(".", "-")
        if symbol.startswith("^"):
            return {"^GSPC": "^spx", "^DJI": "^dji", "^IXIC": "^ndq"}.get(symbol, s)
        return s if "." in s else f"{s}.us"

    async def get_history(self, symbol, start, end, interval="1d"):
        i = {"1d": "d", "1wk": "w", "1mo": "m"}[interval]
        text = await self.http.get_text(self.URL, provider=self.name,
                                        params={"s": self.map_symbol(symbol), "i": i,
                                                "d1": start.strftime("%Y%m%d"), "d2": end.strftime("%Y%m%d")})
        head = text[:200].lower()
        if not head.startswith("date") :
            if "exceeded" in head or "limit" in head:
                raise RateLimited(text[:120], provider=self.name, retry_after=3600)
            raise NoDataError(f"unexpected response: {text[:80]!r}", provider=self.name)
        df = pd.read_csv(io.StringIO(text))
        if df.empty:
            raise NoDataError("no rows", provider=self.name)
        df.index = pd.to_datetime(df["Date"])
        return PriceHistory.from_frame(symbol, df.drop(columns=["Date"]), self.name, interval)
