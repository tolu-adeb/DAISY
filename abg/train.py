"""Training entry points (used by ``abg train ...`` and ``abg ext backtest``).

    abg ext backtest signals.txt            replay your sources' calls -> report + training rows
    abg train grade  [--symbols ...]        learned entry grade from setups (+ backtest rows)
    abg train risk   [--symbols ...]        learned risk model (P(10% drawdown in 21 days), vol)

Models are saved to ABG_DATA_DIR/models/ with their validation metrics and picked up automatically
(the tracker reloads the grade model when the file changes; the risk model loads at start-up).
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_BASKET = ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO",
                  "JPM", "BAC", "XOM", "CVX", "UNH", "LLY", "JNJ", "PG", "KO", "PEP", "WMT", "COST", "HD", "DIS", "NFLX",
                  "CRM", "ORCL", "INTC", "CSCO", "V", "MA", "GS", "CAT", "BA", "GE", "NKE", "SBUX", "TLT", "GLD", "XLE",
                  "XLF", "SMH", "IT", "SMTC", "PLTR", "UBER", "SHOP"]


def models_dir(settings) -> Path:
    d = Path(settings.data_dir).expanduser() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def backtest_rows(settings) -> list[dict]:
    """Training rows from every saved backtest report (filled trades with features)."""
    rows = []
    d = Path(settings.data_dir).expanduser() / "backtests"
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            rep = json.loads(p.read_text())
        except ValueError:
            continue
        for t in rep.get("trades") or []:
            if t.get("filled") and t.get("r") is not None and t.get("features"):
                rows.append({"ts": t["ts"], "r": t["r"], "features": t["features"], "source": "backtest"})
    return rows


async def train_grade(engine, symbols: list[str] | None = None, years: int = 8, progress=None) -> dict:
    from .extsignals.backtest import generate_setups
    from .extsignals.learn import GradeModel
    s = engine.settings
    rows = await generate_setups(engine, symbols or DEFAULT_BASKET, years=years, progress=progress)
    bt = backtest_rows(s)
    model = GradeModel.train(rows + bt)
    model["sources"] = {"setups": len(rows), "backtested_signals": len(bt)}
    path = models_dir(s) / "grade_model.json"
    path.write_text(json.dumps(model, indent=1))
    return {"path": str(path), **model}


async def train_risk(engine, symbols: list[str] | None = None, years: int = 10, progress=None) -> dict:
    from .risk.learned import build_training_frame, model_path, train_risk_model
    s = engine.settings
    frame = await build_training_frame(engine, symbols or DEFAULT_BASKET, years=years, progress=progress)
    model = train_risk_model(frame)
    path = model_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model, indent=1))
    return {"path": str(path), **model}
