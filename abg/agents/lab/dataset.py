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


# spin-off PATCH (PATCHES.md #1): the fingerprint used the path string exactly as given, so "data\\mnq_proxy_5m.csv"
# (Windows) and "data/mnq_proxy_5m.csv" were different datasets and the 60-trial ledger looked empty on Windows.  Same
# key, separator- and working-directory-independent: inside this repo -> "data/mnq_proxy_5m.csv", the ledger's key.
_REPO = Path(__file__).resolve().parents[2]


def _fp_path(p) -> str:
    try:
        return Path(p).resolve().relative_to(_REPO).as_posix().replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


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
    h.update(f"{_fp_path(bars_csv)}|{len(bars)}|{bars.index[0]}|{bars.index[-1]}|{float(bars['close'].sum()):.2f}".encode())
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


# --------------------------------------------------------------------------- keeping the dataset current
PROXY_RATIO = 41.2456          # QQQ -> MNQ points used to build data/mnq_proxy_5m.csv (docs/14.9)


async def refresh_proxy_csv(engine, bars_csv: str | Path, trend_csv: str | Path | None = None, proxy: str = "QQQ",
                            now=None) -> dict:
    """Append finished sessions to the lab's proxy dataset (QQQ 5-min x the dataset's own ratio) and the daily trend
    file.  Only complete regular sessions after the file's last one are added, so it is safe to run repeatedly.
    The ratio is re-measured on overlapping bars when there are any, so the series stays continuous."""
    from datetime import datetime, time as dtime

    from ...alpha.data import NY, fetch_daily, fetch_intraday
    now = now or datetime.now(NY)
    bars_csv = Path(bars_csv)
    old = pd.read_csv(bars_csv, parse_dates=["datetime"])
    last = old["datetime"].max()
    q = await fetch_intraday(engine, proxy, "5m", "60d")
    if q.empty:
        return {"added_sessions": 0, "note": "no proxy bars returned"}
    q = q[[is_rth(t) for t in q.index]].copy()
    q.index = q.index.tz_convert(NY).tz_localize(None)
    ov = old.set_index("datetime")["close"].reindex(q.index).dropna()
    ratio = float((ov / q.loc[ov.index, "close"]).median()) if len(ov) >= 10 else PROXY_RATIO
    cutoff = now.date() if now.time() < dtime(16, 5) else None          # today's session only once it has closed
    new = q[q.index > last]
    if cutoff is not None:
        new = new[[t.date() < cutoff for t in new.index]]
    days = sorted({t.date() for t in new.index})
    full = [d for d in days if (new.index.date == d).sum() >= 70]       # a full session is 78 five-minute bars
    new = new[[t.date() in set(full) for t in new.index]]
    added = 0
    if len(new):
        out = pd.DataFrame({"datetime": new.index.strftime("%Y-%m-%d %H:%M:%S")})
        for c in ("open", "high", "low", "close"):
            out[c] = ((new[c].values * ratio) / 0.25).round() * 0.25
        out["volume"] = new["volume"].fillna(0).astype(int).values if "volume" in new else 0
        out.to_csv(bars_csv, mode="a", header=False, index=False)
        added = len(full)
    res = {"added_sessions": added, "ratio": round(ratio, 4), "last_session": str(full[-1]) if full else str(last.date())}
    if trend_csv and Path(trend_csv).exists():
        td = pd.read_csv(trend_csv, parse_dates=["datetime"])
        dd = await fetch_daily(engine, proxy, "3mo")
        if not dd.empty:
            lastd = td["datetime"].max()
            dd = dd[[pd.Timestamp(i) > lastd and (cutoff is None or pd.Timestamp(i).date() < cutoff) for i in dd.index]]
            if len(dd):
                o = pd.DataFrame({"datetime": [pd.Timestamp(i).strftime("%Y-%m-%d") for i in dd.index]})
                for c in ("open", "high", "low", "close", "volume"):
                    o[c] = dd[c].values if c in dd else 0
                o.to_csv(trend_csv, mode="a", header=False, index=False)
                res["added_daily"] = len(o)
    return res
