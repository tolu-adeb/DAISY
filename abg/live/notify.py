"""Notification channels and the hub that fans signals out to them.

Channels
--------
dashboard  always on: signals are saved to the portfolio DB and pushed to every open
           dashboard over Server-Sent Events (the dashboard can raise browser pop-ups).
desktop    native OS notification via ``plyer`` (optional dependency).
discord    Discord channel webhook (``ABG_DISCORD_WEBHOOK_URL``), one embed per signal.
email      SMTP (``ABG_SMTP_*``); non-critical signals are batched into one digest per
           ``ABG_EMAIL_BATCH_SECONDS`` window, critical ones are sent immediately.
console    prints to the monitor's terminal.

Every channel has a minimum severity.  Delivery runs in the background, is isolated per
channel (a failing webhook never blocks email) and its outcome is recorded on the signal row.
"""
from __future__ import annotations

import asyncio
import html
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from typing import Protocol

from ..errors import ABGError
from ..http import HttpClient
from ..portfolio.store import PortfolioStore
from ..resilience import RateLimiter
from .signals import SEVERITY_RANK, Signal

log = logging.getLogger(__name__)
COLORS = {"bullish": 0x1BAF7A, "bearish": 0xE34948, "neutral": 0x9A9892}


class Notifier(Protocol):
    name: str
    min_severity: str

    async def send(self, sig: Signal) -> str: ...
    async def aclose(self) -> None: ...


def _allowed(sig: Signal, min_sev: str) -> bool:
    return SEVERITY_RANK.get(sig.severity, 0) >= SEVERITY_RANK.get(min_sev, 0)


