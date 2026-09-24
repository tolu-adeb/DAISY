"""Provider-agnostic data models.

Every provider adapter converts its native payload into these types, so the analysis
layer never knows (or cares) where data came from.  ``PriceHistory.sanitize`` is the
single choke point where bad OHLCV data is cleaned or rejected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Generic, TypeVar

import numpy as np
import pandas as pd

from .errors import DataValidationError
from .utils import jsonable, utcnow

OHLCV = ["open", "high", "low", "close", "volume"]


class Capability(str, Enum):
    HISTORY = "history"
    QUOTE = "quote"
    NEWS = "news"
    OPTIONS = "options"
    FUNDAMENTALS = "fundamentals"


# --------------------------------------------------------------------------- price history
@dataclass
class PriceHistory:
    """OHLCV bars.  ``df`` has a tz-naive DatetimeIndex (UTC) named 'date' and float columns
    open/high/low/close/volume (+ optional adj_close)."""

    symbol: str
    df: pd.DataFrame
    interval: str = "1d"
    source: str = "?"
    synthetic: bool = False
    currency: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_frame(cls, symbol: str, df: pd.DataFrame, source: str, interval: str = "1d", **kw) -> "PriceHistory":
        return cls(symbol=symbol, df=cls.sanitize(df, source), interval=interval, source=source, **kw)

    @staticmethod
    def sanitize(df: pd.DataFrame, source: str = "?") -> pd.DataFrame:
        """Normalise and validate OHLCV data.  Raises DataValidationError if unusable.

        - lower-cases columns, coerces to float, sorts by date, drops duplicate dates
        - drops rows with non-positive/NaN close
        - fills missing open/high/low from close, repairs high<low inconsistencies
        - removes timezone (converted to UTC first)
        """
        if df is None or len(df) == 0:
            raise DataValidationError("empty price history", provider=source)
        df = df.copy()
        df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
        if "close" not in df.columns:
            raise DataValidationError(f"no close column (got {list(df.columns)})", provider=source)
        idx = pd.to_datetime(df.index, errors="coerce", utc=True)
        df.index = idx.tz_convert(None)
        df.index.name = "date"
        df = df[~df.index.isna()]
        for c in OHLCV + ["adj_close"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
        df = df[df["close"].notna() & (df["close"] > 0)]
        if df.empty:
            raise DataValidationError("no valid closes", provider=source)
        for c in ("open", "high", "low"):
            if c not in df.columns:
                df[c] = df["close"]
            else:
                df[c] = df[c].where(df[c] > 0, df["close"])
        if "volume" not in df.columns:
            df["volume"] = 0.0
        df["volume"] = df["volume"].fillna(0.0).clip(lower=0)
        hi = df[["open", "high", "low", "close"]].max(axis=1)
        lo = df[["open", "high", "low", "close"]].min(axis=1)
        df["high"], df["low"] = hi, lo
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]
        cols = OHLCV + (["adj_close"] if "adj_close" in df.columns else [])
        return df[cols]

    # convenience -------------------------------------------------------------
    @property
    def last_close(self) -> float:
        return float(self.df["close"].iloc[-1])

    @property
    def last_date(self) -> pd.Timestamp:
        return self.df.index[-1]

    def __len__(self) -> int:
        return len(self.df)

    def tail(self, n: int) -> "PriceHistory":
        return PriceHistory(self.symbol, self.df.iloc[-n:], self.interval, self.source, self.synthetic,
                            self.currency, dict(self.meta))

    def since(self, start: date) -> "PriceHistory":
        return PriceHistory(self.symbol, self.df[self.df.index >= pd.Timestamp(start)], self.interval,
                            self.source, self.synthetic, self.currency, dict(self.meta))

    def to_records(self) -> list[dict]:
        out = self.df.reset_index()
        out["date"] = out["date"].dt.strftime("%Y-%m-%dT%H:%M:%S")
        return jsonable(out.to_dict(orient="records"))


# --------------------------------------------------------------------------- quote
@dataclass
class Quote:
    symbol: str
    price: float
    source: str
    prev_close: float | None = None
    change: float | None = None
    change_pct: float | None = None
    open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    volume: float | None = None
    market_cap: float | None = None
    currency: str | None = None
    name: str | None = None
    timestamp: datetime | None = None
    synthetic: bool = False

    def __post_init__(self) -> None:
        if self.price is None or not np.isfinite(self.price) or self.price <= 0:
            raise DataValidationError(f"invalid quote price {self.price!r}", provider=self.source)
        if self.prev_close and self.change is None:
            self.change = self.price - self.prev_close
        if self.prev_close and self.change_pct is None:
            self.change_pct = (self.price / self.prev_close - 1) * 100

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# --------------------------------------------------------------------------- news
@dataclass
class NewsItem:
    title: str
    source: str                       # provider that served it
    url: str | None = None
    publisher: str | None = None
    summary: str | None = None
    published_at: datetime | None = None
    provider_sentiment: float | None = None   # -1..1 if the provider supplies one
    sentiment: float | None = None            # filled by the sentiment model
    tickers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# --------------------------------------------------------------------------- options
@dataclass
class OptionContract:
    expiry: date
    strike: float
    kind: str                         # "call" | "put"
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    iv: float | None = None           # decimal, e.g. 0.32
    volume: float | None = None
    open_interest: float | None = None
    symbol: str | None = None

    @property
    def mid(self) -> float | None:
        if self.bid and self.ask and self.ask >= self.bid > 0:
            return (self.bid + self.ask) / 2
        return self.last if self.last and self.last > 0 else None


@dataclass
class OptionChain:
    symbol: str
    underlying_price: float
    source: str
    expirations: list[date]
    contracts: list[OptionContract]
    model_generated: bool = False     # True when synthesised from Black-Scholes, not market quotes

    def to_frame(self) -> pd.DataFrame:
        rows = [{**c.__dict__, "mid": c.mid} for c in self.contracts]
        return pd.DataFrame(rows)


# --------------------------------------------------------------------------- fundamentals
@dataclass
class Fundamentals:
    symbol: str
    source: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    description: str | None = None
    market_cap: float | None = None
    pe: float | None = None
    forward_pe: float | None = None
    eps: float | None = None
    beta: float | None = None
    dividend_yield: float | None = None   # decimal
    week52_high: float | None = None
    week52_low: float | None = None
    shares_outstanding: float | None = None
    profit_margin: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    def merge(self, other: "Fundamentals") -> "Fundamentals":
        """Fill our missing fields from another provider's result."""
        for k in self.__dataclass_fields__:
            if k in ("symbol", "source", "extra"):
                continue
            if getattr(self, k) in (None, "") and getattr(other, k) not in (None, ""):
                setattr(self, k, getattr(other, k))
        self.extra = {**other.extra, **self.extra}
        return self


# --------------------------------------------------------------------------- provenance
@dataclass
class Provenance:
    """Where a piece of data came from and what it cost to get it."""

    capability: str
    symbol: str
    provider: str
    latency_ms: float
    cache: str = "miss"               # miss | fresh | stale | coalesced
    age_s: float | None = None
    attempts: list[dict] = field(default_factory=list)   # failed attempts before success
    fetched_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


T = TypeVar("T")


@dataclass
class Fetched(Generic[T]):
    value: T
    provenance: Provenance
