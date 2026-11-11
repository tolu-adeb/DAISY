"""The desk's research day (docs/22): the whole-market screen, news and sentiment, the morning watchlist,
live alerts, Explore, team notes, the calendar and the weekly recap.

``DayMixin`` is mixed into ``StockDesk``; it holds the I/O that ties the pure modules together
(screen, news, watchlist, alerts, rank). Every method degrades instead of raising: a provider that is
down costs that one input, says so, and never costs a pitch.

One poster: everything that goes to Discord goes through ``StockDesk._post_to`` and the stock webhook.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import alerts as AL
from . import news as N
from . import pitch_plan as P
from . import rank as RK
from . import screen as SC
from . import watchlist as WL

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")


def _hm(raw: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = (int(x) for x in str(raw).split(":"))
        return hh, mm
    except (ValueError, TypeError):
        return default


class DayMixin:
    # ================================================================== the whole-market screen
    def _names(self) -> dict:
        """ticker -> company name, from the cached Polygon list (empty without it)."""
        try:
            d = json.loads(self.market._common_cache().read_text(encoding="utf-8"))
            return d.get("names") or {}
        except Exception:
            return {}

    def screen_refresh(self, ls=None) -> dict:
        """Rebuild the screen from the last close: the rule panel (the names the books can pick) plus,
        when the whole-market day files are there, every other listed common stock and ADR."""
        import pandas as pd
        t0 = time.time()
        names, sectors = self._names(), self.store.sectors()
        non_stock = set(self.u.non_stock())
        parts, closes = [], None
        if ls is not None and ls.panel is not None and len(ls.panel.dates):
            parts.append(SC.build(ls.panel, self.cfg, names, sectors, non_stock, ind=ls.engine.ind))
            closes = ls.panel.close
        covered_extra = 0
        try:
            if self.market.enabled and self.market.days.days():
                have = set(parts[0].index) if parts else set()
                common = set(names) or None
                long = self.market.frame(int(getattr(self.s, "stocks_market_sessions", 500) or 500))
                if common is not None:
                    long = long[long["T"].isin(common - have)]
                else:
                    long = long[~long["T"].isin(have)]
                if not long.empty:
                    extra = SC.build(SC.wide_from_long(long), self.cfg, names, sectors, non_stock)
                    # outside the rule panel = the rules cannot pick it, whatever its own numbers say
                    extra["eligible"] = False
                    extra["why_not"] = extra["why_not"].fillna("outside the rule universe (liquidity floor or cap)")
                    covered_extra = len(extra)
                    parts.append(extra)
        except Exception as e:                           # coverage beyond the panel is a bonus
            log.warning("stocks: whole-market screen pass failed (%s)", type(e).__name__)
            self.errors.append({"ts": time.time(), "where": "screen", "error": type(e).__name__})
        if not parts:
            return {"covered": 0}
        df = pd.concat(parts)
        df = df[~df.index.duplicated(keep="first")]
        el = df[df["eligible"]]
        df["mom_pct"] = el["mom_score"].rank(pct=True)
        df["trend_pct"] = el["dist_200"].rank(pct=True)
        df["mom_rank"] = el["mom_score"].rank(ascending=False, method="first")
        day = str(df["day"].max())
        rows = SC.rows(df)
        for sym, _, d in rows:
            if d.get("eligible"):
                d["setups"] = SC.setups(d, self.cfg, closes[sym] if closes is not None and sym in closes.columns else None)
        with self.db._lock:
            self.db._db.execute("DELETE FROM screen")
            self.db._db.executemany("INSERT INTO screen(symbol, day, data) VALUES(?,?,?)",
                                    [(s, d_, json.dumps(d, default=str)) for s, d_, d in rows])
            self.db._db.commit()
        self._screen = None
        # the momentum ranking as it stood before today, so "new to the ranking" means something
        top_n = 2 * int(((self.cfg.get("books") or {}).get("xs_momentum") or {}).get("top_n", 20))
        top = sorted(str(s) for s in el.index[df.loc[el.index, "mom_rank"] <= top_n])
        cur = self.db.kv_get("mom_top") or {}
        if cur.get("day") != day:
            self.db.kv_set("mom_top_prev", cur.get("names"))
            self.db.kv_set("mom_top", {"day": day, "names": top})
        themes = N.load_themes(getattr(self.s, "stocks_themes", None), P.pitch_cfg(self.cfg)["ai_infrastructure"])
        if closes is not None:
            sc = N.news_cfg(self.cfg)["shock"]
            try:
                N.extend_by_correlation(themes, closes, set(el.index), sc["corr_min"], sc["corr_max_add"])
            except Exception as e:
                log.info("stocks: theme correlation pass failed (%s)", type(e).__name__)
        self.db.kv_set("themes", themes)
        etfs = df.loc[[s for s in ("XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY") if s in df.index]]
        meta = {"day": day, "covered": int(df["is_stock"].sum()), "eligible": int(df["eligible"].sum()),
                "in_rule_panel": int(len(parts[0])) if parts else 0, "outside_panel": covered_extra,
                "too_new": int((df["too_new"] & df["is_stock"]).sum()), "breadth": SC.breadth(df),
                "sectors_up": round(float((etfs["close"] > etfs["sma50"]).mean()), 4) if len(etfs) else None,
                "seconds": round(time.time() - t0, 1), "at": datetime.now(NY).isoformat(timespec="seconds")}
        self.db.kv_set("screen_meta", meta)
        return meta

    def screen_rows(self) -> dict[str, dict]:
        cached = getattr(self, "_screen", None)
        if cached is None:
            cached = {}
            for r in self.db.q("SELECT symbol, data FROM screen"):
                try:
                    cached[r["symbol"]] = json.loads(r["data"])
                except ValueError:
                    continue
            self._screen = cached
        return cached

    def screen_meta(self) -> dict:
        return self.db.kv_get("screen_meta") or {}

    def themes(self) -> dict:
        return self.db.kv_get("themes") or N.load_themes(getattr(self.s, "stocks_themes", None),
                                                         P.pitch_cfg(self.cfg)["ai_infrastructure"])

    # ================================================================== news and sentiment
    def _feeds(self):
        if getattr(self, "_news_feeds", None) is None:
            from .feeds import NewsFeeds
            self._news_feeds = NewsFeeds(self.s, self.engine.http)
        return self._news_feeds

    async def news_refresh(self, symbols: list[str] | None = None, market: bool = True, filings: bool | None = None) -> dict:
        """Fetch and store news for ``symbols`` (company news, and their recent filings) and the market
        headlines. Returns the count of new articles and the new ones worth an alert."""
        if not getattr(self.s, "stocks_news", True):
            return {"skipped": "ABG_STOCKS_NEWS=false", "new": 0, "material": []}
        fd = self._feeds()
        if not fd.enabled:
            return {"skipped": "no Finnhub key (ABG_FINNHUB_API_KEY)", "new": 0, "material": []}
        scr = self.screen_rows()
        cap = int(getattr(self.s, "stocks_news_max_names", 40) or 40)
        symbols = list(dict.fromkeys(symbols or []))[:cap]
        arts, failed = [], 0
        if market:
            for cat in ("general", "merger"):
                try:
                    arts += await fd.market_news(cat)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failed += 1
        do_filings = getattr(self.s, "stocks_edgar", True) if filings is None else filings
        for sym in symbols:
            name = (scr.get(sym) or {}).get("name")
            try:
                arts += await fd.company_news(sym, days=3, name=name)
            except asyncio.CancelledError:
                raise
            except Exception:
                failed += 1
            if do_filings:
                try:
                    arts += await fd.filings(sym, days=5, name=name)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failed += 1
        have = {r["id"] for r in self.db.q("SELECT id FROM news WHERE at>=?",
                                           ((datetime.now(NY) - timedelta(days=8)).isoformat(),))}
        fresh = [a for a in arts if a["id"] not in have]
        n = N.store(self.db, arts)
        N.roll_daily(self.db)
        self.last_news = {"at": datetime.now(NY).isoformat(timespec="seconds"), "symbols": len(symbols), "fetched": len(arts),
                          "new": n, "failed_calls": failed}
        return {**self.last_news, "material": [a for a in fresh if a.get("symbol") and N.material(a, self.cfg)]}

    def sentiment_for(self, symbol: str, day: str | None = None) -> dict:
        day = day or datetime.now(NY).date().isoformat()
        return N.name_sentiment(N.series(self.db, symbol, 30), day)

    def market_mood(self) -> dict:
        meta = self.screen_meta()

        def last(sym):
            try:
                f = self.store.frame(sym)
                return float(f["close"].iloc[-1]) if len(f) else None
            except Exception:
                return None
        ms = N.series(self.db, "*", 3)
        tot = sum(r["n"] for r in ms)
        mood = N.market_sentiment(meta.get("breadth"), last("VIX"), last("VIX3M"), meta.get("sectors_up"),
                                  round(sum(r["score"] * r["n"] for r in ms) / tot, 4) if tot else None, tot)
        mood["sentence"] = N.mood_sentence(mood)
        mood["as_of"] = meta.get("day")
        return mood

    def shocks(self, moves: dict | None = None, day: str | None = None, save: bool = True) -> list[dict]:
        """Theme shocks for ``day`` from the day's moves (the last close's by default) and its headlines."""
        day = day or datetime.now(NY).date().isoformat()
        if moves is None:
            moves = {s: {"chg": r["chg_1d"]} for s, r in self.screen_rows().items() if r.get("chg_1d") is not None}
        since = (date.fromisoformat(day) - timedelta(days=1)).isoformat() + "T16:00:00"
        arts = N.recent(self.db, since=since, limit=600, kind="news")
        out = N.theme_shocks(self.themes(), moves, arts, self.cfg, day)
        if save:
            self.db.kv_set("shocks", {"day": day, "shocks": out})
        return out

    def last_shocks(self) -> list[dict]:
        return (self.db.kv_get("shocks") or {}).get("shocks") or []

    def research_context(self, symbol: str, fund: dict | None = None, peers: list[dict] | None = None,
                         day: str | None = None, mood: dict | None = None) -> dict:
        """What a pitch takes from the screen and the news store: grades, sentiment, flags, themes and the
        two conviction terms. Empty pieces when there is no data; never raises."""
        try:
            row = dict(self.screen_rows().get(symbol) or {})
            sent = self.sentiment_for(symbol, day)
            row["sent"], row["sent_n"] = sent.get("level"), sent.get("n")
            fpe = sorted(x["forward_pe"] for x in (peers or []) if isinstance(x, dict) and (x.get("forward_pe") or 0) > 0)
            peer_pe = fpe[len(fpe) // 2] if fpe else None
            since = (datetime.now(NY) - timedelta(days=4)).isoformat()
            arts = N.recent(self.db, symbol, since=since, limit=25)
            nc = N.news_cfg(self.cfg)
            mood = mood or {}
            return {"grades": RK.grades(row, fund, peer_pe),
                    "sentiment": {"name": sent if sent.get("level") is not None else None, "market": mood.get("label")},
                    "flags": N.risk_flags(symbol, arts, sent, self.cfg, self.last_shocks()),
                    "themes": N.themes_of(symbol, self.themes()),
                    "conviction_news": {"level": sent.get("level"), "n": sent.get("n"), "min_n": nc["min_articles"],
                                        "positive": nc["positive"], "negative": nc["negative"], "market": mood.get("label")}}
        except Exception as e:
            log.info("stocks: research context for %s failed (%s)", symbol, type(e).__name__)
            return {}

    # ================================================================== the morning watchlist
    def watch_rows(self, live_only: bool = False) -> list[dict]:
        out = []
        for r in self.db.q("SELECT * FROM watch ORDER BY drank, symbol"):
            try:
                card = json.loads(r["data"])
            except ValueError:
                continue
            if live_only and r["status"] not in WL.LIVE:
                continue
            out.append({"symbol": r["symbol"], "status": r["status"], "added": r["added"], "updated": r["updated"],
                        "drank": r["drank"], "card": card})
        return out

    def watch_meta(self) -> dict:
        return self.db.kv_get("watch_meta") or {}

    def _base_by_book(self) -> dict:
        return self.db.kv_get("base_by_book") or {}

    async def _ipos(self, today: date, wc: dict) -> list[dict]:
        out = []
        fd = self._feeds()
        if fd.enabled:
            try:
                out = await fd.ipo_calendar(today - timedelta(days=wc["ipo_days_back"]), today + timedelta(days=wc["ipo_days_ahead"]))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info("stocks: IPO calendar unavailable (%s)", type(e).__name__)
        seen = {x["symbol"] for x in out if x.get("symbol")}
        since = (today - timedelta(days=wc["ipo_days_back"])).isoformat()
        for sym, r in self.screen_rows().items():                # new listings the calendar missed
            if r.get("is_stock") and r.get("too_new") and (r.get("first_day") or "") >= since and sym not in seen \
                    and (r.get("sessions") or 0) < 40 and (r.get("adv20_usd") or (r.get("close") or 0) * 1e6) > 5e6:
                out.append({"date": r["first_day"], "symbol": sym, "name": r.get("name") or sym, "exchange": None,
                            "price": None, "shares": None, "value": None, "status": "listed"})
        self.db.kv_set("ipos", {"at": today.isoformat(), "rows": out[:80]})
        return out[:40]

    async def morning(self, now: datetime | None = None, post: bool | None = None) -> dict:
        """The morning scan: build today's watchlist, record what changed, and (when asked) post it."""
        now = now or datetime.now(NY)
        today = now.date()
        wc = WL.watch_cfg(self.cfg)
        scr = self.screen_rows()
        prev = {r["symbol"]: r for r in self.watch_rows()}
        pitches = self.pitches()
        open_p = [p for p in pitches if p.get("status") in ("new", "active")]
        board = self.board() or {}
        near = sorted(((min(s["steps"] for s in r["setups"]), -(r.get("mom_pct") or 0), sym) for sym, r in scr.items()
                       if r.get("eligible") and r.get("setups")))[:20]
        movers = sorted((s for s, r in scr.items() if r.get("eligible") and r.get("chg_1d") is not None),
                        key=lambda s: -abs(scr[s]["chg_1d"]))[:12]
        focus = list(dict.fromkeys([p["symbol"] for p in board.get("picks") or []] + [p["symbol"] for p in open_p]
                                   + [s for s, r in prev.items() if r["status"] in WL.LIVE] + [x[2] for x in near] + movers))
        news = await self.news_refresh(focus)
        mood = self.market_mood()
        shocks = self.shocks(day=today.isoformat())
        since = (now - timedelta(hours=24)).isoformat()
        in_play: dict = {}
        nc = N.news_cfg(self.cfg)
        for sym in focus:
            arts = [a for a in N.recent(self.db, sym, since=since, limit=12) if N.material(a, self.cfg)]
            sent = self.sentiment_for(sym, today.isoformat())
            row = scr.get(sym)
            if row is not None:
                row["sent"], row["sent_n"] = sent.get("level"), sent.get("n")
            spike = (sent.get("volume_ratio") or 0) >= nc["volume_spike"] and (sent.get("n_today") or 0) >= nc["min_articles"]
            if arts or spike:
                top = arts[0] if arts else None
                why = (top["title"] if top else f"{sent['n_today']} articles today, {sent['volume_ratio']:.1f}× its usual news flow")
                in_play[sym] = {"why": why, "links": [{"title": a["title"], "url": a["url"]} for a in arts[:3] if a.get("url")],
                                "source": "news"}
        ipos = await self._ipos(today, wc)
        packs = [c for c in (self.today().get("candidates") or [])
                 if ((c.get("plan") or {}).get("entry") or {}).get("date") == today.isoformat()]
        prev_top = self.db.kv_get("mom_top_prev")
        cards = WL.build(scr, lambda r: r.get("setups") or [], packs, today, self.cfg, self._base_by_book(),
                         set(prev_top) if prev_top else None, in_play, ipos, None, shocks, self.themes())
        try:
            cal = await self._earnings([c["symbol"] for c in cards])
            for c in cards:
                if cal.get(c["symbol"]) and not c["timeline"].get("earnings"):
                    c["timeline"]["earnings"] = cal[c["symbol"]]
            self.db.kv_set("earnings", {"at": today.isoformat(), "dates": cal})
        except Exception:
            pass
        latest: dict[str, str] = {}
        for p in sorted(pitches, key=lambda p: p["as_of"]):
            latest[p["symbol"]] = p.get("status")
        rows, ch = WL.advance(prev, cards, latest, today)
        t = today.isoformat()
        for r in rows:
            self.db.x("INSERT INTO watch(symbol, status, added, updated, drank, data) VALUES(?,?,?,?,?,?) "
                      "ON CONFLICT(symbol) DO UPDATE SET status=excluded.status, added=excluded.added, "
                      "updated=excluded.updated, drank=excluded.drank, data=excluded.data",
                      (r["symbol"], r["status"], r["added"], r["updated"], r["drank"], json.dumps(r["card"], default=str)))
            if r["changed"]:
                self.db.log("watch", r["symbol"], r["status"], f"rank {r['drank']}: {(r['card'].get('why') or '')[:160]}")
        for a in ch["added"]:
            row = scr.get(a["symbol"]) or {}
            self.db.x("INSERT OR IGNORE INTO event(key, at, kind, symbol, data) VALUES(?,?,?,?,?)",
                      (f"watch_add|{a['symbol']}|{t}", now.isoformat(timespec="seconds"), "watch_add", a["symbol"],
                       json.dumps({"symbol": a["symbol"], "day": t, "drank": a["drank"], "price": row.get("close")})))
        meta = {"day": t, "at": now.isoformat(timespec="seconds"), "changed": ch, "changed_line": WL.changed_line(ch),
                "mood": mood, "shocks": shocks, "names": sum(1 for r in rows if r["status"] in WL.LIVE),
                "news": {k: v for k, v in news.items() if k != "material"},
                "name_of_the_day": (lambda c: {"symbol": c["symbol"], "text": WL.name_writeup(c)} if c else None)(WL.name_of_the_day(rows)),
                "posted": (self.watch_meta().get("posted") if self.watch_meta().get("day") == t else None)}
        self.db.kv_set("watch_meta", meta)
        self._broadcast("stocks_watch", {"day": t, "names": meta["names"], "changed": meta["changed_line"]})
        if post:
            meta["posted"] = await self.watch_post(now)
        return meta

    async def watch_post(self, now: datetime | None = None) -> str:
        now = now or datetime.now(NY)
        meta = self.watch_meta()
        if meta.get("day") != now.date().isoformat():
            return "no watchlist for today"
        rows = self.watch_rows()
        sm = self.screen_meta()
        e = WL.embed(rows, meta.get("changed") or {}, now, (meta.get("mood") or {}).get("sentence"), meta.get("shocks"),
                     sm.get("day"), sm, (getattr(self.s, "stocks_tag", "") or "").strip(), WL.watch_cfg(self.cfg)["post"])
        meta["posted"] = "pending"
        self.db.kv_set("watch_meta", meta)                       # mark first: a failed post is not retried in a loop
        res = await self._post_to(self._webhook(), [e])
        meta["posted"] = res
        self.db.kv_set("watch_meta", meta)
        return res

    def _watch_due(self, now: datetime) -> str | None:
        """"scan" at ABG_STOCKS_WATCH_SCAN_TIME, "post" at ABG_STOCKS_WATCH_POST_TIME, once each per trading day."""
        from abg.live.market_hours import is_trading_day
        if not getattr(self.s, "stocks_watch", True) or not is_trading_day(now.date()):
            return None
        meta = self.watch_meta()
        mine = meta.get("day") == now.date().isoformat()
        hm = (now.hour, now.minute)
        if not mine and _hm(getattr(self.s, "stocks_watch_scan_time", "08:30"), (8, 30)) <= hm < (16, 0):
            return "scan"
        if mine and not meta.get("posted") and getattr(self.s, "stocks_watch_post", False) \
                and _hm(getattr(self.s, "stocks_watch_post_time", "08:45"), (8, 45)) <= hm < (9, 30):
            return "post"
        return None

    def watch_score(self) -> dict:
        adds = []
        for r in self.db.q("SELECT data FROM event WHERE kind='watch_add' ORDER BY at"):
            try:
                adds.append(json.loads(r["data"]))
            except ValueError:
                continue
        days: dict[str, list[str]] = {}
        for p in self.pitches():
            days.setdefault(p["symbol"], []).append(p["as_of"])
        bench = self.cfg.get("benchmark", "SPY")

        def closes(sym):
            f = self.store.frame(sym)
            return f["close"] if len(f) else None
        return WL.score(adds, days, closes, lambda: closes(bench))

    # ================================================================== the live layer
    async def live_tick(self, now: datetime | None = None, post: bool | None = None) -> dict:
        """One look at the watched names: quotes -> events -> the gate -> one post (if alerts are on)."""
        now = now or datetime.now(NY)
        ac = AL.alert_cfg(self.cfg)
        today = now.date().isoformat()
        scr = self.screen_rows()
        tg = AL.targets(self.board(), self.pitches(), self.watch_rows(live_only=True), scr, today)
        cap = int(getattr(self.s, "stocks_live_max_names", 40) or 40)
        order = {"pick": 0, "pitch": 1, "watch": 2}
        syms = sorted(tg, key=lambda s: (order[tg[s]["role"]], s))[:cap]
        st = self.db.kv_get("live_state") or {}
        if st.get("day") != today:
            st = {"day": today, "states": {}}
        events: list[dict] = []
        quotes = await self._quotes(syms) if syms and AL.in_session(now) else {}
        for sym in syms:
            if sym not in quotes:
                continue
            ev, st["states"][sym] = AL.detect(tg[sym], quotes[sym], st["states"].get(sym), now, ac)
            events += ev
        degraded = None
        if syms and AL.in_session(now) and len(quotes) < max(1, len(syms) // 2):
            self._live_misses = getattr(self, "_live_misses", 0) + 1
            degraded = f"live quotes are unavailable for {len(syms) - len(quotes)} of {len(syms)} watched names"
        else:
            self._live_misses = 0
        # news, on its own slower clock
        every = max(5, int(getattr(self.s, "stocks_news_minutes", 30) or 30))
        last = getattr(self, "_news_at", 0.0)
        if syms and time.time() - last >= every * 60 and getattr(self.s, "stocks_news", True):
            self._news_at = time.time()
            try:
                res = await self.news_refresh(syms)
                for a in res.get("material") or []:
                    if a["symbol"] in tg:
                        t = tg[a["symbol"]]
                        events.append({"kind": "news", "symbol": a["symbol"], "role": t["role"], "id": t.get("id"),
                                       "book": t.get("book"), "ref": a["id"], "url": a.get("url"),
                                       "text": f"{a['symbol']}: {a['title']}" + (f" ({a['source']})" if a.get("source") else "") + ".",
                                       "plan": "News is context: the plan changes only if a level is hit. Read the source.",
                                       "price": (quotes.get(a["symbol"]) or {}).get("price"), "at": now.isoformat(timespec="seconds"),
                                       "untested": False})
                nc = N.news_cfg(self.cfg)
                for sym in syms:
                    s = self.sentiment_for(sym, today)
                    if s.get("change") is not None and abs(s["change"]) >= abs(nc["collapse"]) and (s.get("n") or 0) >= nc["min_articles"]:
                        t = tg[sym]
                        events.append({"kind": "sentiment", "symbol": sym, "role": t["role"], "id": t.get("id"), "book": t.get("book"),
                                       "text": f"{sym}'s news sentiment moved {s['change']:+.2f} against its 20-day baseline ({s['n']} articles in 3 days).",
                                       "plan": "Sentiment is context and a conviction input; it never moves the stop or the targets.",
                                       "price": (quotes.get(sym) or {}).get("price"), "at": now.isoformat(timespec="seconds"), "untested": False})
                moves = {s: {"chg": q["price"] / q["prev_close"] - 1} for s, q in quotes.items() if q.get("prev_close")}
                base = {s: {"chg": r["chg_1d"]} for s, r in scr.items() if r.get("chg_1d") is not None} if not AL.in_session(now) else {}
                for sh in self.shocks({**base, **moves}, today):
                    touched = [n["symbol"] for n in sh["names"] if n["symbol"] in tg]
                    if touched:
                        src = sh.get("source") or {}
                        events.append({"kind": "theme", "symbol": touched[0], "role": tg[touched[0]]["role"], "id": tg[touched[0]].get("id"),
                                       "book": None, "ref": sh["id"], "url": src.get("url"),
                                       "text": N.shock_sentence(sh) + " Watched names it touches: "
                                               + ", ".join(f"{n['symbol']} {n['chg']:+.1%}" for n in sh["names"] if n["symbol"] in tg) + ".",
                                       "plan": "A theme move is context: each name's own plan and stop still decide.",
                                       "price": None, "at": now.isoformat(timespec="seconds"), "untested": False})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                degraded = (degraded + "; " if degraded else "") + "the news feed did not answer"
                log.info("stocks: live news pass failed (%s)", type(e).__name__)
        self.db.kv_set("live_state", st)
        gate = AL.Gate(self.db, ac)
        out, held = gate.admit(events, now)
        want = getattr(self.s, "stocks_live_alerts", False) if post is None else post
        quiet = ac.get("quiet", True) and not AL.in_session(now)
        posted = "dashboard"
        if out and want and not quiet:
            posted = await self._post_to(self._webhook(), [AL.embed(out, now, held, getattr(self.s, "stocks_dashboard_url", None),
                                                                    (getattr(self.s, "stocks_tag", "") or "").strip(), degraded)])
        elif degraded and want and not quiet and getattr(self, "_live_misses", 0) == 3:
            posted = await self._post_to(self._webhook(), [], content=f"Stock desk live check: {degraded}. Plans are unchanged; "
                                                                         "levels are not being watched until quotes return.")
        self.live = {"last_tick": now.isoformat(timespec="seconds"), "watched": len(syms), "quotes": len(quotes),
                     "events": len(out), "held": held, "degraded": degraded, "posted": posted,
                     "alerts_on": bool(getattr(self.s, "stocks_live_alerts", False))}
        if out:
            self._broadcast("stocks_live", {"events": out, "held": held})
        return {**self.live, "list": out}

    def _live_due(self, now: datetime) -> bool:
        if not getattr(self.s, "stocks_live", True):
            return False
        from abg.live.market_hours import is_trading_day
        if not is_trading_day(now.date()) or not (9, 30) <= (now.hour, now.minute) < (16, 0):
            return False
        every = max(60, int(getattr(self.s, "stocks_live_seconds", 120) or 120))
        return time.time() - getattr(self, "_live_at", 0.0) >= every

    def live_feed(self, hours: int = 30) -> list[dict]:
        since = (datetime.now(NY) - timedelta(hours=hours)).isoformat(timespec="seconds")
        return [e for e in AL.Gate(self.db, AL.alert_cfg(self.cfg)).recent(since) if e.get("kind") in AL.KINDS]

    def forming_stats(self) -> dict:
        ev = AL.Gate(self.db, AL.alert_cfg(self.cfg)).recent("2000-01-01", limit=5000)
        return AL.forming_stats(ev, {(p["symbol"], p["as_of"]) for p in self.pitches()})

    # ================================================================== Explore
    def explore(self, q: str = "", limit: int = 25, only: str | None = None) -> list[dict]:
        q = (q or "").strip().upper()
        scr = self.screen_rows()
        hits = []
        for sym, r in scr.items():
            if not r.get("is_stock"):
                continue
            if only == "eligible" and not r.get("eligible"):
                continue
            if only == "new" and not r.get("too_new"):
                continue
            name = (r.get("name") or "").upper()
            if q and not (sym.startswith(q) or q in name):
                continue
            score = (0 if sym == q else 1 if sym.startswith(q) else 2, -(r.get("adv20_usd") or 0))
            hits.append((score, sym, r))
        out = []
        for _, sym, r in sorted(hits, key=lambda h: h[0])[:limit]:
            steps = min((s["steps"] for s in r.get("setups") or []), default=None)
            out.append({"symbol": sym, "name": r.get("name"), "sector": r.get("sector"), "close": r.get("close"),
                        "chg_1d": r.get("chg_1d"), "eligible": r.get("eligible"), "too_new": r.get("too_new"),
                        "why_not": r.get("why_not"), "steps": steps, "grades": RK.grades(r),
                        "drank": RK.daisy_rank(None, steps_away=steps) if r.get("eligible") and steps is not None else None})
        return out

    def name_card(self, symbol: str) -> dict | None:
        """Everything about one name: why it is or isn't pitched, rule by rule, with its news."""
        sym = symbol.strip().upper()
        r = self.screen_rows().get(sym)
        if r is None:
            return None
        r = dict(r)
        day = self.screen_meta().get("day")
        sent = self.sentiment_for(sym)
        r["sent"], r["sent_n"] = sent.get("level"), sent.get("n")
        mine = [p for p in self.pitches() if p["symbol"] == sym]
        open_p = [p for p in mine if p.get("status") in ("new", "active")]
        setups = r.get("setups") or []
        steps = min((s["steps"] for s in setups), default=None)
        if open_p:
            drank = open_p[-1].get("drank") or 3
            verdict = f"Pitched {open_p[-1]['as_of']} ({open_p[-1].get('rating')}); the pitch is {open_p[-1]['status']}."
        elif not r.get("eligible"):
            drank, verdict = None, f"Researched, not pitched: {r.get('why_not') or 'not eligible'}."
        elif r.get("too_new") and not setups:
            drank, verdict = None, "Too new for the tested rules (they need about a year of history): watched and researched, not rule-pitched."
        elif setups:
            drank = RK.daisy_rank(None, steps_away=steps)
            verdict = f"Not pitched yet: {setups[0]['steps']} step(s) from the {setups[0]['book'].replace('_', ' ')} rule. It still needs {setups[0]['trigger']}."
        else:
            drank, verdict = 4, "Not pitched: no rule is close to a trigger on it."
        return {"symbol": sym, "as_of": day, "row": r, "drank": drank, "rank_words": RK.RANK_WORDS.get(drank),
                "verdict": verdict, "grades": RK.grades(r), "setups": setups, "gates": SC.gate_report(r, self.cfg),
                "sentiment": sent, "sentiment_series": N.series(self.db, sym, 30),
                "news": [{k: a[k] for k in ("at", "title", "url", "source", "kind", "sent", "rel")}
                         for a in N.recent(self.db, sym, limit=15)],
                "themes": [{"key": k, "label": self.themes()[k]["label"]} for k in N.themes_of(sym, self.themes())],
                "pitches": [{k: p.get(k) for k in ("id", "as_of", "book", "rating", "status", "close_reason", "track")} for p in mine[-8:]],
                "watch": next((w for w in self.watch_rows() if w["symbol"] == sym), None)}

    def new_listings(self) -> dict:
        return self.db.kv_get("ipos") or {"at": None, "rows": []}

    # ================================================================== Today: heat strip and calendar
    def heat(self) -> dict:
        scr = self.screen_rows()
        etf = {"XLK": "Technology", "XLF": "Financials", "XLV": "Health care", "XLY": "Consumer disc.", "XLP": "Staples",
               "XLI": "Industrials", "XLE": "Energy", "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real estate",
               "XLC": "Communication"}
        sectors = [{"symbol": s, "label": lab, "chg_1d": scr[s].get("chg_1d"), "chg_5d": scr[s].get("chg_5d"),
                    "above_50": bool(scr[s].get("close") and scr[s].get("sma50") and scr[s]["close"] > scr[s]["sma50"])}
                   for s, lab in etf.items() if s in scr]
        themes = []
        for k, t in self.themes().items():
            ch = sorted(scr[s]["chg_1d"] for s in t["symbols"] if s in scr and scr[s].get("chg_1d") is not None)
            if ch:
                themes.append({"key": k, "label": t["label"], "chg_1d": round(ch[len(ch) // 2], 4), "n": len(ch)})
        meta = self.screen_meta()
        st = (self.today().get("state") or {})
        return {"as_of": meta.get("day"), "sectors": sectors, "themes": sorted(themes, key=lambda x: -(x["chg_1d"] or 0)),
                "breadth": meta.get("breadth"), "regime": st.get("regime"), "stress": st.get("stress"), "crisis": st.get("crisis")}

    def calendar(self, days: int = 14) -> dict:
        today = datetime.now(NY).date()
        end = (today + timedelta(days=days)).isoformat()
        names = {r["symbol"] for r in self.watch_rows(live_only=True)} | {p["symbol"] for p in self.pitches() if p.get("status") in ("new", "active")}
        cal = (self.db.kv_get("earnings") or {}).get("dates") or {}
        rows = [{"date": d, "kind": "earnings", "symbol": s, "text": f"{s} reports earnings"} for s, d in cal.items()
                if s in names and today.isoformat() <= d <= end]
        for x in (self.db.kv_get("ipos") or {}).get("rows") or []:
            if today.isoformat() <= x["date"] <= end:
                rows.append({"date": x["date"], "kind": "ipo", "symbol": x.get("symbol"),
                             "text": f"IPO: {x['name']}" + (f" ({x['symbol']})" if x.get("symbol") else "")})
        for p in self.pitches():
            rv = (p.get("plan") or {}).get("review_date")
            if p.get("status") in ("new", "active") and rv and today.isoformat() <= rv <= end:
                rows.append({"date": rv, "kind": "review", "symbol": p["symbol"], "text": f"{p['symbol']}: month-end review"})
        return {"from": today.isoformat(), "to": end, "rows": sorted(rows, key=lambda r: (r["date"], r["kind"], r["symbol"] or ""))}

    # ================================================================== team notes and votes
    def notes(self, pitch_id: str) -> list[dict]:
        return [dict(r) for r in self.db.q("SELECT id, at, name, vote, text FROM note WHERE pitch_id=? ORDER BY id", (pitch_id,))]

    def add_note(self, pitch_id: str, name: str, vote: str | None, text: str | None) -> dict:
        name = " ".join((name or "").split())[:40]
        vote = (vote or "").lower().strip() or None
        text = (text or "").strip()[:600] or None
        if not name:
            raise ValueError("a display name is needed")
        if vote not in (None, "up", "down", "hold"):
            raise ValueError("vote must be up, down or hold")
        if not vote and not text:
            raise ValueError("a note needs a vote or some text")
        if self.book.get(pitch_id) is None and self.pack(pitch_id) is None:
            raise KeyError(pitch_id)
        at = datetime.now(NY).isoformat(timespec="seconds")
        self.db.x("INSERT INTO note(pitch_id, at, name, vote, text) VALUES(?,?,?,?,?)", (pitch_id, at, name, vote, text))
        return {"pitch_id": pitch_id, "at": at, "name": name, "vote": vote, "text": text}

    def votes(self, pitch_id: str) -> dict:
        latest: dict[str, str] = {}
        for n in self.notes(pitch_id):
            if n["vote"]:
                latest[n["name"].lower()] = n["vote"]                # one vote per name: the latest
        return {v: sum(1 for x in latest.values() if x == v) for v in ("up", "down", "hold")}

    # ================================================================== the weekly recap
    def recap(self, now: datetime | None = None) -> dict:
        """What we pitched this week, what closed and how, and the scorecard, in five bullets."""
        now = now or datetime.now(NY)
        monday = (now.date() - timedelta(days=now.weekday())).isoformat()
        rows = self.mark_pitches()
        new = [p for p in rows if p["as_of"] >= monday]
        closed = [p for p in rows if (p.get("closed_at") or "") >= monday]
        from . import pitchbook as PB
        allr = PB.agg(rows)

        def pct(x):
            return "not in data" if x is None else f"{x:+.1%}"
        wins = [p for p in closed if ((p.get("track") or {}).get("ret") or 0) > 0]
        best = max(closed, key=lambda p: (p.get("track") or {}).get("ret") or -9, default=None)
        worst = min(closed, key=lambda p: (p.get("track") or {}).get("ret") or 9, default=None)
        bullets = [
            f"Pitched {len(new)} idea(s) this week" + (": " + ", ".join(sorted({p['symbol'] for p in new})[:12]) if new else "") + ".",
            (f"{len(closed)} pitch(es) closed: {len(wins)} up, {len(closed) - len(wins)} down or flat"
             + (f". Best {best['symbol']} {pct((best.get('track') or {}).get('ret'))}, worst {worst['symbol']} {pct((worst.get('track') or {}).get('ret'))}" if closed else "") + "."),
            f"Open now: {allr['open']} pitch(es). All pitches from the tested entry: average {pct(allr['avg_ret'])}, "
            f"{pct(allr['avg_excess_spy'])} against SPY.",
            f"Beat SPY on {('not in data' if allr['beat_spy'] is None else format(allr['beat_spy'], '.0%'))} of {allr['marked']} marked pitch(es).",
            (lambda f: f"Setup-forming flags (untested): {f['confirmed']} of {f['n']} confirmed at the close." if f["n"] else
             "Setup-forming flags (untested): none recorded yet.")(self.forming_stats()),
        ]
        return {"week_of": monday, "at": now.isoformat(timespec="seconds"), "bullets": bullets,
                "new": [p["id"] for p in new], "closed": [p["id"] for p in closed]}

    def recap_embed(self, rec: dict) -> dict:
        tag = (getattr(self.s, "stocks_tag", "") or "").strip()
        return {"title": (f"[{tag}] " if tag else "") + f"Weekly recap — week of {rec['week_of']}",
                "description": "\n".join(f"• {b}" for b in rec["bullets"])[:3900], "color": 0x446266,
                "footer": {"text": "Stock desk · pitch track record, from the tested entry · research, not advice"}}

    def _recap_due(self, now: datetime) -> bool:
        if not getattr(self.s, "stocks_weekly_recap", False) or now.weekday() != 4:
            return False
        if (now.hour, now.minute) < _hm(getattr(self.s, "stocks_weekly_recap_time", "16:45"), (16, 45)):
            return False
        return self.db.kv_get("recap_posted") != now.date().isoformat()

    async def recap_post(self, now: datetime | None = None) -> str:
        now = now or datetime.now(NY)
        self.db.kv_set("recap_posted", now.date().isoformat())
        rec = await asyncio.to_thread(self.recap, now)
        self.db.kv_set("recap_last", rec)
        return await self._post_to(self._webhook(), [self.recap_embed(rec)])
