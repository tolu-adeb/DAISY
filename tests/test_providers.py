"""Adapter parsing tests against recorded-shape payloads (httpx.MockTransport, no network)."""
from datetime import date

import httpx
import pytest

from abg.config import Settings
from abg.errors import AuthError, NoDataError, ProviderError, RateLimited
from abg.http import HttpClient
from abg.providers.free import StooqProvider
from abg.providers.keyed import (AlphaVantageProvider, FinnhubProvider, FMPProvider, PolygonProvider, TiingoProvider,
                                 TwelveDataProvider)
from abg.providers.local import parse_price_csv
from abg.providers.yahoo import YahooProvider

S = Settings(alphavantage_api_key="k", finnhub_api_key="k", polygon_api_key="k", tiingo_api_key="k", fmp_api_key="k",
             twelvedata_api_key="k", _env_file=None)
D0, D1 = date(2024, 1, 1), date(2024, 1, 10)


def client(routes: dict):
    def handler(req: httpx.Request):
        for frag, resp in routes.items():
            if frag in str(req.url):
                status, body = resp if isinstance(resp, tuple) else (200, resp)
                if isinstance(body, str):
                    return httpx.Response(status, text=body)
                return httpx.Response(status, json=body)
        return httpx.Response(404, text="no route " + str(req.url))
    return HttpClient(transport=httpx.MockTransport(handler))


YAHOO_CHART = {"chart": {"result": [{
    "meta": {"currency": "USD", "regularMarketPrice": 187.5, "previousClose": 185.0, "longName": "Apple Inc.",
             "regularMarketDayHigh": 188, "regularMarketDayLow": 184, "regularMarketVolume": 5e7, "regularMarketTime": 1704900000},
    "timestamp": [1704205800, 1704292200, 1704378600],
    "indicators": {"quote": [{"open": [187.1, 184.2, None], "high": [188.4, 185.9, 182.8], "low": [183.9, 183.4, 180.9],
                              "close": [185.6, 184.3, 181.9], "volume": [82488700, 58414500, 71983600]}],
                   "adjclose": [{"adjclose": [184.9, 183.6, 181.2]}]}}], "error": None}}


async def test_yahoo_history_and_quote():
    p = YahooProvider(S, client({"/v8/finance/chart/": YAHOO_CHART}))
    h = await p.get_history("AAPL", D0, D1)
    assert len(h) == 3 and h.df["open"].iloc[-1] == h.df["close"].iloc[-1]   # missing open repaired
    assert "adj_close" in h.df and h.currency == "USD"
    q = await p.get_quote("AAPL")
    assert q.price == 187.5 and q.prev_close == 185.0 and round(q.change_pct, 2) == 1.35