# --------------------------------------------------------------------------- console
class ConsoleNotifier:
    name, min_severity = "console", "info"

    def __init__(self, console=None):
        from rich.console import Console
        self.console = console or Console()

    async def send(self, sig: Signal) -> str:
        style = {"critical": "bold white on red", "warning": "bold yellow", "info": "cyan"}[sig.severity]
        arrow = {"bullish": "▲", "bearish": "▼", "neutral": "•"}[sig.direction]
        self.console.print(f"[dim]{sig.to_dict()['time'][11:19]}[/dim] [{style}] {sig.severity.upper():8}[/{style}] "
                           f"{arrow} [bold]{sig.title}[/bold] [dim]- {sig.message}[/dim]")
        return "ok"

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- desktop
class DesktopNotifier:
    name = "desktop"

    def __init__(self, min_severity: str = "warning"):
        self.min_severity = min_severity
        try:
            from plyer import notification  # type: ignore
            self._n = notification
        except Exception:
            self._n = None

    @property
    def available(self) -> bool:
        return self._n is not None

    async def send(self, sig: Signal) -> str:
        if self._n is None:
            return "skipped: pip install plyer"
        await asyncio.to_thread(self._n.notify, title=sig.title[:63], message=sig.message[:250],
                                app_name="ABG Terminal", timeout=10)
        return "ok"

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- discord
class DiscordNotifier:
    name = "discord"

    def __init__(self, webhook_url: str, http: HttpClient, min_severity: str = "info"):
        self.url = webhook_url
        self.http = http
        self.min_severity = min_severity
        self.limiter = RateLimiter(25, 60.0, provider="discord")     # Discord allows ~30/min per webhook

    def payload(self, sig: Signal) -> dict:
        fields = []
        for k in ("price", "change_pct", "score", "rsi", "level", "stop", "target", "pnl_pct", "risk_score"):
            v = sig.data.get(k)
            if isinstance(v, (int, float)):
                fields.append({"name": k.replace("_", " ").title(), "value": f"{v:,.2f}", "inline": True})
        prefix = {"critical": "[CRITICAL] ", "warning": "", "info": ""}[sig.severity]
        return {"username": "ABG Terminal",
                "embeds": [{"title": (prefix + sig.title)[:256], "description": sig.message[:2000],
                            "color": COLORS[sig.direction] if sig.severity != "critical" else 0xD03B3B,
                            "fields": fields[:6],
                            "footer": {"text": f"{sig.kind} · {sig.severity} · ABG Intelligence Terminal · not investment advice"},
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(sig.ts))}]}

    async def send(self, sig: Signal) -> str:
        await self.limiter.acquire(max_wait=30)
        for attempt in range(3):
            try:
                await self.http.request("POST", self.url, provider="discord", json=self.payload(sig))
                return "ok"
            except ABGError as e:
                retry_after = getattr(e, "retry_after", None)
                if retry_after and attempt < 2:
                    await asyncio.sleep(min(float(retry_after), 10))
                    continue
                raise
        return "failed"

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- email
class EmailNotifier:
    name = "email"

    def __init__(self, settings, min_severity: str = "warning", batch_seconds: float = 120):
        self.s = settings
        self.min_severity = min_severity
        self.batch_seconds = batch_seconds
        self.to = [a.strip() for a in (settings.email_to or "").split(",") if a.strip()]
        self._buffer: list[Signal] = []
        self._flush_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def send(self, sig: Signal) -> str:
        async with self._lock:
            self._buffer.append(sig)
            if sig.severity == "critical" or self.batch_seconds <= 0:
                return await self._flush_locked()
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.ensure_future(self._delayed_flush())
        return "queued"

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(self.batch_seconds)
        async with self._lock:
            try:
                await self._flush_locked()
            except Exception as e:
                log.warning("email digest failed: %s", e)

    async def _flush_locked(self) -> str:
        if not self._buffer:
            return "empty"
        batch, self._buffer = self._buffer, []
        msg = self.build(batch)
        await asyncio.to_thread(self._smtp_send, msg)
        return "ok"

    def build(self, batch: list[Signal]) -> EmailMessage:
        crit = any(s.severity == "critical" for s in batch)
        subj = batch[0].title if len(batch) == 1 else f"{len(batch)} ABG signals: " + ", ".join(
            sorted({s.symbol for s in batch}))[:120]
        msg = EmailMessage()
        msg["Subject"] = ("[CRITICAL] " if crit else "[ABG] ") + subj
        msg["From"] = self.s.email_from or self.s.smtp_user
        msg["To"] = ", ".join(self.to)
        text = "\n\n".join(f"{s.to_dict()['time']}  {s.severity.upper()}  {s.title}\n{s.message}" for s in batch)
        msg.set_content(text + "\n\n-- ABG Intelligence Terminal (educational, not investment advice)")
        rows = "".join(
            f"<tr><td style='padding:6px;color:#666'>{html.escape(s.to_dict()['time'][11:16])}</td>"
            f"<td style='padding:6px'><b style='color:{'#d03b3b' if s.severity == 'critical' else '#b7791f' if s.severity == 'warning' else '#555'}'>"
            f"{s.severity.upper()}</b></td><td style='padding:6px'><b>{html.escape(s.title)}</b><br>"
            f"<span style='color:#444'>{html.escape(s.message)}</span></td></tr>" for s in batch)
        msg.add_alternative(f"<html><body style='font-family:Segoe UI,Arial,sans-serif'><h3>ABG Intelligence Terminal</h3>"
                            f"<table style='border-collapse:collapse'>{rows}</table><p style='color:#888;font-size:12px'>"
                            f"Educational analysis, not investment advice.</p></body></html>", subtype="html")
        return msg

    def _smtp_send(self, msg: EmailMessage) -> None:
        ctx = ssl.create_default_context()
        if self.s.smtp_ssl:
            with smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, context=ctx, timeout=20) as srv:
                if self.s.smtp_user:
                    srv.login(self.s.smtp_user, self.s.smtp_password or "")
                srv.send_message(msg)
        else:
            with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=20) as srv:
                srv.ehlo()
                srv.starttls(context=ctx)
                srv.ehlo()
                if self.s.smtp_user:
                    srv.login(self.s.smtp_user, self.s.smtp_password or "")
                srv.send_message(msg)

    async def aclose(self) -> None:
        async with self._lock:
            if self._flush_task and not self._flush_task.done():
                self._flush_task.cancel()
            if self._buffer:
                try:
                    await self._flush_locked()
                except Exception as e:
                    log.warning("final email flush failed: %s", e)


