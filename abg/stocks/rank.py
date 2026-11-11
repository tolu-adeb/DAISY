"""DAISY Rank (1-5) and style grades (A-F) (docs/22.4). Pure functions; every input is a number we have.

The rank is a plain mapping from the rule's state and the conviction score, so anyone can check it:

    1  Strong buy   the rule triggered and conviction is High
    2  Buy          the rule triggered and conviction is Medium
    3  Hold / near  triggered with Low conviction, an open pitch we are holding, or a setup one step away
    4  Watch        on the watchlist but more than one step from a trigger, or a trim
    5  Avoid        a rule exit, a closed pitch, or a name the rules reject

Grades are A to F. Momentum, Trend and News/Sentiment are graded against the whole researched universe
(top fifth = A ... bottom fifth = F). Value and Quality use fixed bands, because fundamentals are only
fetched for a few names. A grade that cannot be computed is shown as "—", never guessed.
"""
from __future__ import annotations

NA = "—"
RANK_WORDS = {1: "Strong buy", 2: "Buy", 3: "Hold / near trigger", 4: "Watch", 5: "Avoid"}
GRADE_KEYS = ("momentum", "trend", "value", "quality", "sentiment")
GRADE_LABEL = {"momentum": "Momentum", "trend": "Trend", "value": "Value", "quality": "Quality",
               "sentiment": "News/Sentiment"}


def daisy_rank(rating: str | None, bucket: str | None = None, steps_away: int | None = None) -> int:
    """``steps_away``: for a name that has not triggered, how many of the rule's conditions are still
    missing (1 = one step away)."""
    r = (rating or "").upper()
    if r in ("BUY", "ADD"):
        return {"High": 1, "Medium": 2}.get(bucket or "", 3)
    if r == "HOLD":
        return 3
    if r == "TRIM":
        return 4
    if r in ("SELL", "AVOID"):
        return 5
    if steps_away is not None:
        return 3 if steps_away <= 1 else 4
    return 4


def grade_pct(p: float | None) -> str:
    """A percentile in [0, 1] (1 = best) as a letter."""
    if p is None or p != p:
        return NA
    return "A" if p >= 0.8 else "B" if p >= 0.6 else "C" if p >= 0.4 else "D" if p >= 0.2 else "F"


def grade_bands(x: float | None, bands: tuple[float, float, float, float], higher_is_better: bool = True) -> str:
    if x is None or x != x:
        return NA
    a, b, c, d = bands
    if higher_is_better:
        return "A" if x >= a else "B" if x >= b else "C" if x >= c else "D" if x >= d else "F"
    return "A" if x <= a else "B" if x <= b else "C" if x <= c else "D" if x <= d else "F"


def value_grade(forward_pe: float | None, peer_forward_pe: float | None) -> str:
    """Forward P/E against the sector peers' median: cheaper than peers grades higher."""
    if not forward_pe or not peer_forward_pe or forward_pe <= 0 or peer_forward_pe <= 0:
        return NA
    return grade_bands(forward_pe / peer_forward_pe, (0.80, 0.95, 1.10, 1.30), higher_is_better=False)


def quality_grade(operating_margin: float | None, net_margin: float | None = None) -> str:
    """Operating margin (net margin if that is all we have), as a fraction."""
    m = operating_margin if operating_margin is not None else net_margin
    if m is None:
        return NA
    m = m / 100.0 if abs(m) > 1.5 else m            # some providers send percent points
    return grade_bands(m, (0.25, 0.15, 0.08, 0.0))


def sentiment_grade(score: float | None, n: int | None, min_n: int = 3) -> str:
    """News sentiment in [-1, 1] over at least ``min_n`` articles."""
    if score is None or (n or 0) < min_n:
        return NA
    return grade_bands(score, (0.35, 0.10, -0.10, -0.35))


def grades(row: dict, fund: dict | None = None, peer_forward_pe: float | None = None) -> dict:
    """``row``: a screen row (``mom_pct``, ``trend_pct``, ``sent``, ``sent_n``). Returns the five letters."""
    f = fund or {}
    ex = f.get("extra") or {}
    return {"momentum": grade_pct(row.get("mom_pct")), "trend": grade_pct(row.get("trend_pct")),
            "value": value_grade(f.get("forward_pe"), peer_forward_pe),
            "quality": quality_grade(ex.get("operating_margin"), f.get("profit_margin")),
            "sentiment": sentiment_grade(row.get("sent"), row.get("sent_n"))}


def grade_line(g: dict | None) -> str:
    g = g or {}
    return " · ".join(f"{GRADE_LABEL[k][0] if k != 'sentiment' else 'N'} {g.get(k, NA)}" for k in GRADE_KEYS)
