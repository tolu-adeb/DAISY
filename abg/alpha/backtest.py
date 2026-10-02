"""Backtests for the MNQ strategy - the same ``AlphaStrategy`` code the live bot runs.

* ``run_backtest``   bar-by-bar over intraday bars (1m or 5m, ET), session by session.  Fills: market
                     entries at the signal bar's close plus slippage; stops fill at the stop minus
                     slippage; targets fill at the target.  If one bar touches both the stop and a target,
                     the stop is assumed first.  Costs: commission per micro round turn.
* ``walk_forward``   picks parameters on the first part of the sample and reports them on the rest
                     (the only number worth trusting).
* ``random_walk_check``  runs the strategy on bars with no structure at all.  A sound backtester must
                     show no edge there (about minus the costs); if it shows profit, the simulation leaks.
* ``daily_layer``    what a daily CSV can test: the day-bias call against the next close.

Points are per contract, blended across the scale-out (half at Target 1, half on the runner).
"""
from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from .context import build_context, contexts_from_intraday
from .data import NY, Bar, bar_minutes, is_rth, iter_bars, split_sessions
from .learn import AdaptiveBook
from .strategy import AlphaParams, AlphaStrategy

MNQ_POINT = 2.0


@dataclass
class BTResult:
    trades: list[dict]
    days: list[dict]
    stats: dict
    params: dict
    events: list[dict] = field(default_factory=list)
    learner: dict | None = None

    def to_dict(self) -> dict:
        return {"stats": self.stats, "params": self.params, "trades": self.trades, "days": self.days,
                "learner": self.learner}


def _warmup(sessions: dict, d: date, minutes: int) -> list[Bar]:
    prior = [k for k in sessions if k < d]
    if not prior:
        return []
    g = sessions[max(prior)]
    r = g[[is_rth(t) for t in g.index]]
    return list(iter_bars(r, minutes))


def run_backtest(bars: pd.DataFrame, params: AlphaParams | None = None, daily: pd.DataFrame | None = None,
                 learner: AdaptiveBook | None = None, events_for=None, commission_rt: float = 1.24,
                 point_value: float = MNQ_POINT, collect_events: bool = False,
                 days: list[date] | None = None) -> BTResult:
    params = params or AlphaParams()
    strat = AlphaStrategy(params, learner)
    minutes = bar_minutes(bars)
    sessions = split_sessions(bars)
    ctxs = contexts_from_intraday(bars, daily, events_for, params)
    trades, day_rows, events = [], [], []
    for d, (ctx, rth) in sorted(ctxs.items()):
        if days is not None and d not in days:
            continue
        if len(rth) < 30 // max(1, minutes):            # half days / data gaps
            continue
        ev = strat.start_day(ctx, _warmup(sessions, d, minutes))
        for b in iter_bars(rth, minutes):
            ev += strat.on_bar(b)
        ev += strat.end_day()
        summ = ev[-1]
        day_rows.append({"date": str(d), "n": summ["n"], "net_pts": summ["net_pts"], "net_r": summ["net_r"],
                         "bias": ctx.bias_label, "fomc": ctx.fomc, "done": summ["done_reason"]})
        for t in summ["trades"]:
            t["date"] = str(d)
            trades.append(t)
        if collect_events:
            events += [{**e, "date": str(d)} for e in ev]
    stats = summarize(trades, day_rows, commission_rt, point_value)
    return BTResult(trades, day_rows, stats, params.to_dict(), events, learner.to_dict() if learner else None)


