"""Live MNQ signal bot: runs ``AlphaStrategy`` on today's session and posts each event to Discord.

Data feed (``ABG_ALPHA_FEED``):

* ``proxy`` (default) - real-time QQQ trades from the Finnhub websocket, built into 1-minute bars and
  converted to MNQ points with a calibrated ratio.  Free CME futures data on Yahoo is ~10 minutes
  delayed, which is useless for entries, but QQQ tracks the Nasdaq-100 tick for tick during RTH.  The
  ratio comes from yesterday's 15:59 bars of both (the futures/ETF basis drifts by well under a point a
  day) and is re-checked against the delayed MNQ feed every 30 minutes; levels are therefore accurate to
  a couple of points - check them against your own MNQ chart.
* ``direct`` - poll 1-minute MNQ bars from the configured providers (only real-time if your provider
  is; Yahoo's are delayed).

Levels (yesterday's high/low/close, overnight high/low) and the ATR warm-up come from the futures
bars themselves, where a 10-minute delay doesn't matter.  Destination: ``ABG_ALPHA_WEBHOOK_URL`` or a
bot token + ``ABG_ALPHA_CHANNEL_ID`` (bot mode threads management replies under the signal and edits
cancelled ideas, like a member expects).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import statistics
import threading
import time as _time
from collections import deque
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from .context import build_context, calendar_events
from .data import NY, Bar, is_rth, iter_bars, rth_daily, split_sessions, utc_naive_to_et
from .learn import AdaptiveBook
from .messages import Renderer
from .strategy import AlphaParams, AlphaStrategy

log = logging.getLogger(__name__)
API = "https://discord.com/api/v10"


# =========================================================================== storage
class AlphaStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, day TEXT, type TEXT, data TEXT, msg TEXT, delivered TEXT);
CREATE INDEX IF NOT EXISTS ev_day ON events(day);
CREATE TABLE IF NOT EXISTS trades (day TEXT, num INTEGER, data TEXT, PRIMARY KEY(day, num));
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
""")

    def add_event(self, day: str, ev: dict, msg: str | None, delivered: str) -> None:
        with self._lock:
            self.db.execute("INSERT INTO events (ts, day, type, data, msg, delivered) VALUES (?,?,?,?,?,?)",
                            (_time.time(), day, ev["type"], json.dumps(ev, default=str), msg, delivered))
            self.db.commit()

    def save_trade(self, day: str, t: dict) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO trades (day, num, data) VALUES (?,?,?)", (day, t["id"], json.dumps(t, default=str)))
            self.db.commit()

    def trades(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute("SELECT day, data FROM trades ORDER BY day DESC, num DESC LIMIT ?", (limit,)).fetchall()
        return [{**json.loads(d), "date": day} for day, d in rows]

    def events(self, day: str | None = None, limit: int = 200) -> list[dict]:
        q = "SELECT ts, type, data, delivered FROM events " + ("WHERE day=? " if day else "") + "ORDER BY id DESC LIMIT ?"
        rows = self.db.execute(q, (day, limit) if day else (limit,)).fetchall()
        return [{"at": ts, "type": ty, "data": json.loads(d), "delivered": dl} for ts, ty, d, dl in rows]

    def get(self, k: str):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None

    def set(self, k: str, v) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?,?)", (k, json.dumps(v, default=str)))
            self.db.commit()


# =========================================================================== Discord
class AlphaPoster:
    def __init__(self, settings, http):
        self.s, self.http = settings, http
        self.webhook = settings.alpha_webhook_url
        self.token = settings.discord_bot_token
        self.channel = settings.alpha_channel_id
        self.mode = "bot" if (self.token and self.channel) else "webhook" if self.webhook else None
        self.errors: deque = deque(maxlen=10)
        self.sent = 0

    @property
    def enabled(self) -> bool:
        return self.mode is not None

    async def post(self, content: str | None, embed: dict, reply_to: str | None = None) -> str | None:
        if not self.mode:
            return None
        body = {"embeds": [embed], "allowed_mentions": {"parse": [], "roles": [self.s.alpha_mention_role_id]}
                if self.s.alpha_mention_role_id else {"parse": []}}
        if content:
            body["content"] = content
        try:
            if self.mode == "bot":
                if reply_to:
                    body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
                r = await self.http.request("POST", f"{API}/channels/{self.channel}/messages", provider="discord",
                                            json=body, headers={"Authorization": f"Bot {self.token}"})
            else:
                body["username"] = "ABG MNQ Bot"
                r = await self.http.request("POST", self.webhook + ("&" if "?" in self.webhook else "?") + "wait=true",
                                            provider="discord", json=body)
            self.sent += 1
            return str(r.json().get("id"))
        except Exception as e:
            self.errors.append({"ts": _time.time(), "error": f"{type(e).__name__}: {e}"[:200]})
            log.warning("alpha post failed: %s", e)
            return None

    async def edit(self, msg_id: str, embed: dict) -> bool:
        if not self.mode or not msg_id:
            return False
        try:
            if self.mode == "bot":
                await self.http.request("PATCH", f"{API}/channels/{self.channel}/messages/{msg_id}", provider="discord",
                                        json={"embeds": [embed]}, headers={"Authorization": f"Bot {self.token}"})
            else:
                await self.http.request("PATCH", f"{self.webhook.split('?')[0]}/messages/{msg_id}", provider="discord",
                                        json={"embeds": [embed]})
            return True
        except Exception as e:
            self.errors.append({"ts": _time.time(), "error": f"edit: {e}"[:200]})
            return False


