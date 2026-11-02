"""The swing desk inside the MNQ Bot monitor (docs/19).

    first start            seed: grade + replay every recorded idea silently (history is never posted)
    weekdays 9:30-16:00    every ABG_SWING_INTERVAL_MIN (30): refresh daily bars + quotes, replay open ideas,
                           alert on changes (graded, in zone, filled, target, stop, cancelled, expired)
    weekdays 16:20 ET      after-close pass: scanner (config/universe.txt), final bars, daily digest (+ weekly on Friday)
    Sundays 11:00 ET       R&D agent (only with ABG_SWING_RESEARCH=weekly and an API key) -> proposal for you to review

The replay is the source of truth (``swingdesk.monitor.update``, unchanged): each pass re-simulates every open idea
from its post time with the same engine as the research and diffs the result against the saved book, so re-running
never repeats an alert.  Everything that touches money comes from ``swingdesk.strategy.grade``.

Discord: alerts go to ``ABG_SWING_WEBHOOK_URL`` when it is set, otherwise they stay on the dashboard.  The swing desk
isn't part of the terminal, so the MNQ shadow-mode guard doesn't apply to it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from abg.swing import DESK_ROOT, use_home
from abg.swing.intake import normalize, parse_text, validate
from abg.swing.wide_desk import WideDeskMixin

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
CLOSED = ("WIN", "LOSS", "SCRATCH")
COLORS = {"GRADED": 0x5865F2, "IN_ZONE": 0xF1C40F, "FILLED": 0x2ECC71, "TP_HIT": 0x1ABC9C, "STOPPED": 0xE74C3C,
          "CANCELLED": 0x95A5A6, "EXPIRED": 0x95A5A6, "RESEARCH": 0x9B59B6, "DIGEST": 0x3B82F6, "PITCH": 0x3498DB}
# the variants the audit compared (docs/19 and the House of Traders report) - recomputed on your live data
VARIANTS = [
    ("v1 as configured", {}),
    ("gate needs both conditions", {"gate.require": "all"}),
    ("ATR limit 3.5%", {"gate.max_atr_pct": 3.5}),
    ("ATR limit 4.5%", {"gate.max_atr_pct": 4.5}),
    ("SPY vs 50-day", {"gate.spy_below_sma": 50}),
    ("take profit 0.5R", {"exit.take_profit_r": 0.5}),
    ("take profit 1.0R", {"exit.take_profit_r": 1.0}),
    ("time stop 20 sessions", {"exit.time_stop_bars": 20}),
    ("chase up to 1 ATR", {"entry.chase_atr": 1.0}),         # buy the next open after a close just above the zone
]


def _hm(s: str) -> int:
    h, m = (s or "16:20").split(":")
    return int(h) * 60 + int(m)


def _f(x):
    try:
        v = float(x)
        return None if v != v else v
    except (TypeError, ValueError):
        return None


def sig_key(row: dict) -> str:
    return f"{row['symbol']}@{row['posted_et']}"


class SwingDesk(WideDeskMixin):
    def __init__(self, settings, hub=None, http=None, fetch: bool = True):
        self.s, self.hub, self.http, self.fetch = settings, hub, http, fetch
        self.home = use_home(Path(settings.data_dir).expanduser() / "swing")
        self.state_path = self.home / "state" / "desk.json"
        self.alerts_path = self.home / "state" / "alerts.jsonl"
        self.reports = self.home / "research" / "reports"
        self.errors: deque = deque(maxlen=20)
        self.busy: str | None = None
        self._lock: asyncio.Lock | None = None
        self._stop = asyncio.Event()
        self._px: dict[str, tuple[float, pd.DataFrame]] = {}
        self._variants: tuple[str, list] | None = None
        if settings.swing_use_twelvedata and settings.twelvedata_api_key:
            os.environ.setdefault("TWELVEDATA_API_KEY", settings.twelvedata_api_key)
        self._wide_init()

    # ------------------------------------------------------------------ small state
    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _set(self, **kw) -> dict:
        st = {**self.state(), **kw}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, default=str))
        os.replace(tmp, self.state_path)
        return st

    def _once(self, key: str) -> bool:
        st = self.state()
        jobs = st.get("jobs", {})
        if key in jobs:
            return False
        jobs[key] = time.time()
        self._set(jobs=dict(list(jobs.items())[-200:]))
        return True

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ data
    def signals(self, include_wide: bool = False, only_wide: bool = False) -> pd.DataFrame:
        """Channel + manual + core-scanner ideas; ``include_wide`` adds the wide scan's (docs/19.8)."""
        from swingdesk import cli
        paths = [] if only_wide else [cli.SIGNALS, cli.SCANNER]
        if include_wide or only_wide:
            paths.append(self.wide_csv)
        frames = [pd.read_csv(p) for p in paths if Path(p).exists()]
        if not frames:
            return pd.DataFrame(columns=cli.COLS)
        df = pd.concat(frames, ignore_index=True)
        if "flags" in df.columns:          # "binary_event;..." -> list (grade() reads sig["flags"])
            df["flags"] = df["flags"].apply(lambda x: [f for f in x.split(";") if f] if isinstance(x, str) else [])
        return df

    def cached(self, sym: str) -> pd.DataFrame | None:
        from swingdesk import data_sources as ds
        from swingdesk.backtest import load_prices
        p = ds.price_file(sym)
        try:
            mt = p.stat().st_mtime
        except OSError:
            return None
        hit = self._px.get(sym)
        if hit and hit[0] == mt:
            return hit[1]
        df = load_prices(p)
        self._px[sym] = (mt, df)
        return df

    @staticmethod
    def _last_session(now: datetime | None = None) -> date:
        """The most recent session whose daily bar is final (today after 16:00 ET, else the previous trading day)."""
        from abg.live.market_hours import is_trading_day
        now = now or datetime.now(NY)
        d = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
        while not is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def _prices(self, syms, fetch: bool, fresh_ok: bool = False) -> dict:
        """Daily bars per symbol (+ SPY).  ``fetch`` downloads new sessions; ``fresh_ok`` skips the download when the
        cache already has the last final session (previews: answer in milliseconds, not seconds)."""
        from swingdesk import data_sources as ds
        out = {}
        last = self._last_session() if fresh_ok else None
        for s in sorted(set(syms) | {"SPY"}):
            try:
                c = self.cached(s) if fresh_ok else None
                if c is not None and len(c) and c.index[-1].date() >= last:
                    df = c
                else:
                    df = ds.load_or_update(s) if fetch else self.cached(s)
            except Exception as e:
                df = self.cached(s)
                if df is None:
                    self.errors.append({"ts": time.time(), "where": f"prices {s}", "error": f"{type(e).__name__}: {e}"[:200]})
            if df is not None and len(df):
                out[s] = df
        return out

    def _marks(self, prices: dict, quotes: dict | None = None) -> None:
        p = self.home / "state" / "marks.json"
        try:
            marks = json.loads(p.read_text())
        except (OSError, ValueError):
            marks = {}
        now = time.time()
        for s, df in prices.items():
            m = marks.get(s, {})
            m.update(close=float(df["close"].iloc[-1]), date=str(df.index[-1].date()))
            marks[s] = m
        for s, q in (quotes or {}).items():
            if q:
                marks.setdefault(s, {}).update(quote=float(q), quote_at=now)
        p.write_text(json.dumps(marks))

    def marks(self) -> dict:
        try:
            return json.loads((self.home / "state" / "marks.json").read_text())
        except (OSError, ValueError):
            return {}

    # ------------------------------------------------------------------ the pass (worker thread)
    def _pass_sync(self, kind: str, only: set | None = None) -> dict:
        from swingdesk import data_sources as ds
        from swingdesk.monitor import load_book, save_book, update
        st = self.state()
        seed = not st.get("seeded")
        sigs = self.signals(include_wide=not seed)   # the silent first load is the recorded history only
        book = load_book()
        keys = sigs.symbol + "@" + sigs.posted_et
        live_mask = [k not in book or book[k].get("state") in ("WATCHING", "FILLED") for k in keys]
        live = sigs[live_mask]
        if only is not None:
            live = live[(live.symbol + "@" + live.posted_et).isin(only)]
        wide_only = set(live[live.author == "wide"].symbol) - set(live[live.author != "wide"].symbol)
        prices = self._prices([x for x in live.symbol.unique() if x not in wide_only], self.fetch)
        for x in wide_only:                # wide ideas: end-of-day bars from the nightly whole-market download
            c = self.cached(x)
            if c is not None and len(c):
                prices[x] = c
        if "SPY" not in prices:
            raise RuntimeError("no SPY prices - the gate needs them")
        quotes = {}
        if kind == "intraday" and self.fetch:
            watching = {book[k]["symbol"] for k in book if book[k].get("state") == "WATCHING"}
            quotes = {s: ds.live_quote(s) for s in watching if s in set(live.symbol)}
        book, alerts = update(live, prices, prices["SPY"], quotes=quotes, book=book, plans=self.wide_plans())
        save_book(book)
        self._marks(prices, quotes)
        meta = {k: {"author": r.get("author"), "take": (book.get(k, {}).get("verdict") or {}).get("take")}
                for k, r in zip(keys, sigs.to_dict("records"))}
        out = [{**a.as_dict(), "author": (meta.get(a.key) or {}).get("author"),
                "take": (meta.get(a.key) or {}).get("take")} for a in alerts]
        if seed:
            self._set(seeded=time.time())
        return {"alerts": out, "seed": seed, "signals": len(sigs), "live": int(len(live)), "symbols": len(prices)}

    def _scan_sync(self) -> int:
        from swingdesk import cli
        from swingdesk.scanner import find_setups
        uni = DESK_ROOT / "config" / "universe.txt"
        syms = [x.strip() for x in uni.read_text().splitlines() if x.strip() and not x.startswith("#")]
        rows = []
        for sym, px in self._prices(syms, self.fetch).items():
            if sym == "SPY" and "SPY" not in syms:
                continue
            last = str(px.index[-1].date())
            rows += [x for x in find_setups(sym, px) if x["posted_et"].startswith(last)]
        return cli._append(rows, cli.SCANNER) if rows else 0

    # ------------------------------------------------------------------ async wrappers
    async def run_pass(self, kind: str = "manual", only: set | None = None) -> dict:
        async with self.lock:
            self.busy = kind
            try:
                if kind != "seed" and not self.state().get("seeded"):
                    seed = await asyncio.to_thread(self._pass_sync, "seed")
                    await self._deliver(seed["alerts"], seed=True, kind="seed")
                res = await asyncio.to_thread(self._pass_sync, kind, only)
                res["delivered"] = await self._deliver(res["alerts"], seed=res["seed"], kind=kind)
                self._set(**{f"last_{kind}": time.time(), "last_pass": time.time(), "last_kind": kind})
            finally:
                self.busy = None
        self._broadcast({"type": "pass", "kind": kind, "alerts": len(res["alerts"]), "seed": res["seed"]})
        return res

    async def run_scan(self) -> int:
        async with self.lock:
            self.busy = "scanner"
            try:
                n = await asyncio.to_thread(self._scan_sync)
            finally:
                self.busy = None
        self._set(last_scan=time.time(), last_scan_new=n)
        return n

    # ------------------------------------------------------------------ Discord + the alert log
    def _webhook(self) -> str | None:
        return getattr(self.s, "swing_webhook_url", None) or None

    async def _post(self, embed: dict) -> str:
        url = self._webhook()
        if not url:
            return "dashboard"
        if self.http is None:
            return "dashboard (no http client)"
        body = {"username": "Swing Desk", "allowed_mentions": {"parse": []}, "embeds": [embed]}
        try:
            await self.http.request("POST", url + ("&" if "?" in url else "?") + "wait=true", provider="discord", json=body)
            return "posted"
        except Exception as e:
            self.errors.append({"ts": time.time(), "where": "discord", "error": type(e).__name__})
            return "post failed"

    async def _deliver(self, alerts: list[dict], seed: bool, kind: str) -> dict:
        counts: dict[str, int] = {}
        recs = []
        budget = max(0, int(getattr(self.s, "swing_max_posts", 12)))
        overflow: list[dict] = []
        for a in alerts:
            quiet = a["author"] == "scanner" and a["kind"] == "GRADED" and not a.get("take")
            wide_route = self._wide_route(a)
            a = {**a, "text": self._wide_text(a)}
            if seed:
                how = "history (not posted)"
            elif quiet:
                how = "dashboard"
            elif wide_route and wide_route != "post":
                how = wide_route
            elif budget <= 0:
                overflow.append(a)
                how = "summarised"
            else:
                how = await self._post({"title": a["text"].split("\n")[0][:250], "description": "\n".join(a["text"].split("\n")[1:])[:3900],
                                        "color": COLORS.get(a["kind"], 0x99AAB5),
                                        "footer": {"text": f"swing desk · {a.get('author') or ''} · paper"}})
                budget -= how == "posted"
                if how == "posted" and wide_route == "post" and a["kind"] == "GRADED":
                    self._wide_mark_posted([a["key"]])
            counts[how] = counts.get(how, 0) + 1
            recs.append({"at": time.time(), "pass": kind, "kind": a["kind"], "key": a["key"], "symbol": a["symbol"],
                         "author": a.get("author"), "text": a["text"], "delivered": how})
        if overflow:
            await self._post({"title": f"+{len(overflow)} more swing alerts", "color": COLORS["DIGEST"],
                              "description": "\n".join(x["text"].split("\n")[0] for x in overflow)[:3900]})
        if recs:
            with open(self.alerts_path, "a", encoding="utf-8") as fh:
                for r in recs:
                    fh.write(json.dumps(r, default=str) + "\n")
            if not seed:
                for r in recs:
                    self._broadcast({"type": "alert", **{k: r[k] for k in ("kind", "symbol", "text", "delivered", "key")}})
        return counts

    def alerts(self, limit: int = 200, include_history: bool = False) -> list[dict]:
        try:
            lines = self.alerts_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in reversed(lines):
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if not include_history and r.get("pass") == "seed":
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def _broadcast(self, payload: dict) -> None:
        if self.hub is not None:
            try:
                self.hub.broadcast("swing", payload)
            except Exception:
                pass

    # ------------------------------------------------------------------ schedule (inside the monitor)
    async def run(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            try:
                await self.tick(datetime.now(NY))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("swing desk tick failed")
                self.errors.append({"ts": time.time(), "where": "swing tick", "error": f"{type(e).__name__}: {e}"[:300]})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    def schedule(self, now: datetime) -> dict:
        from abg.live.market_hours import is_trading_day
        st = self.state()
        m = now.hour * 60 + now.minute
        trading = is_trading_day(now.date())
        due = []
        if not st.get("seeded"):
            due.append("seed")
        if trading and 9 * 60 + 30 <= m < 16 * 60 and time.time() - st.get("last_intraday", 0) >= 60 * max(5, self.s.swing_interval_min):
            due.append("intraday")
        if trading and m >= _hm(self.s.swing_close_time) and f"close:{now.date()}" not in st.get("jobs", {}):
            due.append("close")
        wk = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
        if (self.s.swing_research or "off").lower() == "weekly" and self.s.anthropic_api_key and now.weekday() == 6 \
                and now.hour >= 11 and f"research:{wk}" not in st.get("jobs", {}):
            due.append("research")
        if self.wide_due(now, st):
            due.append("wide")
        if self.wide_backtest_due(now, st):
            due.append("wide_backtest")
        return {"due": due, "trading_day": trading, "week": wk}

    def next_pass(self, now: datetime) -> str | None:
        from abg.live.market_hours import is_trading_day, next_open
        st = self.state()
        m = now.hour * 60 + now.minute
        if is_trading_day(now.date()) and 9 * 60 + 30 <= m < 16 * 60:
            t = max(time.time(), st.get("last_intraday", 0) + 60 * max(5, self.s.swing_interval_min))
            return datetime.fromtimestamp(t, NY).isoformat(timespec="minutes")
        if is_trading_day(now.date()) and m < _hm(self.s.swing_close_time) and m >= 16 * 60:
            return datetime.combine(now.date(), datetime.min.time(), NY).replace(
                hour=_hm(self.s.swing_close_time) // 60, minute=_hm(self.s.swing_close_time) % 60).isoformat(timespec="minutes")
        return next_open(now).isoformat(timespec="minutes")

    async def tick(self, now: datetime) -> None:
        sch = self.schedule(now)
        for job in sch["due"]:
            if job == "seed":
                await self.run_pass("seed")
            elif job == "intraday":
                await self.run_pass("intraday")
            elif job == "close" and self._once(f"close:{now.date()}"):
                if self.s.swing_scan:
                    try:
                        await self.run_scan()
                    except Exception as e:
                        self.errors.append({"ts": time.time(), "where": "scanner", "error": f"{type(e).__name__}: {e}"[:200]})
                await self.run_pass("close")
                await self.digest("daily")
                if now.weekday() == 4:
                    await self.digest("weekly")
            elif job == "research" and self._once(f"research:{sch['week']}"):
                await self.research()
            elif job == "wide":
                self.wide_start(now.date())
            elif job == "wide_backtest":
                self.wide_backtest_auto(now)

    # ------------------------------------------------------------------ digests and research
    def digest_text(self, period: str = "daily") -> dict:
        from swingdesk.monitor import scoreboard
        sb = scoreboard(self.main_book())          # wide-scan ideas have their own page (docs/19.8)
        days = {"daily": 1, "weekly": 7, "monthly": 31}.get(period, 1)
        since = time.time() - days * 86400 - 3600
        recent = [a for a in self.alerts(500) if a["at"] >= since and a.get("author") != "wide"]
        ex = [a for a in recent if a["kind"] in ("TP_HIT", "STOPPED", "SCRATCH")]
        lines = [f"**Book**: {sb['open']} open · {sb['watching']} working orders · {sb['skipped']} skipped",
                 f"**Record** (taken ideas): {sb['closed']} closed · {sb['wins']} wins"
                 + (f" · **{sb['win_rate']}%**" if sb['win_rate'] is not None else "") + f" · {sb['sum_R']:+.2f}R",
                 f"**This {'session' if period == 'daily' else period[:-2]}**: {len(recent)} alerts · {len(ex)} exits"]
        lines += [f"• {a['text'].splitlines()[0]}" for a in recent[:12]]
        gate = self.gate()
        if gate:
            lines.append(f"**Gate**: SPY {gate['spy_close']:.2f} vs {gate['sma_n']}-day {gate['spy_sma']:.2f} -> market branch "
                         f"{'ON (any ATR passes)' if gate['market_branch'] else 'off (ATR <= ' + str(gate['max_atr_pct']) + '% needed)'}")
        if sb["closed"] >= 10 and sb["win_rate"] is not None and sb["win_rate"] < 61:
            lines.append("⚠️ win rate under the ~61% break-even - the report says pause and review the gate")
        return {"period": period, "title": f"Swing desk {period} digest", "lines": lines, "scoreboard": sb,
                "built": datetime.now(NY).isoformat(timespec="minutes")}

    async def digest(self, period: str = "daily", post: bool = True) -> dict:
        d = await asyncio.to_thread(self.digest_text, period)
        d["delivered"] = await self._post({"title": d["title"], "description": "\n".join(d["lines"])[:3900],
                                           "color": COLORS["DIGEST"]}) if post else "dashboard"
        p = self.home / "state" / f"digest-{period}.json"
        p.write_text(json.dumps(d, default=str))
        self._broadcast({"type": "digest", "period": period, "delivered": d["delivered"]})
        return d

    def _research_sync(self, deep: bool) -> dict:
        if self.s.anthropic_api_key and not os.getenv("ANTHROPIC_API_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = self.s.anthropic_api_key
        from swingdesk.agents.desk import research
        text = research(deep)
        self.reports.mkdir(parents=True, exist_ok=True)
        p = self.reports / f"{datetime.now(NY):%Y-%m-%d-%H%M}{'-deep' if deep else ''}.md"
        p.write_text(text, encoding="utf-8")
        return {"name": p.name, "text": text}

    async def research(self, deep: bool = False) -> dict:
        if not self.s.anthropic_api_key:
            raise RuntimeError("the R&D agent needs ABG_ANTHROPIC_API_KEY in .env")
        async with self.lock:
            self.busy = "research (deep)" if deep else "research"
            try:
                r = await asyncio.to_thread(self._research_sync, deep)
            finally:
                self.busy = None
        r["delivered"] = await self._post({"title": "Swing desk R&D", "description": r["text"][:3900], "color": COLORS["RESEARCH"]})
        self._broadcast({"type": "research", "name": r["name"]})
        return r

    # ------------------------------------------------------------------ ideas from the dashboard
    def _preview_sync(self, fields: dict) -> dict:
        from swingdesk.strategy import grade, load_config
        f = normalize(fields)
        out: dict = {"fields": f, "errors": [], "verdict": None, "last": None}
        if not f["symbol"]:
            out["errors"] = ["ticker is required"]
            return out
        prices = self._prices([f["symbol"]], self.fetch, fresh_ok=True)
        px = prices.get(f["symbol"])
        if px is None:
            out["errors"] = [f"no daily prices for {f['symbol']} - check the ticker"]
            return out
        out["last"] = {"close": float(px["close"].iloc[-1]), "date": str(px.index[-1].date())}
        out["spark"] = [round(float(x), 4) for x in px["close"].tail(40)]
        out["errors"] = validate(f, out["last"]["close"])
        if out["errors"]:
            return out
        sig = {**f, "entry_lo": f["entry_lo"] if f["entry_lo"] is not None else "", "entry_hi": f["entry_hi"] if f["entry_hi"] is not None else ""}
        try:
            out["verdict"] = grade(sig, px, prices["SPY"], load_config()).as_dict()
        except Exception as e:
            out["errors"] = [f"couldn't grade it: {type(e).__name__}: {e}"]
        return out

    async def preview(self, text: str | None = None, fields: dict | None = None) -> dict:
        parsed = None
        if fields is None:
            parsed = await asyncio.to_thread(parse_text, text or "", self.s.anthropic_api_key, self.s.swing_ai_intake)
            fields = parsed.get("fields")
            if not fields:
                return {"parsed": parsed, "fields": None, "errors": [], "verdict": None}
        res = await asyncio.to_thread(self._preview_sync, fields)
        res["parsed"] = parsed
        return res

    def _add_sync(self, fields: dict, author: str | None, posted_et: str | None) -> dict:
        from swingdesk import cli
        prev = self._preview_sync(fields)
        if prev["errors"]:
            raise ValueError("; ".join(prev["errors"]))
        f = prev["fields"]
        posted = posted_et or datetime.now(NY).strftime("%Y-%m-%d %H:%M")
        row = {"posted_et": posted, "author": (author or "manual").strip()[:40] or "manual", **{k: f[k] for k in
               ("symbol", "direction", "entry_type", "target_lo", "target_hi", "stop", "stop_basis", "levels_source", "note")},
               "entry_lo": "" if f["entry_lo"] is None else f["entry_lo"], "entry_hi": "" if f["entry_hi"] is None else f["entry_hi"]}
        if not cli._append([row], cli.SIGNALS):           # pass the path: the default argument is the repo copy
            raise ValueError(f"{f['symbol']} was already added at {posted}")
        return {"key": sig_key(row), "row": row, "preview": prev}

    async def add(self, fields: dict, author: str | None = None, posted_et: str | None = None) -> dict:
        if not self.state().get("seeded"):                 # grade the history first, so this idea's alert is the live one
            await self.run_pass("seed")
        async with self.lock:
            out = await asyncio.to_thread(self._add_sync, fields, author, posted_et)
        res = await self.run_pass("added", only={out["key"]})
        out["alerts"] = res["alerts"]
        out["idea"] = next((i for i in self.ideas() if i["key"] == out["key"]), None)
        return out

    async def cancel(self, key: str) -> dict:
        from swingdesk.monitor import load_book, save_book
        async with self.lock:
            book = load_book()
            b = book.get(key)
            if not b:
                raise KeyError(key)
            if b.get("state") != "WATCHING":
                raise ValueError(f"only working orders can be cancelled ({key} is {b.get('state')})")
            b.update(state="CANCELLED", cancelled_by="you", updated=datetime.now().isoformat(timespec="seconds"))
            save_book(book)
        a = {"kind": "CANCELLED", "key": key, "symbol": b["symbol"], "text": f"✖️ CANCEL {b['symbol']}: cancelled on the dashboard",
             "author": b.get("author"), "take": True}
        await self._deliver([a], seed=False, kind="manual")
        return {"key": key, "state": "CANCELLED"}

    # ------------------------------------------------------------------ views for the dashboard
    def gate(self) -> dict | None:
        from swingdesk.strategy import load_config
        spy = self.cached("SPY")
        if spy is None or len(spy) < 25:
            return None
        cfg = load_config()
        n = int(cfg["gate"]["spy_below_sma"])
        sma = spy["close"].rolling(n).mean()
        c, s = float(spy["close"].iloc[-1]), float(sma.iloc[-1])
        mk = self.marks().get("SPY", {})
        return {"spy_close": c, "spy_sma": round(s, 2), "sma_n": n, "date": str(spy.index[-1].date()), "market_branch": c < s,
                "gap_pct": round(100 * (c / s - 1), 2), "max_atr_pct": cfg["gate"]["max_atr_pct"], "require": cfg["gate"]["require"],
                "quote": mk.get("quote"), "quote_at": mk.get("quote_at"),
                "spark": [round(float(x), 2) for x in spy["close"].tail(60)],
                "sma_spark": [None if x != x else round(float(x), 2) for x in sma.tail(60)]}

    def ideas(self, source: str = "main") -> list[dict]:
        """``source``: main (channel, manual, core scanner), wide (the wide scan) or all."""
        from swingdesk.monitor import load_book
        book = load_book()
        sigs = self.signals(include_wide=source == "all", only_wide=source == "wide")
        marks = self.marks()
        out = []
        for r in sigs.to_dict("records"):
            key = sig_key(r)
            b = book.get(key, {})
            v = b.get("verdict") or {}
            res = b.get("result") or {}
            state = b.get("state") or "NEW"
            px = self.cached(r["symbol"])
            mk = marks.get(r["symbol"], {})
            last = mk.get("quote") if (mk.get("quote_at") and time.time() - mk["quote_at"] < 6 * 3600) else None
            close = float(px["close"].iloc[-1]) if px is not None and len(px) else mk.get("close")
            price = last or close
            long = r["direction"] == "long"
            sgn = 1 if long else -1
            fill = _f(res.get("fill_px"))
            stop = _f(v.get("stop_px")) or _f(r.get("stop"))
            open_r = None
            if state == "FILLED" and fill and stop and price and fill != stop:
                open_r = round(sgn * (price - fill) / abs(fill - stop), 2)
            to_entry = None
            if state in ("WATCHING", "NEW") and v.get("entry_px") and price:
                to_entry = round(100 * (price / v["entry_px"] - 1), 2)
            out.append({
                "key": key, "symbol": r["symbol"], "author": r.get("author"), "posted_et": r["posted_et"],
                "direction": r["direction"], "entry_type": r["entry_type"], "entry_lo": _f(r.get("entry_lo")),
                "entry_hi": _f(r.get("entry_hi")), "target_lo": _f(r.get("target_lo")), "target_hi": _f(r.get("target_hi")),
                "stop": _f(r.get("stop")), "stop_basis": r.get("stop_basis"), "note": r.get("note") if isinstance(r.get("note"), str) else "",
                "levels_source": r.get("levels_source"), "state": state, "take": v.get("take"), "tier": v.get("tier"),
                "reasons": v.get("reasons") or [], "flags": v.get("flags") or [], "limit": v.get("entry_px"), "stop_px": stop,
                "tp": v.get("tp_px"), "posted_target": v.get("posted_target"), "risk_pct": v.get("risk_pct"),
                "shares_per_10k": v.get("shares_per_10k"), "r_per_share": v.get("r_per_share"),
                "atr_pct": (v.get("metrics") or {}).get("atr_pct"), "spy_below": (v.get("metrics") or {}).get("spy_below_sma"),
                "result": {k: res.get(k) for k in ("status", "r", "fill_px", "fill_date", "exit_date", "bars", "timed_out")},
                "r": res.get("r") if state in CLOSED else open_r, "price": price, "close": close, "quote": last,
                "close_date": mk.get("date") or (str(px.index[-1].date()) if px is not None and len(px) else None),
                "to_entry_pct": to_entry, "spark": [round(float(x), 4) for x in px["close"].tail(30)] if px is not None else [],
                "updated": b.get("updated"), "scanner": r.get("author") == "scanner"})
        order = {"FILLED": 0, "WATCHING": 1, "NEW": 2}
        out.sort(key=lambda i: (order.get(i["state"], 3), -datetime.strptime(i["posted_et"], "%Y-%m-%d %H:%M").timestamp()))
        return out

    def record(self, ideas: list[dict] | None = None) -> dict:
        from swingdesk.monitor import load_book, scoreboard
        ideas = ideas if ideas is not None else self.ideas()
        taken = [i for i in ideas if i["take"]]
        closed = [i for i in taken if i["state"] in CLOSED]

        def group(by):
            g: dict[str, list] = {}
            for i in closed:
                k = by(i)
                g.setdefault(k, []).append(i)
            return [{"group": k, "n": len(v), "wins": sum(x["state"] == "WIN" for x in v),
                     "win_rate": round(100 * sum(x["state"] == "WIN" for x in v) / len(v), 1),
                     "sum_r": round(sum(x["r"] or 0 for x in v), 2)} for k, v in sorted(g.items())]
        curve, cum = [], 0.0
        for i in sorted(closed, key=lambda x: (x["result"].get("exit_date") or "", x["posted_et"])):
            cum += i["r"] or 0
            curve.append({"date": i["result"].get("exit_date"), "r": round(i["r"] or 0, 3), "cum": round(cum, 3), "symbol": i["symbol"],
                          "win": i["state"] == "WIN"})
        wins = [i["r"] for i in closed if i["state"] == "WIN"]
        losses = [i["r"] for i in closed if i["state"] != "WIN"]
        aw, al = (float(np.mean(wins)) if wins else None), (float(np.mean(losses)) if losses else None)
        be = round(100 * abs(al) / (abs(al) + aw), 1) if aw and al else None
        skipped = [i for i in ideas if i["take"] is False and i["state"] in ("SKIPPED",)]
        keys = {i["key"] for i in ideas}
        return {"scoreboard": scoreboard({k: v for k, v in load_book().items() if k in keys}), "by_author": group(lambda i: i["author"] or "?"),
                "by_tier": group(lambda i: i["tier"] or "?"), "by_month": group(lambda i: i["posted_et"][:7]),
                "curve": curve, "avg_win_r": None if aw is None else round(aw, 2), "avg_loss_r": None if al is None else round(al, 2),
                "breakeven_win_rate": be, "skipped": len(skipped)}

    def _variants_sync(self) -> list[dict]:
        import copy
        from swingdesk.monitor import scoreboard, update
        from swingdesk.strategy import load_config
        sigs = self.signals()
        prices = {s: df for s in sigs.symbol.unique() if (df := self.cached(s)) is not None}
        spy = self.cached("SPY")
        base = load_config()
        out = []
        for name, changes in VARIANTS:
            cfg = copy.deepcopy(base)
            for k, val in changes.items():
                a, b = k.split(".")
                cfg[a][b] = val
            book, _ = update(sigs, prices, spy, cfg=cfg, book={})
            row = {"name": name, "changes": changes, "all": scoreboard(book)}
            for half, keep in (("before", lambda k: k.split("@")[1] < "2026-09-01"), ("after", lambda k: k.split("@")[1] >= "2026-09-01")):
                row[half] = scoreboard({k: v for k, v in book.items() if keep(k)})
            out.append(row)
        return out

    async def variants(self) -> list[dict]:
        from swingdesk import cli, data_sources as ds
        sig_m = max((Path(p).stat().st_mtime for p in (cli.SIGNALS, cli.SCANNER) if Path(p).exists()), default=0)
        px_m = max((p.stat().st_mtime for p in Path(ds.PRICE_DIR).glob("*.csv")), default=0)
        stamp = f"{sig_m}-{px_m}-{(DESK_ROOT / 'config' / 'strategy.yaml').stat().st_mtime}"
        if self._variants and self._variants[0] == stamp:
            return self._variants[1]
        rows = await asyncio.to_thread(self._variants_sync)
        self._variants = (stamp, rows)
        return rows

    def config(self) -> dict:
        from swingdesk.strategy import CONFIG_PATH, load_config
        return {"path": str(Path(CONFIG_PATH).relative_to(DESK_ROOT.parent)).replace("\\", "/"), "text": Path(CONFIG_PATH).read_text(),
                "cfg": load_config()}

    def research_files(self) -> dict:
        props = []
        for d in (self.home / "research" / "proposals", DESK_ROOT / "research" / "proposals"):
            for p in sorted(d.glob("*.md"), reverse=True):
                j = p.with_suffix(".json")
                try:
                    meta = json.loads(j.read_text()) if j.exists() else {}
                except ValueError:
                    meta = {}
                props.append({"name": p.name, "title": meta.get("title") or p.stem, "changes": meta.get("config_changes") or {},
                              "text": p.read_text(encoding="utf-8")[:6000], "at": p.stat().st_mtime})
        reps = [{"name": p.name, "text": p.read_text(encoding="utf-8")[:8000], "at": p.stat().st_mtime}
                for p in sorted(self.reports.glob("*.md"), reverse=True)] if self.reports.exists() else []
        return {"proposals": props[:20], "reports": reps[:20]}

    def status(self) -> dict:
        now = datetime.now(NY)
        st = self.state()
        from abg.live.market_hours import session_status
        return {"enabled": True, "busy": self.busy, "seeded": bool(st.get("seeded")), "home": str(self.home),
                "webhook": bool(self._webhook()), "interval_min": self.s.swing_interval_min, "close_time": self.s.swing_close_time,
                "scan": self.s.swing_scan, "research": self.s.swing_research, "pitch": self.s.swing_pitch,
                "api_key": bool(self.s.anthropic_api_key), "ai_intake": bool(self.s.swing_ai_intake and self.s.anthropic_api_key),
                "source": "twelvedata" if os.getenv("TWELVEDATA_API_KEY") else "yfinance",
                "last_pass": st.get("last_pass"), "last_kind": st.get("last_kind"), "last_intraday": st.get("last_intraday"),
                "last_close": st.get("last_close"), "last_scan": st.get("last_scan"), "last_scan_new": st.get("last_scan_new"),
                "next_pass": self.next_pass(now), "session": session_status(now),
                "wide": {"ready": self.wide_ready(), "mode": "posting" if self.s.swing_wide_post else "shadow",
                         "busy": self.wide_busy, "last_day": st.get("last_wide_day")},
                "errors": list(self.errors)[-5:]}
