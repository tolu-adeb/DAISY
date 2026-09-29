"""Economic-event and earnings calendar.

Macro events (US, times in New York):
  * FOMC rate decisions 2026-2027: the Fed's published schedule (decision 14:00 ET on the 2nd day;
    SEP = Summary of Economic Projections meeting).
  * CPI and Employment Situation (jobs report) releases for 2026: the BLS published schedule (08:30).
  * Later months the BLS hasn't published yet are **estimated** (jobs report: first Friday; CPI:
    around the 12th, moved off weekends) and flagged as estimates.
  * Your own events: ``ABG_DATA_DIR/calendar.json`` - a list of {"date": "2027-01-13", "time": "08:30",
    "name": "CPI", "impact": "high"} objects, which override estimates for the same name and month.

Earnings: Finnhub ``/calendar/earnings`` per symbol (free tier), cached for 12 h; a date from the
signal post always wins over the lookup.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..errors import ABGError

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

FOMC = [  # (first day, decision day, SEP)
    ("2026-01-27", "2026-01-28", False), ("2026-03-17", "2026-03-18", True), ("2026-04-28", "2026-04-29", False),
    ("2026-06-16", "2026-06-17", True), ("2026-07-28", "2026-07-29", False), ("2026-09-15", "2026-09-16", True),
    ("2026-10-27", "2026-10-28", False), ("2026-12-08", "2026-12-09", True),
    ("2027-01-26", "2027-01-27", False), ("2027-03-16", "2027-03-17", True), ("2027-04-27", "2027-04-28", False),
    ("2027-06-08", "2027-06-09", True), ("2027-07-27", "2027-07-28", False), ("2027-09-14", "2027-09-15", True),
    ("2027-10-26", "2027-10-27", False), ("2027-12-07", "2027-12-08", True),
]
CPI_2026 = ["2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10", "2026-07-14",
            "2026-08-12", "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10"]
JOBS_2026 = ["2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03", "2026-05-08", "2026-06-05", "2026-07-02",
             "2026-08-07", "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04"]


@dataclass
class Event:
    date: str                   # ISO date (New York)
    time: str | None            # "08:30" ET, None = unknown / all day
    name: str
    impact: str = "high"        # high | medium
    kind: str = "macro"         # macro | earnings
    symbol: str | None = None
    estimated: bool = False
    detail: str = ""

    @property
    def dt(self) -> datetime:
        hh, mm = (self.time or "09:30").split(":")
        d = date.fromisoformat(self.date)
        return datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=NY)

    def to_dict(self) -> dict:
        return {**asdict(self), "ts": self.dt.timestamp()}


def _first_friday(y: int, m: int) -> date:
    from ..live.market_hours import is_trading_day
    d = date(y, m, 1)
    d += timedelta(days=(4 - d.weekday()) % 7)
    return d if is_trading_day(d) else d + timedelta(days=7)          # e.g. Jan 1 -> the next Friday


def _cpi_estimate(y: int, m: int) -> date:
    from ..live.market_hours import is_trading_day
    d = date(y, m, 12)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def builtin_events(start: date, end: date, user_file: Path | None = None) -> list[Event]:
    ev: list[Event] = []
    for d1, d2, sep in FOMC:
        ev.append(Event(d2, "14:00", "FOMC rate decision", "high", detail=("with projections + " if sep else "")
                        + "press conference 14:30 ET"))
    known_cpi = {d[:7] for d in CPI_2026}
    known_jobs = {d[:7] for d in JOBS_2026}
    ev += [Event(d, "08:30", "CPI (inflation)") for d in CPI_2026]
    ev += [Event(d, "08:30", "Jobs report (nonfarm payrolls)") for d in JOBS_2026]
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        key = f"{y:04d}-{m:02d}"
        if key not in known_cpi and y >= 2027:
            ev.append(Event(_cpi_estimate(y, m).isoformat(), "08:30", "CPI (inflation)", estimated=True))
        if key not in known_jobs and y >= 2027:
            ev.append(Event(_first_friday(y, m).isoformat(), "08:30", "Jobs report (nonfarm payrolls)", estimated=True))
        m = m + 1 if m < 12 else 1
        y = y + (m == 1)
    if user_file and user_file.exists():
        try:
            mine = [Event(**{k: v for k, v in e.items() if k in Event.__dataclass_fields__})
                    for e in json.loads(user_file.read_text())]
            names = {(e.name, e.date[:7]) for e in mine}
            ev = [e for e in ev if not (e.estimated and (e.name, e.date[:7]) in names)] + mine
        except Exception as e:  # a broken user file must not break tracking
            log.warning("calendar.json ignored: %s", e)
    return sorted([e for e in ev if start.isoformat() <= e.date <= end.isoformat()], key=lambda e: e.dt)


class Calendar:
    def __init__(self, engine=None, settings=None):
        self.engine = engine
        self.s = settings or (engine.settings if engine else None)
        self._earn: dict[str, tuple[float, dict | None]] = {}

    @property
    def user_file(self) -> Path | None:
        return Path(self.s.data_dir).expanduser() / "calendar.json" if self.s else None

    def macro(self, days: int = 14, start: date | None = None) -> list[Event]:
        start = start or datetime.now(NY).date()
        return builtin_events(start, start + timedelta(days=days), self.user_file)

    def blackout(self, now: datetime | None = None, before_min: int = 30, after_min: int = 15) -> Event | None:
        """The high-impact macro event whose blackout window contains ``now``, if any."""
        now = (now or datetime.now(NY)).astimezone(NY)
        for e in builtin_events(now.date() - timedelta(days=1), now.date() + timedelta(days=1), self.user_file):
            if e.impact != "high" or not e.time:
                continue
            if e.dt - timedelta(minutes=before_min) <= now <= e.dt + timedelta(minutes=after_min):
                return e
        return None

    async def earnings(self, symbol: str, max_age: float = 43_200) -> dict | None:
        """Next earnings {date, hour (bmo/amc), eps_estimate, revenue_estimate} via Finnhub, or None."""
        hit = self._earn.get(symbol)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
        res = None
        key = getattr(self.s, "finnhub_api_key", None) if self.s else None
        if key and self.engine is not None and "=" not in symbol and "^" not in symbol and "-USD" not in symbol:
            today = datetime.now(NY).date()
            try:
                data = await self.engine.http.get_json(
                    "https://finnhub.io/api/v1/calendar/earnings", provider="finnhub",
                    params={"from": today.isoformat(), "to": (today + timedelta(days=120)).isoformat(),
                            "symbol": symbol, "token": key})
                rows = sorted((r for r in (data or {}).get("earningsCalendar") or [] if r.get("date")),
                              key=lambda r: r["date"])
                if rows:
                    r = rows[0]
                    res = {"date": r["date"], "hour": r.get("hour") or None, "eps_estimate": r.get("epsEstimate"),
                           "revenue_estimate": r.get("revenueEstimate"), "source": "finnhub"}
            except ABGError as e:
                log.info("earnings lookup for %s failed: %s", symbol, e.message)
        self._earn[symbol] = (time.time(), res)
        return res

    async def upcoming(self, symbols: list[str], days: int = 14) -> list[Event]:
        """Macro events plus earnings for ``symbols`` in the next ``days`` days."""
        out = self.macro(days)
        end = (datetime.now(NY).date() + timedelta(days=days)).isoformat()
        for s in symbols:
            e = await self.earnings(s)
            if e and e["date"] <= end:
                out.append(Event(e["date"], {"bmo": "08:00", "amc": "16:05"}.get(e.get("hour") or "", None),
                                 f"{s} earnings", "high", "earnings", s,
                                 detail={"bmo": "before the open", "amc": "after the close"}.get(e.get("hour") or "", "")))
        return sorted(out, key=lambda e: e.dt)