# =========================================================================== proxy bars
class MinuteBuilder:
    """Ticks -> 1-minute bars (ET).  ``ratio`` converts proxy prices to MNQ points."""

    def __init__(self):
        self.cur: dict | None = None

    def add(self, ts: float, price: float, hi: float, lo: float, n: int = 1) -> Bar | None:
        t = datetime.fromtimestamp(ts, NY).replace(second=0, microsecond=0)
        done = None
        c = self.cur
        if c is not None and t > c["ts"]:
            done = self.close()
            c = None
        if c is None:
            self.cur = {"ts": t, "open": price, "high": max(price, hi), "low": min(price, lo), "close": price, "n": n}
        elif t == c["ts"]:
            c["high"], c["low"], c["close"] = max(c["high"], hi, price), min(c["low"], lo, price), price
            c["n"] += n
        return done

    def close(self) -> Bar | None:
        c, self.cur = self.cur, None
        if not c:
            return None
        return Bar(c["ts"], c["open"], c["high"], c["low"], c["close"], float(c["n"]), 1)

    def stale(self, now: datetime) -> bool:
        return self.cur is not None and now >= self.cur["ts"] + timedelta(minutes=1, seconds=3)


def _scale(b: Bar, ratio: float, tick: float = 0.25) -> Bar:
    r = lambda x: round(x * ratio / tick) * tick  # noqa: E731
    return Bar(b.ts, r(b.open), r(b.high), r(b.low), r(b.close), b.volume, b.minutes)


