"""Market data, earnings dates and news. yfinance by default (no key needed on GitHub
Actions); Twelve Data / Finnhub are used when their keys are present.

Prices are cached as CSV under data/prices in the same compact format the research
uses (volume in thousands), so research and live runs read identical data.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PRICE_DIR = ROOT / "data/prices"

# spin-off PATCH 4: Windows can't create a file called CON, PRN, AUX, NUL, COM1-9 or LPT1-9 (with any extension),
# so a ticker like CON (Concentra) would make the repo impossible to check out there.  Those get a trailing "_".
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


def price_file(sym: str, folder: Path | str | None = None) -> Path:
    """The cache file for ``sym`` in ``folder`` (default: PRICE_DIR, read at call time)."""
    name = f"{sym}_" if sym.split(".")[0].upper() in _WIN_RESERVED else sym
    return Path(folder if folder is not None else PRICE_DIR) / f"{name}.csv"


def symbol_of(path: Path | str) -> str:
    """Inverse of ``price_file``: the ticker a cache file holds."""
    stem = Path(path).stem
    base = stem[:-1] if stem.endswith("_") else stem
    return base if base.split(".")[0].upper() in _WIN_RESERVED else stem


# ------------------------------------------------------------------ prices
def _yf_symbol(sym: str) -> str:
    return sym.replace(".", "-")          # BRK.B -> BRK-B


def fetch_daily(sym: str, start: str | date, end: str | date | None = None) -> pd.DataFrame:
    """Daily OHLCV with columns open/high/low/close/volume(thousands), DatetimeIndex."""
    key = os.getenv("TWELVEDATA_API_KEY")
    if key:
        import requests
        r = requests.get("https://api.twelvedata.com/time_series", timeout=30, params={
            "symbol": sym, "interval": "1day", "start_date": str(start), "end_date": str(end or date.today()),
            "outputsize": 5000, "apikey": key, "format": "JSON"}).json()
        if "values" in r:
            df = pd.DataFrame(r["values"])
            df["date"] = pd.to_datetime(df["datetime"])
            df = df.set_index("date").sort_index()[["open", "high", "low", "close", "volume"]].astype(float)
            df["volume"] = df["volume"] / 1000
            return df
    import yfinance as yf
    df = yf.download(_yf_symbol(sym), start=str(start), end=str(end) if end else None, interval="1d",
                     auto_adjust=False, progress=False, multi_level_index=False)
    if df.empty:
        raise ValueError(f"no data for {sym}")
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df["volume"] = df["volume"] / 1000
    return df


def load_or_update(sym: str, min_start: str = "2026-04-01") -> pd.DataFrame:
    """Read cache, append any missing sessions, write back. Returns full frame."""
    from .backtest import load_prices
    path = price_file(sym)
    cached = load_prices(path) if path.exists() else pd.DataFrame()
    start = (cached.index[-1] - timedelta(days=5)).date() if len(cached) else min_start
    try:
        fresh = fetch_daily(sym, start)
        df = pd.concat([cached, fresh]).groupby(level=0).last().sort_index() if len(cached) else fresh
    except Exception:
        if not len(cached):
            raise
        df = cached
    save(sym, df)
    return df


def save(sym: str, df: pd.DataFrame) -> None:
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    out = pd.DataFrame({"d": df.index.strftime("%Y%m%d"), "o": df.open.round(4), "h": df.high.round(4),
                        "l": df.low.round(4), "c": df.close.round(4), "v": df.volume.round(0).astype(int)})
    out.iloc[::-1].to_csv(price_file(sym), index=False)


def live_quote(sym: str) -> float | None:
    """Last trade (delayed on free feeds). Used only for in-zone alerts, never for fills."""
    key = os.getenv("TWELVEDATA_API_KEY")
    try:
        if key:
            import requests
            r = requests.get("https://api.twelvedata.com/price", params={"symbol": sym, "apikey": key},
                             timeout=15).json()
            return float(r["price"])
        import yfinance as yf
        return float(yf.Ticker(_yf_symbol(sym)).fast_info["last_price"])
    except Exception:
        return None


# ------------------------------------------------------------------ events
def next_earnings(sym: str) -> date | None:
    try:
        import yfinance as yf
        cal = yf.Ticker(_yf_symbol(sym)).calendar
        d = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if d:
            d0 = d[0] if isinstance(d, (list, tuple)) else d
            return d0 if isinstance(d0, date) else pd.Timestamp(d0).date()
    except Exception:
        pass
    return None


def news(sym: str, days: int = 7, limit: int = 12) -> list[dict]:
    """Recent headlines: [{title, publisher, published, url, summary}]."""
    items: list[dict] = []
    fk = os.getenv("FINNHUB_API_KEY")
    if fk:
        import requests
        try:
            r = requests.get("https://finnhub.io/api/v1/company-news", timeout=20, params={
                "symbol": sym, "from": str(date.today() - timedelta(days=days)), "to": str(date.today()),
                "token": fk}).json()
            for n in r[:limit]:
                items.append({"title": n.get("headline"), "publisher": n.get("source"),
                              "published": datetime.fromtimestamp(n.get("datetime", 0)).isoformat(),
                              "url": n.get("url"), "summary": (n.get("summary") or "")[:400]})
        except Exception:
            pass
    if not items:
        try:
            import yfinance as yf
            for n in (yf.Ticker(_yf_symbol(sym)).news or [])[:limit]:
                c = n.get("content", n)
                items.append({"title": c.get("title"), "publisher": (c.get("provider") or {}).get("displayName")
                              if isinstance(c.get("provider"), dict) else c.get("publisher"),
                              "published": c.get("pubDate") or c.get("providerPublishTime"),
                              "url": (c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict)
                              else c.get("link"), "summary": (c.get("summary") or "")[:400]})
        except Exception:
            pass
    return items


# ------------------------------------------------------------------ fallback sentiment
_POS = {"beat", "beats", "raises", "raised", "upgrade", "upgraded", "record", "surge", "approval", "approved",
        "buyback", "outperform", "strong", "growth", "wins", "contract", "partnership", "accelerat"}
_NEG = {"miss", "misses", "cut", "cuts", "downgrade", "downgraded", "probe", "lawsuit", "recall", "delay",
        "halt", "fraud", "investigation", "weak", "slump", "plunge", "warning", "resign", "terminated", "dilution",
        "offering", "crl", "rejected"}


def lexicon_sentiment(items: list[dict]) -> float:
    """Crude [-1, 1] headline score used only when no LLM is available (tests, outages)."""
    score = n = 0
    for it in items:
        words = set((it.get("title") or "").lower().replace(",", " ").split())
        p = sum(any(w.startswith(k) for w in words) for k in _POS)
        q = sum(any(w.startswith(k) for w in words) for k in _NEG)
        if p or q:
            score += (p - q) / (p + q)
            n += 1
    return round(score / n, 3) if n else 0.0
