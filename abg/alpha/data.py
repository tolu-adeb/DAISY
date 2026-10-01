"""Bars for the MNQ alpha bot: CSV loaders (daily and intraday), roll handling, ET sessions.

Daily CSVs from investing.com look like ``"Date","Price","Open","High","Low","Vol.","Change %"`` with
thousands separators and ``2.17M`` volumes; generic ``date,open,high,low,close,volume`` files work too.

Continuous futures series from free sites splice contracts badly: in the week before a quarterly
expiry the site often keeps quoting the *expiring* contract while volume has already moved to the next
one, then jumps to the new contract (which trades at a carry premium, ~0.8% for NQ in a quarter).
``back_adjust`` finds those stale, low-volume expiry-week rows, drops them, and shifts everything before
the roll up by the carry offset so the series is continuous in front-month terms.

Intraday CSVs: TradingView exports (``time,open,high,low,close,Volume`` with unix seconds or ISO
times), NinjaTrader (``yyyyMMdd HHmmss;o;h;l;c;v``) and generic ``datetime,open,high,low,close,volume``.
All intraday data is converted to America/New_York (tz-aware).
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")
RTH_OPEN, RTH_CLOSE = time(9, 30), time(16, 0)
COLS = ["open", "high", "low", "close", "volume"]


def parse_number(x) -> float:
    if x is None:
        return float("nan")
    if isinstance(x, (int, float, np.floating)):
        return float(x)
    s = str(x).strip().replace(",", "").replace("$", "").replace("%", "")
    if not s or s in {"-", "--"}:
        return float("nan")
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(s[-1].upper(), 1.0)
    if mult != 1.0:
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return float("nan")


def _rename(cols) -> dict:
    out = {}
    for c in cols:
        k = str(c).strip().lower().replace(".", "").replace(" ", "_")
        if k in ("price", "last", "close", "settle", "close_last"):
            out[c] = "close"
        elif k in ("open", "high", "low"):
            out[c] = k
        elif k in ("vol", "volume"):
            out[c] = "volume"
        elif k in ("date", "time", "datetime", "timestamp", "date_time"):
            out[c] = "ts"
    return out


# --------------------------------------------------------------------------- daily
def load_daily_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)
    df = df.rename(columns=_rename(df.columns))
    if "ts" not in df.columns or "close" not in df.columns:
        raise ValueError(f"{path}: need a date column and a close/price column (got {list(df.columns)})")
    out = pd.DataFrame({"date": pd.to_datetime(df["ts"], errors="coerce")})
    for c in COLS:
        out[c] = df[c].map(parse_number) if c in df.columns else np.nan
    out = out.dropna(subset=["date", "close"]).sort_values("date")
    out["date"] = out["date"].dt.normalize()
    out = out.drop_duplicates("date", keep="last").set_index("date")
    for c in ("open", "high", "low"):
        out[c] = out[c].fillna(out["close"])
    return out


def third_friday(y: int, m: int) -> date:
    d = date(y, m, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


@dataclass
class RollInfo:
    expiry: str
    stale: list[str]
    offset: float
    method: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def detect_stale_rows(df: pd.DataFrame, vol_ratio: float = 0.6) -> list[list[pd.Timestamp]]:
    """Groups of low-volume rows in a quarterly expiry week (Mon..third Friday)."""
    if "volume" not in df.columns or df["volume"].isna().all():
        return []
    med = df["volume"].rolling(20, min_periods=5).median().shift(1)
    groups: dict[date, list[pd.Timestamp]] = {}
    for ts, v in df["volume"].items():
        d = ts.date()
        if d.month not in (3, 6, 9, 12):
            continue
        tf = third_friday(d.year, d.month)
        if not (tf - timedelta(days=6) <= d <= tf):
            continue
        m = med.get(ts)
        if m and np.isfinite(m) and np.isfinite(v) and v < vol_ratio * m:
            groups.setdefault(tf, []).append(ts)
    return [g for _, g in sorted(groups.items())]


def back_adjust(df: pd.DataFrame, carry_annual: float = 0.033, offset: float | None = None,
                vol_ratio: float = 0.6) -> tuple[pd.DataFrame, list[RollInfo]]:
    """Drop stale expiry-week rows and add the roll offset to everything before them (see module doc)."""
    df = df.copy()
    rolls: list[RollInfo] = []
    for grp in detect_stale_rows(df, vol_ratio):
        last = grp[-1]
        after = df.index[df.index > last]
        if len(after) == 0:          # roll not complete yet in this file
            continue
        px = float(df.loc[last, "close"])
        off = offset if offset is not None else round(px * carry_annual * 91 / 365 / 0.25) * 0.25
        method = "manual" if offset is not None else f"carry {carry_annual:.1%}/yr x 91d"
        df.loc[df.index <= last, ["open", "high", "low", "close"]] += off
        df = df.drop(index=grp)
        rolls.append(RollInfo(str(third_friday(last.year, last.month)), [str(t.date()) for t in grp], off, method))
    return df, rolls


# --------------------------------------------------------------------------- intraday
_NINJA = re.compile(r"^\d{8} \d{6}$")


def _parse_ts(s: pd.Series, tz: str | None) -> pd.DatetimeIndex:
    s = s.astype(str).str.strip()
    if s.str.fullmatch(r"\d{9,11}").all():                       # unix seconds
        idx = pd.to_datetime(s.astype("int64"), unit="s", utc=True)
    elif s.str.fullmatch(r"\d{12,14}").all():                    # unix ms
        idx = pd.to_datetime(s.astype("int64"), unit="ms", utc=True)
    elif s.map(lambda x: bool(_NINJA.match(x))).all():
        idx = pd.to_datetime(s, format="%Y%m%d %H%M%S")
    else:
        idx = pd.to_datetime(s, errors="coerce", utc=False, format="mixed")
    idx = pd.DatetimeIndex(idx)
    if idx.tz is None:
        idx = idx.tz_localize(tz or NY, ambiguous="NaT", nonexistent="shift_forward")
    return idx.tz_convert(NY)


def load_intraday_csv(path: str | Path, tz: str | None = None) -> pd.DataFrame:
    """Intraday bars in ET.  ``tz`` is the timezone of naive timestamps in the file (default ET)."""
    text = Path(path).read_text(encoding="utf-8-sig")
    first = text.splitlines()[0] if text else ""
    if ";" in first and "," not in first:                        # NinjaTrader export, no header
        rows = list(csv.reader(io.StringIO(text), delimiter=";"))
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"][: len(rows[0])])
    else:
        df = pd.read_csv(io.StringIO(text), dtype=str)
        if "date" in [c.lower() for c in df.columns] and "time" in [c.lower() for c in df.columns]:
            dc = next(c for c in df.columns if c.lower() == "date")
            tc = next(c for c in df.columns if c.lower() == "time")
            df["ts"] = df[dc] + " " + df[tc]
            df = df.drop(columns=[dc, tc])
        df = df.rename(columns=_rename(df.columns))
    if "ts" not in df.columns:
        raise ValueError(f"{path}: no time column")
    idx = _parse_ts(df["ts"], tz)
    out = pd.DataFrame({c: (df[c].map(parse_number).to_numpy() if c in df.columns else np.nan) for c in COLS}, index=idx)
    out = out[~out.index.isna()].dropna(subset=["close"]).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    for c in ("open", "high", "low"):
        out[c] = out[c].fillna(out["close"])
    out["volume"] = out["volume"].fillna(0.0)
    return out


def utc_naive_to_et(df: pd.DataFrame) -> pd.DataFrame:
    """Engine histories are naive UTC; convert to tz-aware ET."""
    df = df.copy()
    idx = pd.DatetimeIndex(df.index)
    df.index = (idx.tz_localize("UTC") if idx.tz is None else idx).tz_convert(NY)
    return df[[c for c in COLS if c in df.columns]]


async def fetch_intraday(engine, symbol: str = "MNQ=F", interval: str = "5m", period: str = "60d") -> pd.DataFrame:
    res = await engine.history(symbol, period=period, interval=interval, use_cache=False)
    return utc_naive_to_et(getattr(res, "value", res).df)


def session_date(ts: pd.Timestamp) -> date:
    """CME equity index session: 18:00 ET belongs to the next trading day."""
    d = ts.date()
    if ts.time() >= time(18, 0):
        d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d = d + timedelta(days=1)
    return d


def split_sessions(bars: pd.DataFrame) -> dict[date, pd.DataFrame]:
    if bars.empty:
        return {}
    keys = pd.Index([session_date(t) for t in bars.index])
    return {k: g for k, g in bars.groupby(keys)}


def is_rth(ts: pd.Timestamp) -> bool:
    return RTH_OPEN <= ts.time() < RTH_CLOSE


def rth_daily(bars: pd.DataFrame) -> pd.DataFrame:
    """Daily OHLC built from the RTH part of intraday bars (index: session date)."""
    rows = []
    for d, g in split_sessions(bars).items():
        r = g[[is_rth(t) for t in g.index]]
        if r.empty:
            continue
        rows.append({"date": pd.Timestamp(d), "open": r["open"].iloc[0], "high": r["high"].max(),
                     "low": r["low"].min(), "close": r["close"].iloc[-1], "volume": r["volume"].sum()})
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame(columns=COLS)


def bar_minutes(bars: pd.DataFrame) -> int:
    if len(bars) < 3:
        return 1
    d = pd.Series(bars.index[1:] - bars.index[:-1]).dt.total_seconds() / 60
    return max(1, int(round(float(d[d > 0].median()))))


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    minutes: int = 1
    extra: dict = field(default_factory=dict)

    @property
    def end(self) -> datetime:
        return self.ts + timedelta(minutes=self.minutes)


def iter_bars(df: pd.DataFrame, minutes: int | None = None):
    m = minutes or bar_minutes(df)
    for ts, r in zip(df.index, df.itertuples(index=False)):
        yield Bar(ts.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close),
                  float(getattr(r, "volume", 0.0) or 0.0), m)
