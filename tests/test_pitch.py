"""Pitch desk v1: valuation maths, screen ranking and a full memo on fixtures (no network)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from abg.pitch import valuation as V
from abg.pitch.screen import rank, raw_factors


def test_dcf_and_reverse_dcf_round_trip():
    v = V.dcf(5.0, 0.10, 0.09, 0.025)
    assert v and v > 5.0 * 10
    g = V.implied_growth(v, 5.0, 0.09, 0.025)
    assert g == pytest.approx(0.10, abs=1e-4)
    assert V.dcf(-1, 0.1, 0.09) is None and V.dcf(1, 0.1, 0.02, 0.025) is None


def test_scenarios_order_and_rate():
    sc = V.scenarios(5.0, 0.12, 0.09)
    assert sc["bear"]["value"] < sc["base"]["value"] < sc["bull"]["value"]
    assert V.discount_rate(5.0) == pytest.approx(0.042 + 2.0 * 0.05)     # beta clamped
    assert V.discount_rate(None) == pytest.approx(0.092)


def test_comps_ignore_bad_multiples():
    c = V.comps({"eps": 2.0, "forward_eps": 2.5}, [{"symbol": "A", "pe": 20, "forward_pe": 18},
                                                   {"symbol": "B", "pe": -5, "forward_pe": 500},
                                                   {"symbol": "C", "pe": 30, "forward_pe": 22}])
    assert c["median_pe"] == 25 and c["value_pe"] == 50 and c["median_forward_pe"] == 20 and c["value"] == 50


def _series(drift, n=300, seed=0):
    r = np.random.default_rng(seed).normal(drift, 0.01, n)
    return pd.Series(100 * np.exp(np.cumsum(r)))


def test_rank_prefers_momentum_value_quality():
    good = raw_factors(_series(0.002), {"pe": 15, "forward_pe": 12, "profit_margin": 0.3})
    bad = raw_factors(_series(-0.002, seed=1), {"pe": 60, "forward_pe": 70, "profit_margin": 0.02})
    mid = raw_factors(_series(0.0, seed=2), {"pe": 25, "forward_pe": 24, "profit_margin": 0.1})
    out = rank({"BAD": bad, "GOOD": good, "MID": mid, "NONE": raw_factors(None, None)})
    assert [r["symbol"] for r in out][:3] == ["GOOD", "MID", "BAD"] and out[-1]["symbol"] == "NONE"


class _Eng:
    """Fixture engine: history / fundamentals / news / quote with the engine's return shapes."""
    def __init__(self):
        self.funds = {"AAA": dict(pe=15, forward_pe=12, eps=6.0, beta=1.0, profit_margin=0.3, sector="Tech", name="Aaa"),
                      "BBB": dict(pe=30, forward_pe=28, eps=3.0, beta=1.2, profit_margin=0.1, sector="Tech", name="Bbb"),
                      "CCC": dict(pe=22, forward_pe=20, eps=4.0, beta=0.9, profit_margin=0.15, sector="Tech", name="Ccc"),
                      "DDD": dict(pe=50, forward_pe=60, eps=1.0, beta=1.6, profit_margin=0.02, sector="Energy", name="Ddd")}
        self.http = None

    def _f(self, v, prov="fixture"):
        return SimpleNamespace(value=v, provenance=SimpleNamespace(provider=prov))

    async def history(self, sym, period="2y", interval="1d"):
        drift = {"AAA": 0.002, "BBB": 0.0005, "CCC": 0.001, "DDD": -0.001, "SPY": 0.0005}[sym]
        s = _series(drift, 400, seed=len(sym) + ord(sym[0]))
        return self._f(SimpleNamespace(df=pd.DataFrame({"close": s})))

    async def fundamentals(self, sym):
        d = dict(self.funds[sym], symbol=sym, extra={"target_mean_price": 120.0})
        return self._f(SimpleNamespace(to_dict=lambda: d))

    async def news(self, sym, limit=8):
        return self._f([SimpleNamespace(title=f"{sym} beats", url=f"https://example.com/{sym}", publisher="Wire",
                                        source="fixture", published_at=None)])

    async def quote(self, sym):
        return self._f(SimpleNamespace(price=100.0))


def _settings(tmp_path, **kw):
    base = dict(data_dir=tmp_path, pitch_universe="AAA,BBB,CCC,DDD", pitch_risk_free=0.042, pitch_equity_premium=0.05,
                pitch_terminal_growth=0.025, pitch_ai_memo=False, pitch_max_names=2, pitch_weekday=5,
                pitch_time="10:00", pitch_enabled=True, pitch_webhook_url=None, anthropic_api_key=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_memo_end_to_end(tmp_path):
    from abg.pitch.desk import PitchDesk
    d = PitchDesk(_settings(tmp_path), _Eng())
    r = asyncio.run(d.make("AAA"))
    assert r["verdict"] in ("LONG", "WATCH", "PASS") and r["fair_value"] and r["delivered"] == "dashboard"
    md = d.memo(r["id"])
    for part in ("## Valuation", "| base |", "## Risks", "## Sources", "https://example.com/AAA", "Research only"):
        assert part in md
    pack = r["pack"]
    assert pack["valuation"]["comps"]["peers"] == ["BBB", "CCC"]           # same-sector peers only
    sc = asyncio.run(d.score())
    assert sc["all"]["n"] == 1 and sc["pitches"][0]["excess"] is not None


def test_weekly_batch_skips_recent(tmp_path):
    from datetime import datetime
    from abg.pitch.desk import NY, PitchDesk
    d = PitchDesk(_settings(tmp_path), _Eng())
    b1 = asyncio.run(d.batch(post=False))
    assert len(b1["made"]) == 2
    b2 = asyncio.run(d.batch(post=False))
    assert not {m["symbol"] for m in b1["made"]} & {m["symbol"] for m in b2["made"]}
    assert not d.due(datetime(2026, 11, 2, 10, 30, tzinfo=NY))              # Monday
