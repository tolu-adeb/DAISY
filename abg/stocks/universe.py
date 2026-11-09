"""Which symbols the stock desk follows.

Three layers, cheapest first:

* ``ETFS``     - the exchange-traded funds the books actually trade (Book A rotates them, Book E hides
                 in them) plus the risk gauges the stress flags read.  Fixed, small, always refreshed.
* ``SEED``     - a core of large US names, used until a real index list is loaded.  It is a seed, not
                 the S&P 500: it is hand-written, so treat any backtest run on it as survivorship-biased.
* the index    - the real constituent list, loaded into the store by ``abg stocks universe --refresh``
                 (FMP's constituent endpoint) or from a local file.  Once present it replaces ``SEED``.

The desk also keeps a ``wide`` tier: everything the store has bars for.  The paged sweep walks that
tier so names outside the index still get refreshed and can be flagged, without the daily pass having
to wait for them.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- ETFs the books need
SECTOR_ETFS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
BROAD_ETFS = ["SPY", "QQQ", "IWM", "DIA", "EFA", "EEM"]
DEFENSIVE_ETFS = ["IEF", "TLT", "SHY", "GLD", "BIL"]
CREDIT_ETFS = ["HYG", "LQD"]
INVERSE_ETFS = ["SH"]
GAUGES = ["^VIX", "^VIX3M"]

ETFS = BROAD_ETFS + SECTOR_ETFS + DEFENSIVE_ETFS + CREDIT_ETFS + INVERSE_ETFS

# Symbols the stress flags and regime engine read under the names the config uses.
# ^VIX is Yahoo's spelling; the store keeps whatever the provider returns, so the panel is built with
# an alias so `VIX` in configs/stocks.yaml finds it.
GAUGE_ALIAS = {"^VIX": "VIX", "^VIX3M": "VIX3M"}

# --------------------------------------------------------------------------- seed single-stock list
# A hand-written core of large, liquid US names. This exists so the desk produces signals before an
# index list is loaded. It is NOT the S&P 500 and it is NOT point-in-time: every name here survived to
# today, which is exactly the bias the rule book warns about. Load the real list before trusting a
# backtest. Kept alphabetical so diffs are readable.
SEED = [
    "AAPL", "ABBV", "ABNB", "ABT", "ACN", "ADBE", "ADI", "ADP", "AEP", "AFL", "AIG", "AJG", "AMAT",
    "AMD", "AMGN", "AMT", "AMZN", "ANET", "AON", "APD", "APH", "AVGO", "AXP", "AZO",
    "BA", "BAC", "BDX", "BK", "BKNG", "BLK", "BMY", "BRK-B", "BSX",
    "C", "CAT", "CB", "CDNS", "CHTR", "CI", "CL", "CMCSA", "CME", "CMG", "COF", "COP", "COST", "CRM",
    "CRWD", "CSCO", "CSX", "CTAS", "CVS", "CVX",
    "D", "DE", "DHR", "DIS", "DUK", "DXCM",
    "EA", "ECL", "EL", "ELV", "EMR", "EOG", "EQIX", "ETN", "EW", "EXC",
    "F", "FDX", "FI", "FTNT",
    "GD", "GE", "GILD", "GIS", "GM", "GOOG", "GOOGL", "GS",
    "HCA", "HD", "HON", "HUM",
    "IBM", "ICE", "IDXX", "INTC", "INTU", "ISRG", "ITW",
    "JNJ", "JPM",
    "KDP", "KHC", "KLAC", "KMB", "KO", "KR",
    "LEN", "LIN", "LLY", "LMT", "LOW", "LRCX", "LULU",
    "MA", "MAR", "MCD", "MCHP", "MCK", "MDLZ", "MDT", "MET", "META", "MMC", "MMM", "MNST", "MO",
    "MPC", "MRK", "MRNA", "MS", "MSFT", "MSI", "MU",
    "NEE", "NFLX", "NKE", "NOC", "NOW", "NSC", "NVDA", "NXPI",
    "ODFL", "OKE", "ORCL", "ORLY", "OXY",
    "PANW", "PAYX", "PCAR", "PEP", "PFE", "PG", "PGR", "PH", "PLD", "PM", "PNC", "PSA", "PSX", "PYPL",
    "QCOM",
    "REGN", "ROP", "ROST", "RTX",
    "SBUX", "SCHW", "SHW", "SLB", "SNPS", "SO", "SPG", "SPGI", "SRE", "STZ", "SYK", "SYY",
    "T", "TDG", "TEL", "TFC", "TGT", "TJX", "TMO", "TMUS", "TRV", "TSLA", "TT", "TXN",
    "UBER", "UNH", "UNP", "UPS", "URI", "USB",
    "V", "VLO", "VRTX", "VZ",
    "WDAY", "WELL", "WFC", "WM", "WMB", "WMT",
    "XOM", "YUM", "ZTS",
]

KV_INDEX = "universe_index"       # JSON {"name":..., "as_of":..., "source":..., "symbols":[...]}
KV_WIDE = "universe_wide"         # JSON [...] extra names to sweep beyond the index


@dataclass
class Universe:
    """The symbol tiers the desk works with."""
    index_name: str = "seed"
    as_of: str | None = None
    source: str = "builtin"
    stocks: list[str] = field(default_factory=lambda: list(SEED))
    etfs: list[str] = field(default_factory=lambda: list(ETFS))
    gauges: list[str] = field(default_factory=lambda: list(GAUGES))
    extra: list[str] = field(default_factory=list)

    # ---- tiers
    def core(self) -> list[str]:
        """Everything the daily pass needs: the books' own ETFs plus the single-stock index."""
        return _dedup(self.etfs + self.gauges + self.stocks)

    @staticmethod
    def stored(symbol: str) -> str:
        """The name a symbol is kept under in the bar store (gauges drop their caret)."""
        return GAUGE_ALIAS.get(symbol, symbol)

    def wide(self) -> list[str]:
        """Core plus anything else being followed, for the paged sweep."""
        return _dedup(self.core() + self.extra)

    def non_stock(self) -> list[str]:
        """Symbols that must never enter a single-stock book's universe."""
        return _dedup(self.etfs + self.gauges + [GAUGE_ALIAS.get(g, g) for g in self.gauges])

    def is_seed(self) -> bool:
        return self.source == "builtin"

    def describe(self) -> dict:
        return {"index": self.index_name, "as_of": self.as_of, "source": self.source,
                "stocks": len(self.stocks), "etfs": len(self.etfs), "gauges": len(self.gauges),
                "extra": len(self.extra), "core": len(self.core()), "wide": len(self.wide()),
                "seed": self.is_seed()}

    # ---- persistence
    @classmethod
    def load(cls, store) -> "Universe":
        u = cls()
        raw = store.kv_get(KV_INDEX)
        if raw:
            try:
                d = json.loads(raw)
                syms = [s.upper().strip() for s in d.get("symbols") or [] if s and s.strip()]
                if syms:
                    u.stocks = _dedup(syms)
                    u.index_name = d.get("name") or "index"
                    u.as_of = d.get("as_of")
                    u.source = d.get("source") or "store"
            except Exception as e:                      # a corrupt list must not stop the desk
                log.warning("universe: stored index list unreadable (%s); using the seed", e)
        raw = store.kv_get(KV_WIDE)
        if raw:
            try:
                u.extra = _dedup(s.upper().strip() for s in json.loads(raw) if s and s.strip())
            except Exception:
                u.extra = []
        return u

    def save_index(self, store, name: str, symbols: list[str], source: str, as_of: str | None = None) -> int:
        syms = _dedup(s.upper().strip() for s in symbols if s and s.strip())
        if len(syms) < 20:
            raise ValueError(f"refusing to store an index list of {len(syms)} symbols - that looks wrong")
        store.kv_set(KV_INDEX, json.dumps({"name": name, "as_of": as_of, "source": source, "symbols": syms}))
        self.stocks, self.index_name, self.source, self.as_of = syms, name, source, as_of
        return len(syms)

    def save_extra(self, store, symbols: list[str]) -> int:
        keep = _dedup(s.upper().strip() for s in symbols if s and s.strip())
        core = set(self.core())
        keep = [s for s in keep if s not in core]
        store.kv_set(KV_WIDE, json.dumps(keep))
        self.extra = keep
        return len(keep)


def _dedup(it) -> list[str]:
    seen, out = set(), []
    for x in it:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def read_symbol_file(path) -> list[str]:
    """One symbol per line, or a CSV whose first column is the symbol. '#' starts a comment."""
    from pathlib import Path
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        sym = line.split(",")[0].strip().upper()
        if sym and sym not in ("SYMBOL", "TICKER"):
            out.append(sym)
    return _dedup(out)
