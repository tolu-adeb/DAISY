"""The pitch desk (docs/20, stage 4).

    screen      every name in the universe: daily bars (1y+) + fundamentals -> factor z-scores -> ranked list
    make SYM    data pack -> valuation (DCF, reverse DCF, comps, bear/base/bull) -> verdict -> memo with sources
    weekly      ABG_PITCH_WEEKDAY / ABG_PITCH_TIME (Sat 10:00 ET): screen, then memos for the top ABG_PITCH_MAX_NAMES
                names not pitched in the last 4 weeks; posted to ABG_PITCH_WEBHOOK_URL when it is set
    scorecard   every pitch is marked against SPY from its pitch-day close (paper, no trades)

State: <data_dir>/pitch/pitches.json (pitches + scorecard marks), <data_dir>/pitch/screen.json (last screen),
<data_dir>/pitch/memos/<id>.md.
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

from . import memo as memo_mod
from .screen import raw_factors, rank, universe
from .valuation import value

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
DAYS = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"]


def _close(ph) -> "object":
    df = ph.df
    return df["adj_close"] if "adj_close" in df and df["adj_close"].notna().sum() > len(df) * 0.9 else df["close"]


class PitchDesk:
    def __init__(self, settings, engine, hub=None, symbols_fn=None):
        self.s = settings
        self.engine = engine
        self.hub = hub
        self.symbols_fn = symbols_fn                 # extra names (your watchlist) - callable -> list[str]
        self.dir = Path(settings.data_dir).expanduser() / "pitch"
        self.errors: deque = deque(maxlen=20)
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self.running = False
        self.last_batch: dict | None = None

    # ------------------------------------------------------------------ storage
    def _path(self, name: str) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir / name

    def _load(self, name: str, default):
        p = self.dir / name
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
        except (OSError, ValueError):
            return default

    def _save(self, name: str, data) -> None:
        p = self._path(name)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        tmp.replace(p)

    def pitches(self) -> list[dict]:
        return self._load("pitches.json", [])

    def last_screen(self) -> dict | None:
        return self._load("screen.json", None)

    # ------------------------------------------------------------------ data
    async def _one(self, sym: str, sem: asyncio.Semaphore) -> tuple[str, object, dict | None, list[str]]:
        async with sem:
            ph = fund = None
            prov = []
            try:
                got = await self.engine.history(sym, period="2y", interval="1d")
                ph = got.value
                prov.append(f"prices: {got.provenance.provider}")
            except Exception as e:
                self.errors.append({"ts": time.time(), "where": f"history {sym}", "error": f"{type(e).__name__}"[:120]})
            try:
                gf = await self.engine.fundamentals(sym)
                fund = gf.value.to_dict()
                prov.append(f"fundamentals: {gf.provenance.provider}")
            except Exception as e:
                self.errors.append({"ts": time.time(), "where": f"fundamentals {sym}", "error": f"{type(e).__name__}"[:120]})
            return sym, ph, fund, prov

    def universe(self) -> list[str]:
        extra = []
        if self.symbols_fn:
            try:
                extra = list(self.symbols_fn() or [])
            except Exception:
                extra = []
        return universe(self.s, extra)

    async def screen(self, symbols: list[str] | None = None) -> dict:
        syms = symbols or self.universe()
        sem = asyncio.Semaphore(4)
        got = await asyncio.gather(*(self._one(s, sem) for s in syms))
        rows, funds, closes, prov = {}, {}, {}, {}
        for sym, ph, fund, pv in got:
            if ph is None or ph.df is None or len(ph.df) < 63:
                continue
            closes[sym] = _close(ph)
            funds[sym] = fund or {}
            prov[sym] = pv
            rows[sym] = raw_factors(closes[sym], funds[sym])
        ranked = rank(rows)
        for r in ranked:
            f = funds[r["symbol"]]
            r.update(name=f.get("name"), sector=f.get("sector") or f.get("industry"),
                     price=float(closes[r["symbol"]].iloc[-1]), pe=f.get("pe"), forward_pe=f.get("forward_pe"))
        out = {"at": datetime.now(NY).isoformat(timespec="seconds"), "n": len(ranked), "asked": len(syms),
               "missing": sorted(set(syms) - set(rows)), "rows": ranked}
        self._save("screen.json", out)
        self._cache = {"funds": funds, "closes": closes, "prov": prov}
        return out

    # ------------------------------------------------------------------ memo
    async def make(self, symbol: str, post: bool = False, ai: bool | None = None, screen: dict | None = None) -> dict:
        sym = symbol.strip().upper()
        async with self._lock:
            scr = screen or self.last_screen()
            if not scr or not any(r["symbol"] == sym for r in scr["rows"]) or not getattr(self, "_cache", None):
                syms = self.universe()
                scr = await self.screen(syms if sym in syms else syms + [sym])
            row = next((r for r in scr["rows"] if r["symbol"] == sym), None)
            if row is None:
                raise ValueError(f"no price data for {sym}")
            funds = self._cache["funds"]
            fund = funds.get(sym) or {}
            sector = fund.get("sector") or fund.get("industry")
            peers = [dict(f, symbol=s) for s, f in funds.items()
                     if s != sym and sector and (f.get("sector") or f.get("industry")) == sector]
            price = row["price"]
            val = value(fund, price, peers, self.s.pitch_risk_free, self.s.pitch_equity_premium,
                        self.s.pitch_terminal_growth)
            news = []
            try:
                gn = await self.engine.news(sym, limit=8)
                news = [{"title": n.title, "url": n.url, "publisher": n.publisher, "source": n.source,
                         "published_at": n.published_at.isoformat() if n.published_at else None} for n in gn.value]
            except Exception:
                pass
            spy = await self._spy()
            sources = {"data": "; ".join(self._cache["prov"].get(sym, [])) or "n/a",
                       "peers": f"{len(peers)} names in '{sector or 'n/a'}' from the screened universe"}
            pack = memo_mod.build_pack(sym, price, fund, row, scr["n"], val, news, sources, spy)
            use_ai = self.s.pitch_ai_memo if ai is None else ai
            note = await asyncio.to_thread(memo_mod.ai_note, self.s, pack) if use_ai else None
            md = memo_mod.render_md(pack, note)
            pid = f"{datetime.now(NY):%Y%m%d}-{sym}"
            self._path("memos")
            (self.dir / "memos").mkdir(exist_ok=True)
            (self.dir / "memos" / f"{pid}.md").write_text(md, encoding="utf-8")
            rec = {"id": pid, "symbol": sym, "name": pack["name"], "made_at": pack["made_at"], "price": price,
                   "spy": spy, "verdict": pack["verdict"], "fair_value": val.get("fair_value"),
                   "upside": val.get("upside"), "rank": row["rank"], "of": scr["n"], "note": bool(note),
                   "pack": pack, "delivered": "dashboard"}
            if post:
                rec["delivered"] = await self._post(memo_mod.embed(pack))
            items = [p for p in self.pitches() if p["id"] != pid] + [rec]
            self._save("pitches.json", items)
            self._broadcast({"type": "pitch", "id": pid, "symbol": sym, "verdict": rec["verdict"]})
            return {**rec, "memo": md}

    def memo(self, pid: str) -> str | None:
        p = self.dir / "memos" / f"{Path(pid).name}.md"
        return p.read_text(encoding="utf-8") if p.exists() else None

    async def _spy(self) -> float | None:
        try:
            q = await self.engine.quote("SPY")
            return float(q.value.price)
        except Exception:
            return None

    # ------------------------------------------------------------------ scorecard
    async def score(self) -> dict:
        items = self.pitches()
        if not items:
            return self.scorecard()
        spy = await self._spy()
        for p in items:
            try:
                q = await self.engine.quote(p["symbol"])
                p["mark"] = float(q.value.price)
                p["mark_spy"] = spy
                p["marked_at"] = datetime.now(NY).isoformat(timespec="seconds")
            except Exception:
                continue
        self._save("pitches.json", items)
        return self.scorecard()

    def scorecard(self, items: list[dict] | None = None) -> dict:
        items = items if items is not None else self.pitches()
        rows = []
        for p in items:
            ret = (p["mark"] / p["price"] - 1) if p.get("mark") and p.get("price") else None
            spy = (p["mark_spy"] / p["spy"] - 1) if p.get("mark_spy") and p.get("spy") else None
            rows.append({k: p.get(k) for k in ("id", "symbol", "name", "made_at", "verdict", "price", "fair_value",
                                                "upside", "rank", "of", "mark", "marked_at", "delivered")}
                        | {"return": ret, "spy_return": spy,
                           "excess": ret - spy if ret is not None and spy is not None else None})
        def agg(rs):
            ex = [r["excess"] for r in rs if r["excess"] is not None]
            return {"n": len(rs), "marked": len(ex), "avg_excess": sum(ex) / len(ex) if ex else None,
                    "beat_spy": sum(1 for x in ex if x > 0) / len(ex) if ex else None}
        by = {v: agg([r for r in rows if r["verdict"] == v]) for v in ("LONG", "WATCH", "PASS")}
        return {"all": agg(rows), "by_verdict": by, "pitches": sorted(rows, key=lambda r: r["made_at"], reverse=True)}

    # ------------------------------------------------------------------ discord
    def _webhook(self) -> str | None:
        return getattr(self.s, "pitch_webhook_url", None) or None

    async def _post(self, emb: dict) -> str:
        url = self._webhook()
        if not url:
            return "dashboard"
        body = {"username": "Pitch Desk", "allowed_mentions": {"parse": []}, "embeds": [emb]}
        try:
            await self.engine.http.request("POST", url + ("&" if "?" in url else "?") + "wait=true",
                                           provider="discord", json=body)
            return "posted"
        except Exception as e:
            self.errors.append({"ts": time.time(), "where": "discord", "error": type(e).__name__})
            return "post failed"

    def _broadcast(self, payload: dict) -> None:
        if self.hub is not None and hasattr(self.hub, "broadcast"):
            try:
                self.hub.broadcast("pitch", payload)
            except Exception:
                pass

    # ------------------------------------------------------------------ weekly batch
    async def batch(self, post: bool = True) -> dict:
        scr = await self.screen()
        recent = {p["symbol"] for p in self.pitches()
                  if p.get("made_at", "") >= (datetime.now(NY) - timedelta(days=28)).isoformat()}
        picks = [r["symbol"] for r in scr["rows"] if r["symbol"] not in recent][: max(0, int(self.s.pitch_max_names))]
        made = []
        for sym in picks:
            try:
                r = await self.make(sym, post=post, screen=scr)
                made.append({"symbol": sym, "verdict": r["verdict"], "delivered": r["delivered"]})
            except Exception as e:
                self.errors.append({"ts": time.time(), "where": f"pitch {sym}", "error": f"{type(e).__name__}: {e}"[:200]})
        await self.score()
        self.last_batch = {"at": datetime.now(NY).isoformat(timespec="seconds"), "made": made, "screened": scr["n"]}
        st = self._load("state.json", {})
        st["last_batch"] = self.last_batch
        self._save("state.json", st)
        return self.last_batch

    def due(self, now: datetime) -> bool:
        h, m = (self.s.pitch_time or "10:00").split(":")
        if now.weekday() != int(self.s.pitch_weekday) or (now.hour, now.minute) < (int(h), int(m)):
            return False
        return (self._load("state.json", {}).get("last_batch") or {}).get("at", "")[:10] != now.date().isoformat()

    async def run(self) -> None:
        self._stop.clear()
        self.running = True
        try:
            while not self._stop.is_set():
                try:
                    if self.due(datetime.now(NY)):
                        await self.batch(post=True)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("pitch batch failed")
                    self.errors.append({"ts": time.time(), "where": "pitch batch", "error": f"{type(e).__name__}: {e}"[:300]})
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.running = False

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        scr = self.last_screen()
        st = self._load("state.json", {})
        return {"enabled": bool(getattr(self.s, "pitch_enabled", False)), "running": self.running,
                "webhook": bool(self._webhook()), "ai_memo": bool(self.s.pitch_ai_memo),
                "schedule": f"{DAYS[int(self.s.pitch_weekday) % 7]} {self.s.pitch_time} ET", "max_names": self.s.pitch_max_names,
                "universe": len(self.universe()), "last_screen": scr["at"] if scr else None,
                "last_batch": st.get("last_batch"), "pitches": len(self.pitches()), "errors": list(self.errors)[-5:]}
