from datetime import date, timedelta

import numpy as np
import pytest

from abg.analysis import options as opt
from abg.models import OptionChain, OptionContract

S, K, T, R, Q, V = 100.0, 105.0, 0.5, 0.04, 0.01, 0.3


def test_put_call_parity():
    c = opt.bs_price(S, K, T, R, V, Q, "call")
    p = opt.bs_price(S, K, T, R, V, Q, "put")
    assert np.isclose(c - p, S * np.exp(-Q * T) - K * np.exp(-R * T))


def test_known_value():
    # Hull, Options Futures & Other Derivatives: S=42 K=40 r=10% sigma=20% T=0.5 -> call 4.76, put 0.81
    assert round(float(opt.bs_price(42, 40, 0.5, 0.10, 0.20, 0, "call")), 2) == 4.76
    assert round(float(opt.bs_price(42, 40, 0.5, 0.10, 0.20, 0, "put")), 2) == 0.81


@pytest.mark.parametrize("kind", ["call", "put"])
def test_greeks_vs_finite_differences(kind):
    g = {k: float(v) for k, v in opt.greeks(S, K, T, R, V, Q, kind).items()}
    f = lambda **kw: float(opt.bs_price(kw.get("S", S), K, kw.get("T", T), kw.get("r", R), kw.get("v", V), Q, kind))  # noqa
    h = 1e-3
    assert np.isclose(g["delta"], (f(S=S + h) - f(S=S - h)) / (2 * h), atol=1e-6)
    assert np.isclose(g["gamma"], (f(S=S + h) - 2 * f() + f(S=S - h)) / h**2, atol=1e-4)
    assert np.isclose(g["vega"], (f(v=V + 0.005) - f(v=V - 0.005)) / 1.0, atol=1e-4)   # per 1 vol point
    assert np.isclose(g["theta"], (f(T=T - 1 / 365) - f()), atol=2e-3)                 # per calendar day
    assert np.isclose(g["rho"], (f(r=R + 0.0005) - f(r=R - 0.0005)) / 0.1, atol=1e-4)  # per 1 %


def test_implied_vol_roundtrip_vectorised():
    strikes = np.array([70, 90, 100, 110, 140.0])
    vols = np.array([0.45, 0.32, 0.28, 0.26, 0.35])
    kinds = np.array([False, True, True, False, True])        # mix of puts / calls
    prices = opt.bs_price(S, strikes, T, R, vols, Q, kinds)
    iv = opt.implied_vol(prices, S, strikes, T, R, Q, kinds)
    assert np.allclose(iv, vols, atol=1e-6)


def test_implied_vol_rejects_arbitrage_violations():
    assert np.isnan(opt.implied_vol(0.0001, S, 50, T, R, 0, "call"))   # below intrinsic
    assert np.isnan(opt.implied_vol(150.0, S, 100, T, R, 0, "call"))   # above spot


def test_theoretical_chain_and_analysis():
    today = date(2026, 1, 5)
    ch = opt.theoretical_chain("X", 100, 0.3, 0.04, today=today)
    assert ch.model_generated and ch.expirations[0] > today
    a = opt.analyze_chain(ch, 0.04, today=today)
    assert a["available"] and a["summary"]["atm_iv"] == pytest.approx(0.3, abs=0.01)
    calls = [c for c in a["contracts"] if c["kind"] == "call"]
    deltas = [c["delta"] for c in sorted(calls, key=lambda c: c["strike"])]
    assert all(x >= y for x, y in zip(deltas, deltas[1:]))            # call delta decreases with strike


def test_market_chain_iv_from_mid_and_max_pain():
    today = date(2026, 1, 5)
    exp = today + timedelta(days=30)
    Tm = opt.year_fraction(exp, today)
    cs = []
    for k in (90, 95, 100, 105, 110):
        for kind, oi in (("call", 100 if k >= 100 else 10), ("put", 100 if k <= 100 else 10)):
            px = float(opt.bs_price(100, k, Tm, 0.04, 0.25, 0, kind))
            cs.append(OptionContract(expiry=exp, strike=k, kind=kind, bid=px - 0.01, ask=px + 0.01, open_interest=oi,
                                     volume=5, iv=0.9))   # deliberately wrong vendor IV
    a = opt.analyze_chain(OptionChain("X", 100, "test", [exp], cs), 0.04, today=today)
    ivs = [c["iv_used"] for c in a["contracts"] if 95 <= c["strike"] <= 105]
    assert np.allclose(ivs, 0.25, atol=5e-3)                          # recomputed from mid, not vendor's 0.9
    assert a["summary"]["max_pain"] == 100
    assert a["summary"]["put_call_oi_ratio"] == pytest.approx(1.0)
