"""Local data: CSV files (MacroTrends / Yahoo / Nasdaq / Investing.com / generic, auto-detected)
and a deterministic synthetic generator for offline demos and tests."""
from __future__ import annotations

import io
import re
import zlib
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..errors import DataValidationError, NoDataError
from ..models import Capability, PriceHistory, Quote
from .base import Provider, register_provider

# ---------------------------------------------------------------------------- CSV parsing
_ALIASES = {
    "date": "date", "datetime": "date", "timestamp": "date", "time": "date", "day": "date",
    "open": "open", "high": "high", "low": "low",
    "close": "close", "close/last": "close", "last": "close", "price": "close", "closing_price": "close",
    "adj_close": "adj_close", "adj._close": "adj_close", "adjusted_close": "adj_close", "adjclose": "adj_close",
    "volume": "volume", "vol.": "volume", "vol": "volume",
}
_SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _num(series: pd.Series) -> pd.Series:
    """Parse '$1,234.50', '12.3M', '(1.2)' etc. into floats."""
    if pd.api.types.is_numeric_dtype(series):
        return series.astype("float64")
    s = series.astype(str).str.strip().str.replace(r"[$,\s]", "", regex=True)
    s = s.str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    mult = s.str[-1].str.upper().map(_SUFFIX).fillna(1.0)
    s = s.where(~s.str[-1].str.upper().isin(list(_SUFFIX)), s.str[:-1])
    return pd.to_numeric(s, errors="coerce") * mult


def detect_csv_format(text: str) -> str:
    head = text[:3000].lower()
    if "macrotrends" in head:
        return "macrotrends"
    if "adj close" in head:
        return "yahoo"
    if "close/last" in head:
        return "nasdaq"
    if "vol." in head and "change %" in head:
        return "investing"
    return "generic"


def parse_price_csv(text: str, symbol: str = "CSV", source: str = "csv") -> PriceHistory:
    """Parse an OHLCV CSV in any of the common export formats.

    MacroTrends files start with several disclaimer lines, so we scan forward to the
    first line that looks like a header containing a date column.
    """
    fmt = detect_csv_format(text)
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines[:60]):
        low = line.lower()
        if re.search(r"\b(date|datetime|timestamp)\b", low) and ("close" in low or "price" in low or "last" in low):
            start = i
            break
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), skipinitialspace=True)
    df.columns = [_ALIASES.get(str(c).strip().lower().replace(" ", "_"), str(c).strip().lower()) for c in df.columns]
    if "date" not in df.columns or "close" not in df.columns:
        raise DataValidationError(f"CSV needs date and close columns; found {list(df.columns)}", provider=source)
    df = df.loc[:, ~df.columns.duplicated()]
    df.index = pd.to_datetime(df["date"], errors="coerce", format="mixed")
    df = df.drop(columns=["date"])
    for c in ("open", "high", "low", "close", "adj_close", "volume"):
        if c in df.columns:
            df[c] = _num(df[c])
    ph = PriceHistory.from_frame(symbol, df, source, "1d")
    ph.meta["csv_format"] = fmt
    return ph


