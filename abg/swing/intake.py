"""Pasted text -> the swing desk's signal row.

Three readers, in order (the first that finds a complete plan wins; otherwise the most complete partial is returned
so the dashboard can show it in the form for you to finish):

1. **channel format** - the House of Traders layout the desk was built on ("TICKER | ...", "Entry Area: 55-60",
   "Target: 70", "Stop Loss: 51 (daily close)"): ``swingdesk.parse_export.extract_signal``.
2. **terminal parser** - the ABG terminal's Signals-tab parser (``mnq/swing/parser.py``, copied unchanged): free text
   such as ``$NVDA swing long entry zone 117.50-119, SL 112, TP1 130 TP2 138`` and labelled cards.
3. **intake agent** - prose the parsers can't read goes to the desk's Strands intake agent (Haiku), only when an
   Anthropic key is set and ``ABG_SWING_AI_INTAKE`` is on.  It is told never to invent a level.

Nothing here invents a stop or a target: a missing level comes back empty with a warning.
"""
from __future__ import annotations

import os
import re

ENTRY_TYPES = ("zone", "cmp", "close_above")
FIELDS = ("symbol", "direction", "entry_type", "entry_lo", "entry_hi", "target_lo", "target_hi", "stop", "stop_basis")


def _num(x):
    if x is None or x == "":
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None          # NaN -> None


def blank() -> dict:
    return {"symbol": None, "direction": "long", "entry_type": "zone", "entry_lo": None, "entry_hi": None,
            "target_lo": None, "target_hi": None, "stop": None, "stop_basis": "close", "levels_source": "stated", "note": ""}


def _from_channel(text: str) -> dict | None:
    from swingdesk.parse_export import extract_signal
    s = extract_signal({"id": None, "author": None, "ts": None, "text": text})
    if not s:
        return None
    low = text.lower()
    f = blank()
    f.update(symbol=s["ticker"], direction=s["direction"],
             entry_type="cmp" if s["cmp_entry"] else ("close_above" if "close above" in low else "zone"),
             stop_basis="close" if "close" in low.split("stop")[-1] else "intraday")
    if s["entry"]:
        f["entry_lo"], f["entry_hi"] = s["entry"]
    if s["target"]:
        f["target_lo"], f["target_hi"] = s["target"]
    if s["stop"]:
        f["stop"] = s["stop"][0]
    return f


def _from_terminal(text: str) -> tuple[dict | None, list[str]]:
    from abg.extsignals.parser import parse
    p = parse(text)
    if p.kind == "update":
        return None, [f"this reads as an update ({(p.action or '').replace('_', ' ')}{' on ' + p.symbol if p.symbol else ''}), "
                      "not a new idea - manage existing ideas from the Book"]
    if p.kind != "idea" or not p.symbol:
        return None, []
    f = blank()
    f["symbol"] = p.symbol.split("=")[0]
    f["direction"] = p.direction or "long"
    long = f["direction"] == "long"
    warn = [w for w in p.warnings if "will be assigned" not in w]
    et = p.entry_type
    if et == "zone":
        f["entry_lo"], f["entry_hi"] = p.entry_low, p.entry_high
    elif et == "market":
        f["entry_type"] = "cmp"
    elif et == "breakout_above" and long:
        f["entry_type"], f["entry_lo"], f["entry_hi"] = "close_above", p.entry_low, p.entry_low
        warn.append("breakout entry: the desk fills on the first daily close above it")
    else:                                   # limit_below / limit_above / breakdown_below -> a one-price zone
        f["entry_lo"] = f["entry_hi"] = p.entry_low
        if et == "breakdown_below":
            warn.append("breakdown entry treated as a limit at that price (the desk has no stop-entry for shorts)")
    if p.stop is not None:
        f["stop"] = p.stop
    f["stop_basis"] = "close" if p.stop_basis == "close" else "intraday"
    if p.targets:
        first, last = p.targets[0], p.targets[-1]
        f["target_lo"], f["target_hi"] = (first, last) if long else (last, first)
    if p.instrument not in ("stock", "etf"):
        warn.append(f"{p.instrument} idea: the desk's rules were fitted on stocks")
    if p.option:
        warn.append("option levels are premiums, not stock prices - enter the underlying's levels")
    meta = p.meta or {}
    f["note"] = "; ".join(x for x in (meta.get("setup"), meta.get("catalyst")) if x)[:200]
    return f, warn


