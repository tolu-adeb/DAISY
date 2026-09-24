"""Example: plugging a custom risk model into the terminal.

Enable it without touching terminal code:

    ABG_RISK_MODELS=examples.custom_risk_model:VolTargetRiskModel abg analyze AAPL

or, for a trained ML model, load weights in __init__ and use ``features.to_array()``
(ordered exactly as ``abg.risk.FEATURE_NAMES``) as the model input.
"""
from __future__ import annotations

import numpy as np

from abg.risk import FEATURE_NAMES, FeatureVector, RiskAssessment, RiskContext
from abg.risk.interface import level_for


class VolTargetRiskModel:
    """Scores risk as realised vol relative to a 20 % annual vol target, adjusted for drawdown."""

    name = "vol_target"
    version = "0.1.0"
    schema_major = 1                      # built against feature schema 1.x

    def __init__(self, target_vol: float = 0.20):
        self.target_vol = target_vol

    def assess(self, features: FeatureVector, ctx: RiskContext) -> RiskAssessment:
        vol = features.get("vol_20d") or features.get("vol_60d")
        if vol is None:
            return RiskAssessment(self.name, self.version, 50.0, "Unknown", warnings=["no volatility feature"])
        ratio = vol / self.target_vol
        dd = features.get("current_dd", 0.0)
        score = float(np.clip(50 * ratio + 60 * dd, 0, 100))
        return RiskAssessment(
            model=self.name, version=self.version, score=round(score, 1), level=level_for(score),
            metrics={"vol_to_target": round(ratio, 3), "suggested_exposure": round(min(1.0, 1 / ratio), 3)},
            drivers=[{"factor": "volatility vs target", "contribution": round(50 * ratio - 50, 2),
                      "detail": f"vol {vol:.1%} vs target {self.target_vol:.0%}"}])


class SklearnRiskModel:
    """Template for a trained model (e.g. from `abg features ... --labels`)."""

    name = "ml_risk"
    version = "0.0.1"
    schema_major = 1

    def __init__(self, path: str = "risk_model.joblib"):
        import joblib                      # noqa: F401  (only needed when you actually use this)
        self.model = joblib.load(path)
        self.columns = FEATURE_NAMES       # the exact column order the model was trained on

    def assess(self, features: FeatureVector, ctx: RiskContext) -> RiskAssessment:
        x = np.nan_to_num(features.to_array(self.columns)).reshape(1, -1)
        p = float(self.model.predict_proba(x)[0, 1])        # e.g. P(>10% drawdown in 21 days)
        return RiskAssessment(self.name, self.version, round(p * 100, 1), level_for(p * 100),
                              metrics={"p_drawdown_event": round(p, 4)})
