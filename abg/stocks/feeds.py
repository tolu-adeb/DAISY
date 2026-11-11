"""Where the desk's news comes from (docs/22.3), all on free plans and all rate-limited:

    Finnhub   market news (general, merger), company news, the IPO calendar
    SEC EDGAR recent filings per company (8-K, S-1/F-1, 13D/G, Form 4) from the free JSON API

Keys travel as request parameters only and are never logged. EDGAR asks for a User-Agent that names the
application; it is a setting (``ABG_STOCKS_EDGAR_USER_AGENT``) and the default names this app, not a person.
Analyst rating changes are not on Finnhub's free plan, so they are "not in data".
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import news as N

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
EDGAR_UA = "AIBG-DAISY research terminal (github.com/tolu-adeb/DAISY)"
FINNHUB = "https://finnhub.io/api/v1"


class NewsFeeds:
    def __init__(self, settings, http):
        from abg.swing.wide import Sources
        self.s = settings
        self.src = Sources(settings, http)
        self.http = http
        self._cik: dict | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.src.finnhub_key)

    def _per_min(self) -> float:
        return float(getattr(self.s, "swing_wide_finnhub_per_min", 50) or 50)

    async def _fh(self, path: str, params: dict):
        return await self.src._get("finnhub", self._per_min(), FINNHUB + path, {**params, "token": self.src.finnhub_key})

    # ------------------------------------------------------------------ Finnhub
    async def market_news(self, category: str = "general") -> list[dict]:
        j = await self._fh("/news", {"category": category})
        out = []
        for r in j or []:
            if not isinstance(r, dict) or not r.get("headline"):
                continue
            at = datetime.fromtimestamp(r["datetime"], NY) if r.get("datetime") else None
            out.append(N.article(None, r["headline"], r.get("summary") or "", r.get("url"), at, r.get("source"),
                                 kind="news", extra={"category": category, "related": r.get("related") or ""}))
        return out

    async def company_news(self, symbol: str, days: int = 3, name: str | None = None, limit: int = 15) -> list[dict]:
        to = datetime.now(NY).date()
        j = await self._fh("/company-news", {"symbol": symbol, "from": (to - timedelta(days=days)).isoformat(),
                                             "to": to.isoformat()})
        rows = sorted([r for r in (j or []) if isinstance(r, dict) and r.get("headline")],
                      key=lambda r: -(r.get("datetime") or 0))[:limit]
        return [N.article(symbol, r["headline"], r.get("summary") or "", r.get("url"),
                          datetime.fromtimestamp(r["datetime"], NY) if r.get("datetime") else None,
                          r.get("source"), name=name) for r in rows]

    async def recommendation(self, symbol: str) -> dict | None:
        """Wall Street's buy / hold / sell counts for the latest month (Finnhub, free plan). Third-party
        opinion, shown as such. Price targets are not on the free plan."""
        j = await self._fh("/stock/recommendation", {"symbol": symbol})
        rows = [r for r in (j or []) if isinstance(r, dict) and r.get("period")]
        if not rows:
            return None
        r = max(rows, key=lambda x: x["period"])
        n = {k: int(r.get(k) or 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
        tot = sum(n.values())
        if not tot:
            return None
        score = (2 * n["strongBuy"] + n["buy"] - n["sell"] - 2 * n["strongSell"]) / tot
        label = "Buy" if score >= 0.75 else "Outperform" if score >= 0.25 else "Hold" if score > -0.25 else "Underperform" if score > -0.75 else "Sell"
        return {"period": r["period"], **n, "analysts": tot, "score": round(score, 3), "label": label, "source": "Finnhub"}

    async def ipo_calendar(self, frm: date, to: date) -> list[dict]:
        j = await self._fh("/calendar/ipo", {"from": frm.isoformat(), "to": to.isoformat()})
        out = []
        for r in (j or {}).get("ipoCalendar") or []:
            if not r.get("date") or not r.get("name"):
                continue
            out.append({"date": r["date"], "symbol": (r.get("symbol") or "").upper() or None, "name": r["name"],
                        "exchange": r.get("exchange"), "price": r.get("price"), "shares": r.get("numberOfShares"),
                        "value": r.get("totalSharesValue"), "status": r.get("status")})
        return sorted(out, key=lambda x: x["date"])

    # ------------------------------------------------------------------ SEC EDGAR
    def _ua(self) -> dict:
        return {"User-Agent": getattr(self.s, "stocks_edgar_user_agent", None) or EDGAR_UA,
                "Accept-Encoding": "gzip, deflate"}

    def _cik_cache(self) -> Path:
        return Path(self.s.data_dir).expanduser() / "stocks" / "sec_tickers.json"

    async def cik_map(self) -> dict:
        """ticker -> CIK, from the SEC's own list; cached for a week."""
        if self._cik is not None:
            return self._cik
        p = self._cik_cache()
        try:
            d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except (OSError, ValueError):
            d = None
        if d and time.time() - float(d.get("at", 0)) < 7 * 86400:
            self._cik = d["map"]
            return self._cik
        try:
            await self.src._pace("sec", 120)
            j = await self.http.get_json("https://www.sec.gov/files/company_tickers.json", provider="sec",
                                         headers=self._ua(), timeout=30)
            m = {str(v["ticker"]).upper().replace(".", "-"): int(v["cik_str"]) for v in (j or {}).values()
                 if v.get("ticker") and v.get("cik_str")}
            if len(m) < 1000:
                raise ValueError("short ticker list")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"at": time.time(), "map": m}), encoding="utf-8")
            self._cik = m
        except Exception as e:
            log.info("EDGAR ticker list unavailable (%s)", type(e).__name__)
            self._cik = (d or {}).get("map") or {}
        return self._cik

    async def filings(self, symbol: str, days: int = 5, name: str | None = None) -> list[dict]:
        cik = (await self.cik_map()).get(symbol)
        if not cik:
            return []
        await self.src._pace("sec", 120)
        j = await self.http.get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", provider="sec",
                                     headers=self._ua(), timeout=30)
        rec = ((j or {}).get("filings") or {}).get("recent") or {}
        since = (datetime.now(NY).date() - timedelta(days=days)).isoformat()
        out = []
        for form, day, acc, doc in zip(rec.get("form") or [], rec.get("filingDate") or [],
                                       rec.get("accessionNumber") or [], rec.get("primaryDocument") or []):
            if day < since or form not in N.FILING_FORMS:
                continue
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}" if acc and doc else None
            a = N.article(symbol, f"{symbol} filed: {N.FILING_FORMS[form]}", "", url, f"{day}T00:00:00-05:00",
                          "SEC EDGAR", kind="filing", name=name, extra={"form": form})
            a["sent"], a["rel"] = 0.0, 1.0
            out.append(a)
        return out[:10]
