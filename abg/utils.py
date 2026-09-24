"""Small shared helpers: symbol validation, period parsing, JSON sanitising, timing."""
from __future__ import annotations

import dataclasses
import math
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterator

import numpy as np
import pandas as pd

from .errors import InvalidSymbolError

_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,14}$")


def normalize_symbol(symbol: str) -> str:
    """Upper-case and validate a ticker.  Rejects anything that could inject into a URL path."""
    s = (symbol or "").strip().upper()
    if not _SYMBOL_RE.match(s):
        raise InvalidSymbolError(f"Invalid symbol: {symbol!r}")
    return s


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- periods
_PERIOD_RE = re.compile(r"^(\d+)(d|wk|mo|y)$")
PERIOD_CHOICES = ["1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y", "10y", "max"]


def period_to_start(period: str, end: date | None = None) -> date:
    """Convert a Yahoo-style period string ("6mo", "1y", "ytd", "max") to a start date."""
    end = end or utcnow().date()
    p = period.strip().lower()
    if p == "max":
        return date(1970, 1, 1)
    if p == "ytd":
        return date(end.year, 1, 1)
    m = _PERIOD_RE.match(p)
    if not m:
        raise ValueError(f"Unrecognised period {period!r}; use one of {PERIOD_CHOICES} or e.g. '45d'")
    n, unit = int(m.group(1)), m.group(2)
    days = {"d": 1, "wk": 7, "mo": 31, "y": 366}[unit] * n
    return end - timedelta(days=days)


def period_days(period: str) -> int:
    end = utcnow().date()
    return (end - period_to_start(period, end)).days


INTERVALS = {"1d", "1wk", "1mo", "1h", "30m", "15m", "5m", "1m"}


def is_intraday(interval: str) -> bool:
    return interval not in {"1d", "1wk", "1mo"}


# --------------------------------------------------------------------------- numbers
def to_float(x: Any) -> float | None:
    """Lenient float conversion: handles '$1,234.5', '12%', {'raw': 1.2}, None, 'None', '-'."""
    if x is None:
        return None
    if isinstance(x, dict):
        x = x.get("raw")
        if x is None:
            return None
    if isinstance(x, (int, float, np.integer, np.floating)):
        f = float(x)
        return None if math.isnan(f) or math.isinf(f) else f
    s = str(x).strip().replace("$", "").replace(",", "").replace("%", "")
    if s in {"", "-", "None", "null", "N/A", "NaN", "nan"}:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def safe_round(x: Any, nd: int = 4) -> float | None:
    f = to_float(x)
    return None if f is None else round(f, nd)


# --------------------------------------------------------------------------- JSON
def jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses / numpy / pandas / datetimes into strict JSON types.

    NaN and +/-inf become ``None`` so the output is valid JSON (the stdlib would emit NaN).
    """
    if obj is None or isinstance(obj, (bool, str, int)):
        return obj
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return jsonable(float(obj))
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        if hasattr(obj, "to_dict"):
            return jsonable(obj.to_dict())
        return {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [jsonable(v) for v in obj.tolist()]
    if isinstance(obj, pd.Series):
        return [jsonable(v) for v in obj.tolist()]
    if isinstance(obj, pd.DataFrame):
        return [jsonable(r) for r in obj.to_dict(orient="records")]
    return str(obj)


# --------------------------------------------------------------------------- timing
class Timer:
    """Collects named stage durations in milliseconds."""

    def __init__(self) -> None:
        self.stages: dict[str, float] = {}
        self._t0 = time.perf_counter()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        t = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = round((time.perf_counter() - t) * 1000, 2)

    def total_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000, 2)