@register_provider
class CSVProvider(Provider):
    name = "csv"
    label = "Local CSV files"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTE})
    rate_limit = (1000, 1.0)
    notes = "Reads <ABG_CSV_DIR>/<SYMBOL>*.csv; formats auto-detected."

    def is_configured(self) -> bool:
        d = self.settings.csv_dir
        return bool(d) and Path(d).is_dir()

    def _find(self, symbol: str) -> Path:
        d = Path(self.settings.csv_dir)
        want = symbol.lower()
        cands = sorted(p for p in d.glob("*.csv") if p.stem.lower() == want or p.stem.lower().startswith(want + "_")
                       or p.stem.lower().startswith(want + "-") or p.stem.lower().startswith(want + " "))
        if not cands:
            raise NoDataError(f"no CSV for {symbol} in {d}", provider=self.name)
        return max(cands, key=lambda p: p.stat().st_mtime)

    def _load(self, symbol: str) -> PriceHistory:
        path = self._find(symbol)
        ph = parse_price_csv(path.read_text(encoding="utf-8-sig", errors="replace"), symbol, self.name)
        ph.meta["file"] = str(path)
        return ph

    async def get_history(self, symbol, start, end, interval="1d"):
        ph = self._load(symbol)
        df = ph.df[(ph.df.index >= pd.Timestamp(start)) & (ph.df.index <= pd.Timestamp(end) + pd.Timedelta(days=1))]
        if interval != "1d" and len(df):
            rule = {"1wk": "W-FRI", "1mo": "ME"}.get(interval)
            if rule:
                df = df.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                            "volume": "sum"}).dropna(subset=["close"])
        if df.empty:
            raise NoDataError("no rows in range", provider=self.name)
        return PriceHistory(symbol, df, interval, self.name, meta=ph.meta)

    async def get_quote(self, symbol):
        df = self._load(symbol).df
        prev = float(df["close"].iloc[-2]) if len(df) > 1 else None
        return Quote(symbol=symbol, price=float(df["close"].iloc[-1]), source=self.name, prev_close=prev,
                     open=float(df["open"].iloc[-1]), day_high=float(df["high"].iloc[-1]),
                     day_low=float(df["low"].iloc[-1]), volume=float(df["volume"].iloc[-1]),
                     timestamp=df.index[-1].to_pydatetime().replace(tzinfo=timezone.utc))


# ---------------------------------------------------------------------------- synthetic
def synthetic_frame(symbol: str, start: date, end: date, seed: int | None = None) -> pd.DataFrame:
    """Deterministic geometric-Brownian-motion OHLCV with regime switches and fat tails."""
    seed = zlib.crc32(symbol.encode()) if seed is None else seed
    rng = np.random.default_rng(seed)
    all_days = pd.bdate_range(date(2000, 1, 3), end)           # generate from fixed origin -> stable prices
    n = len(all_days)
    base_vol = 0.18 + (seed % 25) / 100                        # 18%..42% annual
    regimes = np.repeat(rng.choice([0.7, 1.0, 1.6], size=n // 60 + 1, p=[0.3, 0.5, 0.2]), 60)[:n]
    sig = base_vol / np.sqrt(252) * regimes
    shocks = rng.standard_t(df=4, size=n) / np.sqrt(2.0)       # t(4) has variance 2
    rets = 0.08 / 252 - 0.5 * sig ** 2 + sig * shocks
    close = (20 + seed % 180) * np.exp(np.cumsum(rets))
    gap = sig * rng.normal(0, 0.35, n)
    open_ = close * np.exp(-rets + gap)
    rng_hl = np.abs(rng.normal(0, 1, n)) * sig * close * 0.8
    high = np.maximum(open_, close) + rng_hl
    low = np.maximum(np.minimum(open_, close) - rng_hl, 0.01)
    vol = (2e6 + seed % 5e6) * np.exp(rng.normal(0, 0.35, n) + 3 * np.abs(rets))
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol.round()}, index=all_days)
    return df[df.index >= pd.Timestamp(start)]


@register_provider
class SyntheticProvider(Provider):
    name = "synthetic"
    label = "Synthetic (demo)"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTE})
    rate_limit = (1000, 1.0)
    notes = "Simulated prices for offline demos/tests. Enabled only with --demo / ABG_ALLOW_SYNTHETIC=true."

    def is_configured(self) -> bool:
        return bool(self.settings.allow_synthetic)

    async def get_history(self, symbol, start, end, interval="1d"):
        df = synthetic_frame(symbol, start, end)
        if interval in ("1wk", "1mo"):
            df = df.resample({"1wk": "W-FRI", "1mo": "ME"}[interval]).agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        return PriceHistory.from_frame(symbol, df, self.name, interval, synthetic=True, currency="USD")

    async def get_quote(self, symbol):
        today = datetime.now(timezone.utc).date()
        df = synthetic_frame(symbol, date(today.year - 1, 1, 1), today)
        return Quote(symbol=symbol, price=float(df["close"].iloc[-1]), source=self.name,
                     prev_close=float(df["close"].iloc[-2]), open=float(df["open"].iloc[-1]),
                     day_high=float(df["high"].iloc[-1]), day_low=float(df["low"].iloc[-1]),
                     volume=float(df["volume"].iloc[-1]), currency="USD", name=f"{symbol} (synthetic)", synthetic=True)
