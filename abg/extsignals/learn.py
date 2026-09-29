"""Learned entry grading.

The rule-based grade (``commentary.grade``) uses hand-set weights.  ``GradeModel`` learns them from
outcomes instead, from two sources of labelled trades:

    * backtested signals (``abg ext backtest``): your sources' real calls replayed on history
    * self-generated setups (``abg train grade``): the terminal's own setup detector run through
      years of daily bars for a basket of symbols, every trade simulated with the same lifecycle

Features describe the setup at decision time only (no look-ahead): trend alignment, composite
signal, reward:risk, RSI, stop distance in ATR, entry distance, the market regime, pattern and basis.
Two models are fitted on standardised features with L2 regularisation (plain numpy, no extra
dependencies): a logistic model for P(win) and a ridge model for expected R.  Validation is
walk-forward (train on the earliest 70% by date, test on the latest 30%) and the metrics are saved
with the model.  The tracker only uses the learned grade when the out-of-sample AUC beats 0.55 on at
least 200 trades; otherwise the rule grade stays in charge.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np

PATTERNS = ["Pullback", "Breakout", "Continuation", "Reversal / base", "Mean reversion", "Momentum", "Breakdown"]
FEATURES = ["trend_aligned", "counter_trend", "signal_aligned", "rr_first", "rr_last", "rsi_aligned", "stop_atr",
            "entry_dist_atr", "risk_high", "regime_aligned", "res_before_target", "p_target_first", "has_sim",
            "model_agree", "short", "basis_fund", "basis_tech"] + [f"pat_{p}" for p in PATTERNS]


def grade_features(idea, f: dict, price: float | None = None) -> dict[str, float]:
    sgn = 1.0 if idea.long else -1.0
    rr = [x for x in (f.get("rr") or []) if x is not None]
    rsi = f.get("rsi")
    atr = f.get("atr")
    e = f.get("entry_ref")
    px = price or f.get("price")
    cls = ((idea.meta or {}).get("class") or {})
    rg = (f.get("regime") or {}).get("score")
    mv = f.get("model_view")
    good = {"Buy", "Strong Buy"} if idea.long else {"Sell", "Reduce"}
    bad = {"Sell", "Reduce"} if idea.long else {"Buy", "Strong Buy"}
    res = f.get("resistances") or []
    t_last = (idea.targets or [None])[-1]
    out = {
        "trend_aligned": float(bool(f.get("trend_aligned"))), "counter_trend": float(bool(f.get("counter_trend"))),
        "signal_aligned": (f.get("signal_score") or 0.0) * sgn / 100.0,
        "rr_first": min(rr[0], 5.0) if rr else 1.0, "rr_last": min(rr[-1], 8.0) if rr else 1.0,
        "rsi_aligned": ((rsi - 50.0) / 50.0 * sgn) if rsi is not None else 0.0,
        "stop_atr": min(f.get("stop_atr") or 2.0, 12.0),
        "entry_dist_atr": min(abs(px - e) / atr, 10.0) if (px and e and atr) else 0.0,
        "risk_high": float(f.get("risk_level") in ("High", "Extreme")),
        "regime_aligned": (rg or 0.0) * sgn / 2.0,
        "res_before_target": float(bool(idea.long and e and t_last and any(e * 1.01 < x < t_last * 0.99 for x in res))),
        "p_target_first": f.get("p_t1_first") if f.get("p_t1_first") is not None else 0.0,
        "has_sim": float(f.get("p_t1_first") is not None),
        "model_agree": 1.0 if mv in good else -1.0 if mv in bad else 0.0,
        "short": float(not idea.long),
        "basis_fund": float(cls.get("basis") in ("Fundamental", "Hybrid")),
        "basis_tech": float(cls.get("basis") in ("Technical", "Hybrid")),
    }
    pat = cls.get("pattern", "")
    for p in PATTERNS:
        out[f"pat_{p}"] = float(pat.startswith(p))
    return out


# --------------------------------------------------------------------------- tiny numpy models
def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 60) -> tuple[np.ndarray, float]:
    """L2-regularised logistic regression by Newton / IRLS (intercept not penalised)."""
    n, k = X.shape
    Xb = np.hstack([np.ones((n, 1)), X])
    w = np.zeros(k + 1)
    reg = np.eye(k + 1) * l2
    reg[0, 0] = 0.0
    for _ in range(iters):
        p = _sigmoid(Xb @ w)
        g = Xb.T @ (p - y) + reg @ w
        h = (Xb * (p * (1 - p))[:, None]).T @ Xb + reg + np.eye(k + 1) * 1e-9
        step = np.linalg.solve(h, g)
        w -= step
        if np.max(np.abs(step)) < 1e-7:
            break
    return w[1:], float(w[0])


def fit_ridge(X: np.ndarray, y: np.ndarray, l2: float = 5.0) -> tuple[np.ndarray, float]:
    n, k = X.shape
    Xb = np.hstack([np.ones((n, 1)), X])
    reg = np.eye(k + 1) * l2
    reg[0, 0] = 0.0
    w = np.linalg.solve(Xb.T @ Xb + reg, Xb.T @ y)
    return w[1:], float(w[0])


def auc(y: np.ndarray, p: np.ndarray) -> float | None:
    pos, neg = y == 1, y == 0
    if pos.sum() == 0 or neg.sum() == 0:
        return None
    order = np.argsort(p)
    ranks = np.empty(len(p))
    ranks[order] = np.arange(1, len(p) + 1)
    return float((ranks[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (pos.sum() * neg.sum()))


def standardize(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mu = np.nanmean(X, axis=0)
    sd = np.nanstd(X, axis=0)
    sd[sd < 1e-9] = 1.0
    return mu, sd


# --------------------------------------------------------------------------- grade model
class GradeModel:
    KIND = "grade"

    def __init__(self, d: dict):
        self.d = d
        self.features = d["features"]
        self.mu, self.sd = np.array(d["mean"]), np.array(d["std"])
        self.cw, self.bw = np.array(d["coef_win"]), d["b_win"]
        self.cr, self.br = np.array(d["coef_r"]), d["b_r"]

    @property
    def usable(self) -> bool:
        m = self.d.get("metrics") or {}
        return self.d.get("n", 0) >= 200 and (m.get("test_auc") or 0) > 0.55

    @classmethod
    def load(cls, path: Path) -> "GradeModel | None":
        try:
            return cls(json.loads(Path(path).read_text()))
        except Exception:
            return None

    def predict(self, feats: dict) -> dict:
        x = (np.array([feats.get(k, 0.0) for k in self.features], dtype=float) - self.mu) / self.sd
        x = np.nan_to_num(x)
        p = float(_sigmoid(x @ self.cw + self.bw))
        er = float(x @ self.cr + self.br)
        g = "A" if er >= 0.35 else "B" if er >= 0.15 else "C" if er >= 0.0 else "D"
        contrib = sorted(((k, float(x[i] * self.cr[i])) for i, k in enumerate(self.features)),
                         key=lambda kv: abs(kv[1]), reverse=True)[:4]
        return {"p_win": p, "exp_r": er, "grade": g, "drivers": contrib}

    @staticmethod
    def train(rows: list[dict], l2_logit: float = 2.0, l2_ridge: float = 10.0, split: float = 0.7) -> dict:
        """rows: [{"ts", "features": {...}, "r": realized R}] for FILLED trades."""
        rows = sorted([r for r in rows if r.get("r") is not None and math.isfinite(r["r"])], key=lambda r: r["ts"])
        if len(rows) < 40:
            raise ValueError(f"need at least 40 filled trades to train (have {len(rows)})")
        X = np.array([[r["features"].get(k, 0.0) for k in FEATURES] for r in rows], dtype=float)
        X = np.nan_to_num(X)
        R = np.clip(np.array([r["r"] for r in rows], dtype=float), -3, 6)
        Y = (R > 0.05).astype(float)
        cut = int(len(rows) * split)
        mu, sd = standardize(X[:cut])
        Xs = (X - mu) / sd
        cw, bw = fit_logistic(Xs[:cut], Y[:cut], l2_logit)
        cr, br = fit_ridge(Xs[:cut], R[:cut], l2_ridge)
        pt = _sigmoid(Xs[cut:] @ cw + bw)
        et = Xs[cut:] @ cr + br
        yt, rt = Y[cut:], R[cut:]
        q = np.quantile(et, [1 / 3, 2 / 3]) if len(et) >= 9 else [0, 0]
        top, bottom = rt[et >= q[1]], rt[et <= q[0]]
        metrics = {
            "train_n": cut, "test_n": len(rows) - cut, "base_win_rate": float(Y.mean()), "base_avg_r": float(R.mean()),
            "test_auc": auc(yt, pt), "test_brier": float(np.mean((pt - yt) ** 2)) if len(yt) else None,
            "test_corr_r": float(np.corrcoef(et, rt)[0, 1]) if len(rt) > 2 and np.std(et) > 0 else None,
            "test_top_third_avg_r": float(top.mean()) if len(top) else None,
            "test_bottom_third_avg_r": float(bottom.mean()) if len(bottom) else None,
        }
        # refit on everything for production (validation numbers above stay out-of-sample)
        mu, sd = standardize(X)
        Xs = (X - mu) / sd
        cw, bw = fit_logistic(Xs, Y, l2_logit)
        cr, br = fit_ridge(Xs, R, l2_ridge)
        return {"kind": "grade", "version": 1, "features": FEATURES, "mean": mu.tolist(), "std": sd.tolist(),
                "coef_win": cw.tolist(), "b_win": bw, "coef_r": cr.tolist(), "b_r": br, "n": len(rows),
                "metrics": metrics, "trained_at": time.time(),
                "importance": sorted(({"feature": k, "coef_r": float(cr[i]), "coef_win": float(cw[i])}
                                      for i, k in enumerate(FEATURES)), key=lambda d: abs(d["coef_r"]), reverse=True)}
