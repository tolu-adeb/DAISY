"""The stock desk: a stock-pitching desk. The rule books run every day and the desk pitches what they find.

    daily      after the close (ABG_STOCKS_DAILY_TIME, 16:20 ET): replay the books to today, write a
               thesis for everything they want to open or close, post a digest plus one embed per
               pitch, and record every candidate - including the ones that were skipped
    intraday   every ABG_STOCKS_SWEEP_MINUTES while the market is open: refresh the next page of the
               universe (live alerts on the picks are abg/stocks/alerts.py)
    scorecard  the pitch track record: every pitch from its tested entry (the next open) against SPY
               and its sector ETF. There is no portfolio here: no equity, no positions, no orders

The rule engine still replays history on every pass. That replay is a research tool - it gives the base
rates, the regime and which names the rules pick - and none of its simulated holdings are shown.

State lives in ``<data_dir>/stocks/``: ``state.json`` (schedule bookkeeping), ``ledger.jsonl`` (every
candidate ever seen), ``desk.sqlite3`` (pitches and their lifecycle, watchlist, news), ``memos/<id>.md``.
Bars live in ``<data_dir>/stocks.sqlite3``.

Secrets: the Discord webhook is read from the settings at post time and never written to a file, a
memo, the ledger or the dashboard. Only whether one is configured is ever reported.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from abg.utils import jsonable

from . import board as B
from . import pitch_plan as P
from . import pitchbook as PB
from . import thesis as T
from .deskdb import DeskDB
from .config import build_config, describe as describe_cfg
from .daybook import DayMixin
from .live import LiveSignals
from .market import MarketFeed
from .store import BarStore
from .sweep import Sweeper
from .universe import Universe

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

MAX_EMBEDS_PER_POST = 10          # Discord's own cap
DEFAULT_DAILY_TIME = "16:20"
ENRICH_DEFAULT = 6                # how many top candidates get fundamentals + news


class StockDesk(DayMixin):
    def __init__(self, settings, engine, hub=None, symbols_fn=None):
        self.s = settings
        self.engine = engine                     # the terminal's data engine (providers, http)
        self.hub = hub
        self.symbols_fn = symbols_fn             # extra names to follow - callable -> list[str]
        self.dir = Path(settings.data_dir).expanduser() / "stocks"
        self.store = BarStore.from_settings(settings)
        self.db = DeskDB(self.dir / "desk.sqlite3")
        self.book = PB.PitchBook(self.db)
        self._migrate_pitches()
        self.u = Universe.load(self.store)
        if symbols_fn:
            try:
                extra = [s for s in (symbols_fn() or []) if s]
                if extra:
                    self.u.extra = sorted(set(self.u.extra) | set(extra))
            except Exception:
                log.debug("stocks: symbols_fn failed", exc_info=True)
        self.cfg = build_config(settings, self.u)
        self.sweeper = Sweeper(settings, engine, self.store, self.u)
        self.market = MarketFeed(settings, engine.http, self.store, self.u)
        self.last_market: dict | None = None
        self.errors: deque = deque(maxlen=20)
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self.running = False
        self.last_daily: dict | None = None
        self.last_sweep: dict | None = None
        self.last_state = None
        self.research_busy: str | None = None
        self.live: dict | None = None            # the live layer's heartbeat (docs/22.5)
        self.last_news: dict | None = None
        self._screen = None

    # ------------------------------------------------------------------ storage
    def _p(self, name: str) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir / name

    def _load(self, name: str, default):
        p = self.dir / name
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
        except (OSError, ValueError):
            return default

    def _save(self, name: str, obj) -> None:
        p = self._p(name)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
        tmp.replace(p)

    def _append_ledger(self, rows: list[dict]) -> None:
        """Every candidate, taken or not. This is what lets a ranker learn from more than the few
        positions actually opened (Master Edition, principle 10)."""
        if not rows:
            return
        with self._p("ledger.jsonl").open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")

    def ledger_tail(self, n: int = 200) -> list[dict]:
        p = self.dir / "ledger.jsonl"
        if not p.exists():
            return []
        try:
            lines = p.read_text(encoding="utf-8").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

    def pitches(self) -> list[dict]:
        return self.book.rows()

    def _migrate_pitches(self) -> None:
        """Once: carry the paper-era ``pitches.json`` into the pitch book. The file is left where it is."""
        if self.db.kv_get("pitches_json_migrated"):
            return
        old = self._load("pitches.json", [])
        n = self.book.migrate(old)
        self.db.kv_set("pitches_json_migrated", {"rows_in_file": len(old), "carried_over": n})
        if old:
            log.info("stocks: carried %d of %d pitches.json rows into the pitch book", n, len(old))

    # ------------------------------------------------------------------ the fund's real holdings
    def fund_holdings(self) -> dict | None:
        """``{symbol: weight}`` from Settings (kept in the data folder, never in the repo), else from
        ``configs/fund_holdings.yaml``; ``None`` when neither has any ("holdings not provided")."""
        h = self.db.kv_get("fund_holdings")
        if h:
            return h
        try:
            import yaml
            path = Path(getattr(self.s, "stocks_fund_holdings", None) or
                        Path(__file__).resolve().parents[2] / "configs" / "fund_holdings.yaml")
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
        except Exception:
            return None
        return PB_holdings((raw or {}).get("holdings"))

    def set_fund_holdings(self, rows) -> dict | None:
        h = PB_holdings(rows)
        self.db.kv_set("fund_holdings", h or None)
        return h

    # ------------------------------------------------------------------ data upkeep
    async def backfill(self, symbols: list[str] | None = None, period: str | None = None) -> dict:
        rep = await self.sweeper.backfill(symbols, period)
        await asyncio.to_thread(self.store.checkpoint)
        log.info("stocks backfill: %s", rep.to_dict())
        return rep.to_dict()

    async def sweep(self, page: int | None = None) -> dict:
        rep = await self.sweeper.pass_once(page)
        self.last_sweep = rep.to_dict()
        self._broadcast("stocks_sweep", self.last_sweep)
        return self.last_sweep

    # ------------------------------------------------------------------ signals
    def _signals(self) -> LiveSignals:
        panel_start = None
        if self.u.market:
            # thousands of names: keep the panel to the replay window plus indicator warm-up, or the
            # indicator tables alone run to gigabytes
            import pandas as pd
            last = self.store.stats().get("last_day")
            if last:
                yrs = int(getattr(self.s, "stocks_market_replay_years", 2) or 2)
                panel_start = str((pd.Timestamp(last) - pd.DateOffset(years=yrs) - pd.Timedelta(days=420)).date())
        ls = LiveSignals(self.cfg, self.store, self.u).build(start=panel_start, warn=True)
        ls.run(start=self._replay_start(ls))
        return ls

    def _replay_start(self, ls: LiveSignals) -> str | None:
        """How far back the live replay starts. Positions and Hedge weights converge long before this,
        and replaying from the rule file's 2007 start on every daily pass would cost minutes for nothing."""
        import pandas as pd
        years = int(getattr(self.s, "stocks_replay_years", 5) or 0)
        if self.u.market:
            years = min(years or 99, int(getattr(self.s, "stocks_market_replay_years", 2) or 2))
        if years <= 0 or ls.panel is None or not len(ls.panel.dates):
            return None
        want = ls.panel.dates[-1] - pd.DateOffset(years=years)
        floor = pd.Timestamp(self.cfg.get("start") or ls.panel.dates[0])
        return str(max(want, floor).date())

    async def _peers(self, symbol: str, sector: str | None, limit: int = 6) -> list[dict]:
        """Same-sector names from the store, for the comps leg of the valuation."""
        if not sector:
            return []
        sectors = self.store.sectors()
        same = [t for t, sec in sectors.items()
                if sec == sector and t != symbol and t in set(self.u.stocks)][:limit]
        out = []
        for t in same:
            try:
                f = (await self.engine.fundamentals(t)).value
                d = f.__dict__ if hasattr(f, "__dict__") else dict(f)
                out.append({"symbol": t, "pe": d.get("pe"), "forward_pe": d.get("forward_pe"),
                            "eps": d.get("eps")})
            except Exception:
                continue
        return out

    async def _enrich(self, symbol: str) -> tuple[dict | None, list[dict], list[dict], str | None]:
        """Fundamentals, peers and headlines for one name. Any failure degrades to None, never raises:
        a missing valuation must not lose a signal."""
        fund, peers, news, provider = None, [], [], None
        try:
            res = await self.engine.fundamentals(symbol)
            f = res.value
            fund = dict(f.__dict__) if hasattr(f, "__dict__") else dict(f)
            provider = getattr(res.provenance, "provider", None)
            sec = fund.get("sector")
            if sec and self.store.sectors().get(symbol) != sec:
                self.store.set_sectors({**self.store.sectors(), symbol: sec})
        except Exception as e:
            log.debug("stocks: no fundamentals for %s (%s)", symbol, e)
        try:
            peers = await self._peers(symbol, (fund or {}).get("sector"))
        except Exception:
            peers = []
        try:
            items = (await self.engine.news(symbol, limit=4)).value
            news = [dict(x.__dict__) if hasattr(x, "__dict__") else dict(x) for x in items]
        except Exception:
            news = []
        return fund, peers, news, provider

    def _charts(self, packs: list[dict]) -> None:
        """Price, valuation and peer charts next to each memo (pack["charts"]); never fatal."""
        from . import charts as CH
        if not packs or not CH.available():
            return
        out = self._p("memos")
        for p in packs:
            try:
                p["charts"] = CH.render_all(p, self.store.frame(p["symbol"]), out)
            except Exception as e:                  # a chart is decoration: log, keep the pitch
                log.info("stocks: charts for %s failed (%s)", p["symbol"], type(e).__name__)

    async def _writeups(self, packs: list[dict]) -> None:
        """Claude's write-up for the pitches that will be posted in detail (it costs tokens per call).
        Only fields that pass the validator are used; the rest stay template text."""
        from . import writeup as W
        cap = int(getattr(self.s, "stocks_post_max", 5) or 5)
        top = sorted((p for p in packs if (p.get("rec") or {}).get("rating") in ("BUY", "ADD")
                      and p["book"] != "dual_momentum"),
                     key=lambda p: -((p.get("rec") or {}).get("conviction") or {}).get("score", 0))[:cap]
        for p in top:
            W.apply(p, await W.generate(self.s, self.engine.http, p))

    def _pitch_context(self, ls: LiveSignals, state) -> dict:
        """What every pitch's plan needs: the replay's base rates, our own open pitches and the fund's
        real holdings (docs/21.6, docs/22.1). Nothing from the replay's simulated book."""
        r = ls.result or {}
        eq = r.get("equity")
        period = (str(eq.index[0].date()), str(eq.index[-1].date())) if eq is not None and len(eq) else None
        tp = (self.cfg.get("books") or {}).get("trend_pullback", {})
        ml = tp.get("ml") or {}
        book = getattr(self, "book", None)
        return {"trades": r.get("trades"), "period": period, "store": self.store,
                # pitches made before this session; today's own are this pass, not a prior idea
                "open_pitches": {sym: bk for sym, bk in ((s_, {b: r for b, r in d.items() if r["as_of"] < state.date})
                                                         for s_, d in (book.open_by_symbol() if book else {}).items()) if bk},
                "fund_holdings": self.fund_holdings() if book else None,
                "sectors": self.store.sectors(),
                "ml_threshold": float(ml.get("threshold", 0.55)) if ml.get("enabled") else None}

    async def _earnings(self, symbols: list[str]) -> dict[str, str]:
        """Next earnings date per symbol from Finnhub's calendar (two calls for ~45 days). Empty on any
        failure or without a key: a missing date is shown as not in data, never guessed."""
        if not symbols or not getattr(self.s, "finnhub_api_key", None):
            return {}
        try:
            from abg.swing.wide import Sources
            today = datetime.now(NY).date()
            cal = await Sources(self.s, self.engine.http).earnings(today, today + timedelta(days=45))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("stocks: earnings calendar unavailable (%s)", type(e).__name__)
            return {}
        want = set(symbols)
        return {s: ds[0] for s, ds in cal.items() if s in want and ds}

    async def daily(self, post: bool = True, enrich: int | None = None) -> dict:
        """The main event: today's signals, written up, posted and recorded."""
        await self.load_index_if_seed()            # weekly re-check; never fatal
        async with self._lock:
            t0 = time.time()
            ls = self._signals()
            cands = ls.candidates()
            state = ls.state()
            self.last_state = state
            # research around the rules (docs/22.2-22.3): the whole-market screen, then the news. Each is
            # a bonus: a failure is logged and the pitches go out without it.
            screen_meta: dict = {}
            try:
                screen_meta = await asyncio.to_thread(self.screen_refresh, ls)
                r_ = ls.result or {}
                eq_ = r_.get("equity")
                per_ = (str(eq_.index[0].date()), str(eq_.index[-1].date())) if eq_ is not None and len(eq_) else None
                self.db.kv_set("base_by_book", {b: {k: v for k, v in P.base_rates(r_.get("trades"), b, per_).items()
                                                    if k in ("n", "median_hold_days", "median_win_R", "win_rate", "small_sample")}
                                                for b in ("trend_pullback", "mean_reversion", "xs_momentum")})
            except Exception as e:
                log.warning("stocks: screen refresh failed (%s: %s)", type(e).__name__, str(e)[:160])
                self.errors.append({"ts": time.time(), "where": "screen", "error": f"{type(e).__name__}: {e}"[:300]})
            mood: dict = {}
            try:
                await self.news_refresh(sorted({c.symbol for c in cands if c.book != "dual_momentum"}))
                mood = self.market_mood()
                self.shocks(day=state.date)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info("stocks: news pass failed (%s)", type(e).__name__)

            n_enrich = ENRICH_DEFAULT if enrich is None else enrich
            # only entries in the single-stock books are worth a valuation, and only the best few
            worth = [c for c in cands
                     if c.is_entry and c.book in ("trend_pullback", "mean_reversion", "xs_momentum")]
            worth.sort(key=lambda c: (c.rank or 99))
            enrich_set = {c.symbol for c in worth[:max(0, n_enrich)]}

            await asyncio.to_thread(self.mark_pitches)      # lifecycle first: ratings are against open pitches
            base_ctx = self._pitch_context(ls, state)
            entry_syms = sorted({c.symbol for c in cands if c.is_entry})
            earnings = await self._earnings(entry_syms)
            packs: list[dict] = []
            for c in cands:
                fund = peers = news = provider = None
                if c.symbol in enrich_set:
                    fund, peers, news, provider = await self._enrich(c.symbol)
                ctx = {**base_ctx, "bars": self.store.frame(c.symbol) if c.is_entry else None,
                       "earnings": earnings.get(c.symbol), "fund_provider": provider}
                if c.book != "dual_momentum":
                    ctx.update(self.research_context(c.symbol, fund, peers, state.date, mood))
                try:
                    packs.append(T.build_pack(c, state, self.cfg, fund=fund, peers=peers or [],
                                              news=news or [], provider=provider, ctx=ctx))
                except ValueError as e:
                    # a units mismatch or out-of-order levels is a data fault: say so, keep the signal
                    self.errors.append({"ts": time.time(), "where": f"pitch {c.symbol}", "error": str(e)[:300]})
                    log.warning("stocks: plan for %s failed its checks (%s); pitching without it", c.symbol, e)
                    packs.append(T.build_pack(c, state, self.cfg, fund=None, peers=[], news=news or [],
                                              provider=provider, ctx={**ctx, "earnings": None}))

            if getattr(self.s, "pitch_ai_memo", False) and getattr(self.s, "anthropic_api_key", None):
                await self._writeups(packs)
            memo_packs = [p for p in packs if _rating(p) in ("BUY", "ADD") and p["book"] != "dual_momentum"]
            if getattr(self.s, "stocks_charts", True):
                await asyncio.to_thread(self._charts, memo_packs)
            for p in packs:
                if _rating(p) in ("BUY", "ADD", "SELL", "TRIM") and p["book"] != "dual_momentum":
                    try:
                        self._p("memos").mkdir(parents=True, exist_ok=True)
                        (self._p("memos") / f"{p['id']}.md").write_text(
                            T.render_md(p), encoding="utf-8")
                        # the whole pack, so a deck (abg pitch deck) can be built later from the same numbers
                        (self._p("memos") / f"{p['id']}.json").write_text(
                            json.dumps(jsonable(p), default=str), encoding="utf-8")
                    except OSError as e:
                        log.warning("stocks: could not write memo %s (%s)", p["id"], e)

            self._append_ledger([_ledger_row(p, state) for p in packs])
            self._record_pitches(packs, state)
            board = B.pick_board(packs, size=max(1, int(getattr(self.s, "stocks_board_size", 5) or 5)),
                                 chase_atr=float(P.pitch_cfg(self.cfg)["chase_atr"]))
            self._save("board.json", board)
            sent = await self._post_daily(packs, state, board) if post else "not posted"

            self.last_daily = {
                "at": datetime.now(NY).isoformat(timespec="seconds"), "as_of": state.date,
                "candidates": len(packs), "opens": sum(1 for p in packs if _rating(p) in ("BUY", "ADD")),
                "closes": sum(1 for p in packs if _rating(p) in ("SELL", "TRIM")),
                "holds": sum(1 for p in packs if _rating(p) == "HOLD"),
                "pitches_closing": sum(1 for p in packs if _rating(p) in ("SELL", "TRIM")
                                       and (p.get("rec") or {}).get("open_pitch")),
                "pitch_book": self.book.counts(state.date), "posted": sent,
                "regime": state.regime, "crisis": state.crisis, "stress": state.stress,
                "seconds": round(time.time() - t0, 1), "enriched": sorted(enrich_set),
                "screen": {k: screen_meta.get(k) for k in ("covered", "eligible", "too_new", "in_rule_panel", "seconds")},
                "mood": mood.get("label"), "memory_mb": _peak_mb(),
                # did the wider market have this session's bar? False = only the per-symbol core could signal
                "market_bar": (not self.market.enabled) or (not self.u.market) or self._market_bar(state.date),
            }
            st = self._load("state.json", {})
            st["last_daily"] = self.last_daily
            st["last_daily_day"] = state.date
            self._save("state.json", st)
            await asyncio.to_thread(self.store.checkpoint)
            self._save("today.json", {"state": _public_state(state), "pitch_book": self.book.counts(state.date),
                                      "candidates": [_public(p) for p in packs]})
            self._broadcast("stocks_daily", self.last_daily)
            return self.last_daily

    # ------------------------------------------------------------------ pitch record
    def _record_pitches(self, packs: list[dict], state) -> None:
        """One pitch per name the rules say to buy (status: new), and a rule exit noted on any open pitch."""
        pc = P.pitch_cfg(self.cfg)
        for p in packs:
            rt = _rating(p)
            if rt in ("SELL", "TRIM"):
                self.book.exit_signal(p["symbol"], p["book"], p["as_of"], p.get("price"), p.get("reason") or "rule exit")
                continue
            if rt not in ("BUY", "ADD") or p.get("price") is None:
                continue
            self.book.add({"id": p["id"], "as_of": p["as_of"], "symbol": p["symbol"], "name": p.get("name"),
                           "book": p["book"], "action": p["action"], "rating": rt, "price": p["price"],
                           "stop": p.get("stop"), "rank": p.get("rank"), "of": p.get("of"), "score": p.get("score"),
                           "ml_prob": p.get("ml_prob"), "regime": state.regime, "crisis": state.crisis,
                           "sector": p.get("sector") or self.store.sectors().get(p["symbol"]),
                           "drank": (p.get("rec") or {}).get("daisy_rank"),
                           # kept so the untested question "does news sentiment add edge?" can be answered later
                           "sent_bucket": _sent_bucket(p.get("sentiment"), self.cfg),
                           "fair_value": (p.get("valuation") or {}).get("fair_value"),
                           "upside": (p.get("valuation") or {}).get("upside"),
                           "plan": P.plan_record(p, pc), "plan_mark": None, "track": None,
                           "marked": None, "last": None, "ret": None, "spy_ret": None, "excess": None})

    def mark_pitches(self) -> list[dict]:
        """Walk every pitch forward on the stored bars: the marks, the lifecycle (new -> active ->
        closed) and the return from the tested entry against SPY and the sector ETF."""
        rows = self.pitches()
        bench = self.cfg.get("benchmark", "SPY")
        frames: dict = {}

        def fr(sym):
            if sym not in frames:
                frames[sym] = self.store.frame(sym)
            return frames[sym]
        spy = fr(bench)
        sectors = self.store.sectors()
        for r in rows:
            px = fr(r["symbol"])
            if px.empty or spy.empty:
                continue
            was = json.dumps(r, sort_keys=True, default=str)
            try:
                a = px["close"].asof(r["as_of"])
                b = float(px["close"].iloc[-1])
                sa = spy["close"].asof(r["as_of"])
                sb = float(spy["close"].iloc[-1])
            except Exception:
                continue
            if not a or not sa or a != a or sa != sa:
                continue
            if str(px.index[-1].date()) <= r["as_of"]:
                continue        # no session since the signal yet: unmarked, not "flat and lost to SPY"
            r["last"] = round(b, 4)
            r["ret"] = round(b / float(a) - 1, 5)
            r["spy_ret"] = round(sb / float(sa) - 1, 5)
            r["excess"] = round(r["ret"] - r["spy_ret"], 5)
            r["marked"] = str(px.index[-1].date())
            if r.get("plan"):
                try:
                    r["plan_mark"] = P.mark_plan(r["plan"], r["as_of"], px, spy)
                except Exception as e:                # a bad row must not stop the scorecard
                    log.info("stocks: plan mark for %s failed (%s)", r.get("id"), e)
            if r.get("status") != "closed":           # the lifecycle only ever moves forward
                res = PB.resolve(r, px, r.get("plan_mark"))
                r["sector"] = r.get("sector") or sectors.get(r["symbol"])
                etf = PB.sector_etf(r["sector"])
                r["entry_open"], r["entry_date"] = res["entry_open"], res["entry_date"]
                r["days_open"] = int((px.index > r["as_of"]).sum())
                r["track"] = PB.track(r, res, px, spy, fr(etf) if etf else None, etf)
                if res["status"] == "closed":
                    r.update({"closed_at": res["closed_at"], "close_reason": res["reason"],
                              "close_price": res["close_price"], "close_basis": res["close_basis"]})
                    self.book.set_status(r, "closed", f"{res['reason']}: ${res['close_price']:,.2f} ({res['close_basis']})")
                    continue
                if res["status"] != r.get("status"):
                    self.book.set_status(r, res["status"], f"entered at the {res['entry_date']} open, ${res['entry_open']:,.2f}")
                    continue
            if json.dumps(r, sort_keys=True, default=str) != was:
                self.book.save(r)
        return rows

    def scorecard(self) -> dict:
        """The pitch track record (docs/22.1): every pitch from its tested entry against SPY and its
        sector ETF, by book, conviction, DAISY Rank and sector. Research only: there is no portfolio."""
        rows = self.mark_pitches()

        def agg(rows_):
            rs = [r for r in rows_ if r.get("excess") is not None]
            if not rs:
                return {"n": len(rows_), "marked": 0, "avg_excess": None, "beat_spy": None, "avg_ret": None}
            return {"n": len(rows_), "marked": len(rs),
                    "avg_excess": round(sum(r["excess"] for r in rs) / len(rs), 5),
                    "avg_ret": round(sum(r["ret"] for r in rs) / len(rs), 5),
                    "beat_spy": round(sum(1 for r in rs if r["excess"] > 0) / len(rs), 4)}

        books = sorted({r["book"] for r in rows})
        planned = [r for r in rows if r.get("plan")]
        buckets = [b for b in ("High", "Medium", "Low") if any((r["plan"] or {}).get("bucket") == b for r in planned)]
        # tested = every pitch, entered at the next open; refined = the untested "don't chase" and
        # "gap below the stop" skips applied, kept apart so the refinement earns its place or doesn't
        refined = [r for r in planned if not ((r.get("plan_mark") or {}).get("chase_skip")
                                              or (r.get("plan_mark") or {}).get("gap_skip"))]
        return {"all": agg(rows), "by_book": {b: agg([r for r in rows if r["book"] == b]) for b in books},
                "track": PB.track_record(rows), "counts": self.book.counts(),
                "migration": self.db.kv_get("pitches_json_migrated"),
                "plan": {"all": P.plan_stats(planned),
                         "by_book": {b: P.plan_stats([r for r in planned if r["book"] == b]) for b in books
                                     if any(r["book"] == b for r in planned)},
                         "by_bucket": {b: {**P.plan_stats([r for r in planned if r["plan"].get("bucket") == b]),
                                           **{"vs_spy": agg([r for r in planned if r["plan"].get("bucket") == b])}}
                                       for b in buckets},
                         "tested": {**P.plan_stats(planned), "vs_spy": agg(planned)},
                         "refined": {**P.plan_stats(refined), "vs_spy": agg(refined),
                                     "label": "untested refinement: skip if it opens 1 ATR above the close "
                                              "or below the stop"}},
                "pitches": sorted(rows, key=lambda r: r["as_of"], reverse=True)[:200]}

    # ------------------------------------------------------------------ research (R&D desk, docs/21)
    def research_dir(self) -> Path:
        d = self._p("research")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def last_research(self) -> dict | None:
        """The newest saved study, or None. Cheap: one small JSON read."""
        p = self.research_dir() / "latest.json"
        try:
            ptr = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except (OSError, ValueError):
            return None
        if not ptr:
            return None
        try:
            rep = json.loads((self.research_dir() / ptr["dir"] / "report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            return None
        rep["dir"] = ptr["dir"]
        return rep

    def research_md(self) -> str | None:
        rep = self.last_research()
        if not rep:
            return None
        p = self.research_dir() / rep["dir"] / "report.md"
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None

    async def research(self, post_url: str | None = None, years: int | None = None, grid: dict | None = None,
                       do_wf: bool = True, do_ablation: bool = True, do_ew: bool = True) -> dict:
        """The weekly study (ablations, walk-forward, early warning) on this desk's bar store.

        Runs in a worker thread so the monitor's loop is never blocked, and under the desk lock so a
        daily pass and a study never read the store at the same time. ``post_url`` is the R&D channel;
        the study never posts to the pitch channel. Findings are proposals: nothing here changes a rule.
        """
        from . import research as R
        if self.research_busy:
            return {"skipped": f"a study is already running ({self.research_busy})"}
        if years is None:
            years = int(getattr(self.s, "stocks_research_years", 10) or 10)
        if grid is None:
            grid = R.parse_grid(getattr(self.s, "stocks_research_grid", "") or "")
        stamp = datetime.now(NY).strftime("%Y-%m-%d_%H%M")
        out = self.research_dir() / stamp
        async with self._lock:
            self.research_busy = stamp
            try:
                rep = await asyncio.to_thread(R.run_for_desk, self.cfg, self.store, self.u, out, years, grid,
                                              do_wf, do_ablation, do_ew)
            except Exception as e:
                self.errors.append({"ts": time.time(), "where": "research",
                                    "error": f"{type(e).__name__}: {e}"[:300]})
                raise
            finally:
                self.research_busy = None
        self._save("research/latest.json", {"dir": stamp, "at": rep.get("at"),
                                            "verdict": (rep.get("verdict") or {}).get("overall")})
        delivered = "dashboard"
        if post_url:
            delivered = await self._post_to(post_url, [R.embed(rep)], username="Stock desk R&D")
        rep["delivered"] = delivered
        self._broadcast("stocks_research", {"at": rep.get("at"), "verdict": (rep.get("verdict") or {}).get("overall"),
                                            "delivered": delivered})
        return rep

    # ------------------------------------------------------------------ intraday
    async def intraday(self) -> dict:
        """A sweep pass: refresh the next page of the universe. (The picks and the active pitches are
        watched by the live alert layer, abg/stocks/alerts.py; there are no simulated positions to watch.)"""
        rep = await self.sweep()
        return {"at": datetime.now(NY).isoformat(timespec="seconds"), "sweep": rep, "near_stop": []}

    # ------------------------------------------------------------------ discord
    def _webhook(self) -> str | None:
        return (getattr(self.s, "stocks_webhook_url", None)
                or getattr(self.s, "pitch_webhook_url", None) or None)

    async def _post(self, embeds: list[dict], content: str | None = None) -> str:
        return await self._post_to(self._webhook(), embeds, content=content)

    async def _post_to(self, url: str | None, embeds: list[dict], content: str | None = None,
                       username: str = "Stock Desk", files: list[Path] | None = None) -> str:
        """Post embeds (and optional PNGs, referenced from an embed as ``attachment://<name>``)."""
        if not url:
            return "dashboard"
        body = {"username": username, "allowed_mentions": {"parse": []},
                "embeds": embeds[:MAX_EMBEDS_PER_POST]}
        if content:
            body["content"] = content[:1900]
        target = url + ("&" if "?" in url else "?") + "wait=true"
        files = [f for f in (files or []) if f and Path(f).is_file()][:10]
        try:
            if files:
                body["attachments"] = [{"id": i, "filename": Path(f).name} for i, f in enumerate(files)]
                multipart = {f"files[{i}]": (Path(f).name, Path(f).read_bytes(), _mime(Path(f)))
                             for i, f in enumerate(files)}
                resp = await self.engine.http.raw.post(target, data={"payload_json": json.dumps(body)},
                                                       files=multipart, timeout=60)
                if resp.status_code >= 400:          # status only: the URL carries the webhook token
                    raise RuntimeError(f"HTTP {resp.status_code}")
            else:
                await self.engine.http.request("POST", target, provider="discord", json=body)
            return "posted"
        except Exception as e:
            self.errors.append({"ts": time.time(), "where": "discord", "error": type(e).__name__})
            log.warning("stocks: Discord post failed (%s)", type(e).__name__)
            return "post failed"

    async def _post_daily(self, packs: list[dict], state, board: dict | None = None) -> str:
        cap = int(getattr(self.s, "stocks_post_max", 5))
        tag = (getattr(self.s, "stocks_tag", "") or "").strip()
        digest = T.digest_embed(packs, state, self.cfg, book=self.book.counts(state.date))
        if tag:
            digest["title"] = f"[{tag}] {digest['title']}"
        if board is not None:                       # the day's top picks by conviction (docs/21.7)
            by_id = {p["id"]: p for p in packs}
            detail = [by_id[b["id"]] for b in board["picks"] if b["id"] in by_id]
        else:
            detail = [p for p in packs if _rating(p) in ("BUY", "ADD") and p["book"] != "dual_momentum"]
            detail.sort(key=lambda p: (p.get("rank") or 99))
        embeds, files = [digest], []
        for p in detail[:max(0, cap)]:
            e = T.embed(p)
            png = (p.get("charts") or {}).get("price")
            if png and (self._p("memos") / png).is_file() and getattr(self.s, "stocks_post_charts", True):
                e["image"] = {"url": f"attachment://{png}"}
                files.append(self._p("memos") / png)
            embeds.append(e)
        res = await self._post_to(self._webhook(), embeds, files=files)
        if res == "posted" and board is not None and board["picks"]:
            await self._post_decks(board, [b["id"] for b in board["picks"]], "after the close")
        closes = [p for p in packs if _rating(p) in ("SELL", "TRIM") and (p.get("rec") or {}).get("open_pitch")]
        if closes and res == "posted" and len(embeds) >= MAX_EMBEDS_PER_POST:
            await self._post([T.embed(p) for p in closes[:MAX_EMBEDS_PER_POST]])
        return res

    # ------------------------------------------------------------------ the day's top picks (docs/21.7)
    def board(self) -> dict | None:
        return self._load("board.json", None)

    def _decks(self, ids: list[str]) -> list[Path]:
        """The AIBG deck for each pitch id (built once, next to its memo). Missing python-pptx -> []."""
        from . import deck as DK
        if not getattr(self.s, "stocks_post_decks", True) or not DK.available():
            return []
        out = []
        for pid in ids:
            path = self._p("memos") / f"{pid}.pptx"
            if not path.exists():
                try:
                    self.deck(pid, path)
                except Exception as e:              # a deck is a convenience: never lose the post over it
                    log.info("stocks: deck for %s failed (%s)", pid, type(e).__name__)
                    continue
            out.append(path)
        return out

    async def _post_decks(self, board: dict, ids: list[str], when: str) -> str:
        decks = await asyncio.to_thread(self._decks, ids[:10])
        if not decks:
            return "no decks"
        names = ", ".join(b["symbol"] for b in board["picks"] + board.get("reserves", []) if b["id"] in set(ids))
        res = await self._post_to(self._webhook(), [], content=f"Pitch decks ({when}): {names}", files=decks)
        if res == "posted":
            board["decks_sent"] = sorted(set(board.get("decks_sent") or []) | {d.stem for d in decks})
        return res

    async def _quotes(self, symbols: list[str]) -> dict[str, dict]:
        async def one(sym):
            try:
                q = (await self.engine.quote(sym, use_cache=False)).value
                return sym, {"price": q.price, "open": q.open, "day_high": q.day_high, "day_low": q.day_low,
                             "prev_close": q.prev_close, "volume": q.volume}
            except Exception:
                return sym, None
        return {k: v for k, v in await asyncio.gather(*(one(x) for x in symbols)) if v}

    def _board_slot(self, now: datetime) -> str | None:
        from abg.live.market_hours import is_trading_day
        board = self.board()
        if not B.is_fresh(board, now.date()):
            return None
        slots = [x for x in str(getattr(self.s, "stocks_board_times", "09:00,12:00,15:00") or "").split(",") if x.strip()]
        return B.due_slot(now, slots, list((board.get("posts") or {}).keys()), is_trading_day(now.date()))

    async def board_post(self, slot: str, now: datetime | None = None) -> str:
        """The 9:00 open post (today's picks, with decks) or a check-in (live status, swaps, decks for new picks)."""
        now = now or datetime.now(NY)
        board = self.board()
        if not B.is_fresh(board, now.date()):
            return "no board for today"
        tag = (getattr(self.s, "stocks_tag", "") or "").strip()
        swapped: list[str] = []
        if slot <= "09:29":
            kind = "open"
        else:
            kind = "check"
            syms = [p["symbol"] for p in board["picks"] + (board.get("reserves") or [])]
            board, swapped = B.check_in(board, await self._quotes(syms))
        board.setdefault("posts", {})[slot] = {"at": now.isoformat(timespec="seconds"), "result": "pending"}
        self._save("board.json", board)                     # mark first: a failed post is not retried in a loop
        res = await self._post_to(self._webhook(), [B.board_embed(board, kind, now, tag)])
        ids = [p["id"] for p in board["picks"]] if kind == "open" else swapped
        if res == "posted" and ids:
            await self._post_decks(board, ids, f"{slot} ET")
        board["posts"][slot]["result"] = res
        self._save("board.json", board)
        self._broadcast("stocks_board", {"slot": slot, "posted": res, "picks": [p["symbol"] for p in board["picks"]]})
        return res

    def _broadcast(self, kind: str, payload: dict) -> None:
        if self.hub is not None and hasattr(self.hub, "broadcast"):
            try:
                self.hub.broadcast(kind, payload)
            except Exception:
                pass

    # ------------------------------------------------------------------ schedule
    def _daily_due(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        raw = str(getattr(self.s, "stocks_daily_time", DEFAULT_DAILY_TIME) or DEFAULT_DAILY_TIME)
        try:
            hh, mm = (int(x) for x in raw.split(":"))
        except ValueError:
            hh, mm = 16, 20
        if (now.hour, now.minute) < (hh, mm):
            return False
        done = self._load("state.json", {}).get("last_daily_day")
        # the signal is stamped with the last session in the panel, which on a holiday is yesterday,
        # so compare against the calendar day the pass ran rather than the panel date
        return self._load("state.json", {}).get("last_daily_run") != now.date().isoformat() or not done

    # ---- the morning catch-up: Polygon's free plan publishes a session's whole-market bar overnight
    def _market_bar(self, day: str) -> bool:
        try:
            from datetime import date as _date
            return bool(self.market.days.has(_date.fromisoformat(str(day)[:10])))
        except Exception:
            return False

    def _catchup_due(self, now: datetime) -> bool:
        """True on a trading morning when the last pass ran without the wider market's bar for its
        session. The evening pass cannot wait for it (the free plan publishes after midnight), and without
        that bar no name outside the per-symbol core can signal."""
        from abg.live.market_hours import is_trading_day
        raw = str(getattr(self.s, "stocks_market_catchup", "06:30") or "").strip()
        if not raw or not self.market.enabled or not is_trading_day(now.date()):
            return False
        try:
            hh, mm = (int(x) for x in raw.split(":"))
        except ValueError:
            hh, mm = 6, 30
        if not (hh, mm) <= (now.hour, now.minute) < (9, 20):
            return False
        st = self._load("state.json", {})
        if st.get("catchup_day") == now.date().isoformat():
            return False
        ld = st.get("last_daily") or {}
        if ld.get("market_bar") and self.u.market:
            return False                                   # the last pass already saw the whole market
        return time.time() - getattr(self, "_catchup_try", 0.0) >= 600      # one try every ten minutes

    async def catchup(self, now: datetime) -> dict:
        """Fetch the last session's whole-market bar if it is out, then re-run the pass without posting:
        the watchlist (08:30) and the top 5 (09:00) then come from the whole market."""
        self._catchup_try = time.time()
        st = self._load("state.json", {})
        day = (st.get("last_daily") or {}).get("as_of")
        if day and not self._market_bar(day):
            await self.market_refresh(max_calls=3)
        if day and not self._market_bar(day) and (now.hour, now.minute) < (8, 10):
            return {"waiting": f"the whole-market bar for {day} is not published yet"}
        st = self._load("state.json", {})
        st["catchup_day"] = now.date().isoformat()
        self._save("state.json", st)
        out = await self.daily(post=False)
        log.info("stocks: morning catch-up pass: %s opens, market bar %s", out.get("opens"), out.get("market_bar"))
        return out

    def _mark_daily_run(self, now: datetime) -> None:
        st = self._load("state.json", {})
        st["last_daily_run"] = now.date().isoformat()
        self._save("state.json", st)

    def _sweep_due(self, now: datetime) -> bool:
        every = max(5, int(getattr(self.s, "stocks_sweep_minutes", 30)))
        if now.weekday() >= 5:
            return False
        if not (9, 25) <= (now.hour, now.minute) <= (16, 15):
            return False
        last = (self.last_sweep or {}).get("at")
        if not last:
            return True
        try:
            prev = datetime.fromisoformat(last)
        except ValueError:
            return True
        return (now - prev) >= timedelta(minutes=every)

    INDEX_RECHECK_DAYS = 7

    async def load_index_if_seed(self, force: bool = False) -> dict | None:
        """Load the real S&P 500 (GitHub dataset first, no key needed; FMP as a fallback) when the
        desk is still on the seed list, and re-check it weekly so additions and removals flow in.
        Failure is logged, not fatal: the current list (or the seed) keeps producing signals."""
        import time as _t
        last = float(self.store.kv_get("index_checked") or 0)
        due = force or self.u.is_seed() or (_t.time() - last) > self.INDEX_RECHECK_DAYS * 86400
        if not due:
            return None
        try:
            out = await self.sweeper.refresh_index("sp500")
            self.store.kv_set("index_checked", str(int(_t.time())))
            self.cfg = build_config(self.s, self.u)          # Book A and the non-stock list follow the new list
            log.info("stocks: S&P 500 list from %s (%s symbols, %s membership rows)", out.get("source"),
                     out.get("symbols"), out.get("membership_rows"))
            return out
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.errors.append({"ts": time.time(), "where": "index list",
                                "error": f"{type(e).__name__}: {e}"[:300]})
            log.warning("stocks: could not load the S&P 500 list (%s); keeping the current one", e)
            return {"error": str(e)[:200]}

    def _floors(self) -> tuple[float, float]:
        """The loosest price and dollar-volume floor any single-stock book uses - the market tier keeps
        every name that could pass at least one of them."""
        books = [p for n, p in (self.cfg.get("books") or {}).items()
                 if n in ("xs_momentum", "trend_pullback", "mean_reversion") and p.get("enabled", True)]
        if not books:
            return 5.0, 20e6
        return (min(float(p.get("min_price", 5.0)) for p in books),
                min(float(p.get("min_adv_usd", 2e7)) for p in books))

    async def market_refresh(self, max_calls: int | None = None, force: bool = False) -> dict:
        """Whole-market bars (one Polygon call per session) -> the market tier of the universe."""
        if not self.market.enabled:
            return {"skipped": "no Polygon key or ABG_STOCKS_MARKET=false"}
        mp, mdv = self._floors()
        try:
            out = await self.market.refresh(max_calls=max_calls, min_price=mp, min_dv=mdv, force=force)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.errors.append({"ts": time.time(), "where": "market",
                                "error": f"{type(e).__name__}: {str(e)[:200]}"})
            out = {"error": f"{type(e).__name__}"}
        self.last_market = {"at": datetime.now(NY).isoformat(timespec="seconds"), **out}
        return self.last_market

    def _market_ready(self, now: datetime) -> bool:
        """With the market tier on, wait for Polygon to publish today's whole-market bar - otherwise
        every non-S&P name is missing today's close and none of them can signal. Give up waiting at
        ABG_STOCKS_MARKET_WAIT_UNTIL and run on what there is."""
        if not self.market.enabled or not self.u.market:
            return True
        if self.market.days.has(now.date()):
            return True
        raw = str(getattr(self.s, "stocks_market_wait_until", "21:30") or "21:30")
        try:
            hh, mm = (int(x) for x in raw.split(":"))
        except ValueError:
            hh, mm = 21, 30
        return (now.hour, now.minute) >= (hh, mm)

    def missing(self) -> list[str]:
        """Core symbols still short of history (and not parked as delisted)."""
        need = int(getattr(self.s, "stocks_min_bars", 250))
        cov = self.store.coverage()
        out = []
        for sym in self.u.core():
            c = cov.get(self.sweeper.u_alias(sym))
            if c is None or (c.bars < need and not c.delisted):
                out.append(sym)
        return out

    async def first_start(self) -> dict:
        """What run() does before its loop: real index list, then fill any missing history.

        Called on every start, and cheap when there is nothing to do, so a backfill that was cut off
        (the PC slept, the monitor restarted) simply carries on where it stopped."""
        out: dict = {"index": await self.load_index_if_seed()}
        if getattr(self.s, "stocks_autofill", True):
            try:
                todo = self.missing()
                out["missing"] = len(todo)
                if todo:
                    log.info("stocks: backfilling %d symbol(s) short of history", len(todo))
                    out["backfill"] = await self.backfill(todo)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.errors.append({"ts": time.time(), "where": "autofill",
                                    "error": f"{type(e).__name__}: {e}"[:300]})
        if self.market.enabled:
            # newest sessions first, and only a slice per start: a deep backfill must not hold up the day's posts
            have = len(self.market.days.days())
            out["market"] = await self.market_refresh(
                max_calls=max(60, int(getattr(self.s, "stocks_market_backfill_calls", 25) or 25)) if have else None)
        return out

    async def run(self) -> None:
        self._stop.clear()
        self.running = True
        await self.first_start()
        try:
            while not self._stop.is_set():
                now = datetime.now(NY)
                try:
                    if self._daily_due(now):
                        if self.market.enabled and self.u.market and not self.market.days.has(now.date()):
                            await self.market_refresh(max_calls=3)       # today's bar, if it is out yet
                        if self._market_ready(now):
                            self._mark_daily_run(now)
                            await self.daily(post=True)
                            want = int(getattr(self.s, "stocks_market_sessions", 500) or 500)
                            if self.market.enabled and len(self.market.days.days()) < want:   # gradual backfill
                                await self.market_refresh(
                                    max_calls=int(getattr(self.s, "stocks_market_backfill_calls", 25) or 25))
                        else:
                            await self._sleep(600)                       # Polygon not out yet: check in 10 min
                            continue
                    elif self._catchup_due(now):
                        await self.catchup(now)
                    elif (due := self._watch_due(now)) is not None:
                        if due == "scan":
                            await self.morning(now, post=False)
                        else:
                            await self.watch_post(now)
                    elif (slot := self._board_slot(now)) is not None:
                        await self.board_post(slot, now)
                    elif self._recap_due(now):
                        await self.recap_post(now)
                    elif self._sweep_due(now):
                        await self.intraday()
                    if self._live_due(now):                              # every tick it is due, whatever else ran
                        self._live_at = time.time()
                        await self.live_tick(now)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("stock desk pass failed")
                    self.errors.append({"ts": time.time(), "where": "stocks pass",
                                        "error": f"{type(e).__name__}: {e}"[:300]})
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.running = False

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        """Close both databases (tests, and a clean shutdown)."""
        self.db.close()
        self.store.close()

    # ------------------------------------------------------------------ reporting
    def state(self) -> dict:
        st = self._load("state.json", {})
        return {"last_daily": st.get("last_daily"), "last_daily_run": st.get("last_daily_run"),
                "last_sweep": self.last_sweep, "live": self.live, "news": self.last_news,
                "watchlist": {k: self.watch_meta().get(k) for k in ("day", "at", "names", "posted")}}

    def status(self) -> dict:
        stats = self.store.stats()
        return {
            "enabled": bool(getattr(self.s, "stocks_enabled", False)),
            "running": self.running,
            "webhook": bool(self._webhook()),
            "tag": (getattr(self.s, "stocks_tag", "") or "") or None,
            "daily_time": str(getattr(self.s, "stocks_daily_time", DEFAULT_DAILY_TIME)) + " ET",
            "rules": describe_cfg(self.cfg),
            "universe": self.u.describe(),
            "store": stats,
            "sweep": self.sweeper.status(),
            "market": {**self.market.status(), "last": self.last_market},
            "last_daily": self.last_daily or self._load("state.json", {}).get("last_daily"),
            "pitches": sum(self.book.counts().values()), "pitch_book": self.book.counts(),
            "fund_holdings": (lambda h: len(h) if h else 0)(self.fund_holdings()),
            "fund_equity_set": bool(P.pitch_cfg(self.cfg).get("fund_equity")),
            "research": {"schedule": getattr(self.s, "stocks_research", "weekly"), "busy": self.research_busy,
                         "last": (lambda r: {"at": r.get("at"), "verdict": (r.get("verdict") or {}).get("overall")}
                                  if r else None)(self.last_research())},
            "screen": self.screen_meta(), "live": self.live, "news": self.last_news,
            "watchlist": {k: self.watch_meta().get(k) for k in ("day", "at", "names", "posted")},
            "features": {"watch": bool(getattr(self.s, "stocks_watch", True)),
                         "watch_post": bool(getattr(self.s, "stocks_watch_post", False)),
                         "live": bool(getattr(self.s, "stocks_live", True)),
                         "live_alerts": bool(getattr(self.s, "stocks_live_alerts", False)),
                         "news": bool(getattr(self.s, "stocks_news", True)),
                         "weekly_recap": bool(getattr(self.s, "stocks_weekly_recap", False))},
            "keys": {"polygon": bool(getattr(self.s, "polygon_api_key", None)),
                     "finnhub": bool(getattr(self.s, "finnhub_api_key", None)),
                     "anthropic": bool(getattr(self.s, "anthropic_api_key", None)),
                     "tiingo": bool(getattr(self.s, "tiingo_api_key", None)),
                     "twelvedata": bool(getattr(self.s, "twelvedata_api_key", None)),
                     "fmp": bool(getattr(self.s, "fmp_api_key", None))},
            "errors": list(self.errors)[-5:],
        }

    def today(self) -> dict:
        """What the dashboard shows: the last daily pass, straight from disk so a page load is cheap."""
        return self._load("today.json", {"state": None, "candidates": []})

    def pack(self, key: str) -> dict | None:
        """A saved pitch pack by id, or the newest one for a ticker."""
        d = self.dir / "memos"
        if not d.exists():
            return None
        exact = d / f"{Path(key).name}.json"
        if exact.exists():
            path = exact
        else:
            sym = key.strip().upper()
            hits = sorted(d.glob(f"*-{sym}.json"))
            if not hits:
                return None
            path = hits[-1]                          # ids start with the date, so the last is the newest
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def deck(self, key: str, out: Path | None = None) -> Path:
        """The AIBG 5-slide deck for a saved pitch (id or ticker), next to its memo."""
        from . import deck as DK
        if not DK.available():
            raise RuntimeError("python-pptx is not installed: pip install .[deck]")
        p = self.pack(key)
        if p is None:
            raise FileNotFoundError(f"no saved pitch for {key!r} (packs are saved by the daily pass)")
        return DK.build_deck(p, out or (self.dir / "memos" / f"{p['id']}.pptx"), charts_dir=self.dir / "memos")

    def memo(self, pitch_id: str) -> str | None:
        p = self.dir / "memos" / f"{Path(pitch_id).name}.md"
        try:
            return p.read_text(encoding="utf-8") if p.exists() else None
        except OSError:
            return None


# --------------------------------------------------------------------------- helpers
def _mime(path: Path) -> str:
    return {".png": "image/png", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".pdf": "application/pdf"}.get(path.suffix.lower(), "application/octet-stream")


def _peak_mb() -> float | None:
    """Peak memory of this process in MB (Linux/macOS; None on Windows)."""
    try:
        import resource
        import sys
        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(v / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
    except Exception:
        return None


def _sent_bucket(sn: dict | None, cfg: dict) -> str | None:
    from .news import news_cfg
    nm = (sn or {}).get("name") or {}
    nc = news_cfg(cfg)
    if nm.get("level") is None or (nm.get("n") or 0) < nc["min_articles"]:
        return None
    return "positive" if nm["level"] >= nc["positive"] else "negative" if nm["level"] <= nc["negative"] else "neutral"


def _rating(p: dict) -> str:
    return (p.get("rec") or {}).get("rating") or p.get("action") or ""


def PB_holdings(rows) -> dict | None:
    """``[{symbol, weight}]`` or ``{symbol: weight}`` -> ``{SYMBOL: weight}`` (a weight above 1 is read as
    percent points). ``None`` when the list is empty."""
    out: dict = {}
    items = rows.items() if isinstance(rows, dict) else [(r.get("symbol"), r.get("weight")) for r in (rows or [])
                                                          if isinstance(r, dict)]
    for sym, w in items:
        try:
            w = float(w)
        except (TypeError, ValueError):
            continue
        if sym and w > 0:
            out[str(sym).upper().strip()] = round(w / 100.0 if w > 1 else w, 6)
    return out or None


def _public(p: dict) -> dict:
    """A pack trimmed for the dashboard and the API: no provider internals, no raw frames, and nothing
    from the replay's simulated book (the retired size fields are null)."""
    keep = ("id", "as_of", "symbol", "name", "sector", "action", "book", "book_label", "edge",
            "rank", "of", "score", "ml_prob", "price", "stop", "shares", "notional", "weight",
            "risk_pct", "r_dollars", "atr", "stop_atr", "max_hold", "what_happened", "gates",
            "exits", "risks", "caveats", "valuation", "news", "context", "reason", "facts",
            "rec", "plan", "overview", "charts", "grades", "flags", "sentiment")
    out = {k: p.get(k) for k in keep}
    if out.get("plan"):                             # base rates carry the replay's trade list: keep the summary
        out["plan"] = {k: v for k, v in out["plan"].items() if k != "settings"}
    return out


def _ledger_row(p: dict, state) -> dict:
    """One flat row per candidate: the features a ranker would want, plus the outcome fields to fill
    in later. Deliberately flat so it reads straight into a DataFrame."""
    f = p.get("facts") or {}
    v = p.get("valuation") or {}
    return {"as_of": p["as_of"], "written_at": p["written_at"], "symbol": p["symbol"],
            "book": p["book"], "action": p["action"], "rank": p.get("rank"), "of": p.get("of"),
            "score": p.get("score"), "ml_prob": p.get("ml_prob"), "price": p.get("price"),
            "stop": p.get("stop"), "shares": (p.get("replay") or {}).get("shares"),
            "weight": (p.get("replay") or {}).get("weight"),
            "risk_pct": (p.get("replay") or {}).get("risk_pct"), "atr": p.get("atr"),
            "rating": _rating(p), "conviction": ((p.get("rec") or {}).get("conviction") or {}).get("score"),
            "regime": state.regime, "crisis": state.crisis, "stress": state.stress,
            "vol_scale": state.vol_scale, "book_weight": (state.weights or {}).get(p["book"]),
            "gates_passed": sum(1 for ok in (p.get("gates") or {}).values() if ok),
            "gates_total": len(p.get("gates") or {}),
            "fair_value": v.get("fair_value"), "upside": v.get("upside"),
            "implied_growth": v.get("implied_growth"),
            **{f"f_{k}": val for k, val in f.items()}}


def _public_state(state) -> dict:
    """The market read for the dashboard. The replay's simulated book (equity, drawdown, positions,
    budgets) is not shown: those keys stay in the payload as null (deprecated, docs/22.1)."""
    from dataclasses import asdict
    d = asdict(state)
    for k in ("equity", "drawdown", "gross", "budgets", "positions", "bench_vs"):
        d[k] = None
    return d


_state_dict = _public_state