def _maxdd(series: list[float]) -> float:
    peak, dd, cum = 0.0, 0.0, 0.0
    for x in series:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def summarize(trades: list[dict], days: list[dict] | None = None, commission_rt: float = 1.24,
              point_value: float = MNQ_POINT) -> dict:
    if not trades:
        return {"trades": 0, "days": len(days or []), "note": "no trades"}
    pts = np.array([t["pts"] for t in trades], dtype=float)
    rs = np.array([t["r"] for t in trades], dtype=float)
    wins, losses = pts[pts > 0.5], pts[pts < -0.5]
    usd = pts * point_value - commission_rt
    by_day: dict[str, float] = {}
    for t, u in zip(trades, usd):
        by_day[t["date"]] = by_day.get(t["date"], 0.0) + u
    all_days = [d["date"] for d in days] if days else sorted(by_day)
    daily = np.array([by_day.get(d, 0.0) for d in all_days])
    out = {
        "trades": int(len(pts)), "days": len(all_days), "days_traded": len(by_day),
        "wins": int(len(wins)), "losses": int(len(losses)), "breakeven": int(len(pts) - len(wins) - len(losses)),
        "win_rate": float(len(wins) / len(pts)),
        "net_pts": round(float(pts.sum()), 2), "avg_pts": round(float(pts.mean()), 2),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else None,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else None,
        "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if len(losses) and losses.sum() < 0 else None,
        "avg_r": round(float(rs.mean()), 3), "net_r": round(float(rs.sum()), 2),
        "max_dd_pts": round(_maxdd([t["pts"] for t in trades]), 2),
        "net_usd_per_micro": round(float(usd.sum()), 2), "max_dd_usd_per_micro": round(_maxdd(list(daily)), 2),
        "sharpe_daily": round(float(daily.mean() / daily.std() * math.sqrt(252)), 2) if daily.std() > 0 else None,
        "worst_day_usd": round(float(daily.min()), 2), "best_day_usd": round(float(daily.max()), 2),
        "commission_rt": commission_rt,
    }
    out["by_setup"] = _group(trades, lambda t: t["setup"])
    out["by_regime"] = _group(trades, lambda t: t["regime"])
    out["by_side"] = _group(trades, lambda t: t["side_label"])
    out["by_exit"] = _group(trades, lambda t: t["exit_reason"])
    out["by_time"] = _group(trades, lambda t: "before 10:00" if t["opened"][11:16] < "10:00" else "10:00 or later")
    return out


def _group(trades, keyf) -> dict:
    g: dict[str, list[float]] = {}
    for t in trades:
        g.setdefault(keyf(t), []).append(t["pts"])
    return {k: {"n": len(v), "win_rate": round(sum(1 for x in v if x > 0.5) / len(v), 3),
                "net_pts": round(sum(v), 2), "avg_pts": round(sum(v) / len(v), 2)} for k, v in sorted(g.items())}


# --------------------------------------------------------------------------- walk-forward
DEFAULT_GRID = {
    "t1_r": [0.5, 0.7, 1.0],
    "orx_k": [0.6, 0.8, 1.0],
    "orx_final_r": [1.5, 2.0, 3.0],
    "orx_lookback": [3, 5],
}


def _objective(st: dict, min_trades: int, min_win_rate: float = 0.0) -> float:
    n = st.get("trades", 0)
    if n < min_trades or (st.get("win_rate") or 0) < min_win_rate:
        return -1e9
    return st["avg_r"] * math.sqrt(n) - 0.002 * st.get("max_dd_pts", 0)


def worth_adopting(test: dict, default_test: dict) -> bool:
    """Only replace settings when the fitted ones are profitable out of sample (after costs) on a
    meaningful number of trades AND beat the defaults there."""
    return (test.get("trades", 0) >= 15 and (test.get("avg_r") or 0) > 0.05 and (test.get("net_usd_per_micro") or 0) > 0
            and (test.get("avg_r") or 0) > (default_test.get("avg_r") or -9))


def _subset(res: BTResult, days: list[date], commission_rt: float, point_value: float) -> dict:
    keep = {str(d) for d in days}
    return summarize([t for t in res.trades if t["date"] in keep], [d for d in res.days if d["date"] in keep],
                     commission_rt, point_value)


