"""Black-Scholes-Merton pricing, Greeks, implied volatility and option-chain analytics.

All functions are vectorised over numpy arrays so a full chain (hundreds of contracts)
is priced in well under a millisecond.

Units
-----
* ``T`` in years, ``sigma`` / ``r`` / ``q`` as decimals (0.25 = 25 %).
* vega  = price change per **1 vol point** (sigma + 0.01)
* theta = price change per **calendar day**
* rho   = price change per **1 % rate move**
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..models import OptionChain, OptionContract

try:
    from scipy.special import ndtr as _ndtr  # fast & accurate
except Exception:  # pragma: no cover - scipy optional
    _erf = np.vectorize(math.erf, otypes=[float])

    def _ndtr(x):
        return 0.5 * (1.0 + _erf(np.asarray(x, dtype=float) / math.sqrt(2.0)))

SQRT_2PI = math.sqrt(2 * math.pi)


def ncdf(x):
    return _ndtr(x)


def npdf(x):
    x = np.asarray(x, dtype=float)
    return np.exp(-0.5 * x * x) / SQRT_2PI


def _arr(*xs):
    return np.broadcast_arrays(*[np.asarray(x, dtype=float) for x in xs])


def _is_call(kind) -> np.ndarray:
    k = np.asarray(kind)
    if k.dtype == bool:
        return k
    return np.char.lower(k.astype(str)) == "call"


def d1_d2(S, K, T, r, sigma, q=0.0):
    S, K, T, r, sigma, q = _arr(S, K, T, r, sigma, q)
    with np.errstate(divide="ignore", invalid="ignore"):
        vt = sigma * np.sqrt(T)
        d1 = (np.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / vt
    return d1, d1 - vt


def bs_price(S, K, T, r, sigma, q=0.0, kind="call"):
    S, K, T, r, sigma, q = _arr(S, K, T, r, sigma, q)
    call = np.broadcast_to(_is_call(kind), S.shape)
    d1, d2 = d1_d2(S, K, T, r, sigma, q)
    dfq, dfr = np.exp(-q * T), np.exp(-r * T)
    c = S * dfq * ncdf(d1) - K * dfr * ncdf(d2)
    p = K * dfr * ncdf(-d2) - S * dfq * ncdf(-d1)
    out = np.where(call, c, p)
    intrinsic = np.where(call, np.maximum(S - K, 0), np.maximum(K - S, 0))
    return np.where((T <= 0) | (sigma <= 0), intrinsic, out)


def greeks(S, K, T, r, sigma, q=0.0, kind="call") -> dict[str, np.ndarray]:
    S, K, T, r, sigma, q = _arr(S, K, T, r, sigma, q)
    call = np.broadcast_to(_is_call(kind), S.shape)
    d1, d2 = d1_d2(S, K, T, r, sigma, q)
    dfq, dfr = np.exp(-q * T), np.exp(-r * T)
    pdf = npdf(d1)
    sqrtT = np.sqrt(T)
    with np.errstate(divide="ignore", invalid="ignore"):
        delta = np.where(call, dfq * ncdf(d1), dfq * (ncdf(d1) - 1))
        gamma = dfq * pdf / (S * sigma * sqrtT)
        vega = S * dfq * pdf * sqrtT / 100
        theta_common = -(S * dfq * pdf * sigma) / (2 * sqrtT)
        theta_c = theta_common - r * K * dfr * ncdf(d2) + q * S * dfq * ncdf(d1)
        theta_p = theta_common + r * K * dfr * ncdf(-d2) - q * S * dfq * ncdf(-d1)
        theta = np.where(call, theta_c, theta_p) / 365
        rho = np.where(call, K * T * dfr * ncdf(d2), -K * T * dfr * ncdf(-d2)) / 100
        prob_itm = np.where(call, ncdf(d2), ncdf(-d2))
    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta, "rho": rho, "prob_itm": prob_itm}


def implied_vol(price, S, K, T, r, q=0.0, kind="call", tol=1e-7, max_iter=60) -> np.ndarray:
    """Vectorised Newton-Raphson with a bisection safeguard (always converges when a
    solution exists).  Returns NaN when the price violates no-arbitrage bounds."""
    price, S, K, T, r, q = _arr(price, S, K, T, r, q)
    call = np.broadcast_to(_is_call(kind), S.shape)
    dfq, dfr = np.exp(-q * T), np.exp(-r * T)
    lower = np.where(call, np.maximum(S * dfq - K * dfr, 0), np.maximum(K * dfr - S * dfq, 0))
    upper = np.where(call, S * dfq, K * dfr)
    valid = (price > lower + 1e-10) & (price < upper) & (T > 0) & np.isfinite(price)
    lo = np.full(S.shape, 1e-4)
    hi = np.full(S.shape, 5.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        sig = np.clip(np.sqrt(2 * np.pi / np.maximum(T, 1e-8)) * price / S, 0.05, 2.0)   # Brenner-Subrahmanyam
    sig = np.where(np.isfinite(sig), sig, 0.3)
    for _ in range(max_iter):
        p = bs_price(S, K, T, r, sig, q, call)
        diff = p - price
        if np.all(np.abs(diff[valid]) < tol):
            break
        hi = np.where(diff > 0, sig, hi)
        lo = np.where(diff <= 0, sig, lo)
        d1, _ = d1_d2(S, K, T, r, sig, q)
        v = S * dfq * npdf(d1) * np.sqrt(T)
        with np.errstate(divide="ignore", invalid="ignore"):
            newton = sig - diff / v
        bad = ~np.isfinite(newton) | (newton <= lo) | (newton >= hi) | (v < 1e-8)
        sig = np.where(bad, 0.5 * (lo + hi), newton)
    return np.where(valid, sig, np.nan)


# --------------------------------------------------------------------------- chains
def year_fraction(expiry: date, today: date) -> float:
    return max((expiry - today).days, 0) / 365.0 + 1 / 730   # half-day floor keeps 0DTE finite


def third_fridays(today: date, n: int) -> list[date]:
    out, y, m = [], today.year, today.month
    while len(out) < n:
        d = date(y, m, 15)
        d += timedelta(days=(4 - d.weekday()) % 7)
        if d > today:
            out.append(d)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _strike_step(spot: float) -> float:
    raw = spot * 0.025
    for s in (0.5, 1, 2.5, 5, 10, 25, 50, 100):
        if raw <= s:
            return s
    return 100.0


def theoretical_chain(symbol: str, spot: float, sigma: float, r: float, q: float = 0.0,
                      today: date | None = None, n_expiries: int = 4, n_strikes: int = 21) -> OptionChain:
    """Model-generated chain (no market quotes) using realised vol with a mild smile.
    Used when no provider can serve options, so the Greeks view always works."""
    today = today or date.today()
    nxt_fri = today + timedelta(days=((4 - today.weekday()) % 7) or 7)
    exps = sorted({nxt_fri, *third_fridays(today, n_expiries)})[:n_expiries + 1]
    step = _strike_step(spot)
    atm = round(spot / step) * step
    strikes = np.array([atm + step * i for i in range(-(n_strikes // 2), n_strikes // 2 + 1)])
    strikes = strikes[strikes > 0]
    contracts = []
    for e in exps:
        T = year_fraction(e, today)
        m = np.log(strikes / spot)
        smile = sigma * (1 - 0.35 * m + 1.2 * m ** 2)          # put skew + convexity
        for kind in ("call", "put"):
            px = bs_price(spot, strikes, T, r, smile, q, kind)
            for K, pr, iv in zip(strikes, px, smile):
                contracts.append(OptionContract(expiry=e, strike=float(K), kind=kind, last=round(float(pr), 4),
                                                iv=float(iv)))
    return OptionChain(symbol=symbol, underlying_price=spot, source="model", expirations=exps,
                       contracts=contracts, model_generated=True)


def max_pain(df: pd.DataFrame) -> float | None:
    oi = df.dropna(subset=["open_interest"])
    if oi.empty or oi["open_interest"].sum() == 0:
        return None
    ks = np.sort(oi["strike"].unique())
    calls = oi[oi["kind"] == "call"]
    puts = oi[oi["kind"] == "put"]
    pain = [(np.maximum(0, x - calls["strike"]) * calls["open_interest"]).sum()
            + (np.maximum(0, puts["strike"] - x) * puts["open_interest"]).sum() for x in ks]
    return float(ks[int(np.argmin(pain))])


def analyze_chain(chain: OptionChain, r: float, q: float = 0.0, today: date | None = None,
                  expiry: date | None = None, strikes_each_side: int = 10) -> dict:
    today = today or date.today()
    df = chain.to_frame()
    if df.empty:
        return {"available": False}
    for c in ("strike", "bid", "ask", "last", "mid", "iv", "volume", "open_interest"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    S = chain.underlying_price
    exps = sorted(df["expiry"].unique())
    sel = expiry if expiry in exps else next((e for e in exps if (e - today).days >= 5), exps[0])
    d = df[df["expiry"] == sel].copy()
    T = year_fraction(sel, today)
    is_call = (d["kind"] == "call").to_numpy()
    price = d["mid"].astype(float).to_numpy()
    iv_calc = implied_vol(price, S, d["strike"].to_numpy(float), T, r, q, is_call) if not chain.model_generated \
        else np.full(len(d), np.nan)
    iv_prov = d["iv"].astype(float).to_numpy()
    # prefer our own IV from the mid; fall back to the vendor's if the mid is unusable
    iv = np.where(np.isfinite(iv_calc), iv_calc, np.where(iv_prov > 0.01, iv_prov, np.nan))
    d["iv_used"] = iv
    g = greeks(S, d["strike"].to_numpy(float), T, r, iv, q, is_call)
    for k, v in g.items():
        d[k] = v
    d["theo"] = bs_price(S, d["strike"].to_numpy(float), T, r, iv, q, is_call)
    d["moneyness"] = np.log(d["strike"] / S)
    d["breakeven"] = np.where(is_call, d["strike"] + d["mid"].fillna(d["theo"]), d["strike"] - d["mid"].fillna(d["theo"]))

    # ATM & summary ---------------------------------------------------------
    ks = np.sort(d["strike"].unique())
    atm_k = float(ks[np.argmin(np.abs(ks - S))])
    atm_iv = float(np.nanmean(d.loc[d["strike"] == atm_k, "iv_used"])) if len(d) else float("nan")
    em = S * atm_iv * math.sqrt(T) if np.isfinite(atm_iv) else None

    def near_delta(kind: str, target: float) -> float | None:
        x = d[(d["kind"] == kind) & d["delta"].notna() & d["iv_used"].notna()]
        if x.empty:
            return None
        return float(x.iloc[(x["delta"] - target).abs().argmin()]["iv_used"])

    p25, c25 = near_delta("put", -0.25), near_delta("call", 0.25)
    cv, pv = d.loc[d.kind == "call", "volume"].sum(), d.loc[d.kind == "put", "volume"].sum()
    co, po = d.loc[d.kind == "call", "open_interest"].sum(), d.loc[d.kind == "put", "open_interest"].sum()
    term = []
    for e in exps[:8]:
        de = df[df["expiry"] == e]
        near = de.iloc[np.argsort((de["strike"] - S).abs().to_numpy())[:2]]
        ivs = near["iv"].astype(float)
        term.append({"expiry": e, "days": (e - today).days, "atm_iv": float(ivs.mean()) if ivs.notna().any() else None})

    idx = np.searchsorted(ks, atm_k)
    keep = set(ks[max(0, idx - strikes_each_side): idx + strikes_each_side + 1])
    view = d[d["strike"].isin(keep)].sort_values(["strike", "kind"])
    cols = ["kind", "strike", "bid", "ask", "last", "mid", "theo", "iv_used", "delta", "gamma", "theta", "vega", "rho",
            "prob_itm", "volume", "open_interest", "breakeven"]
    return {
        "available": True, "source": chain.source, "model_generated": chain.model_generated,
        "underlying_price": S, "expirations": sorted(set(chain.expirations) | set(exps)), "selected_expiry": sel, "days_to_expiry": (sel - today).days,
        "summary": {
            "atm_strike": atm_k, "atm_iv": atm_iv if np.isfinite(atm_iv) else None,
            "expected_move": em, "expected_move_pct": em / S * 100 if em else None,
            "skew_25d": (p25 - c25) if (p25 is not None and c25 is not None) else None,
            "put_call_volume_ratio": float(pv / cv) if cv else None,
            "put_call_oi_ratio": float(po / co) if co else None,
            "max_pain": max_pain(d), "term_structure": term,
        },
        "contracts": view[cols].to_dict(orient="records"),
    }