async def test_yahoo_symbol_mapping_and_404():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(404, json={"chart": {"result": None, "error": {"code": "Not Found"}}})
    p = YahooProvider(S, HttpClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(NoDataError):
        await p.get_quote("BRK.B")
    assert seen[0].endswith("/BRK-B")


async def test_yahoo_options_uses_crumb():
    chain = {"optionChain": {"result": [{"quote": {"regularMarketPrice": 100}, "expirationDates": [1706832000],
             "options": [{"expirationDate": 1706832000,
                          "calls": [{"strike": 100, "bid": 2.0, "ask": 2.2, "lastPrice": 2.1, "impliedVolatility": 0.3,
                                     "volume": 10, "openInterest": 100, "contractSymbol": "X240202C00100000"}],
                          "puts": [{"strike": 100, "bid": 1.8, "ask": 2.0, "impliedVolatility": 0.31}]}]}]}}
    p = YahooProvider(S, client({"fc.yahoo.com": (404, "x"), "getcrumb": "abcCRUMB", "/v7/finance/options/": chain}))
    oc = await p.get_options("X")
    assert len(oc.contracts) == 2 and oc.contracts[0].mid == pytest.approx(2.1)


async def test_polygon():
    aggs = {"status": "OK", "results": [{"t": 1704171600000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100},
                                        {"t": 1704258000000, "o": 1.5, "h": 2, "l": 1, "c": 1.8, "v": 120}]}
    news = {"status": "OK", "results": [{"title": "Apple beats estimates", "article_url": "u", "published_utc": "2024-01-02T10:00:00Z",
                                         "publisher": {"name": "Bench"}, "insights": [{"ticker": "AAPL", "sentiment": "positive"}]}]}
    p = PolygonProvider(S, client({"/v2/aggs/ticker/AAPL/range": aggs, "/v2/reference/news": news}))
    h = await p.get_history("AAPL", D0, D1)
    assert len(h) == 2 and str(h.df.index[0].date()) == "2024-01-02"
    n = await p.get_news("AAPL")
    assert n[0].provider_sentiment > 0


async def test_polygon_error_mapping():
    p = PolygonProvider(S, client({"/v2/aggs": {"status": "ERROR", "error": "You've exceeded the maximum requests per minute"}}))
    with pytest.raises(RateLimited):
        await p.get_history("AAPL", D0, D1)


async def test_alphavantage_parse_and_throttle_note():
    daily = {"Meta Data": {}, "Time Series (Daily)": {
        "2024-01-03": {"1. open": "184.2", "2. high": "185.9", "3. low": "183.4", "4. close": "184.3", "5. volume": "58414500"},
        "2024-01-02": {"1. open": "187.1", "2. high": "188.4", "3. low": "183.9", "4. close": "185.6", "5. volume": "82488700"}}}
    p = AlphaVantageProvider(S, client({"TIME_SERIES_DAILY": daily}))
    h = await p.get_history("AAPL", D0, D1)
    assert list(h.df["close"]) == [185.6, 184.3]
    p2 = AlphaVantageProvider(S, client({"GLOBAL_QUOTE": {"Information": "Our standard API rate limit is 25 requests per day."}}))
    with pytest.raises(RateLimited):
        await p2.get_quote("AAPL")
    q = AlphaVantageProvider(S, client({"GLOBAL_QUOTE": {"Global Quote": {"05. price": "184.3", "08. previous close": "185.6",
                                                                        "10. change percent": "-0.70%"}}}))
    assert (await q.get_quote("AAPL")).change_pct == pytest.approx(-0.70)


async def test_finnhub_quote_and_fundamentals():
    p = FinnhubProvider(S, client({"/quote": {"c": 190.1, "d": 1.2, "dp": 0.64, "h": 191, "l": 188, "o": 189, "pc": 188.9, "t": 1704900000},
                                   "/stock/profile2": {"name": "Apple Inc", "marketCapitalization": 2900000, "shareOutstanding": 15500},
                                   "/stock/metric": {"metric": {"beta": 1.2, "peTTM": 30.5, "dividendYieldIndicatedAnnual": 0.5}}}))
    assert (await p.get_quote("AAPL")).price == 190.1
    f = await p.get_fundamentals("AAPL")
    assert f.market_cap == 2.9e12 and f.dividend_yield == pytest.approx(0.005) and f.pe == 30.5


async def test_tiingo_fmp_twelvedata():
    t = TiingoProvider(S, client({"/prices": [{"date": "2024-01-02T00:00:00.000Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
                                              "volume": 10, "adjClose": 1.4}, {"date": "2024-01-03T00:00:00.000Z", "open": 1.5,
                                              "high": 2, "low": 1, "close": 1.7, "volume": 11, "adjClose": 1.6}]}))
    assert len(await t.get_history("AAPL", D0, D1)) == 2
    f = FMPProvider(S, client({"historical-price-eod": [{"date": "2024-01-03", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 9},
                                                      {"date": "2024-01-02", "open": 1, "high": 2, "low": 0.5, "close": 1.4, "volume": 9}]}))
    h = await f.get_history("AAPL", D0, D1)
    assert h.df.index.is_monotonic_increasing
    td = TwelveDataProvider(S, client({"/time_series": {"status": "error", "code": 429, "message": "run out of API credits"}}))
    with pytest.raises(RateLimited):
        await td.get_history("AAPL", D0, D1)


async def test_stooq_csv_and_bad_response():
    csv = "Date,Open,High,Low,Close,Volume\n2024-01-02,187.15,188.44,183.89,185.64,82488700\n2024-01-03,184.22,185.88,183.43,184.25,58414500\n"
    assert len(await StooqProvider(S, client({"stooq.com": csv})).get_history("AAPL", D0, D1)) == 2
    with pytest.raises(NoDataError):
        await StooqProvider(S, client({"stooq.com": "No data"})).get_history("AAPL", D0, D1)


@pytest.mark.parametrize("status,exc", [(429, RateLimited), (401, AuthError), (403, AuthError), (404, NoDataError),
                                        (503, ProviderError)])
async def test_http_status_mapping(status, exc):
    http = client({"x": (status, "err")})
    with pytest.raises(exc):
        await http.get_json("https://example.com/x", provider="t")


async def test_timeout_mapped():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)
    from abg.errors import ProviderTimeout
    with pytest.raises(ProviderTimeout):
        await HttpClient(transport=httpx.MockTransport(handler)).get_json("https://e.com", provider="t")


