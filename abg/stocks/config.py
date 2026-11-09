"""The stock desk's rule file.

The parameters live in ``configs/stocks.yaml`` - the file the Stocks Rule Book documents - so a
strategy change is a config change, not a code change.  This module loads it, points the ETF books at
the universe the desk actually follows, and lets a handful of values be overridden from ``.env`` for
the things an operator changes often (which books are on, how much risk, whether the ML gate runs).

Nothing here reads a secret.  Webhook URLs and API keys stay in ``.env`` and are read by the desk at
post time, never written into a config file, a memo or the dashboard.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("configs/stocks.yaml")

# .env name -> dotted path in the rule file. Only the knobs worth changing without editing the yaml.
ENV_OVERRIDES = {
    "stocks_benchmark": "benchmark",
    "stocks_initial_equity": "initial_equity",
    "stocks_max_gross": "risk.max_gross",
    "stocks_max_name_weight": "risk.max_name_weight",
    "stocks_dd_kill": "risk.dd_kill",
    "stocks_vol_target": "allocation.vol_target",
    "stocks_allocation": "allocation.method",
    "stocks_slippage_bps": "costs.slippage_bps",
    "stocks_commission_bps": "costs.commission_bps",
    "stocks_ml": "books.trend_pullback.ml.enabled",
    "stocks_ml_threshold": "books.trend_pullback.ml.threshold",
    "stocks_crisis": "stress.enabled",
    "stocks_risk_per_trade": "books.trend_pullback.risk_per_trade",
    "stocks_max_positions": "books.trend_pullback.max_positions",
}
BOOK_FLAGS = {           # ABG_STOCKS_BOOKS is a comma list; these are the names it accepts
    "a": "dual_momentum", "dual_momentum": "dual_momentum", "dual": "dual_momentum",
    "b": "xs_momentum", "xs_momentum": "xs_momentum", "momentum": "xs_momentum",
    "c": "trend_pullback", "trend_pullback": "trend_pullback", "pullback": "trend_pullback",
    "d": "mean_reversion", "mean_reversion": "mean_reversion", "reversion": "mean_reversion",
    "e": "crisis_alpha", "crisis_alpha": "crisis_alpha", "crisis": "crisis_alpha",
}


def _set(cfg: dict, dotted: str, value) -> None:
    node = cfg
    parts = dotted.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def config_path(settings) -> Path:
    raw = getattr(settings, "stocks_config", None)
    p = Path(raw).expanduser() if raw else DEFAULT_PATH
    if not p.is_absolute():
        # relative to the repo root (two levels up from abg/stocks/), so it works from any cwd
        root = Path(__file__).resolve().parents[2]
        cand = root / p
        if cand.exists():
            return cand
    return p


def build_config(settings, universe, overrides: list[str] | None = None) -> dict:
    """Load the rule file, wire it to this desk's universe, and apply .env overrides."""
    from abg.tradebook.common.config import load_config
    p = config_path(settings)
    if not p.exists():
        raise FileNotFoundError(
            f"stock rule file not found at {p}. It ships with the terminal at configs/stocks.yaml; "
            f"set ABG_STOCKS_CONFIG to point somewhere else.")
    cfg = load_config(str(p), list(overrides or []))

    # the store is the data source, not per-ticker CSVs
    cfg.setdefault("data", {})
    cfg["data"]["dir"] = None
    cfg["panel_start"] = getattr(settings, "stocks_panel_start", None) or cfg.get("panel_start")
    cfg["min_bars"] = int(getattr(settings, "stocks_min_bars", 250))

    # ETFs and gauges can never be treated as single stocks
    cfg["non_stock_tickers"] = sorted(set(cfg.get("non_stock_tickers", [])) | set(universe.non_stock()))

    # Book A rotates whatever sector/region ETFs the desk actually has bars for
    from .universe import BROAD_ETFS, DEFENSIVE_ETFS, SECTOR_ETFS
    have = set(universe.etfs)
    dm = cfg.setdefault("books", {}).setdefault("dual_momentum", {})
    rot = [t for t in SECTOR_ETFS + ["QQQ", "IWM", "EFA", "EEM"] if t in have]
    if rot:
        dm["universe"] = rot
    dfn = [t for t in DEFENSIVE_ETFS if t in have and t != "BIL"]
    if dfn:
        dm["defensive"] = dfn
        cfg["books"].setdefault("crisis_alpha", {})["defensive"] = dfn
    del BROAD_ETFS

    # which books are enabled
    raw = getattr(settings, "stocks_books", None)
    if raw:
        want = {BOOK_FLAGS[x.strip().lower()] for x in str(raw).split(",")
                if x.strip().lower() in BOOK_FLAGS}
        unknown = [x.strip() for x in str(raw).split(",")
                   if x.strip() and x.strip().lower() not in BOOK_FLAGS]
        if unknown:
            log.warning("ABG_STOCKS_BOOKS: ignoring unknown book(s) %s", ", ".join(unknown))
        if want:
            for name, bp in cfg["books"].items():
                bp["enabled"] = name in want

    for env_name, dotted in ENV_OVERRIDES.items():
        v = getattr(settings, env_name, None)
        if v is not None and v != "":
            _set(cfg, dotted, v)

    # the mean-reversion book shares the swing risk knobs unless it has its own
    for k in ("risk_per_trade", "max_positions"):
        v = getattr(settings, f"stocks_{k}", None)
        if v is not None:
            cfg["books"].setdefault("mean_reversion", {}).setdefault(k, v)

    ef = cfg.get("events_file")
    if ef and not Path(ef).is_absolute():
        root = Path(__file__).resolve().parents[2]
        cfg["events_file"] = str(root / ef) if (root / ef).exists() else None
    return cfg


def enabled_books(cfg: dict) -> list[str]:
    return [n for n, p in (cfg.get("books") or {}).items() if p.get("enabled", True)]


def describe(cfg: dict) -> dict:
    """A short, secret-free summary of the active rules, for the dashboard and the daily post."""
    a, r = cfg.get("allocation", {}), cfg.get("risk", {})
    tp = (cfg.get("books") or {}).get("trend_pullback", {})
    return {
        "benchmark": cfg.get("benchmark"), "books": enabled_books(cfg),
        "allocation": a.get("method"), "vol_target": a.get("vol_target"),
        "hedge_floor": a.get("hedge_floor"), "max_gross": r.get("max_gross"),
        "max_name_weight": r.get("max_name_weight"), "dd_kill": r.get("dd_kill"),
        "crisis_enabled": (cfg.get("stress") or {}).get("enabled", True),
        "ml_gate": bool((tp.get("ml") or {}).get("enabled")),
        "costs_bps": {"slippage": (cfg.get("costs") or {}).get("slippage_bps"),
                      "commission": (cfg.get("costs") or {}).get("commission_bps")},
        "config_file": cfg.get("__path__"),
    }
