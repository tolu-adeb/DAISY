"""The stock desk: the four-book rule system, run every day, pitched to Discord.

    daily      after the close (ABG_STOCKS_DAILY_TIME, 16:20 ET): replay the books to today, write a
               thesis for everything they want to open or close, post a digest plus one embed per
               pitch, and record every candidate - including the ones that were skipped
    intraday   every ABG_STOCKS_SWEEP_MINUTES while the market is open: refresh the next page of the
               universe, watch open paper positions against their stops, and flag names that cross an
               entry condition mid-session
    scorecard  every pitch is marked against SPY from its signal-day close; paper only, no orders

State lives in ``<data_dir>/stocks/``: ``state.json`` (schedule bookkeeping), ``ledger.jsonl`` (every
candidate ever seen), ``pitches.json`` (what was pitched and how it has done), ``memos/<id>.md``.
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

from . import thesis as T
from .config import build_config, describe as describe_cfg
from .live import LiveSignals
from .store import BarStore
from .sweep import Sweeper
from .universe import Universe

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

MAX_EMBEDS_PER_POST = 10          # Discord's own cap
DEFAULT_DAILY_TIME = "16:20"
ENRICH_DEFAULT = 6                # how many top candidates get fundamentals + news


class StockDesk:
    def __init__(self, settings, engine, hub=None, symbols_fn=None):
        self.s = settings
        self.engine = engine                     # the terminal's data engine (providers, http)
        self.hub = hub
        self.symbols_fn = symbols_fn             # extra names to follow - callable -> list[str]
        self.dir = Path(settings.data_dir).expanduser() / "stocks"
        self.store = BarStore.from_settings(settings)
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
        self.errors: deque = deque(maxlen=20)
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self.running = False
        self.last_daily: dict | None = None
        self.last_sweep: dict | None = None
        self.last_state = None
        self.research_busy: str | None = None

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
        return self._load("pitches.json", [])

    # ------------------------------------------------------------------ data upkeep
    async def backfill(self, symbols: list[str] | None = None, period: str | None = None) -> dict:
        rep = await self.sweeper.backfill(symbols, period)
        log.info("stocks backfill: %s", rep.to_dict())
        return rep.to_dict()

    async def sweep(self, page: int | None = None) -> dict:
        rep = await self.sweeper.pass_once(page)
        self.last_sweep = rep.to_dict()
        self._broadcast("stocks_sweep", self.last_sweep)
        return self.last_sweep

    # ------------------------------------------------------------------ signals
    def _signals(self) -> LiveSignals:
        ls = LiveSignals(self.cfg, self.store, self.u).build(warn=True)
        ls.run(start=self._replay_start(ls))
        return ls

    def _replay_start(self, ls: LiveSignals) -> str | None:
        """How far back the live replay starts. Positions and Hedge weights converge long before this,
        and replaying from the rule file's 2007 start on every daily pass would cost minutes for nothing."""
        import pandas as pd
        years = int(getattr(self.s, "stocks_replay_years", 5) or 0)
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

    async def daily(self, post: bool = True, enrich: int | None = None) -> dict:
        """The main event: today's signals, written up, posted and recorded."""
        await self.load_index_if_seed()            # weekly re-check; never fatal
        async with self._lock:
            t0 = time.time()
            ls = self._signals()
            cands = ls.candidates()
            state = ls.state()
            self.last_state = state

            n_enrich = ENRICH_DEFAULT if enrich is None else enrich
            # only entries in the single-stock books are worth a valuation, and only the best few
            worth = [c for c in cands
                     if c.is_entry and c.book in ("trend_pullback", "mean_reversion", "xs_momentum")]
            worth.sort(key=lambda c: (c.rank or 99))
            enrich_set = {c.symbol for c in worth[:max(0, n_enrich)]}

            packs: list[dict] = []
            for c in cands:
                fund = peers = news = provider = None
                if c.symbol in enrich_set:
                    fund, peers, news, provider = await self._enrich(c.symbol)
                packs.append(T.build_pack(c, state, self.cfg, fund=fund, peers=peers or [],
                                          news=news or [], provider=provider))

            for p in packs:
                if p["action"] in ("BUY", "REBALANCE") and p["book"] != "dual_momentum":
                    try:
                        self._p("memos").mkdir(parents=True, exist_ok=True)
                        (self._p("memos") / f"{p['id']}.md").write_text(
                            T.render_md(p), encoding="utf-8")
                    except OSError as e:
                        log.warning("stocks: could not write memo %s (%s)", p["id"], e)

            self._append_ledger([_ledger_row(p, state) for p in packs])
            self._record_pitches(packs, state)
            sent = await self._post_daily(packs, state) if post else "not posted"

            self.last_daily = {
                "at": datetime.now(NY).isoformat(timespec="seconds"), "as_of": state.date,
                "candidates": len(packs), "opens": sum(1 for p in packs if p["action"] in ("BUY", "REBALANCE")),
                "closes": sum(1 for p in packs if p["action"] == "SELL"), "posted": sent,
                "regime": state.regime, "crisis": state.crisis, "stress": state.stress,
                "seconds": round(time.time() - t0, 1), "enriched": sorted(enrich_set),
            }
            st = self._load("state.json", {})
            st["last_daily"] = self.last_daily
            st["last_daily_day"] = state.date
            self._save("state.json", st)
            self._save("today.json", {"state": _state_dict(state),
                                      "candidates": [_public(p) for p in packs]})
            self._broadcast("stocks_daily", self.last_daily)
            return self.last_daily

    # ------------------------------------------------------------------ pitch record
    def _record_pitches(self, packs: list[dict], state) -> None:
        """One row per name we said to open, so it can be marked against SPY later."""
        rows = self.pitches()
        have = {(r["as_of"], r["book"], r["symbol"]) for r in rows}
        for p in packs:
            if p["action"] not in ("BUY", "REBALANCE") or p.get("price") is None:
                continue
            key = (p["as_of"], p["book"], p["symbol"])
            if key in have:
                continue
            rows.append({"id": p["id"], "as_of": p["as_of"], "symbol": p["symbol"], "book": p["book"],
                         "action": p["action"], "price": p["price"], "stop": p.get("stop"),
                         "rank": p.get("rank"), "of": p.get("of"), "score": p.get("score"),
                         "ml_prob": p.get("ml_prob"), "regime": state.regime, "crisis": state.crisis,
                         "fair_value": (p.get("valuation") or {}).get("fair_value"),
                         "upside": (p.get("valuation") or {}).get("upside"),
                         "marked": None, "last": None, "ret": None, "spy_ret": None, "excess": None})
        self._save("pitches.json", rows[-500:])

    def scorecard(self) -> dict:
        """Mark every pitch against SPY from its signal-day close. Paper, and that is the point: it is
        the only honest read on whether the rules are worth anything here."""
        rows = self.pitches()
        bench = self.cfg.get("benchmark", "SPY")
        spy = self.store.frame(bench)
        changed = False
        for r in rows:
            px = self.store.frame(r["symbol"])
            if px.empty or spy.empty:
                continue
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
            changed = True
        if changed:
            self._save("pitches.json", rows)

        def agg(rows_):
            rs = [r for r in rows_ if r.get("excess") is not None]
            if not rs:
                return {"n": len(rows_), "marked": 0, "avg_excess": None, "beat_spy": None, "avg_ret": None}
            return {"n": len(rows_), "marked": len(rs),
                    "avg_excess": round(sum(r["excess"] for r in rs) / len(rs), 5),
                    "avg_ret": round(sum(r["ret"] for r in rs) / len(rs), 5),
                    "beat_spy": round(sum(1 for r in rs if r["excess"] > 0) / len(rs), 4)}

        books = sorted({r["book"] for r in rows})
        return {"all": agg(rows), "by_book": {b: agg([r for r in rows if r["book"] == b]) for b in books},
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
        """A sweep pass, plus a look at what we already hold."""
        rep = await self.sweep()
        watch = []
        try:
            ls = self._signals()
            state = ls.state()
            self.last_state = state
            for book, pos in (state.positions or {}).items():
                for sym, d in pos.items():
                    stop = d.get("stop")
                    price = d.get("price")
                    if stop and price and price <= stop * 1.01:
                        watch.append({"symbol": sym, "book": book, "price": price, "stop": stop,
                                      "note": "at or within 1% of its stop"})
        except Exception as e:
            self.errors.append({"ts": time.time(), "where": "intraday", "error": f"{type(e).__name__}: {e}"[:300]})
        out = {"at": datetime.now(NY).isoformat(timespec="seconds"), "sweep": rep, "near_stop": watch}
        if watch:
            self._broadcast("stocks_watch", out)
        return out

    # ------------------------------------------------------------------ discord
    def _webhook(self) -> str | None:
        return (getattr(self.s, "stocks_webhook_url", None)
                or getattr(self.s, "pitch_webhook_url", None) or None)

    async def _post(self, embeds: list[dict], content: str | None = None) -> str:
        return await self._post_to(self._webhook(), embeds, content=content)

    async def _post_to(self, url: str | None, embeds: list[dict], content: str | None = None,
                       username: str = "Stock Desk") -> str:
        if not url:
            return "dashboard"
        body = {"username": username, "allowed_mentions": {"parse": []},
                "embeds": embeds[:MAX_EMBEDS_PER_POST]}
        if content:
            body["content"] = content[:1900]
        try:
            await self.engine.http.request("POST", url + ("&" if "?" in url else "?") + "wait=true",
                                           provider="discord", json=body)
            return "posted"
        except Exception as e:
            self.errors.append({"ts": time.time(), "where": "discord", "error": type(e).__name__})
            log.warning("stocks: Discord post failed (%s)", type(e).__name__)
            return "post failed"

    async def _post_daily(self, packs: list[dict], state) -> str:
        cap = int(getattr(self.s, "stocks_post_max", 5))
        tag = (getattr(self.s, "stocks_tag", "") or "").strip()
        digest = T.digest_embed(packs, state, self.cfg)
        if tag:
            digest["title"] = f"[{tag}] {digest['title']}"
        detail = [p for p in packs
                  if p["action"] in ("BUY", "REBALANCE") and p["book"] != "dual_momentum"]
        detail.sort(key=lambda p: (p.get("rank") or 99))
        embeds = [digest] + [T.embed(p) for p in detail[:max(0, cap)]]
        res = await self._post(embeds)
        closes = [p for p in packs if p["action"] == "SELL"]
        if closes and res == "posted" and len(embeds) >= MAX_EMBEDS_PER_POST:
            await self._post([T.embed(p) for p in closes[:MAX_EMBEDS_PER_POST]])
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
                        self._mark_daily_run(now)
                        await self.daily(post=True)
                    elif self._sweep_due(now):
                        await self.intraday()
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

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ reporting
    def state(self) -> dict:
        st = self._load("state.json", {})
        return {"last_daily": st.get("last_daily"), "last_daily_run": st.get("last_daily_run"),
                "last_sweep": self.last_sweep}

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
            "last_daily": self.last_daily or self._load("state.json", {}).get("last_daily"),
            "pitches": len(self.pitches()),
            "research": {"schedule": getattr(self.s, "stocks_research", "weekly"), "busy": self.research_busy,
                         "last": (lambda r: {"at": r.get("at"), "verdict": (r.get("verdict") or {}).get("overall")}
                                  if r else None)(self.last_research())},
            "errors": list(self.errors)[-5:],
        }

    def today(self) -> dict:
        """What the dashboard shows: the last daily pass, straight from disk so a page load is cheap."""
        return self._load("today.json", {"state": None, "candidates": []})

    def memo(self, pitch_id: str) -> str | None:
        p = self.dir / "memos" / f"{Path(pitch_id).name}.md"
        try:
            return p.read_text(encoding="utf-8") if p.exists() else None
        except OSError:
            return None


