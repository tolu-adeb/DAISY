"""Forward projections from backtest results (Monte Carlo).

The projection resamples *days* from the backtest's daily results with a
stationary block bootstrap (random block lengths, mean `block`), which keeps
streaks and volatility clustering that a trade-by-trade shuffle would destroy.

Two adjustments make projections more honest than the raw backtest:
  * haircut: the mean daily result is reduced by this fraction before sampling
    (0.5 = assume live keeps only half the backtest edge). Backtest edges
    shrink out of sample; the reference bot studied in Cervo kept about a third.
  * the projection is run both with and without the haircut, so the gap shows
    how much of the result depends on the edge being real.

Outputs: percentile cones of equity over the horizon, the distribution of max
drawdown, probabilities of loss, and (futures) funded-account evaluation odds.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd


def _block_bootstrap_paths(x: np.ndarray, horizon: int, n_sims: int, block: float, rng) -> np.ndarray:
    """Stationary bootstrap (Politis & Romano): returns an (n_sims, horizon) array of resampled values."""
    n = len(x)
    out = np.empty((n_sims, horizon))
    p = 1.0 / max(block, 1.0)
    idx = rng.integers(0, n, size=n_sims)
    for t in range(horizon):
        out[:, t] = x[idx]
        restart = rng.random(n_sims) < p
        idx = np.where(restart, rng.integers(0, n, size=n_sims), (idx + 1) % n)
    return out


def _apply_haircut(x: np.ndarray, haircut: float) -> np.ndarray:
    """Remove `haircut` of a POSITIVE mean edge; a losing strategy is not made to look better."""
    m = x.mean()
    if haircut <= 0 or m <= 0:
        return x
    return x - haircut * m


def project_returns(daily_returns: pd.Series, horizons=(63, 126, 252), n_sims: int = 10000,
                    block: float = 5.0, haircut: float = 0.5, start_equity: float = 100_000.0,
                    seed: int = 11) -> dict:
    """Projection for a percentage-return strategy (stocks). daily_returns are simple returns."""
    x = np.asarray(daily_returns.dropna(), dtype=float)
    if len(x) < 60:
        raise ValueError("need at least 60 daily returns to project")
    rng = np.random.default_rng(seed)
    H = max(horizons)
    res = {"inputs": {"days_sampled": int(len(x)), "n_sims": n_sims, "block": block,
                      "start_equity": start_equity, "haircut": haircut}}
    cones = {}
    for label, h in (("raw", 0.0), ("haircut", haircut)):
        xs = _apply_haircut(x, h)
        paths = _block_bootstrap_paths(xs, H, n_sims, block, rng)
        eq = start_equity * np.cumprod(1.0 + paths, axis=1)
        peak = np.maximum.accumulate(np.concatenate([np.full((n_sims, 1), start_equity), eq], axis=1), axis=1)[:, 1:]
        dd = eq / peak - 1.0
        block_res = {}
        for hz in horizons:
            e = eq[:, hz - 1]
            mdd = dd[:, :hz].min(axis=1)
            block_res[f"{hz}d"] = {
                "equity_p05": float(np.percentile(e, 5)), "equity_p25": float(np.percentile(e, 25)),
                "equity_p50": float(np.percentile(e, 50)), "equity_p75": float(np.percentile(e, 75)),
                "equity_p95": float(np.percentile(e, 95)),
                "return_p50": float(np.percentile(e, 50) / start_equity - 1),
                "prob_loss": float((e < start_equity).mean()),
                "max_dd_p50": float(np.percentile(mdd, 50)), "max_dd_p95": float(np.percentile(mdd, 5)),
                "prob_dd_worse_10pct": float((mdd < -0.10).mean()),
                "prob_dd_worse_20pct": float((mdd < -0.20).mean()),
            }
        res[label] = block_res
        cones[label] = pd.DataFrame({f"p{q}": np.percentile(eq, q, axis=0) for q in (5, 25, 50, 75, 95)},
                                    index=pd.RangeIndex(1, H + 1, name="day"))
    res["_cones"] = cones
    return res


def project_r(daily_r: pd.Series, risk_usd: float, horizons=(63, 126, 252), n_sims: int = 10000,
              block: float = 5.0, haircut: float = 0.5, seed: int = 11) -> dict:
    """Projection for an R-denominated strategy (futures). daily_r = net R per session, zeros included."""
    x = np.asarray(daily_r.dropna(), dtype=float)
    if len(x) < 60:
        raise ValueError("need at least 60 sessions to project")
    rng = np.random.default_rng(seed)
    H = max(horizons)
    res = {"inputs": {"sessions_sampled": int(len(x)), "n_sims": n_sims, "block": block,
                      "risk_usd_per_R": risk_usd, "haircut": haircut}}
    cones = {}
    for label, h in (("raw", 0.0), ("haircut", haircut)):
        xs = _apply_haircut(x, h)
        paths = _block_bootstrap_paths(xs, H, n_sims, block, rng)
        cum = np.cumsum(paths, axis=1)
        peak = np.maximum.accumulate(np.concatenate([np.zeros((n_sims, 1)), cum], axis=1), axis=1)[:, 1:]
        dd = cum - peak
        block_res = {}
        for hz in horizons:
            c = cum[:, hz - 1]
            mdd = dd[:, :hz].min(axis=1)
            block_res[f"{hz}d"] = {
                "R_p05": float(np.percentile(c, 5)), "R_p25": float(np.percentile(c, 25)),
                "R_p50": float(np.percentile(c, 50)), "R_p75": float(np.percentile(c, 75)),
                "R_p95": float(np.percentile(c, 95)),
                "usd_p50": float(np.percentile(c, 50) * risk_usd),
                "usd_p05": float(np.percentile(c, 5) * risk_usd),
                "usd_p95": float(np.percentile(c, 95) * risk_usd),
                "prob_loss": float((c < 0).mean()),
                "max_dd_R_p50": float(np.percentile(mdd, 50)), "max_dd_R_p95": float(np.percentile(mdd, 5)),
                "prob_dd_worse_5R": float((mdd < -5).mean()),
                "prob_dd_worse_10R": float((mdd < -10).mean()),
            }
        res[label] = block_res
        cones[label] = pd.DataFrame({f"p{q}": np.percentile(cum, q, axis=0) for q in (5, 25, 50, 75, 95)},
                                    index=pd.RangeIndex(1, H + 1, name="session"))
    res["_cones"] = cones
    return res


def project_evaluation(daily: pd.DataFrame, risk_usd: float, profit_target_usd: float,
                       trailing_dd_usd: float, daily_loss_usd: float | None = None,
                       max_sessions: int = 60, n_sims: int = 10000, block: float = 5.0,
                       haircut: float = 0.5, consistency_max_share: float | None = None,
                       dd_mode: str = "eod", seed: int = 13) -> dict:
    """Funded-account / evaluation odds.

    daily: DataFrame with columns 'net_R' (session result) and 'min_R' (worst intraday
           open P&L of the session in R, <= min(0, net_R)); both from the futures engine.
    Pass  = cumulative P&L reaches profit_target before any breach (and, if set, the best
            day is at most consistency_max_share of total profit at the pass point).
    Breach = trailing drawdown from the equity peak exceeds trailing_dd_usd (checked on
            the intraday low if dd_mode == 'intraday', else on the close), or a session
            loses more than daily_loss_usd (checked on the intraday low).
    Undecided = neither within max_sessions.
    """
    net = np.asarray(daily["net_R"], dtype=float) * risk_usd
    low = np.asarray(daily["min_R"], dtype=float) * risk_usd
    if haircut > 0 and net.mean() > 0:
        shift = haircut * net.mean()
        net, low = net - shift, low - shift
    rng = np.random.default_rng(seed)
    n = len(net)
    p = 1.0 / max(block, 1.0)
    outcomes = {"pass": 0, "breach": 0, "undecided": 0}
    days_to_pass, days_to_breach = [], []
    for _ in range(n_sims):
        eq, peak, best_day = 0.0, 0.0, 0.0
        i = int(rng.integers(0, n))
        decided = False
        for d in range(max_sessions):
            day_net, day_low = net[i], min(low[i], 0.0, net[i])
            intraday_low = eq + day_low
            breach = False
            if daily_loss_usd is not None and day_low <= -daily_loss_usd:
                breach = True
            check = intraday_low if dd_mode == "intraday" else eq + day_net
            if check <= peak - trailing_dd_usd:
                breach = True
            if breach:
                outcomes["breach"] += 1
                days_to_breach.append(d + 1)
                decided = True
                break
            eq += day_net
            best_day = max(best_day, day_net)
            peak = max(peak, eq)
            if eq >= profit_target_usd:
                if consistency_max_share is None or best_day <= consistency_max_share * eq:
                    outcomes["pass"] += 1
                    days_to_pass.append(d + 1)
                    decided = True
                    break
            i = int(rng.integers(0, n)) if rng.random() < p else (i + 1) % n
        if not decided:
            outcomes["undecided"] += 1
    tot = float(n_sims)
    return {
        "inputs": {"risk_usd_per_R": risk_usd, "profit_target_usd": profit_target_usd,
                   "trailing_dd_usd": trailing_dd_usd, "daily_loss_usd": daily_loss_usd,
                   "max_sessions": max_sessions, "haircut": haircut, "dd_mode": dd_mode,
                   "consistency_max_share": consistency_max_share, "n_sims": n_sims},
        "prob_pass": outcomes["pass"] / tot,
        "prob_breach": outcomes["breach"] / tot,
        "prob_undecided": outcomes["undecided"] / tot,
        "median_sessions_to_pass": float(np.median(days_to_pass)) if days_to_pass else None,
        "median_sessions_to_breach": float(np.median(days_to_breach)) if days_to_breach else None,
    }


def save_projection(res: dict, out_dir: str, name: str = "projection") -> None:
    os.makedirs(out_dir, exist_ok=True)
    cones = res.get("_cones", {})
    for label, df in cones.items():
        df.to_csv(os.path.join(out_dir, f"{name}_cone_{label}.csv"))
    clean = {k: v for k, v in res.items() if k != "_cones"}
    with open(os.path.join(out_dir, f"{name}.json"), "w") as f:
        json.dump(clean, f, indent=2)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(1, len(cones), figsize=(6 * max(len(cones), 1), 4), squeeze=False)
        for ax, (label, df) in zip(axes[0], cones.items()):
            ax.fill_between(df.index, df["p5"], df["p95"], alpha=0.15, color="tab:blue", label="5-95%")
            ax.fill_between(df.index, df["p25"], df["p75"], alpha=0.3, color="tab:blue", label="25-75%")
            ax.plot(df.index, df["p50"], color="tab:blue", label="median")
            ax.set_title(f"Projection ({label})")
            ax.set_xlabel(df.index.name)
            ax.legend(loc="upper left")
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, f"{name}.png"), dpi=110)
        plt.close(fig)
    except Exception:  # matplotlib optional
        pass
