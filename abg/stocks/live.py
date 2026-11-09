"""Today's signals from the books.

``signals_for_today`` replays the books over the panel up to the latest session and returns what each
book wants to do at the next open, with the engine state behind each decision attached - the indicator
values, the ranking, the gates that passed - so the thesis writer can say *why* without guessing, and
so the same row can later be used as a meta-labelling feature.

The replay is the vendored backtester with ``live=True``: live candidates and backtest trades come
from the same lines of code, which is the only way the paper record means anything.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime

import pandas as pd

from abg.tradebook.stocks.engine import Engine

log = logging.getLogger(__name__)

BOOK_LABEL = {
    "dual_momentum": "Dual momentum (ETF rotation)",
    "xs_momentum": "Cross-sectional 12-1 momentum",
    "trend_pullback": "Trend pullback",
    "mean_reversion": "Mean reversion",
    "crisis_alpha": "Crisis alpha",
}
BOOK_SHORT = {"dual_momentum": "Dual mom.", "xs_momentum": "12-1 mom.",
              "trend_pullback": "Pullback", "mean_reversion": "Reversion",
              "crisis_alpha": "Crisis"}
BOOK_EDGE = {
    "dual_momentum": "own the strongest sectors while the market is in an uptrend, and hide in the "
                     "best defensive asset when it is not",
    "xs_momentum": "stocks with the strongest risk-adjusted 12-month trend keep outperforming, so the "
                   "most recent month is skipped because it tends to reverse",
    "trend_pullback": "buy a short pullback inside an established uptrend, as the pullback ends",
    "mean_reversion": "buy sharp short-term weakness in a stock still above its long-term average, "
                      "and sell into the first bounce",
    "crisis_alpha": "in a crisis, hold what is rising and fade the index while it trends down",
}
ACTION = {"buy": "BUY", "sell": "SELL", "target": "REBALANCE"}


@dataclass
class Candidate:
    """One thing a book wants to do at the next open."""
    book: str
    symbol: str
    action: str                      # BUY | SELL | REBALANCE
    shares: float
    reason: str
    price: float | None = None
    stop: float | None = None
    stop_atr: float | None = None
    atr: float | None = None
    risk_pct: float | None = None    # share of the book's budget risked to the initial stop
    weight: float | None = None      # share of the book's budget this position takes
    notional: float | None = None
    rank: int | None = None
    of: int | None = None
    score: float | None = None
    ml_prob: float | None = None
    facts: dict = field(default_factory=dict)      # the indicator values behind the decision
    gates: dict = field(default_factory=dict)      # named checks and whether each passed

    @property
    def is_entry(self) -> bool:
        return self.action == "BUY" or (self.action == "REBALANCE" and self.shares > 0)

    @property
    def r_dollars(self) -> float | None:
        if self.price is None or self.stop is None or self.shares is None:
            return None
        return abs(self.price - self.stop) * abs(self.shares)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["book_label"] = BOOK_LABEL.get(self.book, self.book)
        d["r_dollars"] = self.r_dollars
        return d


@dataclass
class DeskState:
    """What the system holds and believes as of the last session in the panel."""
    date: str
    regime: str
    crisis: bool
    stress: float
    equity: float
    drawdown: float
    vol_scale: float
    gross: float
    weights: dict
    budgets: dict
    flags: dict
    positions: dict                   # book -> {symbol: {...}}
    bench_vs: dict
    universe: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def _num(x):
    """Plain floats for JSON, with NaN dropped rather than serialised as a null that looks like data."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(v) else round(v, 6)


def _pos_dict(pos, price) -> dict:
    open_r = None
    if pos.init_stop is not None and pos.entry_price not in (None, 0) and pos.entry_price != pos.init_stop:
        open_r = (price - pos.entry_price) / (pos.entry_price - pos.init_stop)
    return {"shares": _num(pos.shares), "entry": _num(pos.entry_price),
            "entry_date": str(pos.entry_date.date()) if hasattr(pos.entry_date, "date") else str(pos.entry_date),
            "stop": _num(pos.stop), "init_stop": _num(pos.init_stop), "days_held": pos.days_held,
            "price": _num(price), "open_pct": _num((price / pos.entry_price - 1) if pos.entry_price else None),
            "open_r": _num(open_r)}


