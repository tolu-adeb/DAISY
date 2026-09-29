"""Automatic signal-type classification (what kind of trade it is, never who sent it).

    pattern   Pullback · Breakout · Continuation · Reversal / base · Mean reversion · Momentum · Event · Breakdown
    basis     Technical · Fundamental · Hybrid           (which kind of evidence the post argues from)
    themes    Growth · Value · Quality / income · Turnaround · AI / semis …
    horizon   Day · Swing (days–weeks) · Position swing (weeks–months)
    entry     Limit on a dip · In the zone now · Reclaim from below · Stop-entry above · Market

The pattern comes from the post's "Setup:" label first, then the text, then the geometry of the
levels versus the live price (a long zone below the price is a pullback).  It's deterministic and
every label comes with the evidence that produced it.
"""
from __future__ import annotations

import re

PATTERNS: list[tuple[str, str]] = [
    ("Continuation", r"bull flag|bear flag|\bflag\b|pennant|continuation|cup (?:and|&) handle|ascending triangle|"
                     r"descending triangle|consolidation|high tight"),
    ("Breakout", r"break ?out|breaking out|new highs?|all[- ]time high|\bath\b|range break|breaks? above"),
    ("Breakdown", r"break ?down|breaks? below|lower highs?|distribution top"),
    ("Reversal / base", r"double bottom|triple bottom|inverse head|head (?:and|&) shoulders|reversal|bottom(?:ing)?\b|"
                        r"\bbase\b|basing|w[- ]pattern|broke the bearish structure|trend change"),
    ("Pullback", r"pull ?back|retrac|\bfib\b|fibonacci|0\.5 level|0\.618|golden pocket|buy the dip|\bdip\b|retest|"
                 r"support|buying zone|demand zone"),
    ("Mean reversion", r"mean reversion|oversold|overbought|revert|snap ?back|stretched|back to the mean"),
    ("Event", r"earnings play|into earnings|fda|pdufa|event[- ]driven"),
    ("Momentum", r"momentum|relative strength|surg(?:e|ed|ing)|rall(?:y|ied)|leader|strong trend|impulsive"),
]
FUND = re.compile(r"revenue|sales|\beps\b|earnings per share|margin|cash flow|guidance|outlook|valuation|p/?e\b|"
                  r"times (?:this year's |forward |trailing )?(?:adjusted )?earnings|dividend|buyback|repurchas|"
                  r"return on equity|\broe\b|balance sheet|debt|financials|profitab|analysts? expect|beat", re.I)
TECH = re.compile(r"\bfib|fibonacci|support|resistance|\bflag\b|breakout|trend|structure|impulsive|moving average|"
                  r"\b\d{2,3}[- ]?(?:day|dma|ema|sma)\b|\brsi\b|macd|pattern|double bottom|\bbase\b|retest|zone|chart|"
                  r"\bleg\b|reject|stalled|(?<![$\d])0\.(?:236|382|5|618|786)\b", re.I)
THEMES: list[tuple[str, str]] = [
    ("Growth", r"growing|growth|record|up \d+(?:\.\d+)?%|accelerat|beat .*estimates|raised guidance"),
    ("Value", r"only (?:about |around )?\d+(?:\.\d+)? times|\bcheap\b|undervalued|trades at a discount|low (?:p/?e|multiple)"),
    ("Quality / income", r"dividend|consecutive years|staples?|defensive|moat|cash flows?"),
    ("Turnaround", r"turnaround|restructur|overhaul|transition(?:ed)? to|recover(?:y|ing)"),
    ("AI / semis", r"\bai\b|data center|semiconductor|chips?\b|gpu"),
]


LEVEL_LINE = re.compile(r"(?im)^\s*[*_>•-]*\s*(?:buy(?:ing)? zone|entry(?: zone)?|entries|stop(?:[\s-]*loss)?|sl|"
                        r"take[\s-]*profits?|targets?|tp\s*\d?|pt\s*\d?|risk[\s-]*to[\s-]*reward|ticker|symbol)\s*[*_]*\s*:.*$")


