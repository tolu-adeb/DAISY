"""Live shadow of your Alerio route (runs inside the monitor when ABG_ALERIO_WATCH=true).

Every ``alerio_poll_sec`` during the session it reads Alerio's log feed (read-only) and:

* runs each new TradingMind signal / follow-up through the terminal's guarded route (dry run) and says
  what it would do next to what Alerio is about to do (fixed size at market),
* reports Alerio's execution as it lands: the fill vs the optimal entry, the $ actually at risk, and any
  account the broker rejected (liquidation-only = the prop account is locked),
* refreshes the full snapshot every 30 minutes and after the close (dashboard + ``abg alerio`` use it).

Decisions go to the activity log (routes_log.jsonl), the dashboard, and ``ABG_ALERIO_WEBHOOK_URL``.
It never sends anything to Alerio or a broker.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from collections import deque
from datetime import datetime, time

from .alerio import BASE, account_states, guarded_route, load_snapshot, mgmt_kind, parse_signal_text, save_snapshot
from .alerts import Alert
from .engine import NY, RouteEngine
from .log import append

log = logging.getLogger(__name__)
MNQ_PV = 2.0


def shadow_alert(text: str, ts: datetime, msg_id: str, ref: str | None, signals: dict) -> Alert | None:
    """A TradingMind message -> the alert the terminal's engine acts on (None = not a trade instruction)."""
    s = parse_signal_text(text)
    if s:
        signals[msg_id] = s
        return Alert("entry", side=1 if s["side"] == "LONG" else -1, symbol="NQ", entry=s["opt"], entry_lo=s["zlo"],
                     entry_hi=s["zhi"], stop=s["stop"], targets=[x for x in (s["t1"], s["fin"]) if x is not None],
                     price=s["px"], ts=ts, id=msg_id, num=s["num"], source="tradingmind", confidence=1.0)
    mk = mgmt_kind(text)
    if mk and ref in signals:
        kind, val = mk
        sg = signals[ref]
        act = {"t1": "breakeven", "be": "close", "stopped": "close", "closed": "close", "final": "close", "move": "move_stop"}[kind]
        px = {"t1": sg.get("t1"), "final": sg.get("fin"), "stopped": sg.get("stop"), "be": sg.get("opt")}.get(kind)
        return Alert(act, symbol="NQ", ts=ts, ref=ref, price=px, trim_frac=0.5 if kind == "t1" else None,
                     new_stop=val if kind == "move" else None, source="tradingmind", confidence=1.0)
    return None


