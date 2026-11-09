"""Daily portfolio backtester for the stock books.

Timeline for each trading day d:
  1. Orders created at the previous close fill at today's open (sells, then rebalances, then buys),
     with slippage and commission. Buys are cut back to respect the gross-exposure and name caps.
  2. Swing positions are checked against their stop using today's low (gap-through fills at the open).
  3. Positions are marked at the close; book values, equity and drawdown are updated.
  4. Kill switch: if equity is more than dd_kill below its peak, everything is sold at the next open and
     books are paused for dd_pause_days.
  5. On the last trading day of each month, Hedge weights across books are updated from each book's
     monthly return.
  6. Budgets for tomorrow = equity x Hedge weight x volatility-target scale (crisis_alpha gets its own budget
     in crisis mode).
  7. Each book reads today's close and returns orders for tomorrow's open.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from abg.tradebook.common.fastframe import cache_dataclass
from abg.tradebook.common.hedge import Hedge
from abg.tradebook.stocks import regime as rg
from abg.tradebook.stocks import stress as st
from abg.tradebook.stocks.books import BOOKS, Order, PanelIndicators, Position
from abg.tradebook.stocks.data import Panel, last_trading_day_of_month


@dataclass
class Ctx:
    engine: "Engine"
    book: str
    i: int
    date: pd.Timestamp
    regime: str
    crisis: bool
    budget: float
    equity: float
    month_end: bool
    positions: dict
    ind: PanelIndicators
    benchmark: str
    ml: object | None

    def universe(self, p: dict) -> list[str]:
        e = self.engine
        i = self.i
        allowed = p.get("universe")
        if isinstance(allowed, list):
            cands = [t for t in allowed if t in e.panel.close.columns]
        else:
            excl = e.non_stock
            cands = [t for t in e.panel.tickers if t not in excl]
        out = []
        for t in cands:
            if not e.panel.eligible[t].iat[i]:
                continue
            c = self.ind.close[t].iat[i]
            if pd.isna(c) or c < p.get("min_price", 5.0):
                continue
            adv = self.ind.adv20[t].iat[i]
            if pd.isna(adv) or adv < p.get("min_adv_usd", 2e7):
                continue
            out.append(t)
        return out

    def sentiment(self, t: str) -> float:
        s = self.engine.panel.sentiment
        if s is None or t not in s.columns:
            return 0.0
        v = s[t].iat[self.i]
        return 0.0 if pd.isna(v) else float(v)

    def target_orders(self, book: str, targets: dict[str, float], min_change: float, reason: str) -> list[Order]:
        out = []
        cur = self.positions
        for t in sorted(set(cur) | set(targets)):
            w = targets.get(t, 0.0)
            price = self.ind.close[t].iat[self.i]
            if pd.isna(price) or price <= 0:
                continue
            tgt = math.trunc(w * self.budget / price)
            now = cur[t].shares if t in cur else 0.0
            if tgt == now:
                continue
            if tgt != 0 and now != 0 and abs(tgt - now) * price < min_change * max(self.budget, 1.0):
                continue
            out.append(Order(book, t, "target", float(tgt), reason))
        return out


class Engine:
    def __init__(self, cfg: dict, panel: Panel, macro: pd.DataFrame | None = None,
                 ind: PanelIndicators | None = None):
        # ``ind``: indicators already computed for this same panel. They depend only on the bars, never
        # on the config, so ablations and walk-forward trials can share one set instead of rebuilding
        # every EMA and RSI for each variant.
        self.cfg = cfg
        self.panel = panel = cache_dataclass(panel)    # same data, cached column lookups
        self.bench = cfg["benchmark"]
        self.ind = ind if ind is not None else PanelIndicators(panel)
        self.books = {}
        for name, p in cfg["books"].items():
            if p.get("enabled", True):
                self.books[name] = BOOKS[name](p)
        # tickers that are not single stocks (ETFs, indexes) are excluded from stock universes
        etfs = set(cfg.get("non_stock_tickers", []))
        for p in cfg["books"].values():
            etfs |= set(p.get("universe", []) if isinstance(p.get("universe"), list) and p.get("etf_universe") else [])
            etfs |= set(p.get("defensive", []))
        etfs.add(self.bench)
        self.non_stock = etfs
        self.reg = rg.regime_series(panel.close[self.bench], cfg.get("regime", {}))
        sp = cfg.get("stress", {})
        self.flags = st.warning_flags(panel, self.bench, sp, macro)
        self.stress = st.stress_score(self.flags)
        self.crisis = st.crisis_mode(self.stress, sp.get("enter", 0.5), sp.get("exit", 0.25), sp.get("min_days", 10)) \
            if sp.get("enabled", True) else pd.Series(False, index=panel.dates)
        self.month_end = last_trading_day_of_month(panel.dates)

    # ------------------------------------------------------------------ run
    def run(self, start=None, end=None, live: bool = False) -> dict:
        """Replay the books over the panel.

        ``live=True`` also calls the books on the very last day and returns those orders as
        ``signals``: in a backtest there is no next open to fill them at, but for a live desk that
        final call *is* today's signal set.  Everything else is identical, so a live signal and a
        backtest signal come from the same lines.
        """
        cfg, P, I = self.cfg, self.panel, self.ind
        dates = P.dates
        start = pd.Timestamp(start or cfg.get("start") or dates[0])
        end = pd.Timestamp(end or cfg.get("end") or dates[-1])
        i0 = int(dates.searchsorted(start))
        i1 = int(dates.searchsorted(end, side="right")) - 1
        costs = cfg.get("costs", {})
        slip = costs.get("slippage_bps", 5.0) / 1e4
        comm = costs.get("commission_bps", 1.0) / 1e4
        borrow = costs.get("borrow_bps_per_year", 50.0) / 1e4 / 252
        risk = cfg.get("risk", {})
        alloc = cfg.get("allocation", {})
        eq0 = float(cfg.get("initial_equity", 100_000))

        ml = None
        tp = cfg["books"].get("trend_pullback", {})
        if "trend_pullback" in self.books and tp.get("ml", {}).get("enabled", False):
            from abg.tradebook.stocks.ml import MLFilter
            ml = MLFilter(P, I, self.bench, {**tp, **tp.get("ml", {})}, i0)

        normal_books = [b for b in self.books if b != "crisis_alpha"]
        hedge = Hedge(normal_books, eta=alloc.get("hedge_eta"), horizon=alloc.get("hedge_horizon_months", 60),
                      floor=alloc.get("hedge_floor", 0.10), loss_clip=alloc.get("hedge_clip", 0.10))
        fixed = alloc.get("fixed_weights") or {b: 1.0 / max(len(normal_books), 1) for b in normal_books}
        use_hedge = alloc.get("method", "hedge") == "hedge"

        positions: dict[str, dict[str, Position]] = {b: {} for b in self.books}
        book_cash = {b: 0.0 for b in self.books}
        book_prev_val = {b: 0.0 for b in self.books}
        month_ret = {b: 0.0 for b in self.books}
        pending: list[Order] = []
        trades, fills, rows, signals = [], [], [], []
        equity, peak = eq0, eq0
        paused_until = -1
        eq_hist: list[float] = []
        budgets = {b: 0.0 for b in self.books}

        def last_close(t, i):
            v = I.close[t].iat[i]
            if pd.isna(v):
                s = I.close[t].iloc[: i + 1].dropna()
                return float(s.iat[-1]) if len(s) else 0.0
            return float(v)

        def gross(i):
            return sum(abs(pos.shares) * last_close(t, i) for b in positions for t, pos in positions[b].items())

        def name_exposure(t, i):
            return sum(abs(positions[b][t].shares) for b in positions if t in positions[b]) * last_close(t, i)

        def close_position(b, t, i, price, reason):
            pos = positions[b].pop(t)
            fee = abs(pos.shares) * price * comm
            book_cash[b] += pos.shares * price - fee
            r = None
            if pos.init_stop is not None and pos.entry_price > pos.init_stop:
                r = (price - pos.entry_price) / (pos.entry_price - pos.init_stop) - 2 * comm * pos.entry_price / (pos.entry_price - pos.init_stop)
            trades.append({"book": b, "ticker": t, "entry_date": pos.entry_date.date(), "exit_date": dates[i].date(),
                           "entry": pos.entry_price, "exit": price, "shares": pos.shares, "days": pos.days_held,
                           "pnl": pos.shares * (price - pos.entry_price) - fee, "R": r, "reason": reason})

        for i in range(i0, i1 + 1):
            d = dates[i]
            # 1. fills at the open
            order_rank = {"sell": 0, "target": 1, "buy": 2}
            for o in sorted(pending, key=lambda o: (order_rank[o.kind], o.book, o.ticker)):
                op = P.open[o.ticker].iat[i]
                if pd.isna(op) or op <= 0:
                    continue
                book_pos = positions[o.book]
                if o.kind == "sell":
                    if o.ticker in book_pos:
                        close_position(o.book, o.ticker, i, op * (1 - slip), o.reason)
                elif o.kind == "target":
                    now = book_pos[o.ticker].shares if o.ticker in book_pos else 0.0
                    delta = o.shares - now
                    if delta == 0:
                        continue
                    if delta > 0 and o.shares > 0:       # adding long exposure: respect gross cap
                        room = risk.get("max_gross", 1.0) * equity - gross(i)
                        delta = max(0.0, min(delta, math.floor(room / op))) if room > 0 else 0.0
                        if delta == 0:
                            continue
                    px = op * (1 + slip * np.sign(delta))
                    fee = abs(delta) * px * comm
                    book_cash[o.book] -= delta * px + fee
                    fills.append({"date": d.date(), "book": o.book, "ticker": o.ticker, "delta": delta, "price": px, "reason": o.reason})
                    if o.ticker in book_pos:
                        pos = book_pos[o.ticker]
                        new = pos.shares + delta
                        if new == 0:
                            book_pos.pop(o.ticker)
                        else:
                            if np.sign(new) == np.sign(delta) and abs(new) > abs(pos.shares):
                                pos.entry_price = (pos.entry_price * pos.shares + px * delta) / new
                            pos.shares = new
                    else:
                        book_pos[o.ticker] = Position(delta, px, d)
                elif o.kind == "buy":
                    if o.ticker in book_pos:
                        continue
                    px = op * (1 + slip)
                    sh = o.shares
                    room = risk.get("max_gross", 1.0) * equity - gross(i)
                    name_room = risk.get("max_name_weight", 0.10) * equity - name_exposure(o.ticker, i)
                    sh = math.floor(min(sh, max(room, 0) / px, max(name_room, 0) / px))
                    if sh < 1:
                        continue
                    fee = sh * px * comm
                    book_cash[o.book] -= sh * px + fee
                    stop = px - o.stop_atr * o.atr
                    book_pos[o.ticker] = Position(sh, px, d, stop=stop, init_stop=stop, highest_close=px, atr_at_entry=o.atr)
            pending = []

            # 2. stops on swing positions
            for b, bp in positions.items():
                if not self.books[b].swing:
                    continue
                for t in list(bp):
                    pos = bp[t]
                    lo, op = P.low[t].iat[i], P.open[t].iat[i]
                    if pos.stop is not None and pd.notna(lo) and lo <= pos.stop:
                        px = (min(op, pos.stop) if pd.notna(op) else pos.stop) * (1 - slip)
                        close_position(b, t, i, px, "stop")

            # 3. mark to market
            book_val = {}
            for b, bp in positions.items():
                mv = 0.0
                for t, pos in bp.items():
                    c = last_close(t, i)
                    mv += pos.shares * c
                    if pos.shares < 0:
                        book_cash[b] -= abs(pos.shares) * c * borrow
                    pos.days_held += 0 if pos.entry_date == d else 1
                    pos.highest_close = max(pos.highest_close, c)
                book_val[b] = book_cash[b] + mv
            pnl = {b: book_val[b] - book_prev_val[b] for b in self.books}
            for b in self.books:
                if budgets[b] > 0:
                    month_ret[b] += pnl[b] / budgets[b]
            book_prev_val = book_val
            equity = eq0 + sum(book_val.values())
            eq_hist.append(equity)
            peak = max(peak, equity)
            dd = equity / peak - 1

            # 4. kill switch
            kill = False
            if i > paused_until and dd <= -risk.get("dd_kill", 0.20):
                kill = True
                paused_until = i + risk.get("dd_pause_days", 20)
                for b, bp in positions.items():
                    for t, pos in bp.items():
                        if self.books[b].swing:
                            pending.append(Order(b, t, "sell", pos.shares, "kill_switch"))
                        else:
                            pending.append(Order(b, t, "target", 0.0, "kill_switch"))
            if i == paused_until:
                peak = equity

            # 5. month-end Hedge update
            if self.month_end[i] and use_hedge:
                hedge.update({b: month_ret[b] for b in normal_books}, tag=str(d.date()))
            if self.month_end[i]:
                month_ret = {b: 0.0 for b in self.books}

            # 6. budgets
            w = hedge.weights() if use_hedge else fixed
            scale = 1.0
            vt = alloc.get("vol_target")
            if vt and len(eq_hist) > alloc.get("vol_window", 20):
                r = pd.Series(eq_hist[-alloc.get("vol_window", 20) - 1:]).pct_change().dropna()
                rv = r.std() * math.sqrt(252)
                if rv > 0:
                    scale = min(max(vt / rv, alloc.get("vol_scale_min", 0.5)), alloc.get("vol_scale_max", 1.0))
            crisis = bool(self.crisis.iat[i])
            cb = self.cfg["books"].get("crisis_alpha", {}).get("budget", 0.7) if "crisis_alpha" in self.books else 0.0
            for b in self.books:
                if b == "crisis_alpha":
                    budgets[b] = equity * cb if crisis else 0.0
                else:
                    budgets[b] = equity * w.get(b, 0.0) * scale * ((1.0 - cb) if crisis else 1.0)

            regime = self.reg["regime"].iat[i]
            if crisis:
                regime_label = "crisis"
            else:
                regime_label = regime
            rows.append({"date": d, "equity": equity, "drawdown": dd, "regime": regime_label, "stress": float(self.stress.iat[i]),
                         "crisis": crisis, "gross": gross(i) / equity if equity else 0.0, "vol_scale": scale,
                         **{f"val_{b}": book_val[b] for b in self.books}, **{f"w_{b}": w.get(b, 0.0) for b in normal_books}})

            # 7. books act on the close
            if kill or i <= paused_until or (i == i1 and not live):
                continue
            for b, book in self.books.items():
                ctx = Ctx(self, b, i, d, regime_label, crisis, budgets[b], equity, bool(self.month_end[i]),
                          positions[b], I, self.bench, ml)
                out = book.on_close(ctx)
                pending.extend(out)
                if i == i1:
                    signals.extend(out)

        eq = pd.DataFrame(rows).set_index("date")
        bench = P.close[self.bench].loc[eq.index]
        eq["benchmark"] = bench / bench.iloc[0] * eq0
        return {"equity": eq, "trades": pd.DataFrame(trades), "fills": pd.DataFrame(fills),
                "hedge": pd.DataFrame(hedge.history), "flags": self.flags.loc[eq.index],
                # live state: what the system holds and believes as of the last replayed day
                "signals": signals, "positions": positions, "budgets": dict(budgets),
                "weights": hedge.weights() if use_hedge else dict(fixed),
                "last_i": i1, "last_date": dates[i1], "equity_now": equity, "ml": ml}
