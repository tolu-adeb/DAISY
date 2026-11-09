"""Futures backtest engine.

For every session:
  1. SHADOW: every enabled setup is simulated independently on the session (full information).
  2. DAY TYPE: trend / range / neutral from opening features at the end of the opening range.
  3. CRISIS: daily stress flags on the instrument (and VIX if supplied) -> crisis mode with hysteresis.
  4. LIVE: shadow trades are filtered and sized:
        allowed by day type and crisis rules, not in an economic-event blackout, daily-bias filter,
        size = Hedge multiplier x day-type multiplier x crisis multiplier,
        account rules applied in time order: max trades per day, and a daily loss stop that flattens
        everything (at that minute's close) once combined open + closed P&L reaches -daily_stop_R.
  5. HEDGE: setup weights are updated with each setup's shadow net R for the session.
All results are in R (1R = one setup's initial stop distance); dollars = R x risk_usd.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from abg.tradebook.common import indicators as ind
from abg.tradebook.common.hedge import Hedge
from abg.tradebook.futures.data import Session, load_econ_events
from abg.tradebook.futures.setups import FAMILY, SETUPS, _minute_index, opening_range
from abg.tradebook.futures.sim import Costs
from abg.tradebook.stocks.stress import crisis_mode, stress_score


def day_type(s: Session, p: dict, or_hist: list[float] | None = None) -> tuple[str, dict]:
    """trend / range / neutral, known at the end of the opening range.
    or_pct = percentile of today's OR width among the previous `or_lookback` sessions (scale-free)."""
    rth = s.rth
    orh, orl, n_or = opening_range(rth, p.get("or_minutes", 15))
    o0 = float(rth["open"].iat[0])
    c_or = float(rth["close"].iat[n_or - 1])
    adr = s.adr20
    if not adr or adr != adr:
        return "neutral", {}
    gap = (o0 - s.prev_close) / adr
    width = (orh - orl) / adr
    filled = (orl <= s.prev_close <= orh)
    first_dir = np.sign(c_or - o0)
    inside_prior = s.prev_low <= o0 <= s.prev_high
    hist = (or_hist or [])[-p.get("or_lookback", 20):]
    or_pct = float(np.mean(np.asarray(hist) < (orh - orl))) if len(hist) >= 10 else 0.5
    f = {"gap_adr": gap, "or_width_adr": width, "or_pct": or_pct, "gap_filled": filled, "first_dir": first_dir,
         "prev_er": s.prev_er, "open_inside_prior": inside_prior}
    trend = or_pct <= p.get("narrow_or_pct", 0.30) or (abs(gap) >= p.get("gap_go", 0.35) and not filled and first_dir == np.sign(gap))
    rng = or_pct >= p.get("wide_or_pct", 0.70) or (abs(gap) <= p.get("small_gap", 0.10) and inside_prior
                                                    and (s.prev_er == s.prev_er and s.prev_er < p.get("chop_er", 0.25)))
    if trend and not rng:
        return "trend", f
    if rng and not trend:
        return "range", f
    return "neutral", f


def daily_crisis(daily: pd.DataFrame, p: dict, vix: pd.Series | None) -> pd.DataFrame:
    c = daily["close"]
    f = {}
    sma = ind.sma(c, 200)
    f["trend_break"] = (c < sma).astype(float).where(sma.notna())
    vp = ind.pct_rank(ind.realized_vol(c, 20), 252)
    f["vol_spike"] = (vp >= p.get("vol_pct", 0.80)).astype(float).where(vp.notna())
    hi = c.rolling(252, min_periods=60).max()
    f["drawdown_10"] = (c / hi - 1 <= -p.get("drawdown", 0.10)).astype(float).where(hi.notna())
    if vix is not None:
        v = vix.reindex(c.index).ffill()
        f["vix_high"] = (v >= p.get("vix_level", 25)).astype(float).where(v.notna())
    flags = pd.DataFrame(f, index=c.index)
    sc = stress_score(flags)
    mode = crisis_mode(sc, p.get("enter", 0.5), p.get("exit", 0.25), p.get("min_days", 10)) if p.get("enabled", True) \
        else pd.Series(False, index=c.index)
    out = flags.copy()
    out["stress"], out["crisis"] = sc, mode
    # decisions for session d must use flags known at the prior close
    return out.shift(1).fillna({"crisis": False, "stress": 0.0})


