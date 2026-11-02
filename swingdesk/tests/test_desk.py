"""Offline tests: parser, replay engine, strategy gate, monitor transitions, agent wiring."""
import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

from swingdesk.backtest import load_prices, simulate  # noqa: E402
from swingdesk.data_sources import price_file  # noqa: E402
from swingdesk.engine import Plan, run, summarize  # noqa: E402
from swingdesk.monitor import scoreboard, update  # noqa: E402
from swingdesk.parse_export import extract_signal, parse  # noqa: E402
from swingdesk.strategy import grade, load_config  # noqa: E402

SIGS = pd.read_csv(ROOT / "data/signals.csv")
SIGS["posted"] = SIGS.posted_et.map(lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M"))
PX = {s: load_prices(price_file(s, ROOT / "data/prices")) for s in SIGS.symbol.unique()}
SPY = load_prices(ROOT / "data/prices/SPY.csv")


def _sig(sym, date_prefix):
    r = SIGS[(SIGS.symbol == sym) & SIGS.posted_et.str.startswith(date_prefix)].iloc[0]
    return r.to_dict()


def test_parser_finds_every_structured_post():
    msgs = parse(str(ROOT / "data/export.html"))
    sigs = [s for s in (extract_signal(m) for m in msgs) if s]
    assert len(msgs) == 131 and len(sigs) == 93
    lite = next(s for s in sigs if s["ticker"] == "LITE")
    assert lite["stop"] == (840.0, 840.0) and lite["target"] == (1080.0, 1080.0)


def test_visa_first_call_never_filled():
    # the channel posted "Target hit on VISA" but price never traded down to 345-348 first
    assert simulate(_sig("V", "2026-07-13"), PX["V"]).status == "NOFILL_RAN"


def test_axon_stopped_on_daily_close():
    r = simulate(_sig("AXON", "2026-07-09"), PX["AXON"])
    assert r.status == "LOSS" and r.exit_date == "2026-07-22"


def test_ocul_gap_through_close_stop_costs_more_than_1R():
    r = simulate(_sig("OCUL", "2026-09-23"), PX["OCUL"])
    assert r.status == "LOSS" and r.r_multiple < -1.5


def test_take_profit_plan_raises_win_rate_and_keeps_positive_R():
    b = SIGS[SIGS.author == "b06wi"]
    base = summarize([run(r, PX[r["symbol"]], Plan()) for r in b.to_dict("records")])
    tp = summarize([run(r, PX[r["symbol"]], Plan("tp", tp1_r=0.75)) for r in b.to_dict("records")])
    assert tp["win_rate"] > base["win_rate"] + 15


def test_gate_skips_high_vol_name_in_extended_market():
    s = _sig("OUST", "2026-07-10")
    v = grade(s, PX["OUST"], SPY, asof=pd.Timestamp("2026-07-09"))
    assert not v.take and v.metrics["atr_pct"] > 4


def test_gate_takes_low_vol_name_and_sizes_from_R():
    s = _sig("V", "2026-08-24")
    v = grade(s, PX["V"], SPY, asof=pd.Timestamp("2026-08-24"))
    assert v.take and v.entry_px == 375 and v.stop_px == 355
    assert v.tp_px == pytest.approx(375 + 0.75 * 20)
    assert v.shares_per_10k == int(10_000 * 0.005 // 20)


def test_monitor_reproduces_research_win_rate():
    book, alerts = update(SIGS.drop(columns="posted"), PX, SPY, book={})
    sb = scoreboard(book)
    assert sb["closed"] >= 35 and sb["win_rate"] >= 75
    assert any(a.kind == "GRADED" for a in alerts)


def test_monitor_only_alerts_on_transitions():
    s = SIGS.drop(columns="posted")
    book, _ = update(s, PX, SPY, book={})
    _, again = update(s, PX, SPY, book=book)
    assert again == []


def test_agents_build_without_calling_the_api(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from swingdesk.agents import desk
    from swingdesk.agents.tools import ANALYST_TOOLS, RESEARCH_TOOLS
    a, r = desk.analyst_agent(), desk.research_agent()
    names = set(a.tool_names) | set(r.tool_names)
    assert {"grade_signal", "evaluate_strategy_variant", "write_research_proposal"} <= names
    assert len(ANALYST_TOOLS) == 5 and len(RESEARCH_TOOLS) == 4


def test_research_tool_replays_variants_deterministically():
    from swingdesk.agents.tools import evaluate_strategy_variant
    base = evaluate_strategy_variant()
    strict = evaluate_strategy_variant(gate_require="all")
    assert base["all"]["taken"] > strict["all"]["taken"]
    assert set(base) >= {"all", "before", "after", "variant"}
