"""Instrument registry: what a symbol is and how its money works.

Stocks and ETFs move $1 per share per $1.  Futures don't: one NQ contract makes $20 per index
point, MNQ $2, ZN $1,000 per point (1/64 ticks), CL $1,000 per $1.  Sizing, P&L, stops in ticks and
prop-firm limits all need that, so every symbol resolves to an ``InstrumentSpec``.

Symbols are Yahoo-style (``NQ=F``, ``ZN=F``, ``BTC-USD``, ``EURUSD=X``, ``^TNX``) because that is what
the free data providers serve.  ``spec_for`` also accepts roots (``NQ``), dated contracts (``NQZ26``,
``NQZ2026``) and micro contracts (``MNQ``).
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str                 # data symbol (Yahoo style)
    root: str
    name: str
    asset_class: str            # stock | etf | future | bond_future | bond_etf | yield | crypto | fx | index | volatility
    group: str                  # equity index, rates, energy, metals, ags, fx, crypto, ...
    multiplier: float = 1.0     # $ per 1.0 price move per unit (contract / share / coin)
    tick: float = 0.01
    exchange: str = ""
    session: str = "us_equity"  # us_equity | cme_globex | crypto_24_7 | fx_24_5 | none
    micro: str | None = None    # smaller contract with the same exposure shape
    fractional: bool = False    # units can be fractional (crypto, fractional shares off by default)

    @property
    def tick_value(self) -> float:
        return self.tick * self.multiplier

    @property
    def is_future(self) -> bool:
        return self.asset_class in ("future", "bond_future")

    def to_dict(self) -> dict:
        return {**asdict(self), "tick_value": self.tick_value}


def _f(root, name, group, mult, tick, exch="CME", micro=None, cls="future"):
    return InstrumentSpec(f"{root}=F", root, name, cls, group, mult, tick, exch, "cme_globex", micro)


FUTURES: dict[str, InstrumentSpec] = {s.root: s for s in [
    # equity index
    _f("ES", "E-mini S&P 500", "equity index", 50, 0.25, micro="MES"),
    _f("MES", "Micro E-mini S&P 500", "equity index", 5, 0.25),
    _f("NQ", "E-mini Nasdaq-100", "equity index", 20, 0.25, micro="MNQ"),
    _f("MNQ", "Micro E-mini Nasdaq-100", "equity index", 2, 0.25),
    _f("YM", "E-mini Dow", "equity index", 5, 1.0, "CBOT", micro="MYM"),
    _f("MYM", "Micro E-mini Dow", "equity index", 0.5, 1.0, "CBOT"),
    _f("RTY", "E-mini Russell 2000", "equity index", 50, 0.1, micro="M2K"),
    _f("M2K", "Micro E-mini Russell 2000", "equity index", 5, 0.1),
    # rates (Treasury futures: price in points of par, ticks in 32nds / 64ths / 128ths)
    _f("ZT", "2-Year T-Note", "rates", 2000, 1 / 256, "CBOT", cls="bond_future"),
    _f("ZF", "5-Year T-Note", "rates", 1000, 1 / 128, "CBOT", cls="bond_future"),
    _f("ZN", "10-Year T-Note", "rates", 1000, 1 / 64, "CBOT", cls="bond_future"),
    _f("TN", "Ultra 10-Year T-Note", "rates", 1000, 1 / 64, "CBOT", cls="bond_future"),
    _f("ZB", "30-Year T-Bond", "rates", 1000, 1 / 32, "CBOT", cls="bond_future"),
    _f("UB", "Ultra T-Bond", "rates", 1000, 1 / 32, "CBOT", cls="bond_future"),
    # energy
    _f("CL", "Crude Oil (WTI)", "energy", 1000, 0.01, "NYMEX", micro="MCL"),
    _f("MCL", "Micro WTI Crude", "energy", 100, 0.01, "NYMEX"),
    _f("NG", "Natural Gas", "energy", 10000, 0.001, "NYMEX"),
    _f("RB", "RBOB Gasoline", "energy", 42000, 0.0001, "NYMEX"),
    _f("HO", "Heating Oil", "energy", 42000, 0.0001, "NYMEX"),
    # metals
    _f("GC", "Gold", "metals", 100, 0.1, "COMEX", micro="MGC"),
    _f("MGC", "Micro Gold", "metals", 10, 0.1, "COMEX"),
    _f("SI", "Silver", "metals", 5000, 0.005, "COMEX", micro="SIL"),
    _f("SIL", "Micro Silver (1,000 oz)", "metals", 1000, 0.005, "COMEX"),
    _f("HG", "Copper", "metals", 25000, 0.0005, "COMEX"),
    _f("PL", "Platinum", "metals", 50, 0.1, "NYMEX"),
    # agriculture (cents per bushel -> $0.01 * 5,000 bu = $50 per 1 cent)
    _f("ZC", "Corn", "ags", 50, 0.25, "CBOT"),
    _f("ZS", "Soybeans", "ags", 50, 0.25, "CBOT"),
    _f("ZW", "Wheat", "ags", 50, 0.25, "CBOT"),
    # currencies
    _f("6E", "Euro FX", "fx", 125000, 0.00005, micro="M6E"),
    _f("M6E", "Micro Euro FX", "fx", 12500, 0.0001),
    _f("6J", "Japanese Yen", "fx", 12500000, 0.0000005),
    _f("6B", "British Pound", "fx", 62500, 0.0001),
    _f("6A", "Australian Dollar", "fx", 100000, 0.00005),
    _f("6C", "Canadian Dollar", "fx", 100000, 0.00005),
    # crypto (CME)
    _f("BTC", "Bitcoin (CME)", "crypto", 5, 5.0, micro="MBT"),
    _f("MBT", "Micro Bitcoin (CME)", "crypto", 0.1, 5.0),
    _f("ETH", "Ether (CME)", "crypto", 50, 0.5, micro="MET"),
]}
# Yahoo aliases that differ from the CME root
YAHOO_ALIASES = {"SIL=F": "SIL", "MBT=F": "MBT", "M6E=F": "M6E"}

OTHER: dict[str, InstrumentSpec] = {s.symbol: s for s in [
    InstrumentSpec("^TNX", "TNX", "10-Year Treasury yield", "yield", "rates", 1, 0.001, "CBOE", "none"),
    InstrumentSpec("^FVX", "FVX", "5-Year Treasury yield", "yield", "rates", 1, 0.001, "CBOE", "none"),
    InstrumentSpec("^TYX", "TYX", "30-Year Treasury yield", "yield", "rates", 1, 0.001, "CBOE", "none"),
    InstrumentSpec("^IRX", "IRX", "13-Week T-Bill yield", "yield", "rates", 1, 0.001, "CBOE", "none"),
    InstrumentSpec("^VIX", "VIX", "CBOE Volatility Index", "volatility", "volatility", 1, 0.01, "CBOE", "none"),
    InstrumentSpec("^GSPC", "SPX", "S&P 500 Index", "index", "equity index", 1, 0.01, "", "none"),
    InstrumentSpec("^NDX", "NDX", "Nasdaq-100 Index", "index", "equity index", 1, 0.01, "", "none"),
    InstrumentSpec("DX-Y.NYB", "DXY", "US Dollar Index", "index", "fx", 1, 0.005, "ICE", "fx_24_5"),
]}
BOND_ETFS = {"SHY": "1–3Y Treasuries", "IEI": "3–7Y Treasuries", "IEF": "7–10Y Treasuries", "TLT": "20Y+ Treasuries",
             "GOVT": "All Treasuries", "TIP": "TIPS", "AGG": "US Aggregate", "BND": "Total Bond", "LQD": "IG Corporates",
             "HYG": "High Yield", "JNK": "High Yield", "EMB": "EM Bonds", "MUB": "Municipals", "BIL": "1–3M T-Bills",
             "SGOV": "0–3M T-Bills", "VGIT": "Intermediate Treasuries", "VGLT": "Long Treasuries", "ZROZ": "25Y+ STRIPS"}
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "LTC", "DOT", "BNB", "MATIC", "SHIB", "TRX"}
FX_PAIRS = {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "EURJPY", "GBPJPY", "EURGBP"}
MONTH_CODES = "FGHJKMNQUVXZ"
ETFS = {"SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC",
        "SMH", "SOXX", "GLD", "SLV", "USO", "UNG", "ARKK", "TQQQ", "SQQQ", "SPXL", "UVXY", "VXX", "EEM", "EFA", "VTI",
        "VOO", "IVV", "KRE", "XBI", "GDX", "IBIT"} | set(BOND_ETFS)


# roots that are also common stock tickers (Colgate CL, Zscaler ZS, Planet Labs PL …): futures only with a hint
AMBIGUOUS_ROOTS = {"CL", "GC", "SI", "HG", "NG", "PL", "HO", "RB", "ZC", "ZS", "ZW", "SIL", "BTC", "ETH", "6A"}


def canonical(symbol: str, futures_hint: bool = False) -> str:
    """User / signal spelling -> data symbol.  NQ, NQZ26, /NQ, NQ1! -> NQ=F; BTC -> BTC-USD; EURUSD -> EURUSD=X.
    Roots that double as stock tickers (CL, ZS, GC …) map to futures only with ``futures_hint`` or a /, 1! or
    month-code spelling."""
    raw = symbol.strip().upper()
    futures_hint = futures_hint or raw.startswith("/") or raw.endswith("1!")
    s = raw.lstrip("/").replace("1!", "")
    if futures_hint and s in FUTURES:
        return f"{s}=F"
    if s in YAHOO_ALIASES or s.endswith("=F") or s.endswith("=X") or s.startswith("^") or s.endswith("-USD"):
        return s
    m = re.fullmatch(r"([A-Z0-9]{1,3})([" + MONTH_CODES + r"])(\d{1,4})", s)
    if m and m.group(1) in FUTURES:
        return f"{m.group(1)}=F"
    if s in CRYPTO:                              # "BTC long" means spot; CME contracts are BTC=F / MBT
        return f"{s}-USD"
    if s in FUTURES and s not in ETFS and s not in AMBIGUOUS_ROOTS:
        return f"{s}=F"
    if s in FX_PAIRS:
        return f"{s}=X"
    return s


def spec_for(symbol: str, futures_hint: bool = False) -> InstrumentSpec:
    s = canonical(symbol, futures_hint)
    if s in OTHER:
        return OTHER[s]
    if s in YAHOO_ALIASES:
        return FUTURES[YAHOO_ALIASES[s]]
    if s.endswith("=F") and s[:-2] in FUTURES:
        return FUTURES[s[:-2]]
    if s.endswith("=F"):
        return InstrumentSpec(s, s[:-2], s[:-2] + " futures", "future", "other", 1, 0.01, "", "cme_globex")
    if s.endswith("-USD"):
        return InstrumentSpec(s, s[:-4], s[:-4] + " / USD", "crypto", "crypto", 1, 0.01, "", "crypto_24_7", fractional=True)
    if s.endswith("=X"):
        return InstrumentSpec(s, s[:-2], s[:-2], "fx", "fx", 1, 0.0001, "", "fx_24_5", fractional=True)
    if s.startswith("^"):
        return InstrumentSpec(s, s[1:], s[1:], "index", "index", 1, 0.01, "", "none")
    if s in BOND_ETFS:
        return InstrumentSpec(s, s, BOND_ETFS[s], "bond_etf", "rates", 1, 0.01, "", "us_equity")
    return InstrumentSpec(s, s, s, "etf" if s in ETFS else "stock", "equity", 1, 0.01, "", "us_equity")


def round_to_tick(price: float, spec: InstrumentSpec) -> float:
    if not spec.tick:
        return price
    return round(round(price / spec.tick) * spec.tick, 10)


def size_position(spec: InstrumentSpec, risk_budget: float, entry: float, stop: float,
                  max_units: float | None = None) -> dict:
    """Units (shares / contracts / coins) for a $ risk budget.  Futures size in whole contracts and
    suggest the micro contract when even one full contract would exceed the budget."""
    per_unit = abs(entry - stop) * spec.multiplier
    out = {"units": 0.0, "risk_per_unit": per_unit, "risk": 0.0, "note": None, "symbol": spec.symbol}
    if per_unit <= 0 or risk_budget <= 0:
        return out
    raw = risk_budget / per_unit
    units = raw if spec.fractional else float(math.floor(raw))
    if spec.fractional:
        units = round(units, 6)
    if max_units is not None:
        units = min(units, max_units)
    if units < 1 and not spec.fractional:
        if spec.is_future and spec.micro:
            m = FUTURES[spec.micro]
            mu = math.floor(risk_budget / (abs(entry - stop) * m.multiplier))
            out["note"] = (f"1 {spec.root} risks ${per_unit:,.0f}, above the ${risk_budget:,.0f} budget; "
                           + (f"{mu} {m.root} fits instead" if mu >= 1 else f"even 1 {m.root} is too large"))
        else:
            out["note"] = f"one unit risks ${per_unit:,.0f}, above the ${risk_budget:,.0f} budget"
        units = 0.0
    out["units"] = units
    out["risk"] = units * per_unit
    return out


# --------------------------------------------------------------------------- sessions
def is_session_open(spec: InstrumentSpec, now: datetime | None = None) -> bool:
    """Rough trading-session check (exchange holidays for futures/FX are not modelled)."""
    from ..live.market_hours import is_market_open
    now = now or datetime.now(NY)
    if spec.session == "crypto_24_7":
        return True
    if spec.session == "us_equity":
        return is_market_open(now)
    if spec.session == "cme_globex":             # Sun 17:00 CT -> Fri 16:00 CT, daily halt 16:00-17:00 CT
        c = now.astimezone(CT)
        wd, t = c.weekday(), c.time()
        if wd == 5 or (wd == 4 and t >= time(16)) or (wd == 6 and t < time(17)):
            return False
        return not (time(16) <= t < time(17))
    if spec.session == "fx_24_5":                # Sun 17:00 ET -> Fri 17:00 ET
        n = now.astimezone(NY)
        wd, t = n.weekday(), n.time()
        return not (wd == 5 or (wd == 4 and t >= time(17)) or (wd == 6 and t < time(17)))
    return True


def futures_roll_note(spec: InstrumentSpec, now: datetime | None = None) -> str | None:
    """Quarterly equity-index / rates contracts roll ~8 days before the 3rd Friday of Mar/Jun/Sep/Dec."""
    if spec.group not in ("equity index", "rates") or not spec.is_future:
        return None
    now = (now or datetime.now(NY)).astimezone(NY)
    if now.month not in (3, 6, 9, 12):
        return None
    d = datetime(now.year, now.month, 1, tzinfo=NY)
    third_fri = d + timedelta(days=(4 - d.weekday()) % 7 + 14)
    roll = third_fri - timedelta(days=8)
    if roll.date() - timedelta(days=5) <= now.date() <= third_fri.date():
        return (f"contract roll week: front month expires {third_fri:%b %d}; the continuous {spec.symbol} series "
                f"can jump at the roll")
    return None
