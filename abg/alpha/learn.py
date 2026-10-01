"""Adaptive layer: the bot keeps score of itself and adjusts.

For every (setup, regime) pair - regime is ``trend``, ``range`` or ``news`` at the moment of entry - it
keeps an exponentially weighted average of realised R (recent trades count more; half-life 25 trades)
and shrinks it toward a small prior so a few trades can't swing it.  That number:

* nudges the entry score (+/- up to 12 points), and
* switches a setup off in a regime once it has at least 6 trades there and its shrunk expectancy is
  below -0.15R.  It comes back automatically when results recover (the backtest and paper trades keep
  updating it) - this is how the bot "keeps up" with a market that changes character.

State is a small JSON file, so live trading, paper results and backtests can all feed the same book.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path


class AdaptiveBook:
    def __init__(self, prior_r: float = 0.05, prior_n: float = 8.0, halflife: float = 25.0,
                 min_n: int = 6, off_below: float = -0.15):
        self.prior_r, self.prior_n, self.halflife = prior_r, prior_n, halflife
        self.min_n, self.off_below = min_n, off_below
        self.stats: dict[str, dict] = {}

    @staticmethod
    def key(setup: str, regime: str) -> str:
        return f"{setup}|{regime}"

    def record(self, setup: str, regime: str, r: float, ts=None) -> None:
        if r is None or not math.isfinite(r):
            return
        k = self.key(setup, regime)
        st = self.stats.setdefault(k, {"n": 0, "w": 0.0, "ew": 0.0, "sum": 0.0, "wins": 0, "last": None})
        decay = 0.5 ** (1 / self.halflife)
        st["w"] = st["w"] * decay + 1.0
        st["ew"] = st["ew"] * decay + max(-3.0, min(5.0, r))
        st["n"] += 1
        st["sum"] += r
        st["wins"] += int(r > 0.05)
        st["last"] = str(ts) if ts is not None else time.strftime("%Y-%m-%d")

    def shrunk(self, setup: str, regime: str) -> tuple[float, int]:
        st = self.stats.get(self.key(setup, regime))
        if not st:
            return self.prior_r, 0
        return (st["ew"] + self.prior_r * self.prior_n) / (st["w"] + self.prior_n), st["n"]

    def allowed(self, setup: str, regime: str) -> bool:
        m, n = self.shrunk(setup, regime)
        return n < self.min_n or m >= self.off_below

    def adjust(self, setup: str, regime: str) -> tuple[float, str | None]:
        m, n = self.shrunk(setup, regime)
        if n < 3:
            return 0.0, None
        adj = max(-12.0, min(12.0, m * 25))
        note = (f"{setup} in {regime} conditions: {m:+.2f}R expectancy over {n} recent trades"
                if abs(adj) >= 3 else None)
        return adj, note

    def summary(self) -> list[dict]:
        out = []
        for k, st in sorted(self.stats.items()):
            setup, regime = k.split("|")
            m, n = self.shrunk(setup, regime)
            out.append({"setup": setup, "regime": regime, "n": n, "win_rate": st["wins"] / n if n else None,
                        "avg_r": st["sum"] / n if n else None, "expectancy": round(m, 3),
                        "active": self.allowed(setup, regime), "last": st["last"]})
        return out

    def disabled(self) -> list[str]:
        return [f"{s['setup']} in {s['regime']}" for s in self.summary() if not s["active"]]

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        return {"version": 1, "prior_r": self.prior_r, "prior_n": self.prior_n, "halflife": self.halflife,
                "min_n": self.min_n, "off_below": self.off_below, "stats": self.stats}

    @classmethod
    def from_dict(cls, d: dict) -> "AdaptiveBook":
        b = cls(d.get("prior_r", 0.05), d.get("prior_n", 8.0), d.get("halflife", 25.0), d.get("min_n", 6),
                d.get("off_below", -0.15))
        b.stats = d.get("stats") or {}
        return b

    @classmethod
    def load(cls, path: Path) -> "AdaptiveBook":
        try:
            return cls.from_dict(json.loads(Path(path).read_text()))
        except Exception:
            return cls()

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=1))
        tmp.replace(path)
