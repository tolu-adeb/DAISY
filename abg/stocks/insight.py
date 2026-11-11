"""Explained research (docs/23): the deep Explore profile, analyst pitches, the News tab, fund performance
and the strategy page. ``InsightMixin`` is mixed into ``StockDesk`` next to ``DayMixin``.

The rule on every method here: a sentence may only say what a stored number says. Each summary is built
from fields that are returned alongside it, so the page can show the number next to the claim. Nothing
here changes a rule, a signal or a size.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from . import analyst as A
from . import news as N
from . import pitch_plan as P
from . import portfolio as PF
from . import rank as RK
from . import thesis as T
from .live import BOOK_EDGE, BOOK_LABEL, Candidate

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

BOOK_STORY = {
    "trend_pullback": {
        "steps": ["An uptrend is in place (price above its 200-day average, the 50-day above the 200-day)",
                  "The stock pulls back for a few days (RSI-14 dips to 40 or lower)",
                  "The pullback ends (a close above the prior day's high, momentum turning up)",
                  "Buy at the next open; stop 2 ATR below; trail the stop as it rises"],
        "works": "steady uptrends with ordinary dips", "struggles": "choppy markets where pullbacks keep going, and after bad news",
        "diagram": "pullback"},
    "mean_reversion": {
        "steps": ["The stock is in a long-term uptrend (above its 200-day average)",
                  "It is sold hard for a day or two (RSI-2 under 10)",
                  "Buy at the next open",
                  "Sell into the first bounce (a close above the 5-day average) or after a week"],
        "works": "healthy stocks hit by short, sharp selling", "struggles": "when the selling is the start of real bad news",
        "diagram": "bounce"},
    "xs_momentum": {
        "steps": ["Rank every eligible stock by its 12-month return, skipping the latest month, divided by its volatility",
                  "Own the top 20", "Re-rank at each month-end: sell what fell out, buy what moved in"],
        "works": "markets with clear leaders that keep leading", "struggles": "sharp reversals, when last year's winners fall first",
        "diagram": "rank"},
    "dual_momentum": {
        "steps": ["Is the market itself in an uptrend (above its 200-day average)?",
                  "If yes: own the strongest sector ETFs", "If no: move to the strongest defensive asset (bonds, gold, cash)",
                  "Check again each month-end"],
        "works": "long trends, and it steps aside in long bear markets", "struggles": "quick V-shaped drops and recoveries",
        "diagram": "switch"},
    "crisis_alpha": {
        "steps": ["Early-warning flags turn on (credit, volatility, breadth)", "Crisis mode starts: the other books cut size or stop",
                  "This book holds what is rising and fades the index while it trends down", "It stands down when the flags clear"],
        "works": "prolonged market stress", "struggles": "false alarms: flags that clear within days",
        "diagram": "shield"},
}
TIMELINE = [
    ("2026-10", "Trade copier and signal tools", "The terminal began as tools for following outside signals. Useful for learning, but the edge belonged to someone else."),
    ("2026-11-08", "Stock desk with five rule books", "Replaced copied signals with rules we can test: every entry comes from a written rule on the daily close, entered at the next open."),
    ("2026-11-09", "AIBG pitch format", "Each signal became a full pitch (overview, thesis, data, recommendation) with a plan, charts and a deck, so analysts could judge it."),
    ("2026-11-10", "Pitching desk, not a paper portfolio", "Dropped the simulated book. The desk now pitches ideas and scores each pitch on its own, against SPY and its sector."),
    ("2026-11-10", "Whole-market research, news and the morning watchlist", "Research widened from the S&P 500 to every US-listed stock, with news, sentiment and theme shocks feeding a daily watchlist."),
    ("2026-11-11", "Portfolio and analyst pitches", "The fund's real positions are logged and measured, and any member can pitch a stock in the same format, scored apart from the rules."),
]


def summarize(sym: str, row: dict | None, stats: dict | None, fund: dict | None, sent: dict | None, verdict: str | None,
              val: dict | None = None) -> list[dict]:
    """The plain-English summary: each sentence with the numbers it rests on (so the page can show them)."""
    r, st, f, out = row or {}, stats or {}, fund or {}, []

    def add(text, **nums):
        out.append({"text": text, "numbers": {k: v for k, v in nums.items() if v is not None}})
    c, d1, d21 = r.get("close"), r.get("chg_1d"), r.get("chg_21d")
    if c is not None:
        s = f"{sym} last closed at ${c:,.2f}"
        if d1 is not None:
            s += f", {'up' if d1 > 0 else 'down' if d1 < 0 else 'flat'} {abs(d1):.1%} on the day"
        if d21 is not None:
            s += f" and {'up' if d21 > 0 else 'down'} {abs(d21):.1%} over the last month"
        add(s + ".", close=c, chg_1d=d1, chg_21d=d21)
    tr, br = st.get("total_return_pct"), st.get("benchmark_return_pct")
    if tr is not None and br is not None:
        add(f"Over the past year it returned {tr:+.1f}% against the market's {br:+.1f}%, "
            f"{'ahead' if tr > br else 'behind'} by {abs(tr - br):.1f} points.", total_return_pct=tr, benchmark_return_pct=br)
    d200, mp = r.get("dist_200"), r.get("mom_pct")
    if d200 is not None:
        s = f"It trades {abs(d200):.0%} {'above' if d200 > 0 else 'below'} its 200-day average"
        if mp is not None:
            s += f", and its 12-month momentum beats {mp:.0%} of eligible US stocks"
        add(s + ".", dist_200=d200, mom_pct=mp)
    fpe, pe = f.get("forward_pe"), f.get("pe")
    if fpe or pe:
        s = f"It is priced at {fpe:.1f} times next year's expected earnings" if fpe else f"It is priced at {pe:.1f} times trailing earnings"
        up = (val or {}).get("upside")
        if up is not None:
            s += f"; our valuation cross-check puts fair value {abs(up):.0%} {'above' if up > 0 else 'below'} the price"
        add(s + ".", forward_pe=fpe, pe=pe, upside=up)
    vol, dd = st.get("ann_volatility_pct"), st.get("max_drawdown_pct")
    if vol is not None and dd is not None:
        add(f"It has been {'a volatile' if vol > 35 else 'an average-risk' if vol > 20 else 'a steady'} stock: {vol:.0f}% annual volatility, "
            f"with a worst drop of {abs(dd):.0f}% in the past year.", ann_volatility_pct=vol, max_drawdown_pct=dd)
    sn = sent or {}
    if sn.get("level") is not None and (sn.get("n") or 0) >= 3:
        tone = "positive" if sn["level"] >= 0.25 else "negative" if sn["level"] <= -0.25 else "mixed"
        add(f"Recent news is {tone}: a sentiment score of {sn['level']:+.2f} across {sn['n']} articles in three days.",
            sentiment=sn["level"], articles=sn["n"])
    if verdict:
        add(verdict)
    return out


def grade_reasons(row: dict | None, fund: dict | None, peer_pe: float | None, sent: dict | None) -> dict:
    """Why each grade is what it is, in one sentence with its number."""
    r, f, ex, s = row or {}, fund or {}, (fund or {}).get("extra") or {}, sent or {}
    out = {}
    out["momentum"] = (f"Its 12-month return (skipping the latest month), adjusted for volatility, beats {r['mom_pct']:.0%} of eligible US stocks."
                       if r.get("mom_pct") is not None else "Not graded: the name is not in the eligible universe or lacks a year of history.")
    out["trend"] = (f"Price is {r['dist_200']:+.0%} from its 200-day average, further above it than {r['trend_pct']:.0%} of eligible stocks."
                    if r.get("trend_pct") is not None and r.get("dist_200") is not None else "Not graded: no 200-day average yet, or not eligible.")
    fpe = f.get("forward_pe")
    out["value"] = (f"Forward P/E of {fpe:.1f} against a peer median of {peer_pe:.1f} ({fpe / peer_pe:.2f} times its peers)."
                    if fpe and peer_pe else "Not graded: forward earnings or peer multiples are not in data.")
    om = ex.get("operating_margin") if ex.get("operating_margin") is not None else f.get("profit_margin")
    if om is not None:
        om = om / 100.0 if abs(om) > 1.5 else om
        out["quality"] = f"{'Operating' if ex.get('operating_margin') is not None else 'Net'} margin of {om:.0%}."
    else:
        out["quality"] = "Not graded: margins are not in data."
    out["sentiment"] = (f"News sentiment of {s['level']:+.2f} over {s['n']} articles in three days."
                        if s.get("level") is not None and (s.get("n") or 0) >= 3 else "Not graded: fewer than three scored articles in three days.")
    return out


class InsightMixin:
    # ================================================================== the deep Explore profile
    async def _frame_any(self, sym: str):
        """Daily bars from the desk's store, or from the terminal's history providers for a name it lacks."""
        df = self.store.frame(sym)
        if len(df) >= 30:
            return df, "the desk's bar store"
        try:
            res = await self.engine.history(sym, "2y", "1d", None, warmup=False)
            return res.value.df[["open", "high", "low", "close", "volume"]].astype(float), getattr(res.provenance, "provider", None) or "a history provider"
        except asyncio.CancelledError:
            raise
        except Exception:
            return df, "the desk's bar store"

    async def profile(self, symbol: str, refresh: bool = False) -> dict | None:
        """Everything the Explore page explains about one name, cached per name per screen day so a page
        load never spends the free API budget twice."""
        sym = symbol.strip().upper()
        row = self.screen_rows().get(sym)
        day = self.screen_meta().get("day") or datetime.now(NY).date().isoformat()
        cache = self._p("profiles") / f"{sym.replace('^', '_')}.json"
        if cache.exists() and not refresh:
            try:
                got = json.loads(cache.read_text(encoding="utf-8"))
                if got.get("day") == day:
                    return got
            except (OSError, ValueError):
                pass
        rep = None
        try:
            from abg.engine import AnalyzeOptions
            rep = await self.engine.analyze(sym, AnalyzeOptions(period="2y", ai=False, options=False, include_series=False))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("stocks: profile analysis for %s unavailable (%s)", sym, type(e).__name__)
        if rep is None and row is None:
            return None
        rep = rep or {}
        fund = rep.get("fundamentals")
        peers: list = []
        if fund:
            try:
                peers = await self._peers(sym, fund.get("sector"), limit=4)
            except Exception:
                peers = []
        price = (row or {}).get("close") or (rep.get("quote") or {}).get("price")
        val = None
        if fund and price:
            try:
                from abg.pitch.valuation import value
                val = value(fund, price, peers, rf=float(self.cfg.get("pitch_risk_free", 0.042)),
                            erp=float(self.cfg.get("pitch_equity_premium", 0.05)), tg=float(self.cfg.get("pitch_terminal_growth", 0.025)))
            except Exception as e:
                log.info("stocks: profile valuation for %s failed (%s)", sym, type(e).__name__)
        consensus = None
        try:
            fd = self._feeds()
            if fd.enabled:
                consensus = await fd.recommendation(sym)
        except asyncio.CancelledError:
            raise
        except Exception:
            consensus = None
        sent = self.sentiment_for(sym)
        fpe = sorted(x["forward_pe"] for x in peers if isinstance(x, dict) and (x.get("forward_pe") or 0) > 0)
        peer_pe = fpe[len(fpe) // 2] if fpe else None
        fc = rep.get("forecast") or {}
        risk0 = (rep.get("risk") or [{}])[0] if rep.get("risk") else {}
        card = self.name_card(sym) if row is not None else None
        out = {
            "symbol": sym, "day": day, "available": bool(rep), "name": (fund or {}).get("name") or (row or {}).get("name") or rep.get("name"),
            "fundamentals": {k: (fund or {}).get(k) for k in ("name", "sector", "industry", "description", "market_cap", "pe", "forward_pe", "eps",
                                                               "beta", "dividend_yield", "week52_high", "week52_low", "profit_margin", "extra")} if fund else None,
            "statistics": rep.get("statistics"), "signal": {k: (rep.get("signal") or {}).get(k) for k in ("score", "label")} if rep.get("signal") else None,
            "regime": rep.get("regime"), "levels": rep.get("levels"), "indicators": rep.get("indicators"),
            "risk": {k: risk0.get(k) for k in ("model", "score", "level", "drivers")} if risk0 else None,
            "forecast": ({"horizons": fc.get("horizons"), "scenarios": fc.get("scenarios"), "confidence": fc.get("confidence"),
                          "recommendation": {k: (fc.get("recommendation") or {}).get(k) for k in ("action", "horizon_label", "expected_return_pct", "prob_up", "risk_level", "notes")},
                          "thesis": {k: (fc.get("thesis") or {}).get(k) for k in ("headline", "bull_points", "bear_points", "invalidation")},
                          "model": fc.get("model"), "calibration": fc.get("calibration")} if fc.get("available") else None),
            "valuation": val, "peers": [{k: x.get(k) for k in ("symbol", "pe", "forward_pe", "eps")} for x in peers if isinstance(x, dict)],
            "peer_forward_pe": peer_pe, "consensus": consensus, "sentiment": sent,
            "summary": summarize(sym, row, rep.get("statistics"), fund, sent, (card or {}).get("verdict"), val),
            "grade_reasons": grade_reasons(row, fund, peer_pe, sent), "grades": RK.grades({**(row or {}), "sent": sent.get("level"), "sent_n": sent.get("n")}, fund, peer_pe),
            "sources": {"fundamentals": (fund or {}).get("source") if isinstance(fund, dict) else None, "consensus": "Finnhub (third-party analysts)" if consensus else None,
                        "analysis": "DAISY's own models on stored and provider prices"},
        }
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(out, default=str), encoding="utf-8")
        except OSError:
            pass
        return json.loads(json.dumps(out, default=str))

    # ================================================================== analyst pitches
    async def analyst_pitch(self, body: dict, post: bool | None = None) -> dict:
        """Write up a person's idea in the AIBG format, check its levels, and file it as an analyst pitch."""
        from . import charts as CH
        from abg.utils import jsonable
        v = A.clean(body)
        sym = v["symbol"]
        df, source = await self._frame_any(sym)
        if len(df) < 5:
            raise ValueError(f"no price history for {sym}: DAISY cannot check or track a pitch on it")
        day = str(df.index[-1].date())
        pid = f"{day}-{A.BOOK}-{sym}"
        if self.book.get(pid):
            raise FileExistsError(f"{sym} already has an analyst pitch from the {day} close: add a note or a vote to that one")
        price = float(df["close"].iloc[-1])
        row = self.screen_rows().get(sym) or {}
        atr = row.get("atr14")
        if not atr and len(df) >= 15:
            pc_ = df["close"].shift(1)
            tr = (df["high"] - df["low"]).combine((df["high"] - pc_).abs(), max).combine((df["low"] - pc_).abs(), max)
            atr = float(tr.iloc[-14:].mean())
        st = (self.today().get("state") or {})
        state = SimpleNamespace(date=day, regime=st.get("regime") or "warmup", crisis=bool(st.get("crisis")), stress=st.get("stress") or 0.0,
                                flags=st.get("flags") or {}, weights=st.get("weights") or {}, budgets={}, vol_scale=st.get("vol_scale") or 1.0,
                                equity=None, drawdown=None, gross=None, positions={}, universe=st.get("universe") or {}, warnings=[])
        fund, peers, news, provider = await self._enrich(sym)
        earnings = (await self._earnings([sym])).get(sym)
        pc = P.pitch_cfg(self.cfg)
        entry = v.get("entry") or price
        c = Candidate(book=A.BOOK, symbol=sym, action=v["action"], shares=1.0, reason="analyst pitch", price=price,
                      stop=v.get("stop"), atr=atr, facts={k: row.get(k) for k in ("close", "ema50", "ema200", "sma200", "rsi14", "atr14", "mom_12_1", "adv20_usd") if row.get(k) is not None},
                      gates={})
        mood = (self.watch_meta().get("mood") or {})
        ctx = {"store": self.store, "bars": df, "sectors": self.store.sectors(), "fund_holdings": self.fund_holdings(), "open_pitches": {},
               "earnings": earnings, "fund_provider": provider, **self.research_context(sym, fund, peers, day, mood)}
        pack = T.build_pack(c, state, self.cfg, fund=fund, peers=peers or [], news=news or [], provider=provider, ctx=ctx)
        today = date.fromisoformat(day)
        checks = A.check_levels(v, price, atr, float(pc["min_rr"]), row, earnings, today)
        bucket = ((pack.get("rec") or {}).get("conviction") or {}).get("bucket") or "Low"
        sized = Candidate(book=A.BOOK, symbol=sym, action=v["action"], shares=1.0, reason="", price=entry, stop=v.get("stop"), atr=atr)
        risk = P.risk_and_size(sized, entry, v.get("stop"), v.get("t1"), pc.get("fund_equity"), bucket, pc) if v["direction"] == "buy" else \
            P.risk_and_size(sized, entry, None, None, pc.get("fund_equity"), bucket, pc)
        pack["id"] = pid
        A.apply(pack, v, checks, pc, risk, P.next_trading_day(today).isoformat())
        if getattr(self.s, "stocks_charts", True):
            try:
                pack["charts"] = await asyncio.to_thread(CH.render_all, pack, df, self._p("memos"))
            except Exception as e:
                log.info("stocks: charts for analyst pitch %s failed (%s)", sym, type(e).__name__)
        md = A.render(pack, T.render_md(pack))
        memos = self._p("memos")
        memos.mkdir(parents=True, exist_ok=True)
        (memos / f"{pid}.md").write_text(md, encoding="utf-8")
        (memos / f"{pid}.json").write_text(json.dumps(jsonable(pack), default=str), encoding="utf-8")
        plan = P.plan_record(pack, pc) or {}
        if v["direction"] != "buy":                                    # the level walk is a long's: track these on time only
            plan.update({"stop": None, "t1": None, "t2": None})
        self.book.add({"id": pid, "as_of": day, "symbol": sym, "name": pack.get("name"), "book": A.BOOK, "kind": "analyst",
                       "analyst": v["analyst"], "action": v["action"], "rating": (pack.get("rec") or {}).get("rating") or v["action"],
                       "price": price, "stop": v.get("stop"), "sector": pack.get("sector") or self.store.sectors().get(sym),
                       "drank": None, "plan": plan, "plan_mark": None, "track": None, "regime": state.regime, "crisis": state.crisis,
                       "marked": None, "last": None, "ret": None, "spy_ret": None, "excess": None},
                      note=f"analyst pitch by {v['analyst']}")
        posted = "dashboard"
        want = getattr(self.s, "stocks_post_analyst", False) if post is None else post
        if want and self._webhook():
            e = T.embed(pack)
            tag = (getattr(self.s, "stocks_tag", "") or "").strip()
            e["title"] = ((f"[{tag}] " if tag else "") + f"Analyst pitch by {v['analyst']}: " + (e.get("title") or sym))[:256]
            posted = await self._post_to(self._webhook(), [e])
        return {"id": pid, "symbol": sym, "posted": posted, "checks": checks, "passed": sum(1 for x in checks if x["ok"]), "of": len(checks),
                "rating": (pack.get("rec") or {}).get("rating"), "source": source}

    # ================================================================== the News tab
    def _news_sets(self) -> tuple[set, set, set]:
        holdings = {r["symbol"] for r in self.portfolio.rows("open")}
        watched = {p["symbol"] for p in self.pitches() if p.get("status") in ("new", "active")} | \
            {r["symbol"] for r in self.watch_rows(live_only=True)}
        ai = set()
        for k, t in self.themes().items():
            if k.startswith("ai") or "semi" in k or "tech" in k:
                ai |= set(t["symbols"])
        return holdings, watched, ai

    def newsfeed(self, category: str | None = None, symbol: str | None = None, q: str | None = None, days: int = 4, limit: int = 120) -> dict:
        holdings, watched, ai = self._news_sets()
        since = (datetime.now(NY) - timedelta(days=days)).isoformat()
        rows = N.recent(self.db, symbol.strip().upper() if symbol else None, since=since, limit=800)
        seen, items, counts = set(), [], {k: 0 for k, _ in N.CATEGORIES}
        ql = (q or "").strip().lower()
        for a in rows:
            key = (a.get("title") or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            cats = N.categorize(a, holdings, watched, ai)
            for c in cats:
                counts[c] = counts.get(c, 0) + 1
            if category and category not in cats:
                continue
            if ql and ql not in key and ql != (a.get("symbol") or "").lower():
                continue
            mine = 0 if "holdings" in cats else 1 if "pitches" in cats else 2
            items.append((mine, a, cats))
        items = sorted(items, key=lambda x: x[1]["at"], reverse=True)
        items.sort(key=lambda x: x[0])                                   # holdings first, then pitches, newest first within each
        out = [{"at": a["at"], "symbol": a.get("symbol"), "title": a["title"], "url": a.get("url"), "source": a.get("source"),
                "kind": a.get("kind"), "sent": a.get("sent"), "categories": cats, "mine": ["holding", "pitch", None][m]}
               for m, a, cats in items[:max(1, min(300, limit))]]
        day_ago = (datetime.now(NY) - timedelta(days=1)).isoformat()
        n_day = sum(1 for a in rows if a["at"] >= day_ago)
        n_hold = sum(1 for a in rows if a["at"] >= day_ago and a.get("symbol") in holdings)
        mood = self.watch_meta().get("mood") or self.market_mood()
        return {"categories": [{"key": k, "label": lab, "count": counts.get(k, 0)} for k, lab in N.CATEGORIES], "items": out,
                "brief": N.brief(mood, self.heat(), self.last_shocks(), {"articles": n_day, "holdings": n_hold}),
                "shocks": self.last_shocks(), "last": self.last_news, "category": category, "q": q, "symbol": symbol}

    def holdings_news(self, view: dict | None = None, days: int = 7) -> dict:
        """News about the companies the fund holds, and nothing else: grouped by holding, with one line on
        what changed and the items that need a look flagged at the top."""
        view = view or self.portfolio_view()
        since = (datetime.now(NY) - timedelta(days=days)).isoformat()
        scr, groups, flagged, seen_sym = self.screen_rows(), [], [], set()
        today = datetime.now(NY).date().isoformat()
        for h in view.get("open") or []:
            sym = h["symbol"]
            if sym in seen_sym:
                continue
            seen_sym.add(sym)
            arts = N.recent(self.db, sym, since=since, limit=10)
            sent = self.sentiment_for(sym, today)
            r = scr.get(sym) or {}
            bits = []
            if r.get("chg_1d") is not None:
                bits.append(f"{r['chg_1d']:+.1%} at the last close")
            if r.get("chg_5d") is not None:
                bits.append(f"{r['chg_5d']:+.1%} over five sessions")
            if sent.get("level") is not None:
                bits.append(f"news sentiment {sent['level']:+.2f}" + (f" ({sent['change']:+.2f} against its 20-day baseline)" if sent.get("change") is not None else ""))
            flags = N.risk_flags(sym, arts, sent, self.cfg, self.last_shocks())
            for f in flags:
                flagged.append({"symbol": sym, **f})
            groups.append({"symbol": sym, "name": r.get("name"), "weight": h.get("weight"),
                           "line": (f"{sym}: " + "; ".join(bits) + ".") if bits else f"{sym}: no stored prices or scored news yet.",
                           "numbers": {"chg_1d": r.get("chg_1d"), "chg_5d": r.get("chg_5d"), "sentiment": sent.get("level"), "sentiment_change": sent.get("change"), "articles": sent.get("n")},
                           "items": [{k: a.get(k) for k in ("at", "title", "url", "source", "kind", "sent")} for a in arts], "flags": flags})
        return {"groups": groups, "flagged": flagged[:12], "days": days}

    def portfolio_full(self) -> dict:
        view = self.portfolio_view()
        bench = self.cfg.get("benchmark", "SPY")

        def closes(sym):
            f = self.store.frame(sym)
            return f["close"] if len(f) else None
        cap = float(P.pitch_cfg(self.cfg)["max_position_pct"])
        try:
            perf = PF.performance(self.portfolio.rows(), self.portfolio.flows(), self.portfolio.cash(), closes, closes(bench))
        except Exception as e:
            log.warning("stocks: portfolio performance failed (%s: %s)", type(e).__name__, str(e)[:120])
            perf = {"available": False, "reason": "performance could not be computed from the log", "series": [], "stats": {}, "assumptions": []}
        pitches = self.pitches()
        return {**view, "performance": perf, "breakdown": PF.breakdown(view, self.store.sectors(), cap),
                "review": PF.review(view, pitches, self.screen_rows(), cap), "news": self.holdings_news(view),
                "flows": self.portfolio.flows(), "log": self.portfolio.log()}

    # ================================================================== the strategy page
    def strategy(self) -> dict:
        """Why DAISY uses the rules it uses: each book's idea, when it works, and its record - the replay's
        base rates, our own pitches, and the last research study."""
        base = self.db.kv_get("base_by_book") or {}
        rows = self.pitches()
        from . import pitchbook as PB
        rec = PB.track_record([r for r in rows if r["book"] != "analyst"])["by_book"]
        books_cfg = self.cfg.get("books") or {}
        st = self.today().get("state") or {}
        books = []
        for key in ("trend_pullback", "mean_reversion", "xs_momentum", "dual_momentum", "crisis_alpha"):
            p = books_cfg.get(key) or {}
            if not p.get("enabled", True) and key not in rec:
                continue
            story = BOOK_STORY[key]
            books.append({"key": key, "label": BOOK_LABEL.get(key, key), "edge": BOOK_EDGE.get(key, ""), **story,
                          "enabled": bool(p.get("enabled", True)), "regimes": p.get("regimes") or {},
                          "weight_now": (st.get("weights") or {}).get(key), "base": base.get(key) or {}, "record": rec.get(key),
                          "params": {k: p.get(k) for k in ("rsi_pullback", "rsi_entry", "rsi_exit", "stop_atr", "trail_atr", "max_hold", "top_n", "min_price", "min_adv_usd") if p.get(k) is not None}})
        rep = self.last_research() or {}
        abl = rep.get("ablations") or rep.get("ablation") or []
        pc = P.pitch_cfg(self.cfg)
        sample = next((c for c in (self.today().get("candidates") or []) if ((c.get("rec") or {}).get("conviction") or {}).get("breakdown")), None)
        return {"books": books, "regime": {"now": st.get("regime"), "stress": st.get("stress"), "crisis": st.get("crisis"), "as_of": st.get("date"),
                                           "explain": "The regime is the rules' own read of the market from the benchmark's trend, volatility and choppiness over months. "
                                                      "It decides how much each book may trade. Market mood, on the Today page, is a different thing: a same-day count of "
                                                      "breadth, highs and lows, VIX and headlines. They can disagree: a calm, rising day inside a longer downtrend reads as "
                                                      "risk-on mood and risk-off regime."},
                "conviction": {"weights": pc["conviction"], "buckets": pc["buckets"], "size_scale": pc["size_scale"],
                               "example": {"symbol": sample["symbol"], "breakdown": sample["rec"]["conviction"]["breakdown"],
                                           "score": sample["rec"]["conviction"]["score"], "bucket": sample["rec"]["conviction"]["bucket"]} if sample else None},
                "rank": RK.RANK_WORDS, "research": {"at": rep.get("at"), "verdict": rep.get("verdict"), "findings": rep.get("findings") or [],
                                                    "ablations": [{k: a.get(k) for k in ("variant", "sharpe", "cagr", "max_drawdown")} for a in abl if isinstance(a, dict) and not a.get("error")][:14],
                                                    "walk_forward": {k: (rep.get("walk_forward") or rep.get("wf") or {}).get(k) for k in ("oos", "benchmark", "positive_years", "test_years")}},
                "timeline": [{"when": w, "title": t, "why": y} for w, t, y in TIMELINE]}
