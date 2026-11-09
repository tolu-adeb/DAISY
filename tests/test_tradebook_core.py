import numpy as np
import pandas as pd
import pytest

from abg.tradebook.common import indicators as ind
from abg.tradebook.common.hedge import Hedge
from abg.tradebook.common.projections import _apply_haircut, project_r
from abg.tradebook.futures.setups import cttd
from abg.tradebook.futures.sim import Costs, Plan, simulate
from abg.tradebook.stocks.stress import crisis_mode


def test_indicators_basic():
    s = pd.Series(np.arange(1, 101, dtype=float))
    assert ind.rsi(s, 14).iloc[-1] == pytest.approx(100.0)
    assert ind.efficiency_ratio(s, 20).iloc[-1] == pytest.approx(1.0)
    assert ind.momentum(s, 10, 0).iloc[-1] == pytest.approx(100 / 90 - 1)
    a = ind.atr(s + 1, s - 1, s, 14)
    assert a.iloc[-1] > 0


def test_hedge_moves_weight_to_winner():
    h = Hedge(["a", "b"], eta=0.5, floor=0.0, loss_clip=1.0)
    for _ in range(20):
        h.update({"a": 1.0, "b": -1.0})
    w = h.weights()
    assert w["a"] > 0.99 and abs(sum(w.values()) - 1) < 1e-9
    h2 = Hedge(["a", "b"], eta=0.5, floor=0.1, loss_clip=1.0)
    for _ in range(50):
        h2.update({"a": 1.0, "b": -1.0})
    assert h2.weights()["b"] >= 0.1 - 1e-9


def _bars(prices):
    o = np.array([p[0] for p in prices], float)
    h = np.array([p[1] for p in prices], float)
    l = np.array([p[2] for p in prices], float)
    c = np.array([p[3] for p in prices], float)
    return o, h, l, c


def test_sim_pt1_then_target_long():
    costs = Costs(tick=0.25, point_value=2.0, commission_usd=0.0, slip_ticks=0, through_ticks=0, min_stop_pts=1, max_stop_pts=100)
    # entry at 100, stop 90 (R=10), PT1 110, target 120
    bars = [(100, 101, 99, 100), (100, 111, 100, 110), (110, 121, 109, 120), (120, 121, 119, 120)]
    o, h, l, c = _bars(bars)
    t = simulate(Plan(1, 0, 90, pt1_r=1, pt1_frac=0.5, target_r=2), o, h, l, c, flat_i=4, costs=costs)
    assert t["exit_reason"] == "target"
    assert t["net_R"] == pytest.approx(0.5 * 1 + 0.5 * 2)


def test_sim_stop_gap_and_through_ticks():
    costs = Costs(tick=0.25, point_value=2.0, commission_usd=0.0, slip_ticks=0, through_ticks=4, min_stop_pts=1, max_stop_pts=100)
    # target touched exactly (no trade-through) must NOT fill; then a gap below the stop fills at the open
    bars = [(100, 101, 99, 100), (100, 110, 100, 109), (85, 86, 84, 85)]
    o, h, l, c = _bars(bars)
    t = simulate(Plan(1, 0, 90, pt1_r=1, pt1_frac=0.5, target_r=2), o, h, l, c, flat_i=3, costs=costs)
    assert t["exit_reason"] == "stop" and t["net_R"] == pytest.approx(-1.5)


def test_sim_costs_reduce_R():
    c0 = Costs(commission_usd=0.0, slip_ticks=0, through_ticks=0, min_stop_pts=1)
    c1 = Costs(commission_usd=2.0, slip_ticks=1, through_ticks=0, min_stop_pts=1)
    bars = [(100, 101, 99, 100), (100, 111, 100, 110), (110, 121, 109, 120)]
    o, h, l, c = _bars(bars)
    a = simulate(Plan(1, 0, 90), o, h, l, c, 3, c0)["net_R"]
    b = simulate(Plan(1, 0, 90), o, h, l, c, 3, c1)["net_R"]
    assert b < a


def test_cttd_and_reverse_take_opposite_sides():
    idx = pd.date_range("2024-01-02 09:30", periods=390, freq="1min")
    rng = np.random.default_rng(0)
    p = 100 + np.cumsum(np.r_[np.zeros(30), np.full(150, 0.3), np.full(210, -0.3)] + rng.normal(0, 0.05, 390))
    df = pd.DataFrame({"open": p, "high": p + 0.2, "low": p - 0.2, "close": p + 0.01, "volume": 100}, index=idx)
    from abg.tradebook.futures.data import Session
    s = Session(idx[0].normalize(), df, 100, 101, 99, np.nan, np.nan, 50, 50, 0.3, 0)
    costs = Costs(min_stop_pts=0.5, max_stop_pts=100)
    p_ = {"bar_minutes": 3, "start": "09:45", "last_entry": "15:30", "hard_stop_pts": 100}
    a = cttd(s, p_, costs, 385)
    b = cttd(s, p_, costs, 385, reverse=True)
    assert len(a) == len(b) > 0
    assert all(x["side"] == -y["side"] for x, y in zip(a, b))
    assert sum(t["gross_R"] for t in a) > sum(t["gross_R"] for t in b)   # trending synthetic day favours CTTD


def test_crisis_mode_hysteresis():
    s = pd.Series([0, 0.6, 0.6, 0.4, 0.3, 0.2, 0.1, 0.6])
    m = crisis_mode(s, enter=0.5, exit=0.25, min_days=2).tolist()
    assert m == [False, True, True, True, True, False, False, True]


def test_haircut_never_flatters_losses():
    x = np.array([-1.0, 0.5, -0.5])
    assert np.allclose(_apply_haircut(x, 0.5), x)
    y = np.array([1.0, 0.0, 0.5])
    assert _apply_haircut(y, 0.5).mean() == pytest.approx(0.25)


def test_projection_runs():
    r = pd.Series(np.random.default_rng(1).normal(0.05, 1.0, 300))
    res = project_r(r, 100.0, n_sims=500)
    assert res["raw"]["252d"]["R_p50"] > res["haircut"]["252d"]["R_p50"]