# --------------------------------------------------------------------------- helpers
def _public(p: dict) -> dict:
    """A pack trimmed for the dashboard and the API: no provider internals, no raw frames."""
    keep = ("id", "as_of", "symbol", "name", "sector", "action", "book", "book_label", "edge",
            "rank", "of", "score", "ml_prob", "price", "stop", "shares", "notional", "weight",
            "risk_pct", "r_dollars", "atr", "stop_atr", "max_hold", "what_happened", "gates",
            "exits", "risks", "caveats", "valuation", "news", "context", "reason", "facts")
    return {k: p.get(k) for k in keep}


def _ledger_row(p: dict, state) -> dict:
    """One flat row per candidate: the features a ranker would want, plus the outcome fields to fill
    in later. Deliberately flat so it reads straight into a DataFrame."""
    f = p.get("facts") or {}
    v = p.get("valuation") or {}
    return {"as_of": p["as_of"], "written_at": p["written_at"], "symbol": p["symbol"],
            "book": p["book"], "action": p["action"], "rank": p.get("rank"), "of": p.get("of"),
            "score": p.get("score"), "ml_prob": p.get("ml_prob"), "price": p.get("price"),
            "stop": p.get("stop"), "shares": p.get("shares"), "weight": p.get("weight"),
            "risk_pct": p.get("risk_pct"), "atr": p.get("atr"),
            "regime": state.regime, "crisis": state.crisis, "stress": state.stress,
            "vol_scale": state.vol_scale, "book_weight": (state.weights or {}).get(p["book"]),
            "gates_passed": sum(1 for ok in (p.get("gates") or {}).values() if ok),
            "gates_total": len(p.get("gates") or {}),
            "fair_value": v.get("fair_value"), "upside": v.get("upside"),
            "implied_growth": v.get("implied_growth"),
            **{f"f_{k}": val for k, val in f.items()}}


def _state_dict(state) -> dict:
    from dataclasses import asdict
    return asdict(state)
