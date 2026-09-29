"""Discord slash commands (``/abg ...``) through the bot's gateway connection.

Commands are registered on the signal channel's server (instant) and answered over the gateway,
so no public URL is needed (it works behind Tailscale).  Read-only commands work for everyone in
the server; anything that changes state requires your Discord user id in ``ABG_DISCORD_ADMIN_IDS``.

    /abg ideas                     open ideas                /abg status symbol:NVDA   ideas + live price
    /abg stats                     track record              /abg risk                 equity, heat, prop-firm room
    /abg markets                   market regime + calendar  /abg track text:<signal>  (admin) track a signal
    /abg close id:12 [fraction:.5] (admin)                   /abg cancel id:12         (admin)
    /abg stop id:12 price:118.5    (admin) move the stop

Protocol: HELLO(10) -> heartbeat every interval (1) / ACK (11) -> IDENTIFY(2) with intents 0 ->
DISPATCH(0) READY / INTERACTION_CREATE; RECONNECT(7) and INVALID_SESSION(9) reconnect.  Each
interaction is answered within 3 s with a deferred response, then the result is edited in.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque

from ..errors import ABGError
from .discord import API

log = logging.getLogger(__name__)
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
ADMIN = {"track", "close", "cancel", "stop", "alert"}

S, I, B, N = 3, 4, 5, 10     # option types: string, integer, boolean, number
COMMANDS = [{
    "name": "abg", "description": "ABG Intelligence Terminal", "type": 1,
    "options": [
        {"type": 1, "name": "ideas", "description": "Open tracked ideas"},
        {"type": 1, "name": "status", "description": "Ideas and live price for a symbol",
         "options": [{"type": S, "name": "symbol", "description": "Ticker, e.g. NVDA or NQ", "required": True}]},
        {"type": 1, "name": "stats", "description": "Track record by source and signal type"},
        {"type": 1, "name": "risk", "description": "Paper equity, open risk, prop-firm limits"},
        {"type": 1, "name": "markets", "description": "Market regime and the macro calendar"},
        {"type": 1, "name": "alerts", "description": "Active price alerts on the portfolio / watchlist"},
        {"type": 1, "name": "alert", "description": "(admin) Alert when a symbol's price crosses a level",
         "options": [{"type": S, "name": "symbol", "description": "Ticker, e.g. NVDA or NQ", "required": True},
                     {"type": N, "name": "price", "description": "The level", "required": True},
                     {"type": S, "name": "direction", "description": "Which way (default: either)", "required": False,
                      "choices": [{"name": "either way", "value": "any"}, {"name": "crosses up", "value": "up"},
                                  {"name": "crosses down", "value": "down"}]},
                     {"type": B, "name": "repeat", "description": "Keep alerting on every cross", "required": False}]},
        {"type": 1, "name": "track", "description": "(admin) Track a signal",
         "options": [{"type": S, "name": "text", "description": "The signal text", "required": True}]},
        {"type": 1, "name": "close", "description": "(admin) Close or trim an open idea",
         "options": [{"type": I, "name": "id", "description": "Idea #", "required": True},
                     {"type": N, "name": "fraction", "description": "0-1, default all", "required": False}]},
        {"type": 1, "name": "cancel", "description": "(admin) Cancel an idea",
         "options": [{"type": I, "name": "id", "description": "Idea #", "required": True}]},
        {"type": 1, "name": "stop", "description": "(admin) Move an idea's stop",
         "options": [{"type": I, "name": "id", "description": "Idea #", "required": True},
                     {"type": N, "name": "price", "description": "New stop", "required": True}]},
    ]}]


class DiscordGateway:
    name = "discord_commands"

    def __init__(self, settings, http, tracker, monitor=None, url: str = GATEWAY):
        self.s, self.http, self.tr, self.mon = settings, http, tracker, monitor
        self.url = url
        self.token = settings.discord_bot_token
        self.admins = {x.strip() for x in (settings.discord_admin_ids or "").split(",") if x.strip()}
        self.app_id: str | None = None
        self.guilds: list[str] = []
        self.connected = False
        self.ready = False
        self.seq: int | None = None
        self.handled = 0
        self.errors: deque = deque(maxlen=10)
        self._stop = asyncio.Event()
        self._tasks: set = set()

    @property
    def _h(self) -> dict:
        return {"Authorization": f"Bot {self.token}"}

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {"enabled": True, "connected": self.connected, "ready": self.ready, "guilds": self.guilds,
                "admins": len(self.admins), "handled": self.handled, "errors": list(self.errors)[-3:]}

    # ------------------------------------------------------------------ setup
    async def register(self) -> None:
        me = await self.http.get_json(f"{API}/applications/@me", provider="discord", headers=self._h)
        self.app_id = str(me["id"])
        guilds = [self.s.discord_guild_id] if self.s.discord_guild_id else []
        if not guilds:
            for cid in self.s.ext_channel_list():
                try:
                    ch = await self.http.get_json(f"{API}/channels/{cid}", provider="discord", headers=self._h)
                    if ch.get("guild_id") and ch["guild_id"] not in guilds:
                        guilds.append(ch["guild_id"])
                except ABGError as e:
                    self.errors.append({"ts": time.time(), "error": f"channel {cid}: {e.message}"[:200]})
        self.guilds = guilds
        for g in guilds:
            await self.http.request("PUT", f"{API}/applications/{self.app_id}/guilds/{g}/commands",
                                    provider="discord", json=COMMANDS, headers=self._h)
        if not guilds:
            await self.http.request("PUT", f"{API}/applications/{self.app_id}/commands", provider="discord",
                                    json=COMMANDS, headers=self._h)

    # ------------------------------------------------------------------ gateway loop
    async def run(self) -> None:
        import websockets
        backoff = 2.0
        try:
            await self.register()
        except Exception as e:
            self.errors.append({"ts": time.time(), "error": f"register: {getattr(e, 'message', e)}"[:200]})
            log.warning("slash command registration failed: %s", e)
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, max_size=2 ** 23, open_timeout=15) as ws:
                    self.connected = True
                    backoff = 2.0
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.errors.append({"ts": time.time(), "error": f"{type(e).__name__}: {e}"[:200]})
            finally:
                self.connected = self.ready = False
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 120)

    async def _session(self, ws) -> None:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 20))
        interval = hello["d"]["heartbeat_interval"] / 1000
        hb = asyncio.ensure_future(self._heartbeat(ws, interval))
        try:
            await ws.send(json.dumps({"op": 2, "d": {"token": self.token, "intents": 0,
                                                     "properties": {"os": "linux", "browser": "abg", "device": "abg"}}}))
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), 5)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                if msg.get("s") is not None:
                    self.seq = msg["s"]
                op = msg.get("op")
                if op == 0:
                    if msg.get("t") == "READY":
                        self.ready = True
                    elif msg.get("t") == "INTERACTION_CREATE":
                        t = asyncio.ensure_future(self.handle(msg["d"]))
                        self._tasks.add(t)
                        t.add_done_callback(self._tasks.discard)
                elif op == 1:
                    await ws.send(json.dumps({"op": 1, "d": self.seq}))
                elif op in (7, 9):
                    await asyncio.sleep(random.uniform(1, 3))
                    return
        finally:
            hb.cancel()

    async def _heartbeat(self, ws, interval: float) -> None:
        await asyncio.sleep(interval * random.random())
        while True:
            await ws.send(json.dumps({"op": 1, "d": self.seq}))
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------ interactions
    async def handle(self, it: dict) -> None:
        if it.get("type") != 2:
            return
        iid, tok = it["id"], it["token"]
        user = (it.get("member") or {}).get("user") or it.get("user") or {}
        sub = ((it.get("data") or {}).get("options") or [{}])[0]
        name = sub.get("name")
        args = {o["name"]: o.get("value") for o in sub.get("options") or []}
        eph = name in ADMIN and str(user.get("id")) not in self.admins
        try:
            await self.http.request("POST", f"{API}/interactions/{iid}/{tok}/callback", provider="discord",
                                    json={"type": 5, "data": {"flags": 64} if eph else {}})
        except ABGError as e:
            self.errors.append({"ts": time.time(), "error": f"ack: {e.message}"[:200]})
            return
        try:
            if eph:
                emb = {"title": "Not allowed", "description": "Only the terminal's admins can change ideas or alerts. "
                       "Ask the owner to add your user id to ABG_DISCORD_ADMIN_IDS.", "color": 0xE34948}
            else:
                emb = await self.run_command(name, args, user)
        except Exception as e:  # report, never crash
            log.exception("command %s failed", name)
            emb = {"title": f"/abg {name} failed", "description": str(getattr(e, "message", e))[:1500], "color": 0xE34948}
        self.handled += 1
        await self.http.request("PATCH", f"{API}/webhooks/{self.app_id}/{tok}/messages/@original", provider="discord",
                                json={"embeds": [emb], "allowed_mentions": {"parse": []}})

    async def run_command(self, name: str, a: dict, user: dict) -> dict:
        tr = self.tr
        from . import commentary as cm
        if name == "ideas":
            ideas = tr.store.open_ideas()[:20]
            lines = [f"**#{i.id} {i.symbol}** {i.direction} · {i.status} · {cm.levels_line(i)}"
                     + (f" · {i.total_r(i.last_price):+.2f}R" if i.entry_price else
                        f" · {i.distance_to_entry_pct(i.last_price):+.1f}% to entry" if i.last_price else "")
                     for i in ideas]
            return {"title": f"Open ideas ({len(ideas)})", "description": "\n".join(lines)[:4000] or "None open.",
                    "color": 0x5865F2}
        if name == "status":
            from ..markets.instruments import canonical
            sym = canonical(str(a.get("symbol", "")).upper().lstrip("$"))
            ideas = tr.store.ideas(symbol=sym, limit=5)
            try:
                q = (await tr.engine.quote(sym, use_cache=False)).value
                head = f"Price {q.price:,.2f} ({(q.change_pct or 0):+.2f}% today)"
            except ABGError as e:
                head = f"No quote: {e.message}"
            lines = [head] + [f"#{i.id} {i.direction} {i.status} · {cm.levels_line(i)} · grade {i.grade or 'n/a'}"
                              + (f" · {i.total_r(i.last_price):+.2f}R" if i.entry_price else "") for i in ideas]
            return {"title": f"{sym}", "description": "\n".join(lines)[:4000], "color": 0x5865F2}
        if name == "stats":
            st = tr.stats()
            wr = f"{st['win_rate']:.0%}" if st["win_rate"] is not None else "n/a"
            lines = [f"Closed {st['closed']} · win rate {wr} · avg {st['avg_r'] or 0:+.2f}R · total {st['total_r']:+.2f}R",
                     f"Open {st['pending']} pending / {st['active']} active · paper P&L {st['realized_pnl']:+,.2f}"]
            lines += [f"**{k}**: {b['closed']} closed, {(b['win_rate'] or 0):.0%} wins, {b['total_r']:+.2f}R"
                      for k, b in list(st["by_source"].items())[:10]]
            return {"title": "Track record", "description": "\n".join(lines)[:4000], "color": 0x5865F2}
        if name == "risk":
            eq = tr.gate.equity()
            lines = [f"Paper equity {eq['equity']:,.2f} (today {eq['day_pnl']:+,.2f}) · {eq['open']} open",
                     f"Open risk to stops {eq['heat']:,.2f} = {eq['heat_pct']:.1f}% (limit {self.s.ext_max_heat_pct:g}%)"]
            if eq["daily_room"] is not None:
                lines.append(f"Daily loss room {eq['daily_room']:,.2f}")
            if eq["floor"] is not None:
                lines.append(f"Trailing drawdown floor {eq['floor']:,.2f} (room {eq['drawdown_room']:,.2f})")
            return {"title": "Risk", "description": "\n".join(lines), "color": 0xF2B33D}
        if name == "markets":
            rg = await tr.regime.get() or {}
            evs = tr.calendar.macro(7)
            lines = [f"**Regime:** {rg.get('summary', 'n/a')}"] + [f"• {n}" for n in rg.get("notes", [])[:3]]
            lines += ["**Next 7 days:**"] + [f"• {e.date} {e.time or ''} ET — {e.name}{' (est.)' if e.estimated else ''}"
                                               for e in evs[:10]]
            return {"title": "Markets", "description": "\n".join(lines)[:4000], "color": 0x5865F2}
        if name == "alerts":
            if self.mon is None:
                return {"title": "Alerts unavailable", "description": "The portfolio monitor isn't attached.", "color": 0xE34948}
            rules = [r for r in self.mon.store.rule_dicts(self.mon.pf) if r["enabled"]]
            lines = [f"#{r['id']} **{r['symbol']}** {r['kind'].replace('_', ' ')} {r['value']:g}"
                     + (f" · now {r['state']['side']}" if (r.get("state") or {}).get("side") else "")
                     + (" · repeating" if not r["one_shot"] else "") for r in rules[:40]]
            return {"title": f"Active alerts ({len(rules)})", "description": "\n".join(lines)[:4000] or "None.",
                    "color": 0x5865F2}
        who = user.get("username") or "discord"
        if name == "alert":
            if self.mon is None:
                return {"title": "Alerts unavailable", "description": "The portfolio monitor isn't attached.", "color": 0xE34948}
            from ..markets.instruments import canonical
            sym = canonical(str(a.get("symbol", "")).upper().lstrip("$"))
            d = a.get("direction") or "any"
            kind = {"up": "price_cross_up", "down": "price_cross_down"}.get(d, "price_cross")
            store = self.mon.store
            r = store.add_rule(self.mon.pf, sym, kind, float(a["price"]), not bool(a.get("repeat")), f"added by {who} via /abg")
            try:
                price = (await tr.engine.quote(sym, use_cache=False)).value.price
            except ABGError:
                price = None
            hint = store.seed_cross(r, price) or "No live price right now; the monitor records the starting side on its first quote."
            self.mon.poke()
            return {"title": f"Alert #{r.id}: {sym} {kind.replace('_', ' ')} {r.value:g}",
                    "description": hint + ("\nRepeating." if not r.one_shot else "\nOne-shot (turns off after it fires)."),
                    "color": 0x1BAF7A}
        if name == "track":
            res = await tr.ingest(str(a.get("text", "")), source="discord-command", author=who)
            items = res.get("results") or [res]
            lines = [f"**{r['outcome']}**: {r.get('message')}" for r in items]
            return {"title": "Track", "description": "\n".join(lines)[:4000], "color": 0x1BAF7A}
        if name in ("close", "cancel", "stop"):
            iid = int(a["id"])
            if tr.store.get(iid) is None:
                return {"title": f"#{iid} not found", "color": 0xE34948}
            if name == "close":
                i = await tr.close(iid, None, a.get("fraction"))
            elif name == "cancel":
                i = await tr.cancel(iid, f"cancelled by {who} via /abg")
            else:
                i = await tr.edit(iid, stop=float(a["price"]))
            return {"title": f"#{iid} {i.symbol}: {i.status}", "description": cm.levels_line(i), "color": 0x1BAF7A}
        return {"title": f"Unknown command {name}", "color": 0xE34948}
