"""The Strategy Lab's search space: which ``AlphaParams`` a hypothesis may change, and to what.

A hypothesis is a small, named set of changes to the current settings, with the reason it might work.  The
space is a whitelist so an agent can't wander into settings that don't mean anything, and loss controls
(``SAFETY_KEYS``) are not in it at all - the lab can never loosen them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ...alpha.strategy import SAFETY_KEYS, AlphaParams


@dataclass(frozen=True)
class Knob:
    name: str
    kind: str                      # float | int | bool | choice | time | setups
    help: str
    lo: float | None = None
    hi: float | None = None
    step: float | None = None      # neighbour step for the stability check (numbers only)
    choices: tuple = field(default_factory=tuple)

    def coerce(self, v: Any) -> Any:
        k = self.kind
        if k == "bool":
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes", "on")
            return bool(v)
        if k in ("float", "int"):
            x = float(v)
            if self.lo is not None and x < self.lo or self.hi is not None and x > self.hi:
                raise ValueError(f"{self.name}={v} outside [{self.lo}, {self.hi}]")
            return int(round(x)) if k == "int" else round(x, 4)
        if k == "choice":
            s = "" if v is None else str(v).strip()
            if s not in self.choices:
                raise ValueError(f"{self.name}={v!r} not one of {self.choices}")
            return s
        if k == "time":
            s = "" if v is None else str(v).strip()
            if s == "":
                return s
            h, m = s.split(":")
            if not (9 <= int(h) <= 15 and 0 <= int(m) < 60):
                raise ValueError(f"{self.name}={v!r} must be an ET session time HH:MM")
            return f"{int(h):02d}:{int(m):02d}"
        if k == "setups":
            items = [x.strip() for x in (v.split(",") if isinstance(v, str) else v)]
            bad = [x for x in items if x not in ("orx", "orb", "sweep", "vwap")]
            if bad or not items:
                raise ValueError(f"setups must be a non-empty subset of orx, orb, sweep, vwap (got {v!r})")
            return tuple(dict.fromkeys(items))
        raise ValueError(f"unknown kind {k}")

    def neighbours(self, v: Any) -> list[Any]:
        if self.kind not in ("float", "int") or not self.step:
            return []
        out = []
        for x in (v - self.step, v + self.step):
            if (self.lo is None or x >= self.lo) and (self.hi is None or x <= self.hi):
                out.append(self.coerce(x))
        return [x for x in out if x != v]


KNOBS: dict[str, Knob] = {k.name: k for k in [
    # trade management
    Knob("t1_r", "float", "Target 1 in R (half off, stop to break-even). Lower = higher win rate, smaller wins.", 0.3, 1.5, 0.1),
    Knob("scale_frac", "float", "Fraction banked at Target 1.", 0.25, 0.75, 0.25),
    Knob("be_mode", "choice", "When the stop goes to break-even: at T1 touch, at a close beyond T1, or lock 25% risk.",
         choices=("t1", "t1_close", "lock")),
    Knob("trail_atr", "float", "Runner trail distance in 5-min ATRs once +1.5R.", 0.75, 3.0, 0.25),
    Knob("orx_final_r", "float", "Fallback final target (R) for the opening-range trade.", 1.2, 4.0, 0.5),
    Knob("orx_final_mode", "choice", "Final target: nearest liquidity level or a fixed R.", choices=("liquidity", "fixed")),
    Knob("min_final_r", "float", "Closest a liquidity final target may be (R).", 1.2, 3.0, 0.3),
    Knob("max_final_r", "float", "Farthest a liquidity final target may be (R).", 2.0, 6.0, 0.5),
    Knob("orx_k", "float", "Opening-range-trade stop = this x the opening 5-min ATR.", 0.5, 1.4, 0.1),
    # timing / frequency
    Knob("or_minutes", "int", "Opening-range length in minutes.", 5, 30, 5),
    Knob("entry_end", "time", "No new entries at or after this time (ET)."),
    Knob("flat_time", "time", "Close any open trade at this time (ET)."),
    Knob("orx_last_entry", "time", "The opening-range trade must trigger before this time ('' = no limit)."),
    Knob("max_trades", "int", "Trades per day.", 1, 3, 1),
    Knob("setups", "setups", "Setups traded: orx (opening-range trend trade), orb (breakout-retest), sweep, vwap."),
    # trend rule
    Knob("orx_trend_len", "int", "Days in the trend average that picks the side.", 20, 200, 10),
    Knob("orx_leg", "choice", "Trade only one leg of the trend rule ('' = both).", choices=("", "follow", "fade")),
    Knob("orx_grade_min", "choice", "'A' = only when the 10-, 20- and 50-day trends agree.", choices=("", "A")),
    # day filters (all setups)
    Knob("min_trend_atr", "float", "Skip the day when price is within this many daily ATRs of the trend average (0 = off).", 0.0, 3.0, 0.25),
    Knob("max_atr_ratio", "float", "Skip the day when daily ATR(10)/ATR(100) exceeds this (0 = off).", 0.0, 3.0, 0.1),
    # opening-range-trade filters
    Knob("orx_min_drive", "float", "Skip when the 9:30-9:45 drive toward the trade is below this many range heights (-1 = off).", -1.0, 0.5, 0.1),
    Knob("orx_max_ext", "float", "Skip when the break candle closes more than this x the range height past the edge (0 = off).", 0.0, 1.0, 0.05),
    Knob("orx_or_max_atr", "float", "Skip when the opening range is taller than this x daily ATR (0 = off).", 0.0, 1.5, 0.05),
    Knob("orx_or_min_atr", "float", "Skip when the opening range is shorter than this x daily ATR (0 = off).", 0.0, 0.6, 0.05),
    Knob("orx_vwap_confirm", "bool", "Only enter when the signal close is on the trade's side of VWAP."),
    Knob("orx_gap_mode", "choice", "Only trade when the 9:30 gap agrees ('with') or disagrees ('against').",
         choices=("", "with", "against")),
    Knob("trend_filter", "bool", "orb / sweep / vwap only with the daily trend."),
    Knob("min_score", "float", "Minimum entry score.", 40, 80, 5),
]}

assert not set(KNOBS) & set(SAFETY_KEYS), "loss controls must never be in the search space"
MAX_CHANGES = 4


class Hypothesis(BaseModel):
    """One idea to test: a name, why it might work, and at most four setting changes."""
    name: str = Field(..., min_length=3, max_length=80)
    rationale: str = Field(..., min_length=10, max_length=1200)
    changes: dict[str, Any] = Field(..., min_length=1)

    @field_validator("changes")
    @classmethod
    def _check(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > MAX_CHANGES:
            raise ValueError(f"at most {MAX_CHANGES} changes per hypothesis (got {len(v)})")
        out = {}
        for k, x in v.items():
            if k in SAFETY_KEYS:
                raise ValueError(f"{k} is a loss control - the lab can't change it")
            if k not in KNOBS:
                raise ValueError(f"{k} is not in the search space (see `abg lab space`)")
            out[k] = KNOBS[k].coerce(x)
        return out

    def apply(self, base: AlphaParams) -> AlphaParams:
        return AlphaParams.from_dict({**base.to_dict(), **{k: (list(v) if isinstance(v, tuple) else v)
                                                           for k, v in self.changes.items()}})

    def label(self) -> str:
        return ", ".join(f"{k}={','.join(v) if isinstance(v, tuple) else v}" for k, v in self.changes.items())


def describe_space(base: AlphaParams | None = None) -> list[dict]:
    base = base or AlphaParams()
    out = []
    for k in KNOBS.values():
        cur = getattr(base, k.name)
        out.append({"name": k.name, "kind": k.kind, "current": list(cur) if isinstance(cur, tuple) else cur,
                    "range": [k.lo, k.hi] if k.kind in ("float", "int") else None,
                    "choices": list(k.choices) or None, "help": k.help})
    return out