class LiveSignals:
    """Builds the panel once, then answers questions about today."""

    def __init__(self, cfg: dict, store, universe, macro: pd.DataFrame | None = None):
        self.cfg = cfg
        self.store = store
        self.u = universe
        self.macro = macro
        self.panel = None
        self.engine: Engine | None = None
        self.result: dict | None = None
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ build
    def build(self, start=None, warn: bool = True) -> "LiveSignals":
        import warnings as _w
        bench = self.cfg["benchmark"]
        with _w.catch_warnings(record=True) as caught:
            _w.simplefilter("always")
            self.panel = self.store.panel(self.u.daily(), benchmark=bench,
                                          start=start or self.cfg.get("panel_start"),
                                          min_bars=int(self.cfg.get("min_bars", 250)), warn=warn)
            self.warnings = [str(c.message) for c in caught]
        cfg = dict(self.cfg)
        # the books must never treat an ETF or a gauge as a single stock
        cfg["non_stock_tickers"] = sorted(set(cfg.get("non_stock_tickers", [])) | set(self.u.non_stock()))
        self.engine = Engine(cfg, self.panel, self.macro)
        return self

    def run(self, start=None, end=None) -> dict:
        if self.engine is None:
            self.build()
        self.result = self.engine.run(start=start or self.cfg.get("start"), end=end, live=True)
        return self.result

    # ------------------------------------------------------------------ today
    def candidates(self) -> list[Candidate]:
        if self.result is None:
            self.run()
        r, E = self.result, self.engine
        I, i = E.ind, r["last_i"]
        out = [self._candidate(o, o.book, I, i, r) for o in r["signals"]]
        # Rank the entries inside each book by that book's own score (the same quantity it sorted its
        # candidates by), so a pitch can honestly say "3rd best of the 11 that qualified".
        for book in {c.book for c in out}:
            entries = [c for c in out if c.book == book and c.is_entry]
            ordered = sorted(entries, key=lambda c: (c.score is None, -(c.score or 0), c.symbol))
            for n, c in enumerate(ordered, 1):
                c.rank, c.of = n, len(ordered)
        out.sort(key=lambda c: (0 if c.action == "SELL" else 1, c.book, c.rank or 99))
        return out

    def _candidate(self, o, book: str, I, i: int, r: dict) -> Candidate:
        t = o.symbol if hasattr(o, "symbol") else o.ticker
        price = _num(I.close[t].iat[i]) if t in I.close.columns else None
        budget = r["budgets"].get(book) or 0.0
        c = Candidate(book=book, symbol=t, action=ACTION.get(o.kind, o.kind.upper()),
                      shares=_num(o.shares) or 0.0, reason=o.reason, price=price,
                      stop_atr=_num(o.stop_atr), atr=_num(o.atr))
        if price and o.shares:
            c.notional = _num(abs(o.shares) * price)
            if budget:
                c.weight = _num(abs(o.shares) * price / budget)
        if o.stop_atr and o.atr and price:
            c.stop = _num(price - o.stop_atr * o.atr)
            if budget:
                c.risk_pct = _num(o.stop_atr * o.atr * abs(o.shares) / budget)
        ml = r.get("ml")
        if ml is not None:
            c.ml_prob = _num(ml.prob(t, i))
        c.facts, c.gates, c.score = _explain(book, t, I, i, r, self.cfg)
        return c

    def state(self) -> DeskState:
        if self.result is None:
            self.run()
        r, E = self.result, self.engine
        eq = r["equity"]
        last = eq.iloc[-1]
        i = r["last_i"]
        flags = {k: bool(v) for k, v in E.flags.iloc[i].items()} if len(E.flags) > i else {}
        pos = {}
        for b, bp in r["positions"].items():
            if not bp:
                continue
            pos[b] = {t: _pos_dict(p, _num(E.ind.close[t].iat[i]) or p.entry_price) for t, p in bp.items()}
        bench = eq["benchmark"]
        n = min(len(eq), 252)
        bench_vs = {"strategy_1y": _num(last["equity"] / eq["equity"].iloc[-n] - 1),
                    "benchmark_1y": _num(bench.iloc[-1] / bench.iloc[-n] - 1),
                    "since_start": _num(last["equity"] / eq["equity"].iloc[0] - 1),
                    "benchmark_since_start": _num(bench.iloc[-1] / bench.iloc[0] - 1)}
        return DeskState(
            date=str(pd.Timestamp(r["last_date"]).date()), regime=str(last["regime"]),
            crisis=bool(last["crisis"]), stress=_num(last["stress"]) or 0.0,
            equity=_num(last["equity"]) or 0.0, drawdown=_num(last["drawdown"]) or 0.0,
            vol_scale=_num(last["vol_scale"]) or 1.0, gross=_num(last["gross"]) or 0.0,
            weights={k: _num(v) for k, v in r["weights"].items()},
            budgets={k: _num(v) for k, v in r["budgets"].items()},
            flags=flags, positions=pos, bench_vs=bench_vs,
            universe=self.u.describe(), warnings=list(self.warnings))


