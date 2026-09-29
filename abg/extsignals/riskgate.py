"""Portfolio-level risk across all tracked ideas, and prop-firm (funded account) guardrails.

Before any new entry fills, ``RiskGate.check`` looks at the whole book, not just the one idea:

    positions   no more than ABG_EXT_MAX_OPEN_POSITIONS open at once
    heat        total $ at risk to the stops (open trades + this one) <= ABG_EXT_MAX_HEAT_PCT of the account
    correlation no more than ABG_EXT_MAX_CORRELATED open ideas that are the same bet (90-day daily-return
                correlation >= ABG_EXT_CORRELATION_THRESHOLD, same direction; or strongly negative, opposite)
    sector      no more than ABG_EXT_MAX_SECTOR in one sector / futures group
    prop        the trade's risk must fit what's left of the daily loss limit and the trailing drawdown

When the only problem is size, the entry is shrunk to fit (ABG_EXT_RESIZE_TO_FIT); otherwise it's
blocked with the reason.  ``limits`` produces warnings as a funded account approaches its limits.

Equity is paper equity: account + realized P&L of every tracked idea + open P&L at the last price.
The day's starting equity and the high-water mark are kept in the signals database (kv table).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from .lifecycle import Idea

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")


class RiskGate:
    def __init__(self, tracker):
        self.tr = tracker
        self.s = tracker.s
        self.store = tracker.store

    # ------------------------------------------------------------------ equity
    @property
    def account(self) -> float:
        return self.s.prop_account_size if self.s.prop_enabled else self.s.ext_account_size

    def equity(self) -> dict:
        ideas = self.store.ideas(limit=100_000)
        realized = sum(i.realized_pnl for i in ideas)
        opn = sum(i.open_pnl(i.last_price) for i in ideas if i.status == "active")
        eq = self.account + realized + opn
        now = datetime.now(NY)
        day = now.date().isoformat()
        k_day = f"equity:day:{day}"
        start = self.store.get_kv(k_day)
        if start is None:
            self.store.set_kv(k_day, str(eq))
            start = eq
        start = float(start)
        hwm = float(self.store.get_kv("equity:hwm") or self.account)
        eod_key = f"equity:eod:{day}"
        if self.s.prop_drawdown_mode == "intraday":
            if eq > hwm:
                hwm = eq
                self.store.set_kv("equity:hwm", str(hwm))
        elif now.hour >= 16 and self.store.get_kv(eod_key) is None:      # end-of-day trailing
            self.store.set_kv(eod_key, str(eq))
            if eq > hwm:
                hwm = eq
                self.store.set_kv("equity:hwm", str(hwm))
        floor = None
        if self.s.prop_enabled and self.s.prop_max_drawdown:
            floor = hwm - self.s.prop_max_drawdown
            if self.s.prop_drawdown_lock:
                floor = min(floor, self.account)
        heat = sum(max(0.0, i.open_risk()) for i in ideas if i.status == "active")
        return {"account": self.account, "equity": eq, "realized": realized, "open_pnl": opn,
                "day_start": start, "day_pnl": eq - start, "high_water": hwm, "floor": floor,
                "drawdown_room": (eq - floor) if floor is not None else None,
                "daily_room": (self.s.prop_daily_loss_limit + (eq - start)) if (self.s.prop_enabled and
                                                                                 self.s.prop_daily_loss_limit) else None,
                "heat": heat, "heat_pct": heat / self.account * 100 if self.account else 0.0,
                "open": sum(i.status == "active" for i in ideas)}

    # ------------------------------------------------------------------ entry gate
    def planned_risk(self, idea: Idea) -> float:
        return (idea.risk_per_share or 0.0) * idea.shares * idea.flags.get("multiplier", 1.0)

    async def check(self, idea: Idea, price: float | None) -> tuple[bool, str | None, float | None]:
        """(allowed, reason, reduced $ risk budget or None)."""
        s = self.s
        active = [i for i in self.store.ideas({"active"}, limit=500) if i.id != idea.id]
        if len(active) >= s.ext_max_open_positions:
            return False, (f"{len(active)} ideas are already open (limit {s.ext_max_open_positions}); "
                           f"no new entries until one closes"), None
        # correlation clusters & sector concentration
        same = await self._correlated(idea, active)
        if len(same) >= s.ext_max_correlated:
            names = ", ".join(f"{i.symbol} ({rho:+.2f})" for i, rho in same[:4])
            return False, (f"already {len(same)} open ideas that move with {idea.symbol}: {names} "
                           f"(limit {s.ext_max_correlated} per correlated group)"), None
        sec = self._sector(idea)
        if sec:
            peers = [i for i in active if self._sector(i) == sec]
            if len(peers) >= s.ext_max_sector:
                return False, (f"{len(peers)} open ideas already in {sec} "
                               f"({', '.join(i.symbol for i in peers[:4])}; limit {s.ext_max_sector})"), None
        eq = self.equity()
        want = self.planned_risk(idea)
        room = [("portfolio heat", s.ext_max_heat_pct / 100 * self.account - eq["heat"])]
        if eq["daily_room"] is not None:
            room.append(("daily loss limit", eq["daily_room"] * 0.9))       # keep a 10% cushion
        if eq["drawdown_room"] is not None:
            room.append(("trailing drawdown", eq["drawdown_room"] * 0.9))
        name, left = min(room, key=lambda x: x[1])
        if left <= 0:
            return False, (f"{name} is used up (room ${left:,.0f}); no new risk until it frees up"), None
        if want > left:
            if not s.ext_resize_to_fit:
                return False, f"this trade risks ${want:,.0f} but the {name} only has ${left:,.0f} left", None
            return True, f"size reduced to fit the {name} (${left:,.0f} of room)", left
        return True, None, None

    def _sector(self, idea: Idea) -> str | None:
        inst = (idea.meta or {}).get("instrument") or {}
        if inst.get("asset_class") in ("future", "bond_future", "crypto", "fx"):
            return inst.get("group")
        rep = (self.tr._ctx.get(idea.symbol) or {}).get("report") or {}
        return (rep.get("fundamentals") or {}).get("sector") or idea.meta.get("sector")

    async def _correlated(self, idea: Idea, active: list[Idea]) -> list[tuple[Idea, float]]:
        if not active:
            return []
        base = await self._returns(idea.symbol)
        if base is None:
            return []
        out = []
        for i in active:
            if i.symbol == idea.symbol:
                out.append((i, 1.0 if i.direction == idea.direction else -1.0))
                continue
            r = await self._returns(i.symbol)
            if r is None:
                continue
            j = base.index.intersection(r.index)
            if len(j) < 40:
                continue
            rho = float(np.corrcoef(base.loc[j], r.loc[j])[0, 1])
            same_dir = i.direction == idea.direction
            if (same_dir and rho >= self.s.ext_correlation_threshold) or \
                    (not same_dir and rho <= -self.s.ext_correlation_threshold):
                out.append((i, rho))
        return out

    async def _returns(self, sym: str):
        ctx = self.tr._ctx.get(sym)
        if ctx is None:
            ctx = await self.tr.context(sym, max_age=3600)
        if not ctx or ctx.get("close") is None:
            return None
        c = ctx["close"].dropna()
        return c.pct_change(fill_method=None).dropna().tail(90)

    # ------------------------------------------------------------------ limit warnings
    def limits(self) -> list[dict]:
        """Warnings (once per day per limit and level) as a funded account approaches its limits."""
        eq = self.equity()
        out = []
        day = datetime.now(NY).date().isoformat()
        checks = []
        if self.s.prop_enabled and self.s.prop_daily_loss_limit:
            used = max(0.0, -eq["day_pnl"]) / self.s.prop_daily_loss_limit * 100
            checks.append(("daily", "Daily loss limit", used, f"today {eq['day_pnl']:+,.0f} of "
                           f"-{self.s.prop_daily_loss_limit:,.0f}"))
        if eq["floor"] is not None:
            used = max(0.0, (self.s.prop_max_drawdown - (eq["equity"] - eq["floor"]))) / self.s.prop_max_drawdown * 100
            checks.append(("drawdown", "Trailing drawdown", used, f"equity {eq['equity']:,.0f}, floor {eq['floor']:,.0f}"))
        checks.append(("heat", "Portfolio heat", eq["heat_pct"] / self.s.ext_max_heat_pct * 100 if self.s.ext_max_heat_pct else 0,
                       f"{eq['heat_pct']:.1f}% of the account at risk (limit {self.s.ext_max_heat_pct:g}%)"))
        for key, name, used, detail in checks:
            level = "breach" if used >= 100 else "warn" if used >= self.s.prop_warn_pct else None
            if not level:
                continue
            k = f"limitwarn:{day}:{key}:{level}"
            if self.store.get_kv(k):
                continue
            self.store.set_kv(k, str(time.time()))
            out.append({"key": key, "name": name, "used_pct": used, "level": level, "detail": detail, "equity": eq})
        return out
