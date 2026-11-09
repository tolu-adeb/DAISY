"""Valuation for the pitch desk: earnings DCF, reverse DCF, peer comps and bear/base/bull (pure functions, no I/O).

The free data providers give per-share earnings, P/E, forward P/E, beta and margins but no cash-flow statements, so the
DCF discounts *earnings per share* (an owner-earnings proxy).  That is stated on every memo.  Every input is in the
returned dict so each number on a memo can be traced back.
"""
from __future__ import annotations

from statistics import median


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def discount_rate(beta: float | None, rf: float = 0.042, erp: float = 0.05) -> float:
    """CAPM: risk-free + beta x equity premium, beta clamped to 0.6-2.0 so one odd print can't swing the value."""
    b = _clip(float(beta), 0.6, 2.0) if beta not in (None, 0) else 1.0
    return rf + b * erp


def base_growth(eps: float | None, forward_eps: float | None, eps_growth_pct: float | None) -> tuple[float, str]:
    """First-year growth estimate and where it came from (clipped to -20%..+35%)."""
    if eps and forward_eps and eps > 0 and forward_eps > 0:
        return _clip(forward_eps / eps - 1, -0.20, 0.35), "forward EPS / trailing EPS"
    if eps_growth_pct is not None:
        return _clip(eps_growth_pct / 100, -0.20, 0.35), "trailing EPS growth (year on year)"
    return 0.06, "default 6% (no growth data)"


def dcf(cf0: float, g: float, r: float, tg: float = 0.025, years: int = 10) -> float | None:
    """Per-share value: growth fades in a straight line from ``g`` to ``tg`` over ``years``, then a Gordon terminal value."""
    if cf0 is None or cf0 <= 0 or r <= tg:
        return None
    pv, cf = 0.0, cf0
    for t in range(1, years + 1):
        gt = g + (tg - g) * (t - 1) / max(1, years - 1)
        cf *= 1 + gt
        pv += cf / (1 + r) ** t
    tv = cf * (1 + tg) / (r - tg)
    return pv + tv / (1 + r) ** years


def implied_growth(price: float, cf0: float, r: float, tg: float = 0.025, years: int = 10) -> float | None:
    """Reverse DCF: the first-year growth (fading to ``tg``) that makes the DCF equal today's price."""
    if not price or price <= 0 or not cf0 or cf0 <= 0 or r <= tg:
        return None
    lo, hi = -0.5, 1.5
    if (dcf(cf0, lo, r, tg, years) or 0) > price:
        return lo
    if (dcf(cf0, hi, r, tg, years) or 0) < price:
        return None                                  # priced beyond any plausible growth
    for _ in range(80):
        mid = (lo + hi) / 2
        if (dcf(cf0, mid, r, tg, years) or 0) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def scenarios(cf0: float, g: float, r: float, tg: float = 0.025) -> dict:
    """Bear / base / bull: growth x0.5 / x1 / x1.5 (bear also loses 3 points), discount rate +1% / 0 / -0.5%."""
    cases = {"bear": (g * 0.5 - 0.03, r + 0.01), "base": (g, r), "bull": (g * 1.5, max(tg + 0.01, r - 0.005))}
    # round the inputs first and value from the rounded ones, so the arithmetic a pitch prints
    # ("EPS growing 9.1%, discounted at 10.4%") reproduces the value exactly
    out = {}
    for k, (gg, rr) in cases.items():
        gg, rr = round(gg, 4), round(rr, 4)
        out[k] = {"growth": gg, "rate": rr, "value": dcf(cf0, gg, rr, tg)}
    return out


def comps(target: dict, peers: list[dict]) -> dict:
    """Peer multiples: median P/E and forward P/E of peers (positive multiples only, capped at 150) applied to the
    target's trailing and forward EPS."""
    def med(key):
        xs = [p[key] for p in peers if p.get(key) and 0 < p[key] < 150]
        return (median(xs), len(xs)) if xs else (None, 0)
    pe, n_pe = med("pe")
    fpe, n_fpe = med("forward_pe")
    eps, feps = target.get("eps"), target.get("forward_eps")
    # a peer row with no symbol is still a usable multiple, so don't let a missing key lose the comps
    out = {"peers": [p.get("symbol") or "?" for p in peers],
           "median_pe": pe, "median_forward_pe": fpe, "n_pe": n_pe,
           "n_forward_pe": n_fpe, "value_pe": pe * eps if pe and eps and eps > 0 else None,
           "value_forward_pe": fpe * feps if fpe and feps and feps > 0 else None}
    vals = [v for v in (out["value_pe"], out["value_forward_pe"]) if v]
    out["value"] = sum(vals) / len(vals) if vals else None
    return out


def value(fund: dict, price: float, peers: list[dict], rf: float = 0.042, erp: float = 0.05, tg: float = 0.025) -> dict:
    """Everything a memo needs, from one symbol's fundamentals + price + its peers' fundamentals."""
    eps = fund.get("eps")
    fpe = fund.get("forward_pe")
    feps = price / fpe if fpe and fpe > 0 else None
    g, g_src = base_growth(eps, feps, (fund.get("extra") or {}).get("eps_growth_ttm_yoy_pct"))
    r = discount_rate(fund.get("beta"), rf, erp)
    cf0 = eps if eps and eps > 0 else (feps / (1 + g) if feps else None)
    sc = scenarios(cf0, g, r, tg) if cf0 else {}
    base = (sc.get("base") or {}).get("value")
    cmp_ = comps({"eps": eps, "forward_eps": feps}, peers)
    blend = [v for v in (base, cmp_.get("value")) if v]
    fair = sum(blend) / len(blend) if blend else None
    return {
        "method": "earnings DCF (EPS as owner-earnings proxy; no cash-flow statements from free providers) + peer comps",
        "inputs": {"price": price, "eps": eps, "forward_eps": feps, "beta": fund.get("beta"), "growth": g,
                   "growth_source": g_src, "discount_rate": r, "risk_free": rf, "equity_premium": erp,
                   "terminal_growth": tg, "years": 10},
        "scenarios": sc, "implied_growth": implied_growth(price, cf0, r, tg) if cf0 else None, "comps": cmp_,
        "fair_value": fair, "upside": (fair / price - 1) if fair and price else None,
    }