# --------------------------------------------------------------------------- per-book explanation
def _explain(book: str, t: str, I, i: int, r: dict, cfg: dict) -> tuple[dict, dict, float | None]:
    """The indicator values and gate results behind one book's decision.

    Read straight off the same indicator frames the book read, so the numbers in a pitch are the
    numbers the rule used - not a re-derivation that could drift from it.
    """
    p = (cfg.get("books") or {}).get(book, {})
    g = lambda df: _num(df[t].iat[i]) if t in df.columns else None      # noqa: E731
    f: dict = {"close": g(I.close), "atr14": g(I.atr14)}
    gates: dict = {}
    score = None

    if book == "trend_pullback":
        f.update({"ema50": g(I.ema50), "ema200": g(I.ema200), "rsi14": g(I.rsi14),
                  "min_rsi14_5d": g(I.min_rsi14_5), "macd_hist": g(I.macd_hist),
                  "macd_hist_prev": g(I.macd_hist_prev), "prev_high": g(I.prev_high),
                  "mom126": g(I.mom126), "adv20_usd": g(I.adv20)})
        c, e50, e200 = f["close"], f["ema50"], f["ema200"]
        gates["uptrend: close above the 200-day EMA"] = bool(c and e200 and c > e200)
        gates["uptrend: 50-day EMA above the 200-day"] = bool(e50 and e200 and e50 > e200)
        gates[f"a pullback happened: lowest RSI-14 of the last 5 days at or below {p.get('rsi_pullback', 40)}"] = \
            bool(f["min_rsi14_5d"] is not None and f["min_rsi14_5d"] <= p.get("rsi_pullback", 40))
        gates["the pullback is ending: close above yesterday's high"] = \
            bool(c and f["prev_high"] and c > f["prev_high"])
        gates["the pullback is ending: MACD histogram rising"] = \
            bool(f["macd_hist"] is not None and f["macd_hist_prev"] is not None
                 and f["macd_hist"] > f["macd_hist_prev"])
        score = f["mom126"]
    elif book == "mean_reversion":
        rp = p.get("rsi_period", 2)
        f.update({"sma200": g(I.sma200), "sma5": g(I.sma5), f"rsi{rp}": g(I.rsi2 if rp == 2 else I.rsi14),
                  "bb_low": g(I.bb_low), "adv20_usd": g(I.adv20)})
        rv = f.get(f"rsi{rp}")
        gates["long-term uptrend: close above the 200-day average"] = \
            bool(f["close"] and f["sma200"] and f["close"] > f["sma200"])
        gates[f"oversold: RSI({rp}) below {p.get('rsi_entry', 10)}"] = \
            bool(rv is not None and rv < p.get("rsi_entry", 10))
        if p.get("require_bb"):
            gates["below the lower Bollinger band"] = bool(f["close"] and f["bb_low"] and f["close"] < f["bb_low"])
        score = -(rv if rv is not None else 99)
    elif book == "xs_momentum":
        f.update({"mom_12_1": g(I.mom_12_1), "vol252": g(I.vol252), "vol60": g(I.vol60),
                  "adv20_usd": g(I.adv20)})
        if f["mom_12_1"] is not None and f["vol252"]:
            score = _num(f["mom_12_1"] / f["vol252"])
        gates["12-1 momentum positive after dividing by volatility"] = bool(score and score > p.get("min_score", 0.0))
        gates[f"in the top {p.get('top_n', 20)} of the universe by that score"] = True
    elif book == "dual_momentum":
        f.update({"mom126": g(I.mom126), "mom252": g(I.mom252), "sma200": g(I.sma200), "vol60": g(I.vol60)})
        if f["mom126"] is not None and f["mom252"] is not None:
            score = _num(0.5 * (f["mom126"] + f["mom252"]))
        bench = cfg["benchmark"]
        bc = _num(I.close[bench].iat[i]) if bench in I.close.columns else None
        bm = _num(I.sma200[bench].iat[i]) if bench in I.sma200.columns else None
        f["benchmark_close"], f["benchmark_sma200"] = bc, bm
        gates["market switch is risk-on: the benchmark is above its 200-day average"] = bool(bc and bm and bc > bm)
        gates["its own trend filter: price above its 200-day average"] = \
            bool(f["close"] and f["sma200"] and f["close"] > f["sma200"])
        gates["momentum score above zero"] = bool(score and score > 0)
    elif book == "crisis_alpha":
        f.update({"sma50": g(I.sma50), "mom63": g(I.mom63), "rsi2": g(I.rsi2)})
        gates["crisis mode is on"] = bool(r["equity"].iloc[-1]["crisis"])
    # the regime gate applies to every book
    reg = str(r["equity"].iloc[-1]["regime"])
    mult = (p.get("regimes") or {}).get(reg)
    if mult is not None:
        gates[f"this book trades in a {reg.replace('_', ' ')} market (size x{mult:g})"] = mult > 0
    return f, gates, score


def signals_for_today(cfg: dict, store, universe, macro=None, start=None) -> tuple[list[Candidate], DeskState, LiveSignals]:
    ls = LiveSignals(cfg, store, universe, macro).build()
    ls.run(start=start)
    return ls.candidates(), ls.state(), ls