class FuturesEngine:
    def __init__(self, cfg: dict, sessions: list[Session], daily: pd.DataFrame, vix: pd.Series | None = None):
        self.cfg = cfg
        self.sessions = sessions
        ins = cfg["instrument"]
        cs = cfg.get("costs", {})
        self.costs = Costs(tick=ins["tick"], point_value=ins["point_value"], commission_usd=cs.get("commission_usd", 1.0),
                           slip_ticks=cs.get("slip_ticks", 1.0), through_ticks=cs.get("through_ticks", 1.0),
                           min_stop_pts=cfg["risk"].get("min_stop_pts", 10.0), max_stop_pts=cfg["risk"].get("max_stop_pts", 60.0))
        self.setups = {k: v for k, v in cfg["setups"].items() if v.get("enabled", True)}
        self.crisis = daily_crisis(daily, cfg.get("stress", {}), vix)
        self.events = load_econ_events(cfg.get("econ_events_file"))

    def _blackout(self, rth_index: pd.DatetimeIndex) -> np.ndarray:
        bl = np.zeros(len(rth_index), dtype=bool)
        if self.events.empty:
            return bl
        d = rth_index[0].normalize()
        ev = self.events[(self.events["datetime"] >= d) & (self.events["datetime"] < d + pd.Timedelta(days=1))]
        for _, e in ev.iterrows():
            a = e["datetime"] - pd.Timedelta(minutes=int(e["minutes_before"]))
            b = e["datetime"] + pd.Timedelta(minutes=int(e["minutes_after"]))
            bl |= (rth_index >= a) & (rth_index <= b)
        return bl

    def run(self, start=None, end=None) -> dict:
        cfg = self.cfg
        risk, ad = cfg["risk"], cfg.get("adaptive", {})
        names = list(self.setups)
        hedge = Hedge(names, eta=ad.get("hedge_eta"), horizon=ad.get("hedge_horizon", 250),
                      floor=ad.get("hedge_floor", 0.05), loss_clip=ad.get("hedge_clip", 2.0))
        dt_p = cfg.get("day_type", {})
        mult_tbl = dt_p.get("multipliers", {"trend": {"trend": 1.0, "fade": 0.0}, "range": {"trend": 0.0, "fade": 1.0},
                                            "neutral": {"trend": 0.5, "fade": 0.5}})
        bias_p = cfg.get("bias", {})
        crisis_p = cfg.get("crisis_mode", {})
        start = pd.Timestamp(start) if start else None
        end = pd.Timestamp(end) if end else None
        all_trades, days, or_hist = [], [], []
        for s in self.sessions:
            if (start is not None and s.date < start) or (end is not None and s.date > end):
                continue
            rth = s.rth
            flat_i = _minute_index(rth, risk.get("flat_time", "15:55"))
            # 1. shadow
            shadow = []
            for name, p in self.setups.items():
                for t in SETUPS[name](s, p, self.costs, flat_i):
                    t["setup"] = name
                    shadow.append(t)
            shadow_R = {n: 0.0 for n in names}
            for t in shadow:
                shadow_R[t["setup"]] += t["net_R"]
            # 2-3. context
            if dt_p.get("enabled", True):
                dtype, feats = day_type(s, dt_p, or_hist)
            else:
                dtype, feats = "neutral", {}
            mt = mult_tbl if dt_p.get("enabled", True) else {"neutral": {"trend": 1.0, "fade": 1.0}}
            cr = bool(self.crisis["crisis"].get(s.date, False)) if s.date in self.crisis.index else False
            w = hedge.weights()
            blk = self._blackout(rth.index)
            # 4. live selection
            cands = []
            for t in shadow:
                fam = FAMILY[t["setup"]]
                m_day = mt.get(dtype, mt.get("neutral"))[fam]
                # day type is only known at the end of the opening range; earlier entries use neutral
                if t["entry_i"] < _minute_index(rth, "09:30") + dt_p.get("or_minutes", 15):
                    m_day = mt.get("neutral")[fam]
                m_cr = 1.0
                if cr:
                    m_cr = crisis_p.get("size_mult", 0.5)
                    if fam in crisis_p.get("disable_families", ["fade"]):
                        m_cr = 0.0
                if ad.get("hedge_enabled", True):
                    rel = w[t["setup"]] * len(names)
                    if rel < ad.get("min_rel_weight", 0.5):
                        m_h = 0.0
                    else:
                        m_h = min(max(rel, ad.get("mult_min", 0.5)), ad.get("mult_max", 1.5))
                else:
                    m_h = 1.0
                m_bias = 1.0
                if bias_p.get("enabled", True) and t["setup"] in bias_p.get("apply_to", ["orb", "vwap_pullback"]):
                    if s.bias != 0 and np.sign(t["side"]) != np.sign(s.bias):
                        m_bias = 0.0
                if blk[min(t["entry_i"], len(blk) - 1)] or blk[max(t["entry_i"] - 1, 0)]:
                    continue
                mult = m_day * m_cr * m_h * m_bias
                if mult <= 0:
                    continue
                cands.append({**t, "mult": mult})
            cands.sort(key=lambda t: (t["entry_i"], t["setup"]))
            cands = cands[: risk.get("max_trades_day", 4)]
            # daily loss stop on the combined minute-by-minute P&L
            n = len(rth)
            pnl = np.zeros(n)
            for t in cands:
                path = t["path_R"] * t["mult"]
                a = t["entry_i"]
                b = a + len(path)
                pnl[a:b] += path
                pnl[b:] += t["net_R"] * t["mult"]
            stop_R = -abs(risk.get("daily_stop_R", 2.0))
            hit = np.nonzero(pnl <= stop_R)[0]
            cut = int(hit[0]) if len(hit) else None
            live = []
            for t in cands:
                lt = {k: v for k, v in t.items() if k != "path_R"}
                if cut is not None and t["entry_i"] > cut:
                    continue
                lt["live_R"] = t["net_R"] * t["mult"]
                if cut is not None and t["entry_i"] <= cut < t["entry_i"] + len(t["path_R"]):
                    lt["live_R"] = float(t["path_R"][cut - t["entry_i"]]) * t["mult"]
                    lt["exit_reason"] = "daily_stop"
                live.append(lt)
            day_R = float(sum(t["live_R"] for t in live))
            min_R = float(min(0.0, pnl[: (cut + 1) if cut is not None else n].min())) if n else 0.0
            for t in shadow:
                rec = {k: v for k, v in t.items() if k != "path_R"}
                rec.update({"date": s.date.date(), "entry_time": rth.index[t["entry_i"]].strftime("%H:%M"),
                            "day_type": dtype, "crisis": cr, "family": FAMILY[t["setup"]]})
                all_trades.append(rec)
            for t in live:
                all_trades.append({**{k: v for k, v in t.items()}, "date": s.date.date(),
                                   "entry_time": rth.index[t["entry_i"]].strftime("%H:%M"), "day_type": dtype,
                                   "crisis": cr, "family": FAMILY[t["setup"]], "live": True})
            days.append({"date": s.date, "day_type": dtype, "crisis": cr, "net_R": day_R, "min_R": min_R,
                         "trades": len(live), "daily_stop_hit": cut is not None, "bias": s.bias,
                         **{f"shadow_{k}": v for k, v in shadow_R.items()}, **{f"w_{k}": v for k, v in w.items()},
                         **{f"f_{k}": v for k, v in feats.items()}})
            orh_, orl_, _ = opening_range(rth, dt_p.get("or_minutes", 15))
            or_hist.append(orh_ - orl_)
            # 5. learn
            hedge.update(shadow_R, tag=str(s.date.date()))
        tr = pd.DataFrame(all_trades)
        if not tr.empty:
            tr["live"] = tr.get("live", False)
            tr["live"] = tr["live"].fillna(False).astype(bool)
        d = pd.DataFrame(days).set_index("date") if days else pd.DataFrame()
        return {"trades": tr, "daily": d}