# --------------------------------------------------------------------------- hub
class NotificationHub:
    """Persists signals, pushes them to live dashboard subscribers and fans out to channels."""

    def __init__(self, settings, store: PortfolioStore, http: HttpClient, extra: list | None = None):
        self.s = settings
        self.store = store
        self.channels: list = []
        self.subscribers: set[asyncio.Queue] = set()
        self._tasks: set[asyncio.Task] = set()
        if settings.notify_desktop:
            d = DesktopNotifier(settings.notify_desktop_min_severity)
            if d.available:
                self.channels.append(d)
        if settings.discord_webhook_url:
            self.channels.append(DiscordNotifier(settings.discord_webhook_url, http, settings.notify_discord_min_severity))
        if settings.smtp_host and settings.email_to:
            self.channels.append(EmailNotifier(settings, settings.notify_email_min_severity, settings.email_batch_seconds))
        self.channels.extend(extra or [])

    def describe(self) -> list[dict]:
        out = [{"name": "dashboard", "min_severity": "info", "configured": True}]
        out += [{"name": c.name, "min_severity": c.min_severity, "configured": True} for c in self.channels]
        names = {c.name for c in self.channels}
        if "desktop" not in names:
            out.append({"name": "desktop", "configured": False,
                        "hint": "pip install plyer" if self.s.notify_desktop else "ABG_NOTIFY_DESKTOP=false"})
        if "discord" not in names:
            out.append({"name": "discord", "configured": False, "hint": "set ABG_DISCORD_WEBHOOK_URL"})
        if "email" not in names:
            out.append({"name": "email", "configured": False, "hint": "set ABG_SMTP_HOST, ABG_SMTP_USER, ABG_SMTP_PASSWORD, ABG_EMAIL_TO"})
        return out

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def broadcast(self, event: str, payload: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait({"event": event, "data": payload})
            except asyncio.QueueFull:       # slow client: drop oldest
                try:
                    q.get_nowait()
                    q.put_nowait({"event": event, "data": payload})
                except Exception:
                    pass

    async def publish(self, sig: Signal, persist: bool = True, skip: set[str] | None = None) -> Signal:
        """``skip``: channel names not to deliver to (e.g. external-signal events are relayed to
        Discord by their own relay, so the generic Discord embed is skipped)."""
        if persist:
            sig.id = self.store.save_signal(sig.to_dict())
        self.broadcast("signal", sig.to_dict())
        for ch in self.channels:
            if skip and ch.name in skip:
                continue
            if _allowed(sig, ch.min_severity):
                t = asyncio.ensure_future(self._deliver(ch, sig))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
        return sig

    async def _deliver(self, ch, sig: Signal) -> None:
        try:
            status = await asyncio.wait_for(ch.send(sig), timeout=60)
        except Exception as e:
            status = f"error: {getattr(e, 'message', None) or type(e).__name__}: {str(e)[:120]}"
            log.warning("%s delivery failed for %s: %s", ch.name, sig.title, status)
        if sig.id is not None:
            self.store.mark_delivered(sig.id, ch.name, status)

    async def test(self, channel: str | None = None) -> dict:
        sig = Signal("TEST", "test", str(time.time()), "critical" if channel == "email" else "warning", "neutral",
                     "ABG test alert", "If you can read this, notifications are working.", portfolio=self.s.default_portfolio)
        results = {}
        for ch in self.channels:
            if channel in (None, "all", ch.name):
                try:
                    results[ch.name] = await asyncio.wait_for(ch.send(sig), timeout=60)
                except Exception as e:
                    results[ch.name] = f"error: {getattr(e, 'message', None) or type(e).__name__}: {str(e)[:160]}"
        self.broadcast("signal", {**sig.to_dict(), "test": True})
        results["dashboard"] = f"pushed to {len(self.subscribers)} open dashboard(s)"
        return results

    async def aclose(self) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=10)
        for ch in self.channels:
            try:
                await ch.aclose()
            except Exception:
                pass
