"""The lab's data: intraday bars, the daily trend history, and how the sessions are split.

* **search window** - everything before the holdout.  Hypotheses are judged here, in four sub-periods.
* **holdout** - the most recent ``holdout_months`` (default 6).  Locked: the lab only scores a hypothesis there
  after it has passed every search-window check, and every look is counted in the ledger (``peek budget``).
  Once the budget is spent the holdout is "used up" until enough new sessions roll in to form a fresh one.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from ...alpha.data import is_rth, load_daily_csv, load_intraday_csv, split_sessions


@dataclass
class Dataset:
    bars: pd.DataFrame
    trend_daily: pd.DataFrame | None
    sessions: list[date]
    search: list[date]
    holdout: list[date]
    blocks: list[list[date]]
    source: str
    fingerprint: str

    @property
    def holdout_start(self) -> str:
        return str(self.holdout[0]) if self.holdout else ""

    def summary(self) -> dict:
        return {"source": self.source, "sessions": len(self.sessions), "first": str(self.sessions[0]),
                "last": str(self.sessions[-1]), "search": [str(self.search[0]), str(self.search[-1]), len(self.search)],
                "holdout": [str(self.holdout[0]), str(self.holdout[-1]), len(self.holdout)] if self.holdout else None,
                "blocks": [[str(b[0]), str(b[-1]), len(b)] for b in self.blocks],
                "trend_history_days": int(len(self.trend_daily)) if self.trend_daily is not None else 0,
                "fingerprint": self.fingerprint}


def _scale_to(bars: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """Bring a proxy daily series (e.g. QQQ) onto the bars' price level.  The trend sign and the ATR-based
    features don't depend on it, but levels shown in reasons read better in MNQ points."""
    closes = bars["close"].groupby(bars.index.date).last()
    j = pd.DataFrame({"b": closes.values}, index=pd.DatetimeIndex(closes.index)).join(daily["close"], how="inner")
    if j.empty:
        return daily
    k = float((j["b"] / j["close"]).median())
    if 0.8 < k < 1.25:
        return daily
    out = daily.copy()
    for c in ("open", "high", "low", "close"):
        if c in out:
            out[c] = out[c] * k
    return out


def load_dataset(bars_csv: str | Path, trend_csv: str | Path | None = None, holdout_months: int = 6,
                 n_blocks: int = 4) -> Dataset:
    bars = load_intraday_csv(bars_csv)
    daily = None
    if trend_csv and Path(trend_csv).exists():
        daily = _scale_to(bars, load_daily_csv(trend_csv))
    sess = sorted(d for d, g in split_sessions(bars).items() if any(is_rth(t) for t in g.index))
    if len(sess) < 120:
        raise ValueError(f"the lab needs at least ~120 sessions (have {len(sess)})")
    cut = pd.Timestamp(sess[-1]) - pd.DateOffset(months=holdout_months)
    search = [d for d in sess if pd.Timestamp(d) <= cut] if holdout_months > 0 else list(sess)
    holdout = [d for d in sess if pd.Timestamp(d) > cut] if holdout_months > 0 else []
    size = -(-len(search) // n_blocks)
    blocks = [search[i:i + size] for i in range(0, len(search), size)]
    h = hashlib.sha1()
    h.update(f"{bars_csv}|{len(bars)}|{bars.index[0]}|{bars.index[-1]}|{float(bars['close'].sum()):.2f}".encode())
    if daily is not None:
        h.update(f"|{len(daily)}|{daily.index[0]}|{float(daily['close'].sum()):.2f}".encode())
    return Dataset(bars, daily, sess, search, holdout, blocks, str(bars_csv), h.hexdigest()[:12])


def default_paths(data_dir: Path) -> tuple[Path, Path]:
    """Where the lab looks by default: the repo's data/ folder first, then the terminal's data dir."""
    for root in (Path("data"), Path(data_dir)):
        b = root / "mnq_proxy_5m.csv"
        if b.exists():
            return b, root / "qqq_daily.csv"
    return Path("data") / "mnq_proxy_5m.csv", Path("data") / "qqq_daily.csv"
