"""Risk-model plug-in interface.

A risk model is any object with::

    name: str
    version: str
    schema_major: int                        # FEATURE_SCHEMA major version it was built for
    def assess(self, features: FeatureVector, ctx: RiskContext) -> RiskAssessment   (sync or async)

Ways to plug one in (no terminal code changes needed):

1. In code:          ``register_risk_model(MyModel())``
2. Env / .env:       ``ABG_RISK_MODELS=my_pkg.risk:GarchModel,my_pkg.ml:XGBRisk``
3. Package metadata: expose it under the ``abg.risk_models`` entry-point group:

       [project.entry-points."abg.risk_models"]
       xgb = "my_pkg.ml:XGBRisk"

Every model runs isolated: exceptions, timeouts, or schema mismatches become an error
entry in the report instead of breaking the analysis.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from .features import FEATURE_SCHEMA_VERSION, FeatureVector

log = logging.getLogger(__name__)
SCHEMA_MAJOR = int(FEATURE_SCHEMA_VERSION.split(".")[0])
LEVELS = [(25, "Low"), (45, "Moderate"), (65, "Elevated"), (80, "High"), (101, "Extreme")]


def level_for(score: float) -> str:
    for bound, name in LEVELS:
        if score < bound:
            return name
    return "Extreme"


@dataclass
class RiskContext:
    """Raw material a model may want beyond the feature vector."""

    symbol: str
    prices: pd.DataFrame                         # OHLCV + indicators
    returns: pd.Series                           # simple returns
    benchmark_returns: pd.Series | None = None
    feature_frame: pd.DataFrame | None = None    # historical features (same schema)
    position: dict | None = None                 # e.g. {"account_value": 50000, "risk_pct": 1.0, "shares": 100}
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskAssessment:
    model: str
    version: str
    score: float                                 # 0 (low) .. 100 (extreme)
    level: str
    metrics: dict[str, Any] = field(default_factory=dict)
    drivers: list[dict] = field(default_factory=list)   # [{factor, contribution, detail}]
    warnings: list[str] = field(default_factory=list)
    schema_version: str = FEATURE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@runtime_checkable
class RiskModel(Protocol):
    name: str
    version: str
    schema_major: int

    def assess(self, features: FeatureVector, ctx: RiskContext) -> RiskAssessment: ...


# --------------------------------------------------------------------------- registry
_REGISTRY: dict[str, RiskModel] = {}


def register_risk_model(model: Any) -> Any:
    """Register an instance (or a zero-arg class/factory).  Usable as a decorator."""
    inst = model() if inspect.isclass(model) or (callable(model) and not hasattr(model, "assess")) else model
    if not isinstance(inst, RiskModel):
        raise TypeError(f"{model!r} does not implement RiskModel (name, version, schema_major, assess)")
    _REGISTRY[inst.name] = inst
    return model


def unregister_risk_model(name: str) -> None:
    _REGISTRY.pop(name, None)


def load_model_path(path: str) -> None:
    """Load 'package.module:ClassOrFactory' and register it."""
    import os
    import sys
    if os.getcwd() not in sys.path:          # console scripts don't put the CWD on sys.path
        sys.path.insert(0, os.getcwd())
    mod_name, _, attr = path.partition(":")
    obj = getattr(importlib.import_module(mod_name), attr or "model")
    register_risk_model(obj)


_discovered = False


def discover(extra_paths: list[str] | None = None) -> None:
    global _discovered
    from .baseline import BaselineRiskModel  # built-in
    if "baseline" not in _REGISTRY:
        register_risk_model(BaselineRiskModel())
    if not _discovered:
        _discovered = True
        try:
            eps = entry_points(group="abg.risk_models")
        except TypeError:  # pragma: no cover
            eps = entry_points().get("abg.risk_models", [])
        for ep in eps:
            try:
                register_risk_model(ep.load())
            except Exception as e:
                log.warning("risk model plug-in %s failed to load: %s", ep.name, e)
    for p in extra_paths or []:
        try:
            load_model_path(p)
        except Exception as e:
            log.warning("risk model %s failed to load: %s", p, e)


def registered_models() -> list[RiskModel]:
    return list(_REGISTRY.values())


async def run_models(features: FeatureVector, ctx: RiskContext, models: list[RiskModel] | None = None,
                     timeout: float = 10.0) -> list[dict]:
    """Run every model concurrently with isolation; returns plain dicts (baseline first)."""
    models = models if models is not None else registered_models()

    async def one(m: RiskModel) -> dict:
        if getattr(m, "schema_major", SCHEMA_MAJOR) != SCHEMA_MAJOR:
            return {"model": m.name, "version": getattr(m, "version", "?"),
                    "error": f"schema mismatch: model built for v{m.schema_major}.x, terminal provides {FEATURE_SCHEMA_VERSION}"}
        try:
            if inspect.iscoroutinefunction(m.assess):
                res = await asyncio.wait_for(m.assess(features, ctx), timeout)
            else:
                res = await asyncio.wait_for(asyncio.to_thread(m.assess, features, ctx), timeout)
            d = res.to_dict() if hasattr(res, "to_dict") else dict(res)
            d["score"] = max(0.0, min(100.0, float(d.get("score", 0))))
            d.setdefault("level", level_for(d["score"]))
            return d
        except Exception as e:
            log.warning("risk model %s failed: %s", m.name, e)
            return {"model": m.name, "version": getattr(m, "version", "?"), "error": f"{type(e).__name__}: {e}"[:300]}

    results = await asyncio.gather(*(one(m) for m in models))
    return sorted(results, key=lambda d: d.get("model") != "baseline")
