"""Route engine: alert + account state -> decision (order plan, with every check and its reason).

Nothing here sends orders.  ``Decision.orders`` is the bracket a broker adapter would place; the
replay (``replay.py``) fills them against bars so you can see what a rule set would have done.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .alerts import Alert
from .rules import POINT_VALUE, Route

NY = ZoneInfo("America/New_York")


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _rt(x: float, tick: float = 0.25) -> float:
    return round(x / tick) * tick


@dataclass
class Position:
    key: str
    symbol: str
    side: int
    contracts: int                      # still open
    orig: int
    entry: float
    stop: float
    trims: list[dict]                   # {"price", "qty", "sl_after", "done"}
    final: float | None
    opened: datetime | None
    pv: float
    realized: float = 0.0
    commission: float = 0.0
    mfe: float = 0.0
    events: list[str] = field(default_factory=list)

    def fill(self, qty: int, px: float, why: str, comm_rt: float) -> float:
        qty = min(qty, self.contracts)
        usd = (px - self.entry) * self.side * self.pv * qty - comm_rt * qty
        self.contracts -= qty
        self.realized += usd
        self.commission += comm_rt * qty
        self.events.append(f"{why}: {qty} @ {px:,.2f} ({usd:+,.0f})")
        return usd


@dataclass
class AccountState:
    name: str
    equity: float = 50_000.0
    start_equity: float | None = None
    hwm: float | None = None
    day: date | None = None
    day_pnl: float = 0.0
    trades_today: int = 0
    lost_today: bool = False
    commission_rt: float = 1.24          # per contract round turn
    status: str = "ok"                   # ok | liquidation_only | disabled  (from the broker / Alerio metrics)
    status_reason: str = ""
    floor: float | None = None           # equity level the prop firm liquidates at (None = unknown)
    positions: dict[str, Position] = field(default_factory=dict)
    pending: dict[str, dict] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

    def roll(self, d: date) -> None:
        if self.day != d:
            self.day, self.day_pnl, self.trades_today, self.lost_today = d, 0.0, 0, False
        if self.start_equity is None:
            self.start_equity = self.equity
        if self.hwm is None:
            self.hwm = self.equity

    def book(self, usd: float) -> None:
        self.equity += usd
        self.day_pnl += usd
        self.hwm = max(self.hwm or self.equity, self.equity)


@dataclass
class Decision:
    route: str
    account: str
    alert_action: str
    verdict: str                        # placed | skipped | updated | closed | cancelled | noop
    contracts: int = 0
    symbol: str = ""
    orders: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    checks: list[list] = field(default_factory=list)    # [name, ok, detail]
    risk_usd: float | None = None
    ts: str | None = None
    dry_run: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    def line(self) -> str:
        head = f"[{self.account}] {self.alert_action.upper()} -> {self.verdict.upper()}"
        if self.contracts:
            head += f" {self.contracts} {self.symbol}"
        if self.risk_usd is not None:
            head += f" (risk ${self.risk_usd:,.0f})"
        return head + (" - " + "; ".join(self.reasons) if self.reasons else "")


class RouteEngine:
    def __init__(self, route: Route, accounts: dict[str, AccountState] | None = None, events_for=None):
        self.route = route
        self.accounts = accounts or {a: AccountState(a) for a in route.accounts}
        self.events_for = events_for          # date -> [{"time": "08:30", "name": "CPI"}]

    # ------------------------------------------------------------------ public
    def on_alert(self, a: Alert, now: datetime | None = None, price: float | None = None) -> list[Decision]:
        now = now or a.ts or datetime.now(NY)
        if now.tzinfo is None:
            now = now.replace(tzinfo=NY)
        now = now.astimezone(NY)
        out = []
        for name in self.route.accounts:
            acct = self.accounts.setdefault(name, AccountState(name))
            acct.roll(now.date())
            d = Decision(self.route.name, name, a.action, "noop", ts=now.isoformat(), dry_run=self.route.mode != "live")
            if self.route.mode == "disabled":
                d.verdict, d.reasons = "skipped", ["route is disabled"]
            elif a.action == "entry":
                self._entry(a, acct, now, price, d)
            elif a.action in ("trim", "breakeven", "move_stop", "close", "cancel"):
                self._manage(a, acct, now, price, d)
            else:
                d.reasons = [f"not a trade instruction ({a.action})"]
            out.append(d)
        return out

    # ------------------------------------------------------------------ entries
    def _check(self, d: Decision, name: str, ok: bool, detail: str, ok_detail: str = "ok") -> bool:
        d.checks.append([name, bool(ok), ok_detail if ok else detail])
        if not ok:
            d.verdict = "skipped"
            d.reasons.append(detail)
        return ok

    def _in(self, t: time, windows) -> bool:
        return any(_hm(a) <= t < _hm(b) for a, b in windows)

    def _entry(self, a: Alert, acct: AccountState, now: datetime, price: float | None, d: Decision) -> None:
        r = self.route.rules
        sym = r.contract_map.get(a.symbol, a.symbol)
        d.symbol = sym
        pv = POINT_VALUE.get(sym, 2.0)
        t = now.time()
        if not self._check(d, "account", acct.status == "ok",
                           f"account is {acct.status.replace('_', '-')}" + (f" ({acct.status_reason})" if acct.status_reason else "")):
            return
        ok = self._check(d, "symbol", a.symbol in r.allowed_symbols or sym in r.allowed_symbols,
                         f"{a.symbol} is not on this route's list")
        ok = ok and self._check(d, "parsed", a.side != 0 and a.entry is not None,
                                "could not read a direction and an entry price")
        if not ok:
            return
        if a.ts is not None:
            ats = a.ts if a.ts.tzinfo else a.ts.replace(tzinfo=NY)
            age = (now - ats).total_seconds()
            ok = self._check(d, "fresh", age <= r.stale_sec, f"alert is {age:.0f}s old (max {r.stale_sec}s) - not chasing a late signal",
                             f"{age:.0f}s old")
        ok = ok and self._check(d, "entry window", self._in(t, r.entry_windows), f"{t:%H:%M} ET is outside the entry window")
        ok = ok and self._check(d, "blackout", not self._in(t, r.blackout_windows), f"{t:%H:%M} ET is in a blackout window")
        if ok and self.events_for and r.exclude_events:
            for e in self.events_for(now.date()) or []:
                if not e.get("time") or not any(x.lower() in e.get("name", "").lower() for x in r.exclude_events):
                    continue
                em = _hm(e["time"]).hour * 60 + _hm(e["time"]).minute
                m = t.hour * 60 + t.minute
                if em - r.event_before_min <= m < em + r.event_after_min:
                    ok = self._check(d, "events", False, f"{e['name']} at {e['time']} ET")
                    break
        ok = ok and self._check(d, "trades today", acct.trades_today < r.max_trades_per_day,
                                f"{acct.trades_today} trades already today (max {r.max_trades_per_day})")
        ok = ok and self._check(d, "first loss", not (r.stop_after_first_loss and acct.lost_today),
                                "already took a loss today - stop-after-first-loss")
        ok = ok and self._check(d, "loss limit", not (r.daily_loss_limit_usd and acct.day_pnl <= -r.daily_loss_limit_usd),
                                f"daily loss limit hit ({acct.day_pnl:+,.0f})")
        ok = ok and self._check(d, "profit lock", not (r.daily_profit_target_usd and acct.day_pnl >= r.daily_profit_target_usd),
                                f"day is up {acct.day_pnl:+,.0f} - profit target reached, done")
        ok = ok and self._check(d, "one at a time", not any(p.symbol == sym for p in acct.positions.values())
                                and not any(o["symbol"] == sym for o in acct.pending.values()),
                                f"already in (or waiting to enter) a {sym} position")
        if not ok:
            return
        # ---- prices
        px = price if price is not None else (a.price if a.price is not None else a.entry)
        in_zone = (a.entry_lo is not None and a.entry_hi is not None and a.entry_lo <= px <= a.entry_hi) or abs(px - a.entry) <= 1.0
        mode = r.entry_type
        if mode == "smart":
            mode = "market" if in_zone else "limit"
        fill = _rt(a.entry - a.side * r.limit_offset_ticks * 0.25) if mode == "limit" else _rt(px)
        if mode == "limit" and (px - fill) * a.side < 0:
            mode, fill = "market", _rt(px)          # price is already better than the limit: just take it
        d.checks.append(["entry", True, f"{mode} @ {fill:,.2f}" + ("" if mode == "market" else f" (price {px:,.2f}, good for {r.limit_expiry_min} min)")])
        stop = a.stop
        if stop is None:
            if r.require_stop and not r.default_stop_pts:
                self._check(d, "stop", False, "alert has no stop and no default stop is set")
                return
            stop = fill - a.side * (r.default_stop_pts or r.max_stop_pts)
            d.reasons.append(f"no stop in the alert - default {abs(fill - stop):.0f} pts")
        risk = (fill - stop) * a.side
        if not self._check(d, "stop side", risk > 0, f"price {fill:,.2f} is already through the stop {stop:,.2f}"):
            return
        alert_risk = a.risk_pts or risk
        past = (px - a.entry) * a.side
        lim = min(r.max_chase_pts, r.max_chase_r * alert_risk)
        if mode == "market" and not self._check(d, "chase", past <= lim,
                                                 f"price is {past:.1f} pts past the entry (max {lim:.1f}) - not chasing"):
            return
        if risk > r.max_stop_pts:
            if r.stop_cap_mode == "tighten":
                stop = _rt(fill - a.side * r.max_stop_pts)
                d.reasons.append(f"stop tightened from {risk:.0f} to {r.max_stop_pts:g} pts")
                risk = r.max_stop_pts
            else:
                self._check(d, "stop distance", False, f"stop is {risk:.0f} pts away (max {r.max_stop_pts:g})")
                return
        d.checks.append(["stop distance", True, f"{risk:.1f} pts"])
        # ---- size: the smallest of the per-trade budget, today's remaining loss room and the prop drawdown room
        per_c = risk * pv + acct.commission_rt
        budgets = [("per-trade risk", r.risk_per_trade_usd if r.sizing == "risk" else math.inf)]
        if r.daily_loss_limit_usd:
            budgets.append(("today's loss room", r.daily_loss_limit_usd + min(0.0, acct.day_pnl)))
        if acct.floor is not None:
            budgets.append(("account floor room", 0.9 * (acct.equity - acct.floor)))
        if r.trailing_drawdown_usd:
            floor = (acct.hwm or acct.equity) - r.trailing_drawdown_usd
            budgets.append(("trailing drawdown room", 0.9 * (acct.equity - floor)))
        name, budget = min(budgets, key=lambda x: x[1])
        want = r.contracts if r.sizing == "fixed" else r.max_contracts
        n = min(want, r.max_contracts, int(budget // per_c) if budget < math.inf else want)
        if not self._check(d, "size", n >= 1, f"one contract would risk ${per_c:,.0f} > {name} ${budget:,.0f}",
                           f"{n} x ${per_c:,.0f} within {name} ${budget:,.0f}"):
            return
        if n < want:
            d.reasons.append(f"{n} contract{'s' if n > 1 else ''} - capped by {name} (${budget:,.0f})")
        if mode == "market" and r.half_on_chase_pts and past > r.half_on_chase_pts and n > 1:
            n = max(1, n // 2)
            d.reasons.append(f"half size - entering {past:.0f} pts past the optimal")
        # ---- brackets
        trims, final = self._brackets(a, fill, risk, n)
        key = a.id or f"{sym}:{now:%H%M%S}"
        pos = Position(key, sym, a.side, n, n, fill, _rt(stop), trims, final, now, pv)
        acct.trades_today += 1
        if mode == "market":
            acct.positions[key] = pos
        else:
            acct.pending[key] = {"symbol": sym, "pos": pos, "expires": now + timedelta(minutes=r.limit_expiry_min)}
        d.verdict, d.contracts, d.risk_usd = "placed", n, round(n * per_c, 2)
        side = "BUY" if a.side > 0 else "SELL"
        exit_side = "SELL" if a.side > 0 else "BUY"
        d.orders = [{"type": mode.upper(), "side": side, "qty": n, "price": fill},
                    {"type": "STOP", "side": exit_side, "qty": n, "price": pos.stop}]
        d.orders += [{"type": "LIMIT", "side": exit_side, "qty": x["qty"], "price": x["price"], "then_stop": x["sl_after"]}
                     for x in trims if x["qty"]]
        d.orders += [{"type": "STOP_ADJUST", "side": exit_side, "qty": n, "price": x["price"], "then_stop": x["sl_after"]}
                     for x in trims if not x["qty"] and x["sl_after"] is not None]
        if final is not None:
            d.orders.append({"type": "LIMIT", "side": exit_side, "qty": n - sum(x["qty"] for x in trims), "price": final})

    def _brackets(self, a: Alert, fill: float, risk: float, n: int) -> tuple[list[dict], float | None]:
        r = self.route.rules
        rows = [(fill + a.side * (x.at_r * risk if x.at_r is not None else (x.at_pts or 0)), x.pct, x.sl_after) for x in r.trims]
        tg = list(a.targets)
        if r.skip_reached_targets:
            tg = [x for x in tg if (x - fill) * a.side >= 1.0]     # a target at/through the fill isn't a target
        final = fill + a.side * r.runner_target_r * risk
        if r.alert_override == "override" and tg:
            final = tg[-1]
            mids = tg[:-1]
            pcts = [x.pct for x in r.trims] or [1.0 / (len(mids) + 1)] * len(mids)
            rows = [(p, pcts[i] if i < len(pcts) else pcts[-1], r.trims[i].sl_after if i < len(r.trims) else None)
                    for i, p in enumerate(mids)]
        elif r.alert_override == "merge" and tg:
            used = set()
            for i, (p, pct, sl) in enumerate(rows):
                near = min((x for x in tg if x not in used), key=lambda x: abs(x - p), default=None)
                if near is not None and abs(near - p) <= 0.3 * risk:
                    rows[i] = (near, pct, sl)
                    used.add(near)
            beyond = [x for x in tg if x not in used and (x - (rows[-1][0] if rows else fill)) * a.side > 0]
            if beyond:
                final = beyond[-1]
        trims, left = [], n
        for p, pct, sl in rows:
            q = min(int(math.floor(pct * n)), left - 1) if n > 1 else 0
            q = max(q, 0)
            left -= q
            trims.append({"price": _rt(p), "qty": q, "sl_after": sl, "done": False})
        return trims, (_rt(final) if final is not None else None)

    # ------------------------------------------------------------------ management
    def _find(self, a: Alert, acct: AccountState) -> Position | None:
        if a.ref:                                   # a reply belongs to one signal: never touch a different trade
            return acct.positions.get(a.ref)
        sym = self.route.rules.contract_map.get(a.symbol, a.symbol)
        cands = [p for p in acct.positions.values() if p.symbol in (sym, a.symbol)] or list(acct.positions.values())
        return cands[-1] if len(cands) >= 1 else None

    def _manage(self, a: Alert, acct: AccountState, now: datetime, price: float | None, d: Decision) -> None:
        pend = acct.pending.get(a.ref) if a.ref else None
        if pend is None and not a.ref and len(acct.pending) == 1:
            pend = next(iter(acct.pending.values()))
        if pend is not None and a.action in ("cancel", "close", "trim", "breakeven"):
            acct.pending = {k: v for k, v in acct.pending.items() if v is not pend}
            d.verdict, d.symbol = "cancelled", pend["symbol"]
            d.reasons.append("limit entry never filled - cancelled on the service's update")
            return
        if not self.route.rules.follow_management:
            d.verdict, d.reasons = "noop", ["this route ignores management messages (brackets only)"]
            return
        p = self._find(a, acct)
        if p is None:
            d.verdict, d.reasons = "noop", ["no open position on this route for that update"]
            return
        d.symbol, exit_side = p.symbol, ("SELL" if p.side > 0 else "BUY")
        if a.action == "cancel":
            d.verdict = "noop"
            d.reasons.append("entry already filled - cancel ignored, the bracket keeps protecting it")
            return
        if a.action == "close":
            px = price if price is not None else p.entry
            usd = p.fill(p.contracts, px, "closed on alert", acct.commission_rt) if price is not None else 0.0
            d.verdict, d.orders = "closed", [{"type": "MARKET", "side": exit_side, "qty": p.orig, "price": price}]
            d.reasons.append("flatten on the service's exit message" + ("" if price is not None else " (price unknown - P&L via stop/target)"))
            if price is not None:
                self._settle(acct, p, usd)
            return
        bracket_trimmed = any(x["done"] and x["qty"] for x in p.trims)
        if a.action in ("trim", "breakeven") and a.trim_frac and bracket_trimmed:
            d.reasons.append("the bracket already took this trim")
        elif a.action in ("trim", "breakeven") and a.trim_frac:
            q = min(p.contracts - 1, int(math.floor(a.trim_frac * p.orig))) if p.contracts > 1 else 0
            if q > 0:
                d.orders.append({"type": "MARKET", "side": exit_side, "qty": q, "price": price})
                if price is not None:
                    p.fill(q, price, "trim on alert", acct.commission_rt)
                for x in p.trims:            # the bracket trim this replaces
                    if not x["done"] and x["qty"]:
                        x["done"] = True
                        break
                d.reasons.append(f"trim {q} of {p.orig}")
            else:
                d.reasons.append("single contract - no trim, managing the stop only")
        if a.action == "breakeven":
            if (p.entry - p.stop) * p.side > 0:
                p.stop = p.entry
                d.orders.append({"type": "STOP", "side": exit_side, "qty": p.contracts, "price": p.stop, "modify": True})
                d.reasons.append("stop to break-even")
        if a.action == "move_stop" and a.new_stop is not None:
            if (a.new_stop - p.stop) * p.side > 0:          # NEW: an update may only tighten the stop, never widen it
                p.stop = _rt(a.new_stop)
                d.orders.append({"type": "STOP", "side": exit_side, "qty": p.contracts, "price": p.stop, "modify": True})
                d.reasons.append(f"stop moved to {p.stop:,.2f}")
            else:
                d.reasons.append(f"ignored: {a.new_stop:,.2f} would widen the stop from {p.stop:,.2f}")
        d.verdict = "updated" if d.orders else "noop"

    def _settle(self, acct: AccountState, p: Position, last_usd: float = 0.0) -> None:
        if p.contracts == 0:
            acct.positions.pop(p.key, None)
            acct.book(p.realized)
            if p.realized < 0:
                acct.lost_today = True
            acct.history.append({"key": p.key, "symbol": p.symbol, "side": p.side, "contracts": p.orig, "entry": p.entry,
                                 "opened": p.opened.isoformat() if p.opened else None, "usd": round(p.realized, 2),
                                 "events": list(p.events)})

    # ------------------------------------------------------------------ simulation (replay / paper)
    def on_bar(self, ts: datetime, high: float, low: float, close: float, start: datetime | None = None) -> list[str]:
        """Work every open bracket against one bar (stop first when a bar touches both, like the backtester)."""
        r, out = self.route.rules, []
        for acct in self.accounts.values():
            for k, o in list(acct.pending.items()):
                p = o["pos"]
                if start is not None and p.opened and start < p.opened:      # bar began before the order existed
                    continue
                if ts - timedelta(minutes=1) > o["expires"]:
                    acct.pending.pop(k)
                    acct.trades_today = max(0, acct.trades_today - 1)
                    out.append(f"{acct.name}: {k} limit expired unfilled")
                    continue
                if (low if p.side > 0 else high) * p.side <= p.entry * p.side:     # touched the limit
                    acct.pending.pop(k)
                    p.opened = ts                      # targets start on the next bar (stop was checked above)
                    acct.positions[k] = p
                    p.events.append(f"limit filled @ {p.entry:,.2f}")
                    if ((low if p.side > 0 else high) - p.stop) * p.side <= 0:   # same bar ran through the stop: assume the worst
                        p.fill(p.contracts, p.stop - p.side * 0.25, "stop (same bar)", acct.commission_rt)
                        self._settle(acct, p)
                        out.append(f"{acct.name}: {k} filled and stopped in one bar {p.realized:+,.0f}")
            for p in list(acct.positions.values()):
                if p.opened and (ts <= p.opened or (start is not None and start < p.opened)):
                    continue                                                 # never fill on prices from before the entry
                fav, adv = (high, low) if p.side > 0 else (low, high)
                p.mfe = max(p.mfe, (fav - p.entry) * p.side)
                if (adv - p.stop) * p.side <= 0:
                    p.fill(p.contracts, p.stop - p.side * 0.25, "stop", acct.commission_rt)
                else:
                    for x in p.trims:
                        if not x["done"] and (fav - x["price"]) * p.side >= 0:
                            x["done"] = True
                            if x["qty"]:
                                p.fill(x["qty"], x["price"], "trim", acct.commission_rt)
                            if x["sl_after"] is not None:
                                ns = _rt(p.entry + p.side * x["sl_after"])
                                if (ns - p.stop) * p.side > 0:
                                    p.stop = ns
                    if p.contracts and p.final is not None and (fav - p.final) * p.side >= 0:
                        p.fill(p.contracts, p.final, "final", acct.commission_rt)
                    if p.contracts and r.auto_close and ts.time() >= _hm(r.auto_close):
                        p.fill(p.contracts, close, "auto close", acct.commission_rt)
                if p.contracts == 0:
                    self._settle(acct, p)
                    out.append(f"{acct.name}: {p.key} closed {p.realized:+,.0f}")
        return out