# ---------------------------------------------------------------- CSV formats
YAHOO_CSV = "Date,Open,High,Low,Close,Adj Close,Volume\n2024-01-02,187.15,188.44,183.89,185.64,184.94,82488700\n2024-01-03,184.22,185.88,183.43,184.25,183.55,58414500\n"
MACRO_CSV = ('"Macrotrends Data Download"\n"AAPL - Historical Price and Volume Data"\n\n"DISCLAIMER AND TERMS OF USE: ..."\n\n'
             "date,open,high,low,close,volume\n2024-01-02,187.15,188.44,183.89,185.64,82488700\n2024-01-03,184.22,185.88,183.43,184.25,58414500\n")
NASDAQ_CSV = "Date,Close/Last,Volume,Open,High,Low\n01/03/2024,$184.25,58414460,$184.22,$185.88,$183.43\n01/02/2024,$185.64,82488670,$187.15,$188.44,$183.89\n"
INVESTING_CSV = ('"Date","Price","Open","High","Low","Vol.","Change %"\n"01/03/2024","184.25","184.22","185.88","183.43","58.41M","-0.75%"\n'
                 '"01/02/2024","185.64","187.15","188.44","183.89","82.49M","-3.58%"\n')


@pytest.mark.parametrize("text,fmt", [(YAHOO_CSV, "yahoo"), (MACRO_CSV, "macrotrends"), (NASDAQ_CSV, "nasdaq"),
                                      (INVESTING_CSV, "investing")])
def test_csv_formats(text, fmt):
    ph = parse_price_csv(text, "AAPL")
    assert ph.meta["csv_format"] == fmt
    assert len(ph) == 2 and ph.df.index.is_monotonic_increasing
    assert ph.df["close"].iloc[-1] == pytest.approx(184.25)
    assert ph.df["volume"].iloc[-1] > 5e7


def test_csv_rejects_garbage():
    from abg.errors import DataValidationError
    with pytest.raises(DataValidationError):
        parse_price_csv("foo,bar\n1,2\n")


# ---------------------------------------------------------------- payloads captured live on 2026-09-24
TIINGO_LIVE = [{"date": "2026-09-22T00:00:00.000Z", "close": 339.75, "high": 345.34, "low": 338.75, "open": 340.135,
                "volume": 40711786, "adjClose": 339.75, "adjHigh": 345.34, "adjLow": 338.75, "adjOpen": 340.135,
                "adjVolume": 40711786, "divCash": 0.0, "splitFactor": 1.0},
               {"date": "2026-09-23T00:00:00.000Z", "close": 1348.08, "high": 1367.2, "low": 1342.0, "open": 1364.3,
                "volume": 7914705, "adjClose": 337.02, "adjHigh": 341.8, "adjLow": 335.5, "adjOpen": 341.075,
                "adjVolume": 31658823, "divCash": 0.0, "splitFactor": 1.0}]   # raw row made pre-split to prove adj is used


async def test_tiingo_uses_split_adjusted_prices_and_iex_fallback():
    iex = [{"ticker": "AAPL", "timestamp": "2026-09-24T11:46:06.356374345-04:00", "open": 336.78, "high": 338.25,
            "low": 334.265, "mid": 335.98, "tngoLast": 336.28, "last": None, "prevClose": 337.02, "volume": 274685.0}]
    t = TiingoProvider(S, client({"/prices": TIINGO_LIVE, "/iex/": iex}))
    h = await t.get_history("AAPL", D0, D1)
    assert h.df["close"].tolist() == [339.75, 337.02] and h.meta["adjusted"]
    q = await t.get_quote("AAPL")
    assert q.price == 336.28 and q.prev_close == 337.02 and q.timestamp is not None


