"""When the monitor was actually running, and keeping Windows awake for the session.

``data/uptime.json`` holds spans of continuous running ([start, end] epoch seconds; a gap of more than 3 minutes
between heartbeats starts a new span).  ``coverage(day)`` turns them into what matters for trading: did the bot see the
9:30 open, and which minutes of the 9:25-16:00 window did it miss.  The live-vs-backtest check uses it to say "the bot
wasn't running at 10:15" instead of guessing.

``KeepAwake`` asks Windows not to sleep while the monitor runs during the session (08:55-16:35 ET on trading days).
It can't stop a closed lid from sleeping the laptop - that's the power setting "When I close the lid: Do nothing".
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
GAP_S = 180
KEEP_DAYS = 90
WINDOW = (dtime(9, 25), dtime(16, 0))


class Uptime:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._spans: list[list[float]] | None = None

    def spans(self) -> list[list[float]]:
        if self._spans is None:
            try:
                self._spans = json.loads(self.path.read_text(encoding="utf-8")).get("spans") or []
            except (OSError, ValueError):
                self._spans = []
        return self._spans

    def beat(self, now: float | None = None, started: float | None = None) -> None:
        now = now or time.time()
        sp = self.spans()
        if sp and now - sp[-1][1] <= GAP_S:
            sp[-1][1] = now
        else:
            sp.append([started if started and now - started < GAP_S else now, now])
        cut = now - KEEP_DAYS * 86400
        self._spans = [s for s in sp if s[1] >= cut]
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"spans": [[round(a, 1), round(b, 1)] for a, b in self._spans]}), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def running_at(self, ts: float) -> bool:
        return any(a - 60 <= ts <= b + 60 for a, b in self.spans())

    def coverage(self, day: date) -> dict:
        """Minutes of the 9:25-16:00 ET window covered, the first minute seen and the gaps (ET clock times)."""
        lo = datetime.combine(day, WINDOW[0], NY).timestamp()
        hi = min(datetime.combine(day, WINDOW[1], NY).timestamp(), max(lo + 60, time.time()))    # today: so far
        cov, gaps, first = 0.0, [], None
        cur = lo
        for a, b in sorted(self.spans()):
            a, b = max(a, lo), min(b, hi)
            if b <= a:
                continue
            first = a if first is None else first
            if a - cur > 120:
                gaps.append([_hm(cur), _hm(a)])
            cov += b - a
            cur = max(cur, b)
        if hi - cur > 120:
            gaps.append([_hm(cur), _hm(hi)])
        total = hi - lo
        return {"day": str(day), "pct": round(100 * cov / total, 1), "first_seen": _hm(first) if first else None,
                "so_far": hi < datetime.combine(day, WINDOW[1], NY).timestamp(),
                "saw_open": bool(first is not None and first <= datetime.combine(day, dtime(9, 30), NY).timestamp()),
                "gaps": gaps}

    def since(self) -> str | None:
        """When the record starts (ET, 'YYYY-MM-DD HH:MM'): days before it are unknown, not missed."""
        sp = self.spans()
        return datetime.fromtimestamp(min(a for a, _ in sp), NY).strftime("%Y-%m-%d %H:%M") if sp else None

    def days(self, n: int = 10) -> list[dict]:
        from abg.live.market_hours import is_trading_day
        out, d = [], datetime.now(NY).date()
        while len(out) < n:
            if is_trading_day(d) and (d < datetime.now(NY).date() or datetime.now(NY).time() >= WINDOW[0]):
                out.append(self.coverage(d))
            d -= timedelta(days=1)
            if (datetime.now(NY).date() - d).days > 40:
                break
        return out


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, NY).strftime("%H:%M")


class KeepAwake:
    """Windows only: SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) during the session, cleared after."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and os.name == "nt"
        self.active = False

    @staticmethod
    def wanted(now: datetime) -> bool:
        from abg.live.market_hours import is_trading_day
        return is_trading_day(now.date()) and dtime(8, 55) <= now.time() < dtime(16, 35)

    def update(self, now: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        want = self.wanted(now or datetime.now(NY))
        if want != self.active:
            try:
                import ctypes
                flags = self.ES_CONTINUOUS | (self.ES_SYSTEM_REQUIRED if want else 0)
                ctypes.windll.kernel32.SetThreadExecutionState(flags)          # type: ignore[attr-defined]
                self.active = want
            except Exception:
                self.enabled = False
        return self.active


def autostart_registered() -> bool | None:
    """Windows: is the Task Scheduler task from deploy\\windows-autostart.ps1 there?  None elsewhere / unknown."""
    if os.name != "nt":
        return None
    try:
        import subprocess
        r = subprocess.run(["schtasks", "/query", "/tn", "ABG Monitor"], capture_output=True, text=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return None
