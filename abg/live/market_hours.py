"""NYSE/Nasdaq regular-session calendar (rule-based, no network).

Regular hours 09:30-16:00 America/New_York, Monday-Friday, excluding exchange holidays.
Holidays are computed from their rules (so it works for any year): New Year's Day, MLK Day,
Presidents' Day, Good Friday, Memorial Day, Juneteenth (from 2022), Independence Day, Labor
Day, Thanksgiving, Christmas - with Saturday->Friday / Sunday->Monday observance (NYSE does
not observe a Saturday New Year's Day on the prior Dec 31).  Early 13:00 closes are ignored.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
OPEN, CLOSE = time(9, 30), time(16, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:  # Anonymous Gregorian algorithm
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(d: date, allow_saturday_shift: bool = True) -> date | None:
    if d.weekday() == 5:
        return d - timedelta(days=1) if allow_saturday_shift else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=32)
def holidays(year: int) -> frozenset[date]:
    days = [
        _observed(date(year, 1, 1), allow_saturday_shift=False),
        _nth_weekday(year, 1, 0, 3),                  # MLK Day
        _nth_weekday(year, 2, 0, 3),                  # Presidents' Day
        _easter(year) - timedelta(days=2),            # Good Friday
        _last_weekday(year, 5, 0),                    # Memorial Day
        _observed(date(year, 6, 19)) if year >= 2022 else None,
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),                  # Labor Day
        _nth_weekday(year, 11, 3, 4),                 # Thanksgiving
        _observed(date(year, 12, 25)),
    ]
    return frozenset(d for d in days if d)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def is_market_open(now: datetime | None = None) -> bool:
    now = (now or datetime.now(NY)).astimezone(NY)
    return is_trading_day(now.date()) and OPEN <= now.time() < CLOSE


def next_open(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(NY)).astimezone(NY)
    d = now.date()
    if is_trading_day(d) and now.time() < OPEN:
        return datetime.combine(d, OPEN, NY)
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return datetime.combine(d, OPEN, NY)


def session_status(now: datetime | None = None) -> dict:
    now = (now or datetime.now(NY)).astimezone(NY)
    open_ = is_market_open(now)
    return {"open": open_, "now_et": now.isoformat(timespec="minutes"),
            "next_open_et": None if open_ else next_open(now).isoformat(timespec="minutes"),
            "closes_et": datetime.combine(now.date(), CLOSE, NY).isoformat(timespec="minutes") if open_ else None}