def _from_agent(text: str, api_key: str) -> dict | None:
    if api_key and not os.getenv("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = api_key              # the desk's agents read it from the environment
    from swingdesk.agents.desk import parse_post
    p = parse_post(text)
    if not p or not p.is_trade_idea:
        return None
    f = blank()
    f.update(symbol=p.symbol, direction=p.direction, entry_type=p.entry_type, entry_lo=p.entry_lo,
             entry_hi=p.entry_hi if p.entry_hi is not None else p.entry_lo, target_lo=p.target_lo, target_hi=p.target_hi,
             stop=p.stop, stop_basis=p.stop_basis, levels_source=p.levels_source, note=(p.notes or "")[:200])
    return f


def missing(f: dict) -> list[str]:
    out = []
    if not f.get("symbol"):
        out.append("ticker")
    if f.get("entry_type") != "cmp" and _num(f.get("entry_lo")) is None:
        out.append("entry")
    if _num(f.get("target_lo")) is None and _num(f.get("target_hi")) is None:
        out.append("target")
    if _num(f.get("stop")) is None:
        out.append("stop")
    return out


def parse_text(text: str, api_key: str | None = None, use_ai: bool = True) -> dict:
    """-> {"fields": dict|None, "source": str|None, "warnings": [...], "missing": [...]}"""
    text = (text or "").strip()
    if not text:
        return {"fields": None, "source": None, "warnings": ["paste a trade idea first"], "missing": []}
    cands: list[tuple[str, dict, list[str]]] = []
    warns: list[str] = []
    try:
        f = _from_channel(text)
        if f:
            cands.append(("channel format", f, []))
    except Exception as e:                                       # a parser bug must not break the form
        warns.append(f"channel parser: {type(e).__name__}")
    if not cands or missing(cands[0][1]):
        try:
            f, w = _from_terminal(text)
            if f:
                cands.append(("terminal parser", f, w))
            else:
                warns += w
        except Exception as e:
            warns.append(f"terminal parser: {type(e).__name__}")
    best = min(cands, key=lambda c: len(missing(c[1]))) if cands else None
    if (best is None or missing(best[1])) and use_ai and api_key:
        try:
            f = _from_agent(text, api_key)
            if f and (best is None or len(missing(f)) < len(missing(best[1]))):
                best = ("intake agent", f, ["read by the intake agent - check every level against the post"])
        except Exception as e:
            warns.append(f"intake agent unavailable ({type(e).__name__})")
    if best is None:
        hint = "" if api_key else " (no Anthropic key, so the intake agent can't help with prose)"
        return {"fields": None, "source": None, "warnings": warns + [f"no ticker and levels found{hint}"], "missing": []}
    src, f, w = best
    f = normalize(f)
    m = missing(f)
    return {"fields": f, "source": src, "warnings": warns + w + ([f"missing: {', '.join(m)} - fill it in below"] if m else []),
            "missing": m}


def normalize(f: dict) -> dict:
    g = blank()
    g.update({k: v for k, v in (f or {}).items() if k in g})
    g["symbol"] = re.sub(r"[^A-Z0-9.\-]", "", str(g["symbol"] or "").upper().lstrip("$")) or None
    g["direction"] = "short" if str(g["direction"]).lower() == "short" else "long"
    g["entry_type"] = g["entry_type"] if g["entry_type"] in ENTRY_TYPES else "zone"
    g["stop_basis"] = "close" if str(g["stop_basis"]).lower() == "close" else "intraday"
    for k in ("entry_lo", "entry_hi", "target_lo", "target_hi", "stop"):
        g[k] = _num(g[k])
    if g["entry_type"] == "cmp":
        g["entry_lo"] = g["entry_hi"] = None
    elif g["entry_lo"] is not None and g["entry_hi"] is None:
        g["entry_hi"] = g["entry_lo"]
    if g["entry_lo"] is not None and g["entry_hi"] is not None and g["entry_lo"] > g["entry_hi"]:
        g["entry_lo"], g["entry_hi"] = g["entry_hi"], g["entry_lo"]
    if g["entry_type"] == "close_above":
        g["entry_hi"] = g["entry_lo"]
    if g["target_lo"] is None:
        g["target_lo"] = g["target_hi"]
    if g["target_hi"] is None:
        g["target_hi"] = g["target_lo"]
    if g["target_lo"] is not None and g["target_lo"] > g["target_hi"]:
        g["target_lo"], g["target_hi"] = g["target_hi"], g["target_lo"]
    g["note"] = str(g.get("note") or "")[:200]
    return g


def validate(f: dict, last: float | None = None) -> list[str]:
    """Errors that stop an idea being tracked (the gate decides take / skip; this only checks the plan is coherent)."""
    errs = [f"{x} is required" for x in missing(f)]
    if errs:
        return errs
    long = f["direction"] == "long"
    entry = (f["entry_hi"] if long else f["entry_lo"]) if f["entry_type"] == "zone" else \
        (f["entry_lo"] if f["entry_type"] == "close_above" else last)
    if f["entry_type"] == "close_above" and not long:
        errs.append("'daily close above' entries are long only")
    if entry is not None:
        if (f["stop"] >= entry) if long else (f["stop"] <= entry):
            errs.append(f"stop {f['stop']:g} is on the wrong side of the entry {entry:g}")
        t = f["target_lo"] if long else f["target_hi"]
        if (t <= entry) if long else (t >= entry):
            errs.append(f"target {t:g} is on the wrong side of the entry {entry:g}")
    return errs
