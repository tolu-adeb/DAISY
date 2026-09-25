"""Parse free-text trade ideas / signals into a structured plan.

Handles the formats signal groups actually post, e.g.::

    $NVDA swing long 🟢 entry zone 117.50-119, SL 112, TP1 130 TP2 138
    BUY AAPL @ 180 - 182 | Stop: 175 (daily close) | Targets: 190 / 195 / 200
    Short SPY below 505, stop 512, target 490, 480
    TSLA breakout over 252 -> 265 / 280, invalidation 244
    entries 101, 99.5, 98 on AMD, stop 94, pt 110
    AMD 170c 11/15 entry 3.20-3.50            (option: premium levels are flagged for review)
    NQ long 18250-18270 sl 18190 tp 18400     (futures -> NQ=F)

and follow-ups that update an existing idea::

    TP1 hit on NVDA, moving stop to breakeven  |  AAPL stopped out  |  closing TSLA here
    cancel the SPY short  |  NVDA: raise stop to 121  |  trim half AMD

How it works: known phrases are normalised ("take profit" -> tp, "stop loss" -> sl, "entry
zone" -> entry), dates / percentages / option strikes / holding periods are extracted and
removed, then a small tokenizer walks the text.  Role keywords (entry / stop / target) switch
the current role, and the numbers that follow are assigned to it; modifiers such as
"above", "below", "breakout" or "market" decide the entry trigger.  The parser is
deterministic; every result carries a ``confidence`` (0-1) and ``warnings``.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

STOPWORDS = {
    "LONG", "SHORT", "BUY", "SELL", "SL", "TP", "PT", "ENTRY", "ENTRIES", "STOP", "TARGET", "TARGETS", "ZONE", "CALL",
    "PUT", "CALLS", "PUTS", "AND", "THE", "NEW", "ALERT", "SWING", "IDEA", "USD", "ATH", "EMA", "SMA", "RSI", "MACD",
    "ETF", "IPO", "CEO", "EPS", "BE", "DAY", "WEEK", "NOW", "OR", "AT", "TO", "NFA", "DCA", "HOD", "LOD", "BTO", "STC",
    "STO", "IF", "ON", "IN", "OUT", "FOR", "OF", "A", "I", "MY", "WE", "RISK", "SIZE", "HALF", "FULL", "ADD", "TRIM",
    "EXIT", "CLOSE", "OPEN", "HIT", "UPDATE", "NOTE", "PLAN", "LEVEL", "LEVELS", "SUPPORT", "RESISTANCE", "BREAKOUT",
    "BREAKDOWN", "RR", "R", "TF", "D", "W", "H", "M", "PM", "AM", "EST", "ET", "GL", "LFG", "IMO", "FYI", "TA", "FA",
    "VWAP", "OI", "IV", "DTE", "ATR", "BB", "YOLO", "LOTTO", "PNL", "WATCH", "WATCHLIST", "SETUP", "TRADE", "TRADES",
    "NEXT", "WEEKLY", "DAILY", "HOURLY", "MOVE", "RAISE", "LOWER", "RUNNER", "RUNNERS", "PROFIT", "PROFITS", "LOSS",
    "INVALIDATION", "BELOW", "ABOVE", "OVER", "UNDER", "BREAK", "RETEST", "HOLD", "HOLDING", "CANCEL", "CANCELLED",
    "STOPPED", "FILLED", "FILL", "LIMIT", "MARKET", "MKT", "CMP", "IDEAS", "LOOKING", "GM", "GN", "TODAY", "TOMORROW",
    "OK", "NO", "YES", "US", "UK", "EU", "AI", "PT1", "PT2", "PT3", "TP1", "TP2", "TP3", "TP4", "T1", "T2", "T3", "DD",
}
FUTURES = {"ES", "NQ", "YM", "RTY", "MES", "MNQ", "MYM", "M2K", "CL", "MCL", "GC", "MGC", "SI", "NG", "ZB", "ZN"}
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "LTC", "DOT", "BNB"}
ETFS = {"SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV", "SMH", "SOXX", "TLT", "GLD", "SLV", "ARKK", "TQQQ", "SQQQ"}

PHRASES = [  # order matters: longest first
    (r"take[\s-]*profits?|profit[\s-]*targets?|price[\s-]*targets?", " tp "),
    (r"stop[\s-]*loss(?:es)?|s/l|stoploss", " sl "),
    (r"(?:entry|buy|sell|short|long)[\s-]*(?:zone|area|range|box)", " entry "),
    (r"scale[\s-]*in", " entry "), (r"break[\s-]*out(?:s)?", " breakout "), (r"break[\s-]*down(?:s)?", " breakdown "),
    (r"at[\s-]*(?:the[\s-]*)?market|market[\s-]*order|at[\s-]*current|at[\s-]*the[\s-]*open|market[\s-]*open", " market "),
]
ROLE_WORDS = {
    "entry": "entry", "entries": "entry", "enter": "entry", "zone": "entry", "buy": "entry", "buying": "entry", "bto": "entry",
    "long": "entry", "short": "entry", "shorting": "entry", "sell": "entry", "sto": "entry", "add": "entry", "adding": "entry",
    "accumulate": "entry", "between": "entry", "@": "entry", "fill": "entry", "in": None,
    "sl": "stop", "stop": "stop", "invalidation": "stop", "invalid": "stop", "invalidated": "stop", "risk": "stop",
    "tp": "target", "pt": "target", "target": "target", "targets": "target", "goal": "target", "goals": "target",
    "objective": "target", "objectives": "target", "exit": "target", "exits": "target",
}
UP_MOD = {"above", "over", "breakout", "reclaim", "reclaims", "through", "clears", "clear"}
DOWN_MOD = {"below", "under", "breakdown", "loses", "lose", "sub"}
MARKET_WORDS = {"market", "mkt", "cmp", "now", "here", "current"}
LONG_WORDS = {"long", "buy", "buying", "bto", "bullish", "calls", "call", "accumulate", "add", "adding"}
SHORT_WORDS = {"short", "shorting", "sto", "bearish", "puts", "put", "fade", "sell"}

NUMRE = r"\d{1,6}(?:,\d{3})*(?:\.\d+)?|\.\d+"


@dataclass
class ParsedSignal:
    kind: str = "idea"                      # idea | update | none
    symbol: str | None = None
    direction: str | None = None            # long | short
    entry_type: str | None = None           # zone | market | breakout_above | breakdown_below | limit_below | limit_above
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    stop_basis: str = "touch"               # touch | close
    targets: list[float] = field(default_factory=list)
    timeframe: str = "swing"
    horizon_days: int | None = None
    instrument: str = "stock"               # stock | etf | option | future | crypto
    option: str | None = None
    action: str | None = None               # updates: close | cancel | stop_hit | target_hit | move_stop | breakeven | trim
    new_stop: float | None = None
    fraction: float | None = None
    target_index: int | None = None
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)
    raw: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def trackable(self) -> bool:
        return self.kind == "idea" and bool(self.symbol and self.direction and self.entry_type)


def _f(x: str) -> float:
    return float(x.replace(",", ""))


def clean(text: str) -> str:
    t = text or ""
    t = t.replace("–", "-").replace("—", "-").replace("−", "-")
    t = re.sub(r"\s*(?:->|=>|→|➡|⇒)\s*", " tp ", t)                        # "entry 252 -> 265/280"
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️‍]", " ", t)     # emoji
    t = re.sub(r"[*_`~|#>]+", " ", t)                                        # markdown / separators
    return re.sub(r"\s+", " ", t).strip()


# --------------------------------------------------------------------------- symbol
def find_symbol(t: str) -> tuple[str | None, str]:
    m = re.search(r"\$([A-Za-z]{1,5}(?:[.\-][A-Za-z]{1,2})?)(?![A-Za-z])", t)
    if m:
        return _map_symbol(m.group(1).upper())
    m = re.search(r"\b(?:ticker|symbol)\s*[:=]\s*([A-Za-z]{1,5}(?:\.[A-Za-z])?)\b", t, re.I)
    if m:
        return _map_symbol(m.group(1).upper())
    for tok in re.findall(r"(?<![\w$.])([A-Z]{1,5}(?:\.[A-Z])?)(?![\w])", t):
        if tok in STOPWORDS or (len(tok) == 1 and tok not in {"F", "T", "C", "V", "X", "O", "K"}):
            continue
        return _map_symbol(tok)
    return None, "stock"


def _map_symbol(s: str) -> tuple[str, str]:
    if s in FUTURES:
        return f"{s}=F", "future"
    if s in CRYPTO:
        return f"{s}-USD", "crypto"
    return s, "etf" if s in ETFS else "stock"


# --------------------------------------------------------------------------- follow-ups
UPDATE_PATTERNS = [
    ("stop_hit", r"\b(stopped out|stop(?:ped)? (?:was )?hit|sl hit|hit (?:the |my |our )?(?:stop|sl))\b"),
    ("target_hit", r"\b(?:tp|pt|target)\s*(\d)?\s*(?:hit|reached|filled|done|smashed|achieved)\b|\bhit (?:tp|pt|target)\s*(\d)?\b"),
    ("breakeven", r"\b(?:stops?|sl)\s*(?:to|at|->|now)\s*(?:be|b/e|break\s*even|entry)\b|\bmov\w* (?:the |our |my )?(?:stops?|sl) "
                  r"(?:up )?to (?:be|b/e|break\s*even|entry)\b"),
    ("move_stop", r"\b(?:mov\w*|rais\w*|lower\w*|trail\w*|adjust\w*|new|tighten\w*)\s*(?:the |our |my )?(?:stops?|sl)\s*"
                  r"(?:up |down )?(?:to|at|->|:)?\s*\$?(" + NUMRE + ")"),
    ("cancel", r"\b(cancel(?:led|ing)?|invalidated|scratch(?:ed)?|no longer valid|not taking|void(?:ed)?|delete (?:this|the))\b"),
    ("close", r"\b(clos(?:e|ed|ing)|exit(?:ed|ing)?|sold (?:all|everything|the rest)|out of|flat (?:on|here)|"
              r"taking (?:it )?off|done with|cut(?:ting)?(?: it)?)\b"),
    ("trim", r"\b(trim(?:med|ming)?|take (?:some|partials?|half)|partials?|scale out|sold (?:half|some|a third|1/3))\b"),
]


def _update(t: str) -> ParsedSignal | None:
    low = t.lower()
    for action, pat in UPDATE_PATTERNS:
        m = re.search(pat, low)
        if not m:
            continue
        sym, inst = find_symbol(t)
        p = ParsedSignal(kind="update", action=action, symbol=sym, instrument=inst, raw=t, confidence=0.75 if sym else 0.5)
        if action == "move_stop":
            p.new_stop = _f(m.group(1))
        elif action == "target_hit":
            g = next((x for x in m.groups() if x), None)
            p.target_index = int(g) - 1 if g else None
            if re.search(dict(UPDATE_PATTERNS)["breakeven"], low):  # "TP1 hit, stop to BE"
                p.new_stop = None
                p.warnings.append("also moving stop to breakeven")
                p.fraction = -1.0          # marker: breakeven requested alongside
        elif action == "trim":
            p.fraction = 0.5 if "half" in low else 1 / 3 if ("third" in low or "1/3" in low) else 0.5
        if not sym:
            p.warnings.append("no ticker in the update; applies to the idea it replies to, if any")
        return p
    return None


# --------------------------------------------------------------------------- ideas
def parse(text: str) -> ParsedSignal:
    raw = text or ""
    t = clean(raw)
    p = ParsedSignal(raw=raw)
    low = " " + t.lower() + " "

    p.symbol, p.instrument = find_symbol(t)

    # ---- extract & strip things that look like prices but aren't levels
    hm = re.search(r"\b(\d{1,3})(?:\s*-\s*(\d{1,3}))?\s*(d|days?|w|wks?|weeks?|mos?|months?)\b", low)
    if hm:
        n = int(hm.group(2) or hm.group(1))
        p.horizon_days = n * {"d": 1, "w": 7, "m": 30}[hm.group(3)[0]]
        low = low.replace(hm.group(0), " ")
    om = re.search(r"\b(\d+(?:\.\d+)?)\s*(c|p|calls?|puts?)\b(?:\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?))?", low)
    if om:
        p.instrument, p.option = "option", om.group(0).strip()
        low = low.replace(om.group(0), " ")
    elif re.search(r"\b(calls?|puts?|options?|contracts?)\b", low):
        p.instrument, p.option = "option", "calls" if "call" in low else "puts"
    if p.option and not re.search(r"\b(long|short|buy|sell|bto|sto|bullish|bearish)\b", low):
        p.direction = "short" if re.search(r"\d\s*p\b|puts?", p.option) else "long"
    low = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", low)           # dates
    low = re.sub(r"\b\d{1,2}:\d{2}\b", " ", low)                           # times
    low = re.sub(r"[+-]?\d+(?:\.\d+)?\s*%", " ", low)                      # percentages
    low = re.sub(r"\b\d(?:\.\d+)?\s*r\b", " ", low)                        # "2R", "1.5 r"
    if p.symbol:
        base = p.symbol.split("=")[0].split("-")[0].lower()
        low = re.sub(rf"\$?\b{re.escape(base)}\b", " ", low)
    close_basis = bool(re.search(r"\b(daily|weekly|hourly|4h|1h)?\s*clos(?:e|ing)\s*(below|under|above|over)|on a (daily )?close|"
                                 r"\(daily close\)|\bclose basis\b", low))
    for pat, rep in PHRASES:
        low = re.sub(pat, rep, low)

    # ---- direction
    words = re.findall(r"[a-z]+", low)
    for w in ([] if p.direction else words):
        if w in LONG_WORDS:
            p.direction = "long"
            break
        if w in SHORT_WORDS:
            p.direction = "short"
            break

    # ---- tokenize & assign numbers to roles
    toks = re.findall(r"@|(?:tp|pt|t)\d\b|" + NUMRE + r"|[a-z]+|-|/|,", low)
    role, mod = None, None
    vals: dict[str, list[float]] = {"entry": [], "stop": [], "target": []}
    entry_mod, market = None, False
    for tok in toks:
        if re.fullmatch(r"(?:tp|pt|t)\d", tok):
            role, mod = "target", None
            continue
        if re.fullmatch(NUMRE, tok):
            if role:
                v = _f(tok)
                if role == "entry" and mod and entry_mod is None:
                    entry_mod = mod
                vals[role].append(v)
            continue
        if tok in ROLE_WORDS:
            new = ROLE_WORDS[tok]
            if new == "entry" and role in ("stop", "target") and vals[role]:
                role = "entry"
            elif new:
                if not (new == "stop" and tok == "risk" and not re.search(r"\brisk\s*(?:to|at|:)", low)):
                    role = new
            mod = None
            continue
        if tok in UP_MOD:
            mod = "up"
            if role is None:
                role = "entry"
        elif tok in DOWN_MOD:
            mod = "down"
            if role is None:
                role = "entry"
        elif tok in MARKET_WORDS and role in (None, "entry"):
            market = True

    # ---- entry
    ev = vals["entry"]
    if ev:
        lo, hi = min(ev[:4]), max(ev[:4])
        if len(ev) >= 2 and hi / lo - 1 > 0.25:                   # second number wasn't part of the zone
            lo = hi = ev[0]
        p.entry_low, p.entry_high = lo, hi
        if entry_mod == "up":
            p.entry_type = "breakout_above" if p.direction != "short" else "limit_above"
        elif entry_mod == "down":
            p.entry_type = "breakdown_below" if p.direction == "short" else "limit_below"
        else:
            p.entry_type = "zone"
        if p.entry_type in ("breakout_above", "breakdown_below", "limit_below", "limit_above"):
            p.entry_low = p.entry_high = ev[0]
    elif market or (p.symbol and p.direction and (vals["stop"] or vals["target"])):
        p.entry_type = "market"
        if not market:
            p.warnings.append("no entry price found; treating as a market entry at the current price")

    if not p.direction and p.entry_type:
        if p.entry_type in ("breakout_above", "limit_below"):
            p.direction = "long"
        elif p.entry_type in ("breakdown_below", "limit_above"):
            p.direction = "short"
        elif vals["target"] and p.entry_high:
            p.direction = "long" if vals["target"][0] > p.entry_high else "short"
        elif vals["stop"] and p.entry_high:
            p.direction = "long" if vals["stop"][0] < p.entry_low else "short"

    # ---- stop & targets
    if vals["stop"]:
        p.stop = vals["stop"][0]
        p.stop_basis = "close" if close_basis else "touch"
    tg = vals["target"]
    ref = p.entry_high if p.direction == "long" else p.entry_low
    if tg and p.direction:
        tg = sorted(set(tg), reverse=(p.direction == "short"))
        if ref:
            good = [x for x in tg if (x > ref if p.direction == "long" else x < ref)]
            if len(good) < len(tg):
                p.warnings.append("ignored target(s) on the wrong side of the entry")
            tg = good
    p.targets = tg[:5]

    # ---- timeframe
    if re.search(r"\b(scalp|0dte)\b", low):
        p.timeframe = "scalp"
    elif re.search(r"\b(day ?trade|intraday)\b", low):
        p.timeframe = "day"
    elif re.search(r"\b(long[\s-]?term|investment|position|leaps?)\b", low):
        p.timeframe = "position"

    # ---- decide idea vs update vs noise
    upd = _update(t)
    plan_like = bool(ev or market) and bool(p.stop or p.targets)
    if upd and not plan_like:
        return upd
    if not p.symbol or not p.direction or not p.entry_type:
        p.kind = "none"

    # ---- sanity & confidence
    if p.stop is not None and p.entry_low is not None and p.direction:
        wrong = (p.stop >= p.entry_low) if p.direction == "long" else (p.stop <= p.entry_high)
        if wrong:
            p.warnings.append("stop is on the wrong side of the entry; check the message")
    if p.kind == "idea":
        if p.stop is None:
            p.warnings.append("no stop given; a 2xATR stop will be assigned from live data")
        if not p.targets:
            p.warnings.append("no targets given; 2R and 3R targets will be assigned")
    if not p.symbol:
        p.warnings.append("no ticker found")
    score = (0.3 * bool(p.symbol) + 0.15 * bool(p.direction) + 0.2 * bool(ev or market) + 0.2 * bool(p.stop)
             + 0.15 * bool(p.targets))
    p.confidence = round(max(0.0, score - 0.15 * sum("wrong side" in w for w in p.warnings)), 2)
    return p


def looks_like_signal(text: str) -> bool:
    """Cheap pre-filter for chat channels: skip chatter before full parsing."""
    low = clean(text).lower()
    kw = re.search(r"\$[a-z]{1,5}\b|\b(entry|entries|sl|stop|tp\d?|pt\d?|targets?|long|short|buy|sell|zone|stopped|"
                   r"closing|closed|trim|breakeven|cancel|invalidated|exit)\b", low)
    return bool(kw) and bool(re.search(r"\d|stopped|clos|cancel|trim|breakeven|invalidated|exit", low))
