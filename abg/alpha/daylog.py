"""What the live bot actually saw each day, so the day can be replayed exactly (docs/14.12, the live-vs-backtest check).

``data/live/<day>/pack.json``  the morning's inputs: context (levels, trend, follow/fade mode, macro events), settings,
                               the adaptive filter's state, the warm-up bars, the feed; plus every restart that day
``data/live/<day>/bars.csv``   every bar the strategy processed, in MNQ points, with its source (stream / quote /
                               delayed) and the price ratio in force

Nothing here posts or decides anything; it only records.  Days older than ``keep_days`` are removed.
"""
from __future__ import annotations

import csv
import json
import shutil
import threading
import time
from datetime import date, datetime
from pathlib import Path

from abg.alpha.data import Bar

FIELDS = ["ts", "open", "high", "low", "close", "volume", "minutes", "source", "ratio", "seen_at"]


def bar_dict(b: Bar) -> dict:
    return {"ts": b.ts.isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close,
            "volume": b.volume, "minutes": b.minutes}


def bar_from(d: dict) -> Bar:
    return Bar(datetime.fromisoformat(str(d["ts"])), float(d["open"]), float(d["high"]), float(d["low"]),
               float(d["close"]), float(d.get("volume") or 0), int(float(d.get("minutes") or 1)))


class DayLog:
    def __init__(self, root: Path, keep_days: int = 120):
        self.root = Path(root)
        self.keep_days = keep_days
        self._lock = threading.Lock()

    def folder(self, day) -> Path:
        return self.root / str(day)

    # ------------------------------------------------------------------ writing (live bot)
    def start(self, day: date, ctx: dict, params: dict, learner: dict | None, warm: list[Bar], **extra) -> None:
        """The first start of the day writes the pack; a restart only adds itself to ``restarts``."""
        f = self.folder(day)
        f.mkdir(parents=True, exist_ok=True)
        p = f / "pack.json"
        with self._lock:
            if p.exists():
                try:
                    pack = json.loads(p.read_text(encoding="utf-8"))
                except ValueError:
                    pack = None
                if pack is not None:
                    pack.setdefault("restarts", []).append({"at": time.time(), "ctx_same": _same_ctx(pack.get("ctx"), ctx),
                                                            **{k: v for k, v in extra.items() if k in ("feed", "ratio")}})
                    _write(p, pack)
                    return
            _write(p, {"day": str(day), "started_at": time.time(), "ctx": ctx, "params": params, "learner": learner,
                       "warm": [bar_dict(b) for b in warm], "restarts": [], **extra})
        self.prune()

    def bar(self, day: date, b: Bar, source: str, ratio: float | None) -> None:
        f = self.folder(day)
        f.mkdir(parents=True, exist_ok=True)
        p = f / "bars.csv"
        with self._lock:
            new = not p.exists()
            with open(p, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                w.writerow({**bar_dict(b), "source": source, "ratio": round(ratio, 5) if ratio else "", "seen_at": round(time.time(), 1)})

    def prune(self) -> None:
        if not self.root.exists():
            return
        days = sorted(d for d in self.root.iterdir() if d.is_dir())
        for d in days[:-self.keep_days] if self.keep_days > 0 else []:
            shutil.rmtree(d, ignore_errors=True)

    # ------------------------------------------------------------------ reading (the check)
    def load(self, day) -> tuple[dict | None, list[dict]]:
        f = self.folder(day)
        try:
            pack = json.loads((f / "pack.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pack = None
        rows = []
        try:
            with open(f / "bars.csv", newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        except OSError:
            pass
        return pack, rows

    def days(self) -> list[str]:
        return sorted(d.name for d in self.root.iterdir() if d.is_dir()) if self.root.exists() else []


def _same_ctx(a: dict | None, b: dict | None) -> bool:
    keys = ("orx_mode", "trend_long", "pdh", "pdl", "pdc", "onh", "onl")
    return bool(a and b) and all(a.get(k) == b.get(k) for k in keys)


def _write(p: Path, obj) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, default=str), encoding="utf-8")
    tmp.replace(p)
