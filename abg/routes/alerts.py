"""Alert parsing: free text / Discord embeds -> one structured ``Alert``.

Deterministic keyword + number parsing (Alerio offers an AI parser and a regex parser; an LLM adds
latency and can hallucinate a price, so this stays rule-based and says how sure it is).  Understands
the TradingMind embed format ("SIGNAL VALIDATED #12 — SHORT", "Entry zone:", "Stop loss:", "Target 1:",
"Final target:", "Price now:") and plain trader text ("short NQ 30709 sl 30792 tp 30651 30543",
"TP1 hit, stop to BE", "trim half", "move stop to 30650", "flat", "cancel").
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d|,\d)|\d+(?:\.\d+)?(?!\d|,\d))"


def _n(s: str | None) -> float | None:
    return float(s.replace(",", "")) if s else None


@dataclass
class Alert:
    action: str                         # entry | trim | breakeven | move_stop | close | cancel | info | unknown
    side: int = 0                       # +1 long / -1 short (entries)
    symbol: str = "NQ"
    entry: float | None = None          # optimal / limit price
    entry_lo: float | None = None
    entry_hi: float | None = None
    stop: float | None = None
    targets: list[float] = field(default_factory=list)
    price: float | None = None          # market price quoted in the alert
    trim_frac: float | None = None
    new_stop: float | None = None
    pts: float | None = None            # P&L quoted in an update ("+40 pts")
    ts: datetime | None = None
    ref: str | None = None              # id of the signal this message replies to
    id: str | None = None
    num: int | None = None              # the service's own trade number
    source: str = ""
    raw: str = ""
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def risk_pts(self) -> float | None:
        if self.entry is None or self.stop is None or not self.side:
            return None
        return (self.entry - self.stop) * self.side

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts"] = self.ts.isoformat() if self.ts else None
        return d


_SIG = re.compile(r"SIGNAL VALIDATED\s*#?(\d+)?\s*[—–-]\s*(LONG|SHORT)", re.I)
_SIDE = re.compile(r"\b(buy|long|sell|short)\b", re.I)
_SYM = re.compile(r"\b(M?NQ|M?ES|M?YM|M?RTY|MGC|GC|CL|MCL)(?:[FGHJKMNQUVXZ]\d{1,2})?\b")
_ZONE = re.compile(rf"entry(?: zone)?\s*[:@]?\**\s*{NUM}\s*[–-]\s*{NUM}", re.I)
_OPT = re.compile(rf"optimal\s*\**\s*{NUM}", re.I)
_ENTRY = re.compile(rf"(?:entry|@|at|limit)\s*[:@]?\**\s*{NUM}", re.I)
_STOP = re.compile(rf"(?:stop(?: loss)?|sl)\b\s*[:@]?\**\s*{NUM}", re.I)
_TGT = re.compile(rf"(?:target\s*\d?|final target|tp\s*\d?|t\d)\b\s*[:@]?\**\s*{NUM}", re.I)
_TPLIST = re.compile(rf"\b(?:tp|targets?)\s*[:@]?\s*({NUM}(?:(?:\s*/\s*|,\s+| +){NUM})+)", re.I)
_AFTER_SIDE = re.compile(rf"\b(?:buy|long|sell|short)\s+(?:[A-Z]{{1,4}}\s+)?@?\s*{NUM}", re.I)
_PRICE = re.compile(rf"price now\s*[:@]?\**\s*{NUM}", re.I)
_PTS = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*(?:pts|points|handles)\b", re.I)
_MOVE = re.compile(rf"(?:move|moving|raise|lower|trail)\w*\s+(?:the\s+)?(?:stop|sl)\s+(?:to|@|at)\s*{NUM}|new stop\s*[:@]?\**\s*{NUM}", re.I)


def parse_alert(text: str, ts: datetime | None = None, default_symbol: str = "NQ", source: str = "",
                trim_on_pnl_update: bool = True, ref: str | None = None, id: str | None = None) -> Alert:
    raw = text or ""
    t = raw.replace("**", "").replace("__", "")
    low = t.lower()
    a = Alert("unknown", ts=ts, source=source, raw=raw[:2000], ref=ref, id=id)
    m = _SYM.search(t)
    a.symbol = m.group(1).upper() if m else default_symbol
    a.price = _n(m.group(1)) if (m := _PRICE.search(t)) else None
    pts = _PTS.search(t)
    a.pts = float(pts.group(1)) if pts else None

    # ---- management messages first (they often also contain words like "long")
    if re.search(r"\b(cancel(?:led|ed)?|invalidated|scratch the idea|no longer valid)\b", low):
        a.action, a.confidence = "cancel", 0.9
        return a
    if re.search(r"stopped out|stop(?:ped)? hit|final target hit|trade closed|close (?:it|the trade|all|position)|\bflat\b|exit (?:all|now)", low):
        a.action, a.confidence = "close", 0.9
        return a
    mv = _MOVE.search(t)
    if mv:
        a.action, a.new_stop, a.confidence = "move_stop", _n(mv.group(1) or mv.group(2)), 0.85
        return a
    if re.search(r"(?:stop|sl)\s*(?:to|@|at)?\s*(?:b/?e|break[- ]?even|entry)\b|\bb/?e\s+stop|\bbreakeven\b", low):
        a.action, a.confidence = "breakeven", 0.85
        if re.search(r"target 1 hit|tp ?1 hit|t1 hit|trim|bank|scale", low):
            a.trim_frac = 0.5
            a.notes.append("also a Target 1 trim")
        return a
    if re.search(r"target 1 hit|tp ?1 hit|t1 hit|\btrim(?:med|ming)?\b|take (?:some|partial)|bank(?:ed)? (?:half|some)|scal(?:e|ing) out|pay yourself", low):
        a.action, a.confidence = "trim", 0.85
        fr = re.search(r"(\d{1,3})\s*%", t)
        a.trim_frac = (int(fr.group(1)) / 100) if fr else (0.5 if "half" in low or "target 1" in low else 0.5)
        return a

    # ---- entries
    sig = _SIG.search(t)
    side_m = _SIDE.search(t)
    if sig or side_m:
        a.side = 1 if (sig.group(2) if sig else side_m.group(1)).lower() in ("buy", "long") else -1
        if sig and sig.group(1):
            a.num = int(sig.group(1))
        z = _ZONE.search(t)
        if z:
            lo, hi = sorted((_n(z.group(1)), _n(z.group(2))))
            a.entry_lo, a.entry_hi = lo, hi
        opt = _OPT.search(t)
        if opt:
            a.entry = _n(opt.group(1))
        elif z:
            a.entry = round((a.entry_lo + a.entry_hi) / 2 * 4) / 4
        else:
            e = _ENTRY.search(t) or _AFTER_SIDE.search(t)
            a.entry = _n(e.group(1)) if e else None
        s = _STOP.search(t)
        a.stop = _n(s.group(1)) if s else None
        tl = _TPLIST.search(t)
        if tl:
            a.targets = [_n(x) for x in re.findall(NUM, tl.group(1))]
        else:
            a.targets = [_n(x.group(1)) for x in _TGT.finditer(t)]
        a.targets = [x for x in a.targets if x is not None and x != a.stop]
        if a.entry is None and a.price is not None:
            a.entry = a.price
            a.notes.append("no entry price - using the quoted price")
        # sanity: stop and targets on the right sides
        if a.entry is not None and a.stop is not None and (a.entry - a.stop) * a.side <= 0:
            a.notes.append("stop is on the wrong side of the entry - ignored")
            a.stop = None
        if a.entry is not None:
            a.targets = sorted([x for x in a.targets if (x - a.entry) * a.side > 0], key=lambda x: (x - a.entry) * a.side)
        a.action = "entry"
        a.confidence = 0.5 + 0.2 * (a.entry is not None) + 0.2 * (a.stop is not None) + 0.1 * bool(a.targets)
        return a

    if a.pts is not None and trim_on_pnl_update and re.search(r"\+\s*\d", t):
        a.action, a.trim_frac, a.confidence = "trim", 0.25, 0.5
        a.notes.append("P&L update read as a small trim (Alerio's 'Trim P&L updates')")
        return a
    a.action = "info"
    return a


def alert_from_alpha_event(ev: dict, symbol: str = "MNQ") -> Alert | None:
    """The terminal's own MNQ bot events -> alerts (so a route can follow the bot in dry run)."""
    k, tr = ev.get("type"), ev.get("trade") or {}
    ts = ev.get("ts")
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            ts = None
    ref = f"alpha:{tr.get('id')}" if tr else None
    if k == "signal":
        return Alert("entry", side=int(tr["side"]), symbol=symbol, entry=tr["entry"], stop=tr["stop"],
                     targets=[tr["t1"], tr["final"]], price=ev.get("price"), ts=ts, id=ref, source="alpha", confidence=1.0)
    if k == "t1":
        return Alert("trim", symbol=symbol, trim_frac=0.5, ts=ts, ref=ref, source="alpha", confidence=1.0)
    if k in ("final", "stopped", "failed", "breakeven", "closed"):
        return Alert("close", symbol=symbol, ts=ts, ref=ref, source="alpha", confidence=1.0)
    if k == "stop_moved":
        return Alert("move_stop", symbol=symbol, new_stop=ev.get("new_stop"), ts=ts, ref=ref, source="alpha", confidence=1.0)
    return None
