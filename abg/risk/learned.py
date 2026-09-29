"""A risk model trained on your own data (``abg train risk``), plugged in next to the baseline.

Target: will this symbol suffer a >= 10% drawdown from today's close within the next 21 trading
days?  (``y_fwd_max_loss_21d >= 0.10``), plus the next 21 days' realised volatility.  Inputs: the
same versioned time-series features the baseline model sees (no look-ahead), for a basket of
symbols over many years.  Two plain-numpy models (logistic + ridge, standardised, L2) are fitted;
validation is walk-forward by date (earliest 70% train, latest 30% test) and saved with the model.

Score (0-100) = 70 x min(1, P(drawdown) / 0.40) + 30 x min(1, predicted vol / 80%).  Drivers are the
features pushing P(drawdown) up or down the most for this symbol today.
"""
from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

from ..extsignals.learn import _sigmoid, auc, fit_logistic, fit_ridge, standardize
from .features import FEATURE_SCHEMA_VERSION, TIMESERIES_FEATURES, FeatureVector, add_forward_labels, build_feature_frame
from .interface import RiskAssessment, RiskContext, level_for

log = logging.getLogger(__name__)
DD_THRESHOLD, HORIZON = 0.10, 21
EXCLUDE = {"beta_252d", "corr_252d"}          # need a benchmark series; not always present


def model_path(settings) -> Path:
    return Path(settings.data_dir).expanduser() / "models" / "risk_model.json"


class LearnedRiskModel:
    name = "learned"
    schema_major = int(FEATURE_SCHEMA_VERSION.split(".")[0])

    def __init__(self, d: dict):
        self.d = d
        self.version = f"1.{int(d.get('trained_at', 0))}"
        self.features = d["features"]
        self.mu, self.sd = np.array(d["mean"]), np.array(d["std"])
        self.cw, self.bw = np.array(d["coef_dd"]), d["b_dd"]
        self.cv, self.bv = np.array(d["coef_vol"]), d["b_vol"]

    @classmethod
    def load(cls, path: Path) -> "LearnedRiskModel | None":
        try:
            return cls(json.loads(Path(path).read_text()))
        except Exception:
            return None

    def assess(self, features: FeatureVector, ctx: RiskContext) -> RiskAssessment:
        raw = [features.get(k) for k in self.features]
        missing = sum(v is None or (isinstance(v, float) and not math.isfinite(v)) for v in raw)
        x = np.array([self.mu[i] if (v is None or (isinstance(v, float) and not math.isfinite(v))) else float(v)
                      for i, v in enumerate(raw)])
        z = np.nan_to_num((x - self.mu) / self.sd)
        p = float(_sigmoid(z @ self.cw + self.bw))
        vol = max(0.0, float(z @ self.cv + self.bv))
        score = 70 * min(1.0, p / 0.40) + 30 * min(1.0, vol / 0.80)
        contrib = sorted(((k, float(z[i] * self.cw[i])) for i, k in enumerate(self.features)),
                         key=lambda kv: abs(kv[1]), reverse=True)[:5]
        drivers = [{"factor": k, "contribution": round(c, 3), "detail": f"{k}={x[i]:.4g}"}
                   for k, c in contrib for i in [self.features.index(k)]]
        m = self.d.get("metrics") or {}
        warn = [f"{missing} of {len(self.features)} inputs missing (filled with typical values)"] if missing > 5 else []
        return RiskAssessment(self.name, self.version, round(score, 1), level_for(score),
                              metrics={"prob_drawdown_10pct_21d": round(p, 4), "predicted_vol_21d": round(vol, 4),
                                       "base_rate": m.get("base_rate"), "test_auc": m.get("test_auc"),
                                       "trained_on": self.d.get("n")},
                              drivers=drivers, warnings=warn)