async def test_tiingo_news_disabled_by_default_and_404_mapping():
    from abg.models import Capability
    t = TiingoProvider(S, client({"/prices": (404, '{"detail":"Error: Ticker \'ZZZZZZ\' not found"}')}))
    assert not t.supports(Capability.NEWS)
    assert TiingoProvider(S.model_copy(update={"tiingo_news_enabled": True}), None).supports(Capability.NEWS)
    with pytest.raises(NoDataError):
        await t.get_history("ZZZZZZ", D0, D1)


async def test_twelvedata_intraday_timezone_and_quote():
    ts = {"meta": {"symbol": "AAPL", "interval": "1h", "currency": "USD", "exchange_timezone": "America/New_York"},
          "values": [{"datetime": "2026-09-23 14:30:00", "open": "337.05", "high": "337.39", "low": "336.2",
                      "close": "336.33", "volume": "2037450"},
                     {"datetime": "2026-09-24 09:30:00", "open": "336.72", "high": "338.25", "low": "334.33",
                      "close": "337.45", "volume": "202907"}], "status": "ok"}
    quote = {"symbol": "AAPL", "name": "Apple Inc.", "currency": "USD", "open": "336.72", "high": "338.25", "low": "334.33",
             "close": "336.13", "volume": "325084", "previous_close": "337.019989", "change": "-0.88998901",
             "percent_change": "-0.26407603", "last_quote_at": 1790264700, "is_market_open": True}
    td = TwelveDataProvider(S, client({"/time_series": ts, "/quote": quote}))
    h = await td.get_history("AAPL", D0, D1, "1h")
    assert str(h.df.index[-1]) == "2026-09-24 13:30:00"          # 09:30 New York (EDT) == 13:30 UTC
    q = await td.get_quote("AAPL")
    assert q.price == 336.13 and q.name == "Apple Inc." and round(q.change_pct, 2) == -0.26
    bad = TwelveDataProvider(S, client({"/quote": (404, '{"code":404,"message":"symbol invalid","status":"error"}')}))
    with pytest.raises(NoDataError):
        await bad.get_quote("ZZZZZZ")


async def test_finnhub_news_relevance_and_unknown_symbol():
    news = [{"category": "company", "datetime": 1790258172, "headline": "Meta's new keychain gadget", "related": "AAPL",
             "source": "Yahoo", "summary": "Meta unveiled a device.", "url": "u1"},
            {"category": "company", "datetime": 1790200000, "headline": "Apple price prediction", "related": "AAPL",
             "source": "Yahoo", "summary": "Why our target sits above Wall Street.", "url": "u2"}]
    p = FinnhubProvider(S, client({"/company-news": news, "/stock/profile2": {"name": "Apple Inc", "ticker": "AAPL"},
                                   "/quote": {"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0}}))
    items = await p.get_news("AAPL")
    assert items[0].title == "Apple price prediction"             # relevant article ranked above the newer Meta one
    with pytest.raises(NoDataError):
        await p.get_quote("ZZZZZZ")                                 # Finnhub answers unknown tickers with c=0


async def test_per_capability_order(tmp_path):
    from abg.cache import TieredCache
    from abg.models import Capability
    from abg.providers.router import ProviderRouter, build_providers
    s = Settings(finnhub_api_key="k", tiingo_api_key="k", twelvedata_api_key="k", quote_order="finnhub,twelvedata,yahoo",
                 history_order="tiingo,twelvedata,yahoo", cache_dir=tmp_path, _env_file=None)
    r = ProviderRouter(build_providers(s, HttpClient()), s, TieredCache(tmp_path))
    assert [x.name for x in r.candidates(Capability.QUOTE)] == ["finnhub", "twelvedata", "yahoo"]
    assert [x.name for x in r.candidates(Capability.HISTORY)] == ["tiingo", "twelvedata", "yahoo"]
    assert r.candidates(Capability.NEWS)[0].name == "yahoo"          # falls back to the global order