def classify(text: str, *, setup: str | None = None, direction: str | None = None, entry_type: str | None = None,
             entry_low: float | None = None, entry_high: float | None = None, stop: float | None = None,
             price: float | None = None, timeframe: str | None = None) -> dict:
    low = LEVEL_LINE.sub(" ", text or "").lower()          # level labels ("Buying zone:") aren't evidence
    evidence: list[str] = []
    pattern = None
    if setup:
        for name, rx in PATTERNS:
            if re.search(rx, setup.lower()):
                pattern = name
                evidence.append(f"setup label '{setup}'")
                break
    if pattern is None:
        scores = {name: len(re.findall(rx, low)) for name, rx in PATTERNS}
        best = max(scores.items(), key=lambda kv: kv[1])
        if best[1]:
            pattern = best[0]
            evidence.append(f"{best[1]} '{best[0].lower()}' cue(s) in the text")
    # geometry vs the live price refines / fills the pattern
    below = above = inside = False
    if price and entry_low is not None and entry_high is not None:
        below, above = entry_high < price, entry_low > price
        inside = not below and not above
        if (direction == "long" and below or direction == "short" and above) and pattern in (None, "Momentum", "Breakout"):
            prior = pattern
            pattern = "Pullback"
            evidence.append("entry zone sits " + ("below" if direction == "long" else "above") + " the live price"
                            + (f" (after a {prior.lower()} move: buying the retest)" if prior else ""))
            if prior == "Momentum":
                evidence.append("momentum context")
    if entry_type in ("breakout_above", "breakdown_below") and pattern is None:
        pattern = "Breakout" if entry_type == "breakout_above" else "Breakdown"
    if direction == "short" and pattern in (None, "Pullback"):
        pattern = "Breakdown" if pattern is None else "Pullback (short)"
    pattern = pattern or "Discretionary"

    f, t = len(FUND.findall(low)), len(TECH.findall(low))
    if f >= 2 and t >= 2:
        basis = "Hybrid"
    elif f > t:
        basis = "Fundamental"
    elif t:
        basis = "Technical"
    else:
        basis = "Levels only"
    evidence.append(f"{f} fundamental / {t} technical references")
    themes = [name for name, rx in THEMES if re.search(rx, low)]
    if "momentum context" in evidence:
        evidence.remove("momentum context")
        themes.append("Momentum")

    stop_pct = None
    ref = entry_high if direction == "long" else entry_low
    if ref and stop:
        stop_pct = abs(ref - stop) / ref * 100
    if timeframe in ("scalp", "day"):
        horizon = "Day trade"
    elif timeframe == "position" or (stop_pct and stop_pct >= 12 and basis in ("Fundamental", "Hybrid")):
        horizon = "Position swing (weeks–months)"
    else:
        horizon = "Swing (days–weeks)"
    if stop_pct:
        evidence.append(f"stop {stop_pct:.1f}% from the entry edge")

    if entry_type == "market":
        entry_style = "Market (enter now)"
    elif entry_type in ("breakout_above", "breakdown_below"):
        entry_style = "Stop-entry on a break" if pattern in ("Breakout", "Breakdown", "Continuation") else "Reclaim of the zone"
    elif below and direction == "long":
        entry_style = "Limit on a dip into the zone"
    elif above and direction == "long":
        entry_style = "Wait for a reclaim of the zone from below"
    elif inside:
        entry_style = "In the zone now"
    else:
        entry_style = "Limit at the zone"
    side = "long" if direction != "short" else "short"
    label = f"{pattern} {side} · {basis}" + (f" ({', '.join(themes[:2])})" if themes else "")
    return {"pattern": pattern, "basis": basis, "themes": themes, "horizon": horizon, "entry_style": entry_style,
            "label": label, "evidence": evidence}
