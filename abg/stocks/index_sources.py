"""Where the stock desk gets its index list - and the index's history.

Primary source: the ``fja05680/sp500`` dataset on GitHub (MIT licence, updated a few times a year): a
snapshot of S&P 500 membership on every change date since 1996. It needs no key, it is plain HTTPS to
``raw.githubusercontent.com``, and because it is dated it gives the bar store real point-in-time
membership - a stock is only eligible on the days it was actually in the index. That removes the
biggest survivorship effect in the backtests (buying 2015's Tesla because we know it got in in 2020).

What it cannot remove: companies that died. Free price providers don't carry delisted tickers, so a
name that went to zero simply has no bars, and backtests still never hold it. Reports keep saying so.

Fallback: FMP's constituent endpoint (current list only, and not on every plan) - see Sweeper.

Ticker renames: the dataset records a stock under the ticker it had at the time, while price providers
serve the whole history under today's ticker. Read naively, META would be "not in the index" until its
2022 rename and silently vanish from every backtest over 2013-2022. So known renames are merged into
the current ticker - but only when the data agrees: the old ticker must leave on the exact day the new
one joins. Every pair below was checked against the dataset; a pair the data doesn't confirm is ignored.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

SP500_HISTORY_URL = ("https://raw.githubusercontent.com/fja05680/sp500/master/"
                     "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv")
SOURCE_NAME = "github:fja05680/sp500"

# (old ticker, the ticker it became), applied in order so chains work (SYMC -> NLOK -> GEN).
RENAMES: list[tuple[str, str]] = [
    ("FB", "META"), ("UTX", "RTX"), ("ANTM", "ELV"), ("WLTW", "WTW"), ("ABC", "COR"),
    ("FISV", "FI"), ("FI", "FISV"),            # Fiserv: FISV -> FI (2023) -> FISV (2025)
    ("PKI", "RVTY"), ("RE", "EG"), ("FLT", "CPAY"), ("TMK", "GL"), ("COG", "CTRA"),
    ("SYMC", "NLOK"), ("NLOK", "GEN"), ("DISCA", "WBD"), ("MMC", "MRSH"), ("PEAK", "DOC"),
    ("HFC", "DINO"), ("FBHS", "FBIN"), ("PCLN", "BKNG"), ("ADS", "BFH"),
]


def norm(ticker: str) -> str:
    """Dataset tickers use a dot for share classes (BRK.B); providers and the store use a dash."""
    return ticker.strip().upper().replace(".", "-")


@dataclass
class IndexHistory:
    name: str
    as_of: str                                   # date of the newest snapshot
    current: list[str]                           # members on that date
    stints: dict[str, list[tuple[str, str | None]]]   # ticker -> [(start, end or None)]
    renamed: list[tuple[str, str]] = field(default_factory=list)
    snapshots: int = 0

    def membership_rows(self) -> list[tuple[str, str, str | None]]:
        return [(t, a, b) for t, ss in sorted(self.stints.items()) for a, b in ss]


def parse_history(text: str, name: str = "sp500") -> IndexHistory:
    """``date,tickers`` rows (tickers a comma list in one quoted field) -> members and stints."""
    rd = csv.reader(io.StringIO(text))
    header = next(rd, None)
    if not header or [h.strip().lower() for h in header[:2]] != ["date", "tickers"]:
        raise ValueError(f"unexpected header {header!r}; the dataset format may have changed")
    snaps: list[tuple[str, set[str]]] = []
    for row in rd:
        if len(row) < 2 or not row[0].strip():
            continue
        d = row[0].strip()[:10]
        members = {norm(t) for t in row[1].split(",") if t.strip()}
        snaps.append((d, members))
    if len(snaps) < 10:
        raise ValueError(f"only {len(snaps)} snapshots - that is not the full history")
    snaps.sort(key=lambda x: x[0])

    stints: dict[str, list[list]] = {}
    open_: dict[str, str] = {}
    for d, members in snaps:
        for t in members - set(open_):
            open_[t] = d
        for t in set(open_) - members:
            stints.setdefault(t, []).append([open_.pop(t), d])
    for t, start in open_.items():
        stints.setdefault(t, []).append([start, None])

    # Confirm every rename against the untouched data first, then fold each connected group into the
    # ticker that is live today. Doing it pair by pair breaks round trips: Fiserv went FISV -> FI -> FISV,
    # and merging the first hop would delete the ticker the second hop has to be checked against.
    last_members = snaps[-1][1]
    confirmed = []
    for old, new in RENAMES:
        o, n = stints.get(norm(old)), stints.get(norm(new))
        if o and n and any(a[1] is not None and a[1] == b[0] for a in o for b in n):
            confirmed.append((norm(old), norm(new)))
        elif o or n:
            log.debug("index: %s -> %s not confirmed by the data; left alone", old, new)
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            x = parent[x]
        return x
    for a, b in confirmed:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups: dict[str, list[str]] = {}
    for t in {t for pair in confirmed for t in pair}:
        groups.setdefault(find(t), []).append(t)
    renamed = []
    for members in groups.values():
        live = [t for t in members if t in last_members]
        target = live[0] if live else next(new for _, new in reversed(confirmed) if new in members)
        merged: list[list] = []
        for t in members:
            merged += stints.pop(t, [])
        stints[target] = _merge(merged)
        renamed += [(t, target) for t in sorted(members) if t != target]

    last_d, last_m = snaps[-1]
    current = sorted(last_m)
    if not 450 <= len(current) <= 520 and name == "sp500":
        raise ValueError(f"the newest snapshot has {len(current)} members - not an S&P 500 list")
    # every live member must have an open stint, or it would read as "never in the index"
    dangling = [t for t in current if not any(b is None for _, b in stints.get(t, []))]
    if dangling:
        raise ValueError(f"current members without an open membership stint: {dangling[:5]}")
    return IndexHistory(name, last_d, current,
                        {t: [(a, b) for a, b in _merge(ss)] for t, ss in stints.items()},
                        renamed, len(snaps))


def _merge(ss: list[list]) -> list[list]:
    """Sort stints and join any that touch (one ends the day the next starts)."""
    out: list[list] = []
    for a, b in sorted(([s[0], s[1]] for s in ss), key=lambda x: x[0]):
        if out and out[-1][1] is not None and out[-1][1] >= a:
            out[-1][1] = None if b is None else max(out[-1][1], b)
        elif out and out[-1][1] is None:
            continue                                  # already open-ended: nothing to add
        else:
            out.append([a, b])
    return out


async def fetch_sp500_history(http) -> IndexHistory:
    """Download and parse the dataset (about 5 MB). Raises with a plain reason on failure."""
    text = await http.get_text(SP500_HISTORY_URL, provider="github", timeout=90.0)
    return parse_history(text, "sp500")