class AlerioWatcher:
    def __init__(self, settings, hub=None):
        self.s, self.hub = settings, hub
        self.events: deque = deque(maxlen=100)
        self.errors: deque = deque(maxlen=20)
        self.seen: set[str] = set()
        self.reported: set[str] = set()
        self.signals: dict[str, dict] = {}
        self.primed = False
        self.last_poll = 0.0
        self.last_sync = 0.0
        self.snapshot: dict | None = None
        self.engine: RouteEngine | None = None
        self.day = None
        self._stop = asyncio.Event()
        self._http = None

    # ------------------------------------------------------------------ plumbing
    def stop(self) -> None:
        self._stop.set()

    async def _get(self, path: str, **params) -> dict:
        import httpx
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=BASE, timeout=20, headers={
                "Cookie": self.s.alerio_cookie or "", "Accept": "application/json", "User-Agent": "abg-terminal (read-only)"})
        r = await self._http.get(path, params=params)
        if r.status_code in (401, 403) or "text/html" in r.headers.get("content-type", ""):
            raise PermissionError("Alerio session expired - paste a fresh cookie into ABG_ALERIO_COOKIE")
        r.raise_for_status()
        return r.json()

    def _engine(self, now: datetime) -> RouteEngine:
        if self.engine is None or self.day != now.date():
            self.day = now.date()
            snap = self.snapshot or {}
            route = guarded_route(self.s.alerio_budget_usd, self.s.alerio_max_contracts)
            from .engine import AccountState
            accts = {"paper": AccountState("paper")}          # the rules alone, on a fresh account
            accts.update(account_states(snap))                 # + your real accounts as Alerio reports them
            route.accounts = list(accts)
            self.engine = RouteEngine(route, accts)
        return self.engine

    async def run(self) -> None:
        try:
            self.snapshot = load_snapshot(self.s.data_dir)
        except Exception:
            self.snapshot = None
        while not self._stop.is_set():
            now = datetime.now(NY)
            try:
                live = now.weekday() < 5 and time(9, 20) <= now.time() <= time(16, 10)
                if live or not self.primed:
                    await self.poll(now)
                if (_time.time() - self.last_sync > 1800 and live) or (not live and now.time() >= time(16, 15)
                                                                      and _time.time() - self.last_sync > 6 * 3600):
                    await self.sync()
            except PermissionError as e:
                self._error(str(e))
                await self._sleep(600)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never kill the monitor
                log.exception("alerio watch failed")
                self._error(f"{type(e).__name__}: {e}")
            await self._sleep(self.s.alerio_poll_sec if (now.weekday() < 5 and time(9, 20) <= now.time() <= time(16, 10)) else 300)

    async def _sleep(self, sec: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=sec)
        except asyncio.TimeoutError:
            pass

    def _error(self, msg: str) -> None:
        self.errors.append({"ts": _time.time(), "error": msg[:300]})

    async def sync(self) -> None:
        from .alerio import build_snapshot, mark_rejections
        pages, cursor = [], None
        for _ in range(10):
            q = {"limit": 200, "order": "desc", **({"cursor": cursor} if cursor else {})}
            p = await self._get("/api/logs/feed", **q)
            pages.append(p)
            if not p.get("has_more") or not p.get("next_cursor"):
                break
            cursor = p["next_cursor"]
        snap = mark_rejections(build_snapshot(pages, await self._get("/api/user/trades", status="all", limit=100),
                                              await self._get("/api/user/accounts/metrics"), await self._get("/api/user/workspace")))
        save_snapshot(self.s.data_dir, snap)
        self.snapshot, self.last_sync = snap, _time.time()

    # ------------------------------------------------------------------ the shadow
    async def poll(self, now: datetime) -> list[dict]:
        page = await self._get("/api/logs/feed", limit=60, order="desc")
        self.last_poll = _time.time()
        entries = sorted(page.get("entries") or [], key=lambda e: e.get("timestamp", ""))
        out = []
        for e in entries:
            if e.get("event") != "received":
                continue
            md = (e.get("details") or {}).get("metadata") or {}
            mid = str(md.get("message_id") or e.get("log_id"))
            if mid in self.seen:
                continue
            self.seen.add(mid)
            ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).astimezone(NY)
            ref = str(md["reply_to_message_id"]) if md.get("reply_to_message_id") else None
            a = shadow_alert((e.get("details") or {}).get("content_preview") or "", ts, mid, ref, self.signals)
            if a is None or not self.primed:        # first poll only learns what was already there
                continue
            eng = self._engine(ts)
            for d in eng.on_alert(a, now=ts, price=a.price):
                ev = {"kind": "shadow", "ts": ts.isoformat(), "flow": e.get("flow_id"), "alert": a.to_dict(), "decision": d.to_dict()}
                out.append(ev)
                self.events.append(ev)
                append(self.s.data_dir, [{"kind": "alerio-shadow", "route": eng.route.name, "alert": a.to_dict(), "decisions": [d.to_dict()]}])
                if a.action == "entry":
                    await self._post(self._shadow_embed(a, d))
        out += await self._executions(page)
        self.primed = True
        if out and self.hub is not None:
            try:
                self.hub.broadcast("alerio", {"events": out[-10:]})
            except Exception:
                pass
        return out

    async def _executions(self, page: dict) -> list[dict]:
        """Alerio's own execution results: fills vs the optimal, $ at risk, rejected (locked) accounts."""
        out = []
        accts = {a["id"]: a for a in (self.snapshot or {}).get("accounts") or []}
        for k, v in (page.get("related_entries") or {}).items():
            for x in (v if isinstance(v, list) else [v]):
                if not x or x.get("event") != "execution_result":
                    continue
                key = x.get("log_id") or k
                if key in self.reported:
                    continue
                self.reported.add(key)
                if not self.primed:
                    continue
                flow = x.get("flow_id") or k.split(":")[-1]
                sig = next((s for s in self.signals.values() if s.get("flow") == flow), None)
                for a in (x.get("details") or {}).get("accounts") or []:
                    res = ((a.get("data") or {}).get("result") or {})
                    name = (accts.get(a.get("account_id")) or {}).get("nickname") or str(a.get("account_id"))
                    ev = {"kind": "alerio-exec", "ts": x.get("timestamp"), "account": name, "outcome": a.get("outcome"),
                          "contracts": a.get("contracts"), "reject": res.get("reject_reason"), "text": res.get("rejection_text")}
                    out.append(ev)
                    self.events.append(ev)
                    if res.get("reject_reason") == "LiquidationOnly":
                        if name in (self.engine.accounts if self.engine else {}):
                            self.engine.accounts[name].status = "liquidation_only"
                        await self._post({"title": f"🔒 {name} is liquidation-only", "color": 0xD50000,
                                          "description": f"The broker rejected Alerio's entry: _{res.get('rejection_text') or 'LiquidationOnly'}_\n"
                                                         "Alerio keeps sending orders to it. Turn the account off in the route until the firm resets it."})
        return out

    def _shadow_embed(self, a: Alert, d) -> dict:
        n_alerio = max([x.get("contracts") or 0 for x in (self.snapshot or {}).get("accounts") or []] + [8])
        side = "LONG" if a.side > 0 else "SHORT"
        alerio_risk = abs((a.price or a.entry) - (a.stop or a.entry)) * MNQ_PV * n_alerio
        take = d.verdict == "placed"
        lines = [f"**Alerio** (as configured): {n_alerio} MNQ at market ≈ **${alerio_risk:,.0f}** at risk to the stop.",
                 f"**Terminal**: " + (f"{d.contracts} MNQ, risk **${d.risk_usd:,.0f}**" if take else "**skip**")
                 + (f" — {'; '.join(d.reasons)}" if d.reasons else "")]
        if a.price is not None and a.entry is not None:
            past = (a.price - a.entry) * a.side
            if past > 0:
                lines.append(f"Price is {past:.0f} pts past the optimal entry {a.entry:,.2f}.")
        return {"title": f"🛰️ TradingMind #{a.num or ''} {side} — terminal says {'TAKE' if take else 'SKIP'} ({d.account})",
                "color": 0x00C853 if take else 0xFFA000, "description": "\n".join(lines),
                "footer": {"text": "shadow only · nothing was sent to Alerio or the broker"}}

    async def _post(self, embed: dict) -> None:
        url = self.s.alerio_webhook_url or self.s.alpha_webhook_url
        if not url:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(url, json={"username": "ABG Copy Guard", "embeds": [embed], "allowed_mentions": {"parse": []}})
        except Exception as e:
            self._error(f"webhook: {e}")

    def status(self) -> dict:
        return {"enabled": True, "cookie": bool(self.s.alerio_cookie), "primed": self.primed,
                "last_poll": self.last_poll or None, "last_sync": self.last_sync or None,
                "budget_usd": self.s.alerio_budget_usd, "events": list(self.events)[-20:], "errors": list(self.errors)[-5:]}