# =========================================================================== bot
class AlphaBot:
    def __init__(self, engine, settings, hub=None):
        self.engine, self.s, self.hub = engine, settings, hub
        self.data_dir = Path(settings.data_dir).expanduser()
        self.store = AlphaStore(self.data_dir / "alpha.sqlite3")
        self.params_path = self.data_dir / "alpha_params.json"
        self.learner_path = self.data_dir / "alpha_learner.json"
        self.params = self.load_params()
        self.learner = AdaptiveBook.load(self.learner_path)
        self.renderer = Renderer(settings.alpha_tag, settings.alpha_mention_role_id, 2.0, settings.alpha_contracts)
        self.poster = AlphaPoster(settings, engine.http)
        self.feed = (settings.alpha_feed or "proxy").lower()
        self.proxy = settings.alpha_proxy_symbol
        self.symbol = settings.alpha_symbol
        self.builder = MinuteBuilder()
        self.strat: AlphaStrategy | None = None
        self.day: date | None = None
        self.ratio: float | None = None
        self.ratio_src: str | None = None
        self.ratio_at = 0.0
        self.brief: dict | None = None
        self.brief_posted = False
        self.ended = False
        self.last_bar: datetime | None = None
        self.last_tick = 0.0
        self.msg_ids: dict[str, str] = {}          # "idea:3" / "trade:3" -> Discord message id
        self.open_trade: dict | None = None
        self.today_events: deque = deque(maxlen=200)
        self.errors: deque = deque(maxlen=20)
        self.notes: list[str] = []
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ config
    def load_params(self) -> AlphaParams:
        try:
            d = json.loads(self.params_path.read_text())
            return AlphaParams.from_dict(d.get("params", d))
        except Exception:
            return AlphaParams()

    def stream_symbols(self) -> list[str]:
        return [self.proxy] if self.feed == "proxy" and self.proxy else []

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick(datetime.now(NY))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never kill the monitor
                log.exception("alpha tick failed")
                self.errors.append({"ts": _time.time(), "error": f"{type(e).__name__}: {e}"[:300]})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def tick(self, now: datetime) -> None:
        from ..live.market_hours import is_trading_day
        if not is_trading_day(now.date()):
            return
        t = now.time()
        if t >= time(9, 10) and self.day != now.date() and t < time(16, 0):
            await self.prepare_day(now.date())
        if self.strat is None or self.day != now.date():
            return
        if t >= time(9, 25) and not self.brief_posted and self.brief:
            self.brief_posted = True
            if self.s.alpha_post_brief:
                await self.emit(self.brief)
        if self.feed == "proxy" and (self.ratio is None or _time.time() - self.ratio_at > 1800) and t >= time(9, 15):
            await self.calibrate()
        if time(9, 30) <= t < time(16, 1):
            if self.feed == "direct" or (t >= time(9, 33) and _time.time() - self.last_tick > 180):
                if self.feed == "proxy" and "stream quiet" not in self.notes:
                    self.notes.append("stream quiet")
                    log.warning("alpha: proxy stream silent - polling %s (delayed data)", self.symbol)
                await self.poll_direct(now)
            elif self.builder.stale(now):
                b = self.builder.close()
                if b:
                    await self.process(b)
        if t >= time(16, 1) and not self.ended:
            await self.end_day()

    # ------------------------------------------------------------------ day setup
    async def _bars(self, symbol: str, interval: str, period: str) -> pd.DataFrame:
        res = await self.engine.history(symbol, period=period, interval=interval, use_cache=False)
        return utc_naive_to_et(getattr(res, "value", res).df)   # engine returns Fetched(value=PriceHistory)

    async def prepare_day(self, d: date) -> None:
        self.day, self.brief_posted, self.ended = d, False, False
        self.msg_ids, self.open_trade, self.notes = {}, None, []
        self.today_events.clear()
        self.builder = MinuteBuilder()
        self.last_bar = None
        self.params = self.load_params()
        self.learner = AdaptiveBook.load(self.learner_path)
        bars = pd.DataFrame()
        try:
            bars = await self._bars(self.symbol, "5m", "15d")
        except Exception as e:
            self.errors.append({"ts": _time.time(), "error": f"levels: {e}"[:300]})
        sessions = split_sessions(bars) if len(bars) else {}
        today = sessions.get(d, pd.DataFrame())
        prior = {k: v for k, v in sessions.items() if k < d}
        daily = rth_daily(pd.concat(prior.values())) if prior else pd.DataFrame()
        pre = today[[x.time() < time(9, 30) or x.time() >= time(18, 0) for x in today.index]] if len(today) else None
        ctx = build_context(d, daily, pre if pre is not None and len(pre) else None, calendar_events(d, self.s))
        from .context import apply_orx, orx_shadow
        history = []
        for k in sorted(prior):
            g = prior[k]
            r = g[[is_rth(x) for x in g.index]]
            if len(r) >= 30:
                history.append({"date": str(k), **orx_shadow(r, self.params)})
        apply_orx(ctx, history, self.params)
        warm = []
        if prior:
            g = prior[max(prior)]
            warm = list(iter_bars(g[[is_rth(x) for x in g.index]], 5))
        self.strat = AlphaStrategy(self.params, self.learner)
        evs = self.strat.start_day(ctx, warm)
        self.brief = evs[0]
        self.store.set("today", {"day": str(d), "ctx": ctx.to_dict()})

    async def calibrate(self) -> None:
        """MNQ/proxy price ratio from same-minute closes (yesterday 15:59, or today's delayed bars)."""
        self.ratio_at = _time.time()
        try:
            f = await self._bars(self.symbol, "1m", "5d")
            q = await self._bars(self.proxy, "1m", "5d")
        except Exception as e:
            self.errors.append({"ts": _time.time(), "error": f"calibrate: {e}"[:300]})
            return
        j = f[["close"]].join(q[["close"]], lsuffix="_f", rsuffix="_q", how="inner").dropna()
        j = j[[is_rth(x) for x in j.index]]
        if len(j) < 5:
            return
        tail = j.tail(30)
        self.ratio = float(statistics.median(tail["close_f"] / tail["close_q"]))
        self.ratio_src = f"median of {len(tail)} same-minute bars ending {tail.index[-1]:%m-%d %H:%M} ET"

    # ------------------------------------------------------------------ feeds
    async def on_ticks(self, batch: dict[str, dict]) -> None:
        q = batch.get(self.proxy)
        if not q or self.feed != "proxy" or self.strat is None:
            return
        self.last_tick = _time.time()
        b = self.builder.add(q.get("ts") or _time.time(), q["price"], q.get("tick_high", q["price"]),
                             q.get("tick_low", q["price"]), q.get("n", 1))
        if b is not None:
            await self.process(b)

    async def poll_direct(self, now: datetime) -> None:
        try:
            df = await self._bars(self.symbol, "1m", "1d")
        except Exception as e:
            self.errors.append({"ts": _time.time(), "error": f"poll: {e}"[:300]})
            return
        for b in iter_bars(df, 1):
            if b.ts.date() != self.day or not is_rth(pd.Timestamp(b.ts)) or b.end > now:
                continue
            if self.last_bar is None or b.ts > self.last_bar:
                await self.process(b, scaled=True)

    async def process(self, bar: Bar, scaled: bool = False) -> None:
        if self.strat is None or (self.last_bar is not None and bar.ts <= self.last_bar):
            return
        if not scaled:
            if not self.ratio:
                return
            bar = _scale(bar, self.ratio)
        self.last_bar = bar.ts
        for ev in self.strat.on_bar(bar):
            await self.emit(ev)

    async def end_day(self) -> None:
        self.ended = True
        if self.strat is None:
            return
        for ev in self.strat.end_day():
            await self.emit(ev)
        self.learner.save(self.learner_path)

    # ------------------------------------------------------------------ output
    async def emit(self, ev: dict) -> None:
        day = str(self.day)
        kind = ev["type"]
        if kind in ("idea", "idea_cancel") and not self.s.alpha_post_ideas:
            self.store.add_event(day, ev, None, "muted")
            return
        r = self.renderer.render(ev)
        msg, delivered = None, "dashboard"
        if r is not None and self.poster.enabled:
            content, emb = r
            if kind == "idea_cancel" and self.msg_ids.get(f"idea:{ev['id']}"):
                ok = await self.poster.edit(self.msg_ids[f"idea:{ev['id']}"], emb)
                delivered = "edited" if ok else "edit failed"
            else:
                reply = None
                tid = ev.get("id") or (ev.get("trade") or {}).get("id")
                if kind not in ("signal", "idea", "brief", "done", "session_closed", "day_summary") and tid:
                    reply = self.msg_ids.get(f"trade:{tid}")
                elif kind == "signal":
                    reply = self.msg_ids.get(f"idea:{tid}")
                msg = await self.poster.post(content, emb, reply)
                delivered = "ok" if msg else "failed"
                if msg and kind == "idea":
                    self.msg_ids[f"idea:{ev['id']}"] = msg
                if msg and kind == "signal":
                    self.msg_ids[f"trade:{tid}"] = msg
        if kind == "signal":
            self.open_trade = ev["trade"]
        if kind in ("final", "stopped", "breakeven", "closed"):
            self.open_trade = None
            self.store.save_trade(day, ev["trade"])
        self.store.add_event(day, ev, msg, delivered)
        self.today_events.append({"type": kind, "ts": str(ev.get("ts") or datetime.now(NY)), "delivered": delivered,
                                  "id": ev.get("id") or (ev.get("trade") or {}).get("id")})
        if self.hub is not None:
            try:
                self.hub.broadcast("alpha", {"type": kind, "event": json.loads(json.dumps(ev, default=str))})
            except Exception:
                pass

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        st = self.strat
        return {"enabled": True, "feed": self.feed, "symbol": self.symbol, "proxy": self.proxy if self.feed == "proxy" else None,
                "ratio": self.ratio, "ratio_src": self.ratio_src, "day": str(self.day) if self.day else None,
                "last_bar": str(self.last_bar) if self.last_bar else None,
                "stream_age_s": round(_time.time() - self.last_tick, 1) if self.last_tick else None,
                "discord": {"mode": self.poster.mode, "sent": self.poster.sent, "errors": list(self.poster.errors)[-3:]},
                "brief": self.brief, "open_trade": self.open_trade,
                "done_reason": st.done_reason if st else None,
                "levels": {"OR": [st.or_lo, st.or_hi] if st else None, "VWAP": round(st.vwap, 2) if st and st.vwap else None,
                           "ATR5": round(st.atr5, 2) if st and st.atr5 else None},
                "params": self.params.to_dict(), "learner": self.learner.summary(),
                "events": list(self.today_events)[-30:], "errors": list(self.errors)[-5:], "notes": self.notes}

    # ------------------------------------------------------------------ weekly re-fit
    async def optimize(self, save: bool = True) -> dict:
        from .backtest import walk_forward
        bars = await self._bars(self.symbol, "5m", "60d")
        res = await asyncio.to_thread(walk_forward, bars, self.params, min_win_rate=self.s.alpha_min_win_rate)
        ts, tst = res["test_stats"], res["default_test_stats"]
        from .backtest import worth_adopting
        better = worth_adopting(ts, tst)
        res["adopted"] = bool(save and better)
        if res["adopted"]:
            self.params_path.write_text(json.dumps({"params": res["params"], "fitted": str(datetime.now(NY)),
                                                    "test_stats": {k: ts.get(k) for k in ("trades", "avg_r", "net_pts", "win_rate")}},
                                                   indent=1))
            self.params = AlphaParams.from_dict(res["params"])
        self.store.set("last_optimize", {"at": str(datetime.now(NY)), "adopted": res["adopted"], "best": res["best"],
                                         "test": {k: ts.get(k) for k in ("trades", "avg_r", "net_pts", "win_rate")}})
        return res
