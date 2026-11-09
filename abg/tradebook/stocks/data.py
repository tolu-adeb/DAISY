"""Daily equity/ETF data loading.

Expected layout: one CSV per ticker in data_dir, named <TICKER>.csv, with columns
date, open, high, low, close, volume (prices split- and dividend-adjusted).
Optional files:
  membership.csv  ticker,start,end   point-in-time universe (end blank = still a member)
  sectors.csv     ticker,sector
  sentiment.csv   date,ticker,score  daily news score in [-1, 1] known before the next open
"""
from __future__ import annotations

import glob
import os
import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Panel:
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    eligible: pd.DataFrame          # bool, point-in-time membership
    sectors: dict
    sentiment: pd.DataFrame | None  # date x ticker

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index

    @property
    def tickers(self) -> list[str]:
        return list(self.close.columns)


def _read_one(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    df = df.drop_duplicates("date").set_index("date").sort_index()
    need = ["open", "high", "low", "close", "volume"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing columns {missing}")
    return df[need].astype(float)


def load_panel(data_dir: str, benchmark: str, membership_file: str | None = None,
               sector_file: str | None = None, sentiment_file: str | None = None,
               tickers: list[str] | None = None) -> Panel:
    files = sorted(glob.glob(os.path.join(data_dir, "*.csv")))
    skip = {"membership", "sectors", "sentiment"}
    frames = {}
    for p in files:
        t = os.path.splitext(os.path.basename(p))[0]
        if t.lower() in skip:
            continue
        if tickers is not None and t not in tickers and t != benchmark:
            continue
        frames[t] = _read_one(p)
    if benchmark not in frames:
        raise FileNotFoundError(f"benchmark {benchmark}.csv not found in {data_dir}")
    cal = frames[benchmark].index
    fields = {}
    for f in ["open", "high", "low", "close", "volume"]:
        fields[f] = pd.DataFrame({t: df[f].reindex(cal) for t, df in frames.items()}, index=cal)
    close = fields["close"]

    if membership_file and os.path.exists(membership_file):
        m = pd.read_csv(membership_file)
        elig = pd.DataFrame(False, index=cal, columns=close.columns)
        for _, row in m.iterrows():
            t = row["ticker"]
            if t not in elig.columns:
                continue
            start = pd.to_datetime(row["start"])
            end = pd.to_datetime(row["end"]) if isinstance(row.get("end"), str) and row["end"] else cal[-1]
            elig.loc[(cal >= start) & (cal <= end), t] = True
    else:
        warnings.warn("No membership file: every ticker with data is eligible on every date. "
                      "If the ticker list is today's index members, results carry survivorship bias.")
        elig = close.notna()
    elig = elig & close.notna()

    sectors = {}
    if sector_file and os.path.exists(sector_file):
        s = pd.read_csv(sector_file)
        sectors = dict(zip(s["ticker"], s["sector"]))

    sent = None
    if sentiment_file and os.path.exists(sentiment_file):
        s = pd.read_csv(sentiment_file, parse_dates=["date"])
        sent = s.pivot_table(index="date", columns="ticker", values="score", aggfunc="mean").reindex(cal)

    return Panel(fields["open"], fields["high"], fields["low"], close, fields["volume"], elig, sectors, sent)


def last_trading_day_of_month(dates: pd.DatetimeIndex) -> np.ndarray:
    s = pd.Series(dates, index=dates)
    month = s.dt.to_period("M")
    return (month != month.shift(-1)).to_numpy()
