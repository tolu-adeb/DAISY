"""The wide scan inside the swing desk (``SwingDesk`` inherits this).  See ``mnq/swing/wide.py`` for the method.

Wide ideas live in ``data/swing/data/wide_signals.csv`` (author ``wide``) and are replayed by the same pass as every
other idea, but they are kept out of the Book, the main scoreboard, the Track record and the digests: they have their
own page until the shadow period shows whether they're worth posting.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta

import pandas as pd

from abg.swing import wide as W

NY = W.NY


class WideDeskMixin:
    # ------------------------------------------------------------------ setup
    def _wide_init(self) -> None:
        self.wide_store = W.Store(self.home)
        self.wide_csv = self.home / "data" / "wide_signals.csv"
        self.sources = W.Sources(self.s, self.http)
        self.wide_busy: str | None = None
        self.wide_progress: dict = {}
        self._wide_lock: asyncio.Lock | None = None
        self._wide_task: asyncio.Task | None = None
        self._wide_allow: set[str] = set()

    @property
    def wide_lock(self) -> asyncio.Lock:
        if self._wide_lock is None:
            self._wide_lock = asyncio.Lock()
        return self._wide_lock

    def wide_ready(self) -> bool:
        return bool(getattr(self.s, "swing_wide", True) and self.sources.polygon_key and self.http is not None)

    def wide_plans(self) -> dict:
        """{author: Plan} for the replay: the wide scan's ideas may chase (ABG_SWING_WIDE_CHASE_ATR); the channel's
        ideas never do."""
        k = float(getattr(self.s, "swing_wide_chase_atr", 0) or 0)
        if k <= 0:
            return {}
        from dataclasses import replace

        from swingdesk.strategy import plan_from_config
        return {W.AUTHOR: replace(plan_from_config(), chase_atr=k)}

    def wide_evidence(self) -> tuple[bool, str]:
        """Posting is allowed only when the latest backtest of exactly what would post (same ranking, same chase rule)
        has enough closed trades, a high enough win rate and a positive total R."""
        bt = self.wide_store.load("backtest") or {}
        p = (bt.get("summary") or {}).get("posting_unseen")
        need_n, need_w = int(self.s.swing_wide_post_min_trades), float(self.s.swing_wide_post_min_win)
        if not p:
            return False, "no backtest of the current ranking yet (it runs Saturdays, or press Backtest)"
        cfg = bt.get("settings") or {}
        if cfg.get("rank") != self.s.swing_wide_rank or float(cfg.get("chase_atr") or 0) != float(self.s.swing_wide_chase_atr or 0):
            return False, "the last backtest used different settings (ranking / chase) - it re-runs Saturday"
        try:
            age = (datetime.now(NY).date() - date.fromisoformat(str(bt.get("end"))[:10])).days
        except ValueError:
            age = 999
        if age > 45:
            return False, f"the last backtest ends {bt.get('end')} - too old"
        n, w, r = p.get("closed") or 0, p.get("win_rate"), p.get("sum_r") or 0
        lo, hi = W.DESIGN_WINDOW
        if n < need_n:
            return False, f"{n} closed trades of what would post outside {lo} to {hi} (need {need_n}); the history grows each night"
        if w is None or w < need_w or r <= 0:
            return False, f"what would post, outside {lo} to {hi}: {w}% wins, {r:+.2f}R over {n} trades (need {need_w:.0f}% and > 0R)"
        return True, f"what would post, outside {lo} to {hi}: {w}% wins, {r:+.2f}R over {n} trades"

    # ------------------------------------------------------------------ signals / book helpers
    def wide_signals(self) -> pd.DataFrame:
        if not self.wide_csv.exists():
            return pd.DataFrame(columns=self._cols() + ["flags"])
        return pd.read_csv(self.wide_csv)

    @staticmethod
    def _cols() -> list[str]:
        from swingdesk import cli
        return list(cli.COLS)

    def main_book(self) -> dict:
        from swingdesk.monitor import load_book
        return {k: v for k, v in load_book().items() if v.get("author") != W.AUTHOR}

    def wide_book(self) -> dict:
        from swingdesk.monitor import load_book
        return {k: v for k, v in load_book().items() if v.get("author") == W.AUTHOR}

    def _append_wide(self, rows: list[dict]) -> list[str]:
        old = self.wide_signals()
        have = set(zip(old.posted_et, old.symbol))
        new = [r for r in rows if (r["posted_et"], r["symbol"]) not in have]
        if new:
            df = pd.DataFrame(new)
            df["flags"] = df["flags"].apply(lambda f: ";".join(f) if isinstance(f, list) else (f or ""))
            out = pd.concat([old, df[self._cols() + ["flags"]]], ignore_index=True)
            tmp = self.wide_csv.with_suffix(".tmp")
            out.to_csv(tmp, index=False)
            tmp.replace(self.wide_csv)
        return [f"{r['symbol']}@{r['posted_et']}" for r in new]

    # ------------------------------------------------------------------ delivery rules for wide alerts
    def _wide_route(self, a: dict) -> str | None:
        """None = not a wide alert (normal rules).  Otherwise "post" or a dashboard reason."""
        if a.get("author") != W.AUTHOR:
            return None
        if not getattr(self.s, "swing_wide_post", False):
            return "dashboard (wide scan, shadow)"
        if a["kind"] == "GRADED":
            return "post" if a["key"] in self._wide_allow and a.get("take") else "dashboard (wide scan)"
        posted = self.wide_store.load("posted", {}) or {}
        return "post" if a["key"] in posted else "dashboard (wide scan)"

    def _wide_text(self, a: dict) -> str:
        m = (self.wide_store.load("meta", {}) or {}).get(a["key"])
        if a.get("author") != W.AUTHOR or a["kind"] != "GRADED" or not m:
            return a["text"]
        f, n = m.get("features") or {}, m.get("news") or {}
        bits = [f"📡 wide scan #{m.get('rank')} · score {m.get('score')}"]
        if f.get("rs20") is not None:
            bits.append(f"20d vs SPY {f['rs20']:+.1f}%")
        if m.get("sector_etf") and f.get("sector_rs20") is not None:
            bits.append(f"{m['sector_etf']} {f['sector_rs20']:+.1f}% vs SPY")
        if f.get("vol_surge"):
            bits.append(f"volume ×{f['vol_surge']:.1f}")
        line2 = []
        if n.get("why"):
            line2.append(f"news: {n['why']}")
        if m.get("earnings"):
            line2.append(f"earnings {m['earnings']} (size halved)")
        return a["text"] + "\n" + " · ".join(bits) + ("\n" + " · ".join(line2) if line2 else "")

    def _wide_mark_posted(self, keys: list[str]) -> None:
        if not keys:
            return
        p = self.wide_store.load("posted", {}) or {}
        for k in keys:
            p[k] = time.time()
        self.wide_store.save("posted", p)

    # ------------------------------------------------------------------ data refresh
    async def wide_bars(self, day: date, n: int, progress=None) -> dict:
        """Make sure the store has ``day`` and the ``n`` sessions before it.  Returns counts.  None-day = not out yet."""
        need = [d for d in W.sessions_back(day, n) if not self.wide_store.has(d)]
        got = 0
        for i, d in enumerate(reversed(need)):              # newest first: the scan can run as soon as possible
            self.wide_progress = {"step": "bars", "done": i, "of": len(need), "day": str(d),
                                  "eta_min": round((len(need) - i) * 60 / max(0.1, self.s.swing_wide_polygon_per_min) / 60, 1)}
            if progress:
                progress(self.wide_progress)
            rows = await self.sources.grouped(d)
            if rows is None:
                if d == day:
                    return {"ready": False, "missing": len(need)}
                continue
            await asyncio.to_thread(self.wide_store.write_day, d, rows)
            got += 1
        self.wide_progress = {}
        return {"ready": self.wide_store.has(day), "fetched": got}

    async def wide_tickers(self, force: bool = False) -> dict:
        t = self.wide_store.load("tickers")
        if t and not force and time.time() - t.get("at", 0) < 7 * 86400:
            return t
        rows = await self.sources.common_stocks("CS")
        t = {"at": time.time(), "symbols": sorted({r["ticker"] for r in rows}),
             "names": {r["ticker"]: r["name"] for r in rows if r.get("name")}}
        self.wide_store.save("tickers", t)
        return t

    async def wide_profiles(self, syms) -> dict:
        prof = self.wide_store.load("profiles", {}) or {}
        need = [s for s in syms if s not in prof or time.time() - prof[s].get("at", 0) > 30 * 86400]
        if need and self.sources.finnhub_key:
            for i, s in enumerate(need):
                self.wide_progress = {"step": "sectors", "done": i, "of": len(need)}
                try:
                    prof[s] = {**await self.sources.profile(s), "at": time.time()}
                except Exception as e:
                    prof[s] = {"error": type(e).__name__, "at": time.time() - 29 * 86400}   # retry tomorrow
            self.wide_store.save("profiles", prof)
            self.wide_progress = {}
        return prof

    async def wide_earnings(self, frm: date, to: date, cache: str = "earnings") -> dict:
        c = self.wide_store.load(cache) or {}
        if c.get("from") == str(frm) and c.get("to") == str(to) and time.time() - c.get("at", 0) < 12 * 3600:
            return c["cal"]
        if not self.sources.finnhub_key:
            return {}
        try:
            cal = await self.sources.earnings(frm, to)
        except Exception as e:
            self.errors.append({"ts": time.time(), "where": "wide earnings", "error": f"{type(e).__name__}: {e}"[:200]})
            return c.get("cal") or {}
        self.wide_store.save(cache, {"at": time.time(), "from": str(frm), "to": str(to), "cal": cal})
        return cal

    # ------------------------------------------------------------------ the nightly scan
    def _wide_panel(self, day: date, tickers: dict) -> tuple[dict, list[str], dict, pd.DataFrame]:
        core = set(self._core_universe())
        keep = set(tickers.get("symbols") or []) | set(W.SECTOR_ETFS) | {"SPY"}
        panel = self.wide_store.panel(day, self.s.swing_wide_history, keep)
        spy = panel.get("SPY")
        cands = [s for s in panel if s in set(tickers.get("symbols") or []) and s not in core]
        liq = W.liquid(panel, cands, self.s.swing_wide_min_price, self.s.swing_wide_min_dollar_vol_m)
        return panel, liq, W.breadth(panel, liq), spy

    def _core_universe(self) -> list[str]:
        from abg.swing import DESK_ROOT
        uni = DESK_ROOT / "config" / "universe.txt"
        return [x.strip() for x in uni.read_text().splitlines() if x.strip() and not x.startswith("#")]

    def _wide_scan_sync(self, day: date, tickers: dict) -> dict:
        panel, liq, br, spy = self._wide_panel(day, tickers)
        desk_spy = self.cached("SPY")              # grade with the SPY the book's replay uses, when it's current
        if desk_spy is not None and len(desk_spy) and desk_spy.index[-1] >= pd.Timestamp(day):
            spy = desk_spy
        if spy is None:
            raise RuntimeError("no SPY bars in the store")
        live_syms = {v["symbol"] for v in self.main_book().values() if v.get("state") in ("WATCHING", "FILLED")} | \
                    {v["symbol"] for v in self.wide_book().values() if v.get("state") in ("WATCHING", "FILLED")}
        found = [x for x in W.scan(panel, liq, spy, day) if x["sig"]["symbol"] not in live_syms]
        return {"panel": panel, "liquid": liq, "breadth": br, "found": found, "spy": spy,
                "universe": {"stored": len(panel), "common": len(tickers.get("symbols") or []), "liquid": len(liq)}}

    async def run_wide(self, day: date | None = None, post: bool | None = None, progress=None) -> dict:
        """The nightly wide scan for ``day`` (default: the last finished session)."""
        if not self.sources.polygon_key:
            raise RuntimeError("set ABG_POLYGON_API_KEY in .env to use the wide scan")
        day = day or W.last_session()
        if self.wide_lock.locked():
            return {"ready": False, "busy": self.wide_busy}
        async with self.wide_lock:
            try:
                self.wide_busy = "fetching the market's bars"
                bars = await self.wide_bars(day, self.s.swing_wide_history, progress)
                if not bars["ready"]:
                    return {"ready": False, "day": str(day), "why": "the day's bars aren't published yet"}
                self.wide_busy = "loading the stock list"
                tickers = await self.wide_tickers()
                self.wide_busy = "scanning"
                sc = await asyncio.to_thread(self._wide_scan_sync, day, tickers)
                found, panel, spy = sc["found"], sc["panel"], sc["spy"]
                takes = [x for x in found if x["verdict"]["take"]]
                self.wide_busy = "sectors, earnings and news"
                prof = await self.wide_profiles([x["sig"]["symbol"] for x in takes])
                cal = await self.wide_earnings(day, day + timedelta(days=45))
                for x in found:
                    s = x["sig"]["symbol"]
                    p = prof.get(s) or {}
                    etf = panel.get(p.get("sector_etf") or "")
                    hist = panel[s][panel[s].index <= pd.Timestamp(day)]
                    x["features"] = W.features(hist, spy, etf[etf.index <= pd.Timestamp(day)] if etf is not None else None)
                    x["profile"] = {k: p.get(k) for k in ("name", "industry", "mcap_m", "sector_etf")}
                    x["earnings"] = W.earnings_soon(s, cal, day, self.s.swing_wide_earnings_sessions)
                    x["pre"] = W.score(x["features"], None, bool(x["earnings"]))["score"]
                news = await self._wide_news(sorted(takes, key=lambda x: -x["pre"])[: max(0, self.s.swing_wide_news_top)], day)
                for x in found:
                    n = news.get(x["sig"]["symbol"])
                    x["news"] = n
                    sc_ = W.score(x["features"], n, bool(x["earnings"]))
                    x["score"], x["parts"] = sc_["score"], sc_["parts"]
                return await self._wide_finish(day, sc, found, post, tickers)
            finally:
                self.wide_busy, self.wide_progress = None, {}

    async def _wide_news(self, cands: list[dict], day: date) -> dict:
        if not cands or not self.sources.finnhub_key:
            return {}
        heads: dict[str, list] = {}
        for x in cands:
            s = x["sig"]["symbol"]
            try:
                heads[s] = await self.sources.news(s, day - timedelta(days=10), day)
            except Exception as e:
                self.errors.append({"ts": time.time(), "where": f"wide news {s}", "error": type(e).__name__})
                heads[s] = []
        out = {}
        if self.sources.anthropic_key:
            try:
                out = await self.sources.read_news(heads)
            except Exception as e:
                self.errors.append({"ts": time.time(), "where": "wide news (Claude)", "error": f"{type(e).__name__}: {e}"[:200]})
        for s, h in heads.items():
            if s not in out:
                flags = W.keyword_flags([f"{n['headline']} {n.get('summary') or ''}" for n in h])
                out[s] = {"score": -1 if flags else 0, "red_flags": flags, "by": "keywords",
                          "why": ("; ".join(flags) if flags else ("no headlines" if not h else "nothing notable in the headlines"))}
            out[s]["headlines"] = [{"at": n["at"], "headline": n["headline"], "url": n.get("url"), "source": n.get("source")}
                                   for n in h[:4]]
        return out

    async def _wide_finish(self, day: date, sc: dict, found: list[dict], post: bool | None, tickers: dict) -> dict:
        from swingdesk import data_sources as ds
        post = getattr(self.s, "swing_wide_post", False) if post is None else post
        br = sc["breadth"]
        mode = getattr(self.s, "swing_wide_rank", "entry")
        found.sort(key=lambda x: (not x["verdict"]["take"], W.rank_key(x, mode)))
        evidence_ok, evidence_why = self.wide_evidence()
        post_now = bool(post and evidence_ok)
        allow, rank = [], 0
        breadth_ok = br.get("above_50d_pct") is not None and br["above_50d_pct"] >= self.s.swing_wide_min_breadth
        main_syms = set(self.signals().symbol) if len(self.signals()) else set()
        rows, meta = [], self.wide_store.load("meta", {}) or {}
        for x in found:
            sig, v = x["sig"], x["verdict"]
            key = f"{sig['symbol']}@{sig['posted_et']}"
            x["key"] = key
            if not v["take"]:
                x["status"] = "skipped by the gate"
                continue
            rank += 1
            x["rank"] = rank
            red = (x.get("news") or {}).get("red_flags") or []
            if red:
                x["status"] = "kept off Discord: " + ", ".join(red)
            elif not breadth_ok:
                x["status"] = f"kept off Discord: breadth {br.get('above_50d_pct')}% < {self.s.swing_wide_min_breadth}%"
            elif len(allow) < self.s.swing_wide_max_posts:
                allow.append(key)
                x["status"] = ("posted" if post_now else f"would post - held: {evidence_why}" if post
                               else "would post (shadow)")
            else:
                x["status"] = "ranked below the daily limit"
            flags = ["binary_event"] if x.get("earnings") else []
            rows.append({**sig, "flags": flags})
            meta[key] = {"rank": rank, "score": x["score"], "parts": x["parts"], "features": x["features"],
                         "dist_atr": x.get("dist_atr"),
                         "sector_etf": (x.get("profile") or {}).get("sector_etf"), "profile": x.get("profile"),
                         "news": x.get("news"), "earnings": x.get("earnings"), "status": x["status"]}
            px = sc["panel"][sig["symbol"]]
            if sig["symbol"] not in main_syms or not ds.price_file(sig["symbol"]).exists():
                await asyncio.to_thread(ds.save, sig["symbol"], px)
        self.wide_store.save("meta", meta)
        new_keys = self._append_wide(rows)
        # keep the price files of live wide-only ideas current (they aren't downloaded intraday)
        for k, b in self.wide_book().items():
            s = b.get("symbol")
            if b.get("state") in ("WATCHING", "FILLED") and s in sc["panel"] and s not in main_syms:
                await asyncio.to_thread(ds.save, s, sc["panel"][s])
        self._wide_allow = set(allow) if post_now else set()
        try:
            res = await self.run_pass("wide", only=set(new_keys) | {k for k, b in self.wide_book().items()
                                                                   if b.get("state") in ("WATCHING", "FILLED")})
        finally:
            self._wide_allow = set()
        run = {"day": str(day), "built": datetime.now(NY).isoformat(timespec="minutes"),
               "mode": "posting" if post_now else ("held" if post else "shadow"), "held_why": None if post_now else evidence_why,
               "rank": mode,
               "universe": sc["universe"], "breadth": br, "breadth_ok": breadth_ok, "setups": len(found),
               "taken": sum(1 for x in found if x["verdict"]["take"]), "new": len(new_keys), "posted": len(allow) if post_now else 0,
               "sources": self.sources.configured(),
               "candidates": [{"key": x.get("key"), "symbol": x["sig"]["symbol"], "name": (x.get("profile") or {}).get("name")
                               or (tickers.get("names") or {}).get(x["sig"]["symbol"]),
                               "take": x["verdict"]["take"], "tier": x["verdict"]["tier"], "reasons": x["verdict"]["reasons"][:3],
                               "limit": x["verdict"]["entry_px"], "stop": x["verdict"]["stop_px"], "tp": x["verdict"]["tp_px"],
                               "risk_pct": x["verdict"]["risk_pct"], "atr_pct": (x["verdict"].get("metrics") or {}).get("atr_pct"),
                               "score": x.get("score"), "parts": x.get("parts"), "features": x.get("features"),
                               "dist_atr": x.get("dist_atr"),
                               "profile": x.get("profile"), "news": x.get("news"), "earnings": x.get("earnings"),
                               "rank": x.get("rank"), "status": x.get("status"), "note": x["sig"].get("note")}
                              for x in found],
               "delivered": res.get("delivered")}
        tmp = self.wide_store.run_path(day).with_suffix(".tmp")
        tmp.write_text(json.dumps(run, default=str), encoding="utf-8")
        tmp.replace(self.wide_store.run_path(day))
        self._set(last_wide=time.time(), last_wide_day=str(day))
        self._broadcast({"type": "wide", "day": str(day), "taken": run["taken"]})
        return {"ready": True, **{k: run[k] for k in ("day", "mode", "universe", "breadth", "setups", "taken", "new", "posted")}}

    # ------------------------------------------------------------------ schedule hook
    def wide_due(self, now: datetime, st: dict) -> bool:
        from abg.live.market_hours import is_trading_day
        if not self.wide_ready() or not is_trading_day(now.date()):
            return False
        m = now.hour * 60 + now.minute
        h, mi = (self.s.swing_wide_time or "17:15").split(":")
        return (int(h) * 60 + int(mi) <= m < 21 * 60 + 30 and f"wide:{now.date()}" not in st.get("jobs", {})
                and time.time() - st.get("wide_try", 0) >= 1800 and (self._wide_task is None or self._wide_task.done()))

    def wide_start(self, day: date) -> None:
        """Run the scan in the background (the first run backfills ~6 months of bars at 5 calls/min: ~30 min), then
        extend the stored history a little (ABG_SWING_WIDE_BACKFILL_PER_NIGHT) toward ABG_SWING_WIDE_BACKFILL_TO."""
        self._set(wide_try=time.time())

        async def go():
            try:
                r = await self.run_wide(day)
                if r.get("ready"):
                    self._once(f"wide:{day}")
                    await self.wide_backfill(day)
            except Exception as e:
                self.errors.append({"ts": time.time(), "where": "wide scan", "error": f"{type(e).__name__}: {e}"[:300]})
        self._wide_task = asyncio.ensure_future(go())

    async def wide_backfill(self, day: date, limit: int | None = None) -> dict:
        """Older whole-market sessions for a longer backtest: newest-missing first, at most ``limit`` calls."""
        target = int(getattr(self.s, "swing_wide_backfill_to", 0) or 0)
        limit = int(getattr(self.s, "swing_wide_backfill_per_night", 0) or 0) if limit is None else limit
        if target <= 0 or limit <= 0 or self.wide_lock.locked():
            return {"fetched": 0}
        need = [d for d in reversed(W.sessions_back(day, target)) if not self.wide_store.has(d)][:limit]
        got = 0
        async with self.wide_lock:
            try:
                for i, d in enumerate(need):
                    self.wide_busy = "extending history"
                    self.wide_progress = {"step": "history", "done": i, "of": len(need), "day": str(d)}
                    rows = await self.sources.grouped(d)
                    if rows:
                        await asyncio.to_thread(self.wide_store.write_day, d, rows)
                        got += 1
            finally:
                self.wide_busy, self.wide_progress = None, {}
        self._set(wide_backfill={"at": time.time(), "fetched": got, "stored": len(self.wide_store.days()), "target": target})
        return {"fetched": got}

    def wide_backtest_due(self, now: datetime, st: dict) -> bool:
        wk = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
        return bool(self.wide_ready() and getattr(self.s, "swing_wide_backtest_weekly", True) and now.weekday() == 5
                    and now.hour >= 9 and f"wide_bt:{wk}" not in st.get("jobs", {})
                    and len(self.wide_store.days()) >= 120 and (self._wide_task is None or self._wide_task.done())
                    and (getattr(self, "_wide_bt_task", None) is None or self._wide_bt_task.done()))

    def wide_backtest_auto(self, now: datetime) -> None:
        """The weekly re-run on all stored history (the first 60 sessions only warm the indicators up)."""
        wk = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
        if not self._once(f"wide_bt:{wk}"):
            return
        days = self.wide_store.days()
        self.wide_backtest_start(days[min(60, len(days) - 1)])

    def wide_backtest_start(self, start: date) -> None:
        """Backtest in the background (waits for a running scan: they share the Polygon rate limit)."""
        self._wide_bt_state = {"queued": time.time(), "start": str(start)}

        async def go():
            try:
                await self.wide_backtest(start)
                self._wide_bt_state = {}
                self._broadcast({"type": "wide", "backtest": True})
            except Exception as e:
                self._wide_bt_state = {"error": f"{type(e).__name__}: {e}"[:300]}
                self.errors.append({"ts": time.time(), "where": "wide backtest", "error": f"{type(e).__name__}: {e}"[:300]})
        self._wide_bt_task = asyncio.ensure_future(go())

    # ------------------------------------------------------------------ views
    def wide_ideas(self) -> list[dict]:
        meta = self.wide_store.load("meta", {}) or {}
        out = [i for i in self.ideas(source="wide")]
        for i in out:
            i["wide"] = meta.get(i["key"])
        return out

    def wide_view(self) -> dict:
        from swingdesk.monitor import scoreboard
        st = self.state()
        days = self.wide_store.days()
        ideas = self.wide_ideas()
        bt = self.wide_store.load("backtest")
        return {"status": {"ready": self.wide_ready(), "sources": self.sources.configured(),
                           "mode": "posting" if getattr(self.s, "swing_wide_post", False) else "shadow",
                           "max_posts": self.s.swing_wide_max_posts, "time": self.s.swing_wide_time,
                           "min_price": self.s.swing_wide_min_price, "min_dollar_vol_m": self.s.swing_wide_min_dollar_vol_m,
                           "min_breadth": self.s.swing_wide_min_breadth, "busy": self.wide_busy, "progress": self.wide_progress,
                           "bars": {"days": len(days), "first": str(days[0]) if days else None, "last": str(days[-1]) if days else None},
                           "last_run": st.get("last_wide"), "last_day": st.get("last_wide_day"),
                           "backtest_job": getattr(self, "_wide_bt_state", {}) or {},
                           "rank": getattr(self.s, "swing_wide_rank", "entry"), "chase_atr": getattr(self.s, "swing_wide_chase_atr", 0),
                           "evidence": dict(zip(("ok", "why"), self.wide_evidence())),
                           "evidence_min_trades": self.s.swing_wide_post_min_trades, "evidence_min_win": self.s.swing_wide_post_min_win,
                           "history": {"stored": len(days), "target": getattr(self.s, "swing_wide_backfill_to", 0),
                                       "last": st.get("wide_backfill")}},
                "run": self.wide_store.latest_run(), "scoreboard": scoreboard(self.wide_book()),
                "record": self.record(ideas), "ideas": [i for i in ideas if i["state"] in ("FILLED", "WATCHING")][:60],
                "recent_closed": [i for i in ideas if i["state"] in ("WIN", "LOSS", "SCRATCH")][:30],
                "backtest": (bt or {}).get("summary") and bt}

    # ------------------------------------------------------------------ backtest
    async def wide_backtest(self, start: date, end: date | None = None, progress=None) -> dict:
        if not self.sources.polygon_key:
            raise RuntimeError("set ABG_POLYGON_API_KEY in .env first")
        end = end or W.last_session()
        sess = W.sessions_between(start, end)
        n = len(sess) + 75                                       # 60-bar warm-up + a little
        async with self.wide_lock:
            try:
                self.wide_busy = "backtest: fetching bars"
                await self.wide_bars(end, n, progress)
                tickers = await self.wide_tickers()
                self.wide_busy = "backtest: scanning"
                core = set(self._core_universe())
                keep = set(tickers["symbols"]) | set(W.SECTOR_ETFS) | {"SPY"}

                def phase1():
                    panel = self.wide_store.panel(end, n, keep)
                    syms = [s for s in panel if s in set(tickers["symbols"])]
                    return panel, syms
                panel, syms = await asyncio.to_thread(phase1)
                spy = panel["SPY"]
                self.wide_busy = "backtest: earnings calendar"
                cal = await self.wide_earnings(start, end + timedelta(days=30), cache="earnings_backtest")
                args = (panel, syms, spy, start, end)
                kw = dict(cal=cal, min_price=self.s.swing_wide_min_price, min_dv_m=self.s.swing_wide_min_dollar_vol_m,
                          earn_sessions=self.s.swing_wide_earnings_sessions, top_n=self.s.swing_wide_max_posts, core=core)
                rank, chase = getattr(self.s, "swing_wide_rank", "entry"), float(getattr(self.s, "swing_wide_chase_atr", 0) or 0)
                first = await asyncio.to_thread(W.backtest, *args, profiles={}, chase_atr=chase, rank=rank, **kw)
                taken_syms = sorted(set(first["trades"][first["trades"]["take"]].symbol)) if len(first["trades"]) else []
                self.wide_busy = "backtest: sectors"
                prof = await self.wide_profiles(taken_syms)
                self.wide_busy = "backtest: scoring"
                bt = await asyncio.to_thread(W.rescore, first["trades"], panel, spy, prof, self.s.swing_wide_max_posts, rank)
                trades = bt["trades"]
                trades.to_csv(self.wide_store.root / "backtest_trades.csv", index=False)
                main = self.record(self.ideas())
                out = {"built": datetime.now(NY).isoformat(timespec="minutes"), "start": str(start), "end": str(end),
                       "sessions": len(sess), "universe": {"common": len(tickers["symbols"]), "with_bars": len(syms)},
                       "summary": bt["summary"], "earnings_data": bool(cal),
                       "settings": {"rank": rank, "chase_atr": chase, "top_n": self.s.swing_wide_max_posts},
                       "channel": main["scoreboard"],
                       "notes": ["News can't be replayed, so the score here leaves it out (it counts 20% live).",
                                 "The stock list is today's: companies delisted since the start are missing.",
                                 "Fills and exits use daily bars, like the audit."]}
                self.wide_store.save("backtest", out)
                return out
            finally:
                self.wide_busy, self.wide_progress = None, {}