def autoload(settings) -> None:
    """Register the trained model if its file exists (called by the engine at start-up)."""
    from .interface import register_risk_model
    p = model_path(settings)
    if p.exists():
        m = LearnedRiskModel.load(p)
        if m is not None:
            register_risk_model(m)


async def build_training_frame(engine, symbols: list[str], years: int = 10, progress=None) -> pd.DataFrame:
    from ..analysis import indicators as ta
    from ..errors import ABGError
    frames = []
    for k, sym in enumerate(symbols):
        try:
            df = (await engine.history(sym, f"{years}y", "1d", ttl=86_400)).value.df
        except ABGError as e:
            log.warning("risk training: %s skipped: %s", sym, e.message)
            continue
        ind = ta.compute_all(df)
        fr = add_forward_labels(build_feature_frame(ind), ind["close"], (HORIZON,))
        fr["symbol"] = sym
        frames.append(fr.iloc[252:])                    # need a year of warm-up for the 252-day features
        if progress:
            progress(k + 1, len(symbols), sym)
    if not frames:
        raise ValueError("no history could be loaded for any symbol")
    return pd.concat(frames)


def train_risk_model(frame: pd.DataFrame, split: float = 0.7) -> dict:
    feats = [f for f in TIMESERIES_FEATURES if f not in EXCLUDE]
    fr = frame.dropna(subset=[f"y_fwd_max_loss_{HORIZON}d", f"y_fwd_vol_{HORIZON}d"]).sort_index()
    X = fr[feats].to_numpy(dtype=float)
    col_mu = np.nanmean(X, axis=0)
    X = np.where(np.isnan(X), col_mu, X)
    y = (fr[f"y_fwd_max_loss_{HORIZON}d"].to_numpy() >= DD_THRESHOLD).astype(float)
    v = np.clip(fr[f"y_fwd_vol_{HORIZON}d"].to_numpy(dtype=float), 0, 3)
    if len(y) < 500:
        raise ValueError(f"need at least 500 labelled days (have {len(y)})")
    dates = fr.index.to_numpy()
    cut_date = np.quantile(dates.astype("datetime64[ns]").astype(np.int64), split)
    tr_mask = dates.astype("datetime64[ns]").astype(np.int64) <= cut_date
    te_mask = ~tr_mask
    # skip the 21 days before the cut so test labels don't overlap training labels
    gap = pd.Timestamp(int(cut_date)) + pd.Timedelta(days=31)
    te_mask &= fr.index >= gap
    mu, sd = standardize(X[tr_mask])
    Z = (X - mu) / sd
    cw, bw = fit_logistic(Z[tr_mask], y[tr_mask], l2=5.0)
    cv, bv = fit_ridge(Z[tr_mask], v[tr_mask], l2=20.0)
    pt, vt = _sigmoid(Z[te_mask] @ cw + bw), Z[te_mask] @ cv + bv
    yt, vtt = y[te_mask], v[te_mask]
    metrics = {"train_n": int(tr_mask.sum()), "test_n": int(te_mask.sum()), "base_rate": float(y.mean()),
               "test_auc": auc(yt, pt), "test_brier": float(np.mean((pt - yt) ** 2)) if len(yt) else None,
               "test_vol_corr": float(np.corrcoef(vt, vtt)[0, 1]) if len(vt) > 2 else None,
               "symbols": sorted(set(fr["symbol"])), "horizon_days": HORIZON, "threshold": DD_THRESHOLD}
    mu, sd = standardize(X)
    Z = (X - mu) / sd
    cw, bw = fit_logistic(Z, y, l2=5.0)
    cv, bv = fit_ridge(Z, v, l2=20.0)
    return {"kind": "risk", "version": 1, "features": feats, "mean": mu.tolist(), "std": sd.tolist(),
            "coef_dd": cw.tolist(), "b_dd": bw, "coef_vol": cv.tolist(), "b_vol": bv, "n": int(len(y)),
            "metrics": metrics, "trained_at": time.time(), "schema": FEATURE_SCHEMA_VERSION}