def walk_forward(bars: pd.DataFrame, base: AlphaParams | None = None, grid: dict | None = None,
                 train_frac: float = 0.67, min_trades: int = 12, adaptive: bool = True, min_win_rate: float = 0.65,
                 commission_rt: float = 1.24, point_value: float = MNQ_POINT, **kw) -> dict:
    """Grid-search on the first ``train_frac`` of sessions, report the winner on the rest.  With
    ``adaptive`` each run gets a fresh learner that learns online (no peeking ahead)."""
    base = base or AlphaParams()
    grid = grid or DEFAULT_GRID
    all_days = sorted(d for d, g in split_sessions(bars).items() if any(is_rth(t) for t in g.index))
    if len(all_days) < 10:
        raise ValueError(f"need at least 10 sessions for walk-forward (have {len(all_days)})")
    cut = int(len(all_days) * train_frac)
    train, test = all_days[:cut], all_days[cut:]
    rows = []
    keys = list(grid)
    for combo in itertools.product(*[grid[k] for k in keys]):
        p = AlphaParams.from_dict({**base.to_dict(), **dict(zip(keys, combo))})
        st = run_backtest(bars, p, days=train, learner=AdaptiveBook() if adaptive else None,
                          commission_rt=commission_rt, point_value=point_value, **kw).stats
        rows.append((_objective(st, min_trades, min_win_rate), dict(zip(keys, combo)), st))
    rows.sort(key=lambda r: r[0], reverse=True)
    best = AlphaParams.from_dict({**base.to_dict(), **rows[0][1]})
    full = run_backtest(bars, best, learner=AdaptiveBook() if adaptive else None, commission_rt=commission_rt,
                        point_value=point_value, **kw)
    base_full = run_backtest(bars, base, learner=AdaptiveBook() if adaptive else None, commission_rt=commission_rt,
                             point_value=point_value, **kw)
    return {"train_days": [str(train[0]), str(train[-1]), len(train)], "test_days": [str(test[0]), str(test[-1]), len(test)],
            "best": rows[0][1], "train_stats": rows[0][2],
            "test_stats": _subset(full, test, commission_rt, point_value),
            "default_test_stats": _subset(base_full, test, commission_rt, point_value), "params": best.to_dict(),
            "learner": full.learner,
            "leaderboard": [{"params": r[1], "train_avg_r": r[2].get("avg_r"), "train_trades": r[2].get("trades"),
                             "train_net_pts": r[2].get("net_pts")} for r in rows[:8]]}


# --------------------------------------------------------------------------- integrity check
def random_walk_bars(n_days: int = 60, seed: int = 7, start_px: float = 29000.0, daily_vol: float = 0.012,
                     minutes: int = 1) -> pd.DataFrame:
    """Session-shaped bars with NO exploitable structure (Gaussian steps, constant volatility)."""
    rng = random.Random(seed)
    px = start_px
    rows = []
    d = date(2026, 1, 5)
    sig_min = start_px * daily_vol / math.sqrt(390)
    while len({r[0].date() for r in rows if r[0].time() >= time(9, 30)}) < n_days:
        if d.weekday() < 5:
            t = datetime(d.year, d.month, d.day, 7, 0, tzinfo=NY)
            end = datetime(d.year, d.month, d.day, 16, 0, tzinfo=NY)
            while t < end:
                o = px
                hi = lo = o
                for _ in range(4):
                    px += rng.gauss(0, sig_min * math.sqrt(minutes) / 2) * (0.5 if t.time() < time(9, 30) else 1.0)
                    hi, lo = max(hi, px), min(lo, px)
                rows.append((t, round(o * 4) / 4, round(hi * 4) / 4, round(lo * 4) / 4, round(px * 4) / 4,
                             rng.randint(200, 2000)))
                t += timedelta(minutes=minutes)
        d += timedelta(days=1)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")
    df.index = pd.DatetimeIndex(df.index)
    return df


def random_walk_check(n_days: int = 120, seeds=(1, 2, 3), **kw) -> dict:
    res = []
    for s in seeds:
        st = run_backtest(random_walk_bars(n_days, seed=s), **kw).stats
        res.append(st)
    n = sum(r.get("trades", 0) for r in res)
    avg_r = sum(r.get("avg_r", 0) * r.get("trades", 0) for r in res) / n if n else 0.0
    return {"runs": len(res), "trades": n, "avg_r": round(avg_r, 3),
            "net_usd_per_micro": round(sum(r.get("net_usd_per_micro", 0) for r in res), 2),
            "verdict": ("OK - no edge on structureless data" if abs(avg_r) < 0.15 else
                        "WARNING - edge on random data means the simulation leaks")}


