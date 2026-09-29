"""Timed jobs inside the monitor (no cron needed): recaps, calendar posts, backups, health pings.

    Friday 16:15 ET     weekly performance recap -> Discord relay + dashboard feed
    Sunday 18:00 ET     week ahead: macro events + earnings for tracked symbols
    weekdays 07:30 ET   today's high-impact macro releases (only when there are any)
    daily ABG_BACKUP_HOUR   SQLite backups (portfolio + signals), keeping ABG_BACKUP_KEEP days
    every 5 min         dead-man's-switch ping to ABG_HEALTHCHECK_URL (/fail when degraded)
    every minute        watchdog: alert (max hourly) when quotes stop updating or the price stream is down

Each job records its last run in the signals database, so restarts never double-post.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .market_hours import is_market_open

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")


class Scheduler:
    def __init__(self, monitor):
        self.m = monitor
        self.s = monitor.s
        self.tr = monitor.ext
        self._stop = asyncio.Event()
        self.last: dict[str, float] = {}
        self.errors: list[str] = []

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {"jobs": {k: v for k, v in self.last.items()}, "errors": self.errors[-3:]}

    def _once(self, key: str) -> bool:
        """True the first time ``key`` is seen (persisted)."""
        store = self.tr.store
        if store.get_kv(f"job:{key}"):
            return False
        store.set_kv(f"job:{key}", str(time.time()))
        return True

    async def run(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(NY)
            for name, due, fn in self._jobs(now):
                if due:
                    try:
                        await fn()
                        self.last[name] = time.time()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # a failing job must not stop the others
                        log.exception("job %s failed", name)
                        self.errors.append(f"{name}: {type(e).__name__}: {e}"[:200])
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    def _jobs(self, now: datetime):
        s, d = self.s, now.date().isoformat()
        wk = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
        yield ("weekly_recap", s.ext_weekly_recap and now.weekday() == 4 and now.hour * 60 + now.minute >= 975
               and self._once(f"recap:{wk}"), self.weekly_recap)
        yield ("week_ahead", s.ext_week_ahead and now.weekday() == 6 and now.hour >= 18 and self._once(f"ahead:{wk}"),
               self.week_ahead)
        yield ("today_macro", now.weekday() < 5 and now.hour * 60 + now.minute >= 450 and now.hour < 16
               and self._once(f"today:{d}"), self.today_macro)
        yield ("backup", s.backup_hour >= 0 and now.hour == s.backup_hour and self._once(f"backup:{d}"), self.backup)
        yield ("healthcheck", bool(s.healthcheck_url) and time.time() - self.last.get("healthcheck", 0) >= 290,
               self.healthcheck)
        yield ("watchdog", time.time() - self.last.get("watchdog", 0) >= 60, self.watchdog)

    # ------------------------------------------------------------------ jobs
    async def weekly_recap(self) -> None:
        tr = self.tr
        since = time.time() - 7 * 86400
        ideas = tr.store.ideas(limit=100_000)
        closed = [i for i in ideas if i.status == "closed" and (i.closed_at or 0) >= since and i.entry_price is not None]
        new = [i for i in ideas if i.created_at >= since and i.status != "rejected"]
        rs = [i.realized_r for i in closed]
        wins = [r for r in rs if r > 0.05]
        eq = tr.gate.equity()
        lines = [f"{len(new)} new ideas · {len(closed)} closed · win rate "
                 f"{(len(wins) / len(closed)) if closed else 0:.0%} · {sum(rs):+.2f}R · paper P&L "
                 f"{sum(i.realized_pnl for i in closed):+,.2f}"]
        if closed:
            best = max(closed, key=lambda i: i.realized_r)
            worst = min(closed, key=lambda i: i.realized_r)
            lines.append(f"Best: {best.symbol} #{best.id} {best.realized_r:+.2f}R · worst: {worst.symbol} #{worst.id} "
                         f"{worst.realized_r:+.2f}R")
        by: dict[str, list[float]] = {}
        for i in closed:
            by.setdefault(i.author or i.channel_name or i.source, []).append(i.realized_r)
            by.setdefault("type: " + (((i.meta or {}).get("class") or {}).get("pattern") or "n/a"), []).append(i.realized_r)
        lines += [f"{k}: {len(v)} trades, {sum(v):+.2f}R" for k, v in sorted(by.items())[:12]]
        lines.append(f"Open now: {eq['open']} · risk to stops {eq['heat']:,.2f} ({eq['heat_pct']:.1f}%) · "
                     f"equity {eq['equity']:,.2f}")
        await tr.post_portfolio("📊 Weekly recap", lines, "info", key="recap")

    async def week_ahead(self) -> None:
        evs = await self.tr.calendar.upcoming(self.tr.symbols(), days=7)
        if not evs:
            return
        lines = [f"{e.dt:%a %b %d} {e.time or ''} ET — {e.name}" + (" (est.)" if e.estimated else "")
                 + (f" · {e.detail}" if e.detail else "") for e in evs[:20]]
        lines.append(f"Entries pause {self.s.ext_event_blackout_before_min} min before / "
                     f"{self.s.ext_event_blackout_after_min} min after each macro release.")
        await self.tr.post_portfolio("🗓️ Week ahead", lines, "info", key="week_ahead")

    async def today_macro(self) -> None:
        today = datetime.now(NY).date()
        evs = [e for e in self.tr.calendar.macro(0, today) if e.impact == "high"]
        if evs:
            await self.tr.post_portfolio("📅 Today's market-moving releases",
                                         [f"{e.time} ET — {e.name}" + (f" ({e.detail})" if e.detail else "") for e in evs],
                                         "info", key="today_macro")

    async def backup(self) -> None:
        from ..ops.backup import backup_databases
        await asyncio.to_thread(backup_databases, self.s.data_dir, self.s.backup_keep)

    async def healthcheck(self) -> None:
        url = self.s.healthcheck_url.rstrip("/")
        bad = self._degraded()
        try:
            await self.m.engine.http.request("GET", url + ("/fail" if bad else ""), provider="healthcheck", timeout=10)
        except Exception as e:
            self.errors.append(f"healthcheck: {e}"[:200])

    def _degraded(self) -> str | None:
        m = self.m
        if m.last_quote_sweep and time.time() - m.last_quote_sweep > max(900, 4 * self.s.monitor_quote_interval) \
                and is_market_open():
            return f"no quote sweep for {(time.time() - m.last_quote_sweep) / 60:.0f} min"
        st = m.stream
        if st is not None and st.wanted and not st.connected and is_market_open() and \
                st.last_msg_at and time.time() - st.last_msg_at > 600:
            return "real-time price stream down for 10+ minutes"
        return None

    async def watchdog(self) -> None:
        bad = self._degraded()
        if not bad:
            return
        hour = datetime.now(NY).strftime("%Y-%m-%d %H")
        if self._once(f"watchdog:{hour}"):
            await self.tr.post_portfolio("🩺 Terminal degraded", [bad, "Tracking falls back to slower polling; "
                                         "check the server if this repeats."], "warning", key="watchdog")


def next_weekday_time(now: datetime, weekday: int, hh: int, mm: int) -> datetime:
    d = now + timedelta(days=(weekday - now.weekday()) % 7)
    return d.replace(hour=hh, minute=mm, second=0, microsecond=0)
