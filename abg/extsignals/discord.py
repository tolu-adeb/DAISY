"""Discord in/out for external signals.

Inbound  - ``DiscordPoller`` reads new messages from signal channels with a **bot token** over the
           REST API (``GET /channels/{id}/messages?after=<cursor>``) every ``ABG_EXT_POLL_SECONDS``.
           No gateway / websocket, no extra dependency.  Replies (message_reference) are passed on so
           "TP1 hit, stop to BE" replying to the original call updates the right idea.  Embeds and
           forwarded messages are flattened to text (many signal bots post embeds).
           Only bot accounts are supported: reading channels with a user token ("self-bot") breaks
           Discord's Terms of Service.  To follow a server you don't control, use Discord's
           "Follow" (announcement channels) or forward the calls into a channel your bot can read.

Outbound - ``DiscordRelay`` posts every tracked decision back to the channel as a rich embed,
           either through a webhook (``ABG_EXT_RELAY_WEBHOOK_URL``) or as a bot reply threaded to
           the original call (``ABG_EXT_RELAY_MODE=reply``).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque

from ..errors import ABGError, RateLimited
from ..http import HttpClient
from ..resilience import RateLimiter
from .parser import looks_like_signal

log = logging.getLogger(__name__)
API = "https://discord.com/api/v10"
COLORS = {"ingested": 0x5865F2, "approaching": 0xF2B33D, "entry": 0x1BAF7A, "entry_blocked": 0x9A9892,
          "target_hit": 0x2ECC71, "stop_moved": 0x3498DB, "stop_hit": 0xE34948, "breakeven_stop": 0x9A9892,
          "trailing_stop": 0xF1C40F, "time_exit": 0x9A9892, "exit": 0x3498DB, "trim": 0x1ABC9C,
          "invalidated": 0xE34948, "missed": 0x9A9892, "expired": 0x9A9892, "cancelled": 0x9A9892,
          "rejected": 0xE34948, "advisory": 0xE67E22, "source_update": 0x5865F2}
WEBHOOK_RE = re.compile(r"/webhooks/(\d+)/")


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def embed_for(idea, kind: str, x: dict) -> dict:
    fields = []
    for name, key, n in (("Why", "why", 8), ("Plan", "plan", 6), ("Risks", "risks", 5)):
        items = x.get(key) or []
        if items:
            fields.append({"name": name, "value": _clip("\n".join(f"• {i}" for i in items[:n]), 1024), "inline": False})
    for k, v in list((x.get("fields") or {}).items())[:9]:
        fields.append({"name": k, "value": _clip(str(v), 256), "inline": True})
    src = f" · source: {idea.author}" if idea.author else ""
    return {"title": _clip(x["title"], 256), "description": _clip(x.get("summary") or "", 4000),
            "color": COLORS.get(kind, 0x9A9892), "fields": fields[:25],
            "footer": {"text": _clip(f"idea #{idea.id} · {idea.symbol} {idea.direction}{src} · paper tracking by "
                                     f"ABG Intelligence Terminal · not investment advice", 2048)},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


class DiscordRelay:
    name = "discord_relay"

    def __init__(self, settings, http: HttpClient):
        self.s, self.http = settings, http
        self.mode = (settings.ext_relay_mode or "webhook").lower()
        self.webhook = settings.ext_relay_webhook_url or settings.discord_webhook_url
        self.token = settings.discord_bot_token
        self.limiter = RateLimiter(25, 60.0, provider="discord")
        self.enabled = bool((self.mode == "webhook" and self.webhook) or (self.mode == "reply" and (self.token or self.webhook)))

    @property
    def webhook_id(self) -> str | None:
        m = WEBHOOK_RE.search(self.webhook or "")
        return m.group(1) if m else None

    def describe(self) -> dict:
        return {"enabled": self.enabled, "mode": self.mode, "webhook": bool(self.webhook), "bot": bool(self.token)}

    async def send(self, idea, kind: str, x: dict) -> tuple[str, str | None]:
        payload = {"username": "ABG Signal Tracker", "embeds": [embed_for(idea, kind, x)],
                   "allowed_mentions": {"parse": []}}
        if self.mode == "reply" and self.token and idea.channel_id:
            body = {"embeds": payload["embeds"], "allowed_mentions": {"parse": []}}
            if idea.message_id:
                body["message_reference"] = {"message_id": idea.message_id, "fail_if_not_exists": False}
            resp = await self._post(f"{API}/channels/{idea.channel_id}/messages", body,
                                    headers={"Authorization": f"Bot {self.token}"})
        elif self.webhook:
            resp = await self._post(self.webhook + ("&" if "?" in self.webhook else "?") + "wait=true", payload)
        else:
            return "skipped: no relay destination for this idea", None
        try:
            return "ok", str(resp.json().get("id"))
        except Exception:
            return "ok", None

    async def _post(self, url: str, body: dict, headers: dict | None = None):
        await self.limiter.acquire(max_wait=30)
        for attempt in range(3):
            try:
                return await self.http.request("POST", url, provider="discord", json=body, headers=headers)
            except RateLimited as e:
                if attempt < 2:
                    await asyncio.sleep(min(float(e.retry_after or 2), 10))
                    continue
                raise


def message_text(m: dict) -> str:
    """Flatten a Discord message (content + embeds + forwarded snapshots) into plain text."""
    parts = [m.get("content") or ""]
    for e in m.get("embeds") or []:
        parts += [e.get("title") or "", e.get("description") or ""]
        for f in e.get("fields") or []:
            parts.append(f"{f.get('name', '')}: {f.get('value', '')}")
    for snap in m.get("message_snapshots") or []:
        parts.append(message_text(snap.get("message") or {}))
    return "\n".join(p for p in parts if p).strip()


class DiscordPoller:
    name = "discord_poller"

    def __init__(self, settings, http: HttpClient, tracker, relay: DiscordRelay | None = None):
        self.s, self.http, self.tracker = settings, http, tracker
        self.store = tracker.store
        self.relay = relay
        self.channels = settings.ext_channel_list()
        self.token = settings.discord_bot_token
        self.enabled = bool(self.token and self.channels)
        self.names: dict[str, str] = {}
        self.me: str | None = None
        self.errors: deque = deque(maxlen=20)
        self.last_poll: float | None = None
        self.processed = 0
        self.running = False
        self._stop = asyncio.Event()

    @property
    def _h(self) -> dict:
        return {"Authorization": f"Bot {self.token}"}

    async def _get(self, path: str, params: dict | None = None):
        for attempt in range(3):
            try:
                return await self.http.get_json(f"{API}{path}", provider="discord", params=params, headers=self._h)
            except RateLimited as e:
                if attempt < 2:
                    await asyncio.sleep(min(float(e.retry_after or 2), 15))
                    continue
                raise

    async def check(self) -> dict:
        """Verify the token and channel access (used by `abg ext discord-test`)."""
        out: dict = {"bot": None, "channels": {}, "relay": self.relay.describe() if self.relay else None}
        if not self.token:
            out["error"] = "ABG_DISCORD_BOT_TOKEN is not set"
            return out
        try:
            me = await self._get("/users/@me")
            out["bot"] = f"{me.get('username')} ({me.get('id')})"
        except ABGError as e:
            out["error"] = f"token rejected: {e.message}"
            return out
        for cid in self.channels:
            try:
                ch = await self._get(f"/channels/{cid}")
                msgs = await self._get(f"/channels/{cid}/messages", {"limit": 1})
                empty = bool(msgs) and not any(message_text(m) for m in msgs)
                out["channels"][cid] = f"ok: #{ch.get('name')}" + (
                    " (latest message has no readable text: enable the Message Content intent)" if empty else "")
            except ABGError as e:
                out["channels"][cid] = f"error: {e.message}"
        return out

    async def _name(self, cid: str) -> str:
        if cid not in self.names:
            try:
                self.names[cid] = "#" + ((await self._get(f"/channels/{cid}")) or {}).get("name", cid)
            except ABGError:
                self.names[cid] = cid
        return self.names[cid]

    def _own(self, m: dict) -> bool:
        wid = self.relay.webhook_id if self.relay else None
        return bool((wid and m.get("webhook_id") == wid) or (self.me and (m.get("author") or {}).get("id") == self.me))

    async def poll_once(self) -> dict:
        stats = {"messages": 0, "ideas": 0, "updates": 0, "ignored": 0}
        if self.me is None:
            try:
                self.me = str((await self._get("/users/@me")).get("id"))
            except ABGError as e:
                self.errors.append({"ts": time.time(), "where": "discord auth", "error": e.message[:200]})
                raise
        for cid in self.channels:
            key = f"discord:{cid}:after"
            cursor = self.store.get_kv(key)
            try:
                if cursor is None:
                    n = max(1, int(self.s.ext_backfill_messages or 0))
                    msgs = await self._get(f"/channels/{cid}/messages", {"limit": min(100, n)}) or []
                    if not self.s.ext_backfill_messages:        # first start: begin from "now", don't replay history
                        if msgs:
                            self.store.set_kv(key, max((m["id"] for m in msgs), key=int))
                        else:
                            self.store.set_kv(key, "0")
                        continue
                else:
                    msgs = await self._get(f"/channels/{cid}/messages", {"after": cursor, "limit": 100}) or []
            except ABGError as e:
                self.errors.append({"ts": time.time(), "where": f"channel {cid}", "error": e.message[:200]})
                continue
            name = await self._name(cid)
            for m in sorted(msgs, key=lambda m: int(m["id"])):
                stats["messages"] += 1
                if not self._own(m):
                    text = message_text(m)
                    if text and looks_like_signal(text):
                        ref = (m.get("message_reference") or {})
                        res = await self.tracker.ingest(
                            text, source="discord", channel_id=cid, channel_name=name,
                            author=(m.get("author") or {}).get("username"), message_id=m["id"],
                            reply_to=ref.get("message_id") if ref.get("channel_id", cid) == cid else None)
                        oc = res.get("outcome")
                        stats["ideas" if oc == "tracking" else "updates" if oc == "updated" else "ignored"] += 1
                    else:
                        stats["ignored"] += 1
                self.store.set_kv(key, m["id"])
                self.processed += 1
        self.last_poll = time.time()
        return stats

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if not self.enabled:
            return
        self.running = True
        self._stop.clear()
        backoff = self.s.ext_poll_seconds
        try:
            while not self._stop.is_set():
                try:
                    await self.poll_once()
                    backoff = self.s.ext_poll_seconds
                except asyncio.CancelledError:
                    raise
                except ABGError as e:
                    backoff = min(max(backoff * 2, 30), 600)
                    self.errors.append({"ts": time.time(), "where": "discord poll", "error": e.message[:200]})
                except Exception as e:  # never let the poller die
                    log.exception("discord poll failed")
                    backoff = min(max(backoff * 2, 30), 600)
                    self.errors.append({"ts": time.time(), "where": "discord poll", "error": f"{type(e).__name__}: {e}"[:200]})
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.running = False

    def describe(self) -> dict:
        return {"enabled": self.enabled, "running": self.running, "channels": self.channels,
                "names": self.names, "last_poll": self.last_poll, "processed": self.processed,
                "errors": list(self.errors)[-5:],
                "hint": None if self.enabled else "set ABG_DISCORD_BOT_TOKEN and ABG_EXT_DISCORD_CHANNEL_IDS"}