# --------------------------------------------------------------------------- daily layer
def daily_layer(daily: pd.DataFrame) -> dict:
    """Trend call (prior closes vs their 10-day mean, made before the day) and "same as yesterday" vs the
    day's close-to-close direction.  Overnight data isn't in a daily CSV, so this is the trend half of the bias."""
    rows = []
    for i in range(6, len(daily)):
        d = daily.index[i]
        ctx = build_context(d.date(), daily.iloc[:i])
        ret = daily["close"].iloc[i] - daily["close"].iloc[i - 1]
        prev = daily["close"].iloc[i - 1] - daily["close"].iloc[i - 2]
        rows.append({"date": str(d.date()), "bias": ctx.trend_d, "momo": int(np.sign(prev)), "ret": float(ret), "range": float(daily["high"].iloc[i] - daily["low"].iloc[i]),
                     "atr": ctx.atr_d})
    df = pd.DataFrame(rows)
    called = df[df.bias != 0]
    hit = float((np.sign(called.ret) == called.bias).mean()) if len(called) else None
    up = float((df.ret > 0).mean()) if len(df) else None
    momo = float((np.sign(df.ret) == df.momo).mean()) if len(df) else None
    return {"days": len(df), "bias_calls": int(len(called)), "bias_hit_rate": round(hit, 3) if hit is not None else None,
            "follow_yesterday_hit_rate": round(momo, 3) if momo is not None else None,
            "base_rate_up": round(up, 3) if up is not None else None,
            "avg_range_pts": round(float(df["range"].mean()), 1) if len(df) else None,
            "avg_atr_pts": round(float(df["atr"].dropna().mean()), 1) if len(df) else None, "rows": rows}


# --------------------------------------------------------------------------- bootstrap (repeated sampling)
def day_pnl(res: BTResult, commission_rt: float = 1.24, point_value: float = MNQ_POINT) -> tuple[np.ndarray, np.ndarray]:
    """Per-session net $ (per micro) and that session's worst single trade, in session order (0 on no-trade days)."""
    days = [d["date"] for d in res.days]
    net = {d: 0.0 for d in days}
    worst = {d: 0.0 for d in days}
    for t in res.trades:
        u = t["pts"] * point_value - commission_rt
        net[t["date"]] = net.get(t["date"], 0.0) + u
        worst[t["date"]] = min(worst.get(t["date"], 0.0), u)
    return np.array([net[d] for d in days]), np.array([worst[d] for d in days])


def bootstrap(res: BTResult, samples: int = 2000, length: int = 21, block: int = 5, seed: int = 42,
              big_loss_usd: float = 150.0, dd_limit_usd: float = 300.0, commission_rt: float = 1.24,
              point_value: float = MNQ_POINT) -> dict:
    """Repeated sampling of whole sessions (with replacement) into ``samples`` synthetic ~months of ``length``
    sessions.  ``iid`` draws sessions independently; ``block`` draws runs of ``block`` consecutive sessions so
    streaks (the follow/fade regimes) survive.  Reports the distribution, not just the one path we lived."""
    x, w = day_pnl(res, commission_rt, point_value)
    if len(x) == 0:
        return {"note": "no sessions"}
    out = {"sessions": int(len(x)), "samples": samples, "length": length}
    for mode in ("iid", "block"):
        rng = np.random.default_rng(seed)
        nets, dds, worst = np.empty(samples), np.empty(samples), np.empty(samples)
        for k in range(samples):
            if mode == "iid":
                idx = rng.integers(0, len(x), length)
            else:
                starts = rng.integers(0, len(x), -(-length // block))
                idx = np.concatenate([(s + np.arange(block)) % len(x) for s in starts])[:length]
            cum = np.cumsum(x[idx])
            peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
            nets[k], dds[k], worst[k] = cum[-1], float(np.max(peak - cum)), float(w[idx].min())
        pct = lambda a, q: round(float(np.percentile(a, q)), 2)  # noqa: E731
        out[mode] = {"mean_net": round(float(nets.mean()), 2), "median_net": pct(nets, 50), "p05_net": pct(nets, 5),
                     "p95_net": pct(nets, 95), "prob_losing": round(float((nets < 0).mean()), 4),
                     "median_max_dd": pct(dds, 50), "p95_max_dd": pct(dds, 95),
                     "prob_dd_over_limit": round(float((dds > dd_limit_usd).mean()), 4),
                     "median_worst_trade": pct(worst, 50), "p05_worst_trade": pct(worst, 5),
                     "prob_trade_loss_over": round(float((worst < -big_loss_usd).mean()), 4),
                     "big_loss_usd": big_loss_usd, "dd_limit_usd": dd_limit_usd}
    return out
