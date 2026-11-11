"""One glossary for the dashboard tooltips and the memos (docs/22.6). Plain words, written once."""
from __future__ import annotations

TERMS: dict[str, str] = {
    "ATR": "average true range: the stock's typical daily move in dollars over 14 days",
    "R": "one unit of risk: entry minus stop. A +2R trade made twice what it risked",
    "RSI": "relative strength index, 0-100: low means recently sold hard, high means bought hard",
    "EMA / SMA": "exponential / simple moving average of the close",
    "Momentum (12-1)": "the return over the last twelve months, skipping the most recent one",
    "Regime": "the market's state from the benchmark's trend, volatility and choppiness",
    "Reward:risk": "distance to the first target divided by distance to the stop",
    "Conviction": "a 0-100 score built from named inputs (rule checks, valuation, regime, news); it scales the size, "
                  "it never creates a trade",
    "DAISY Rank": "1 to 5. 1 = the rule triggered with high conviction, 2 = triggered with medium conviction, "
                  "3 = hold or one step from a trigger, 4 = watching, 5 = avoid or exited",
    "Grades": "A to F for Momentum, Trend, Value, Quality and News/Sentiment. A dash means we do not have the data",
    "Entry zone": "a band around the last close (half an ATR each side). The tested entry is the next open",
    "Stop": "the price at which the plan exits. Touching it closes the pitch",
    "T1 / T2": "the first and the stretch price targets",
    "Tested / untested": "tested = the rule as it was back-tested. Untested = a refinement or flag we are still "
                         "measuring, always labelled and scored apart",
    "Eligible": "clears the price and liquidity floor, so the rules may pitch it. Every listed name is researched "
                "either way",
    "Too new": "under about a year of history: the tested rules cannot run on it yet, so it is watched, not pitched",
    "Pitch": "one idea with a plan. New = entering at the next open, Active = entered, Closed = hit a target, the "
             "stop, the time limit or a rule exit",
    "Theme shock": "an event that moves a whole group of related names at once",
    "Market mood": "risk-on, neutral or risk-off, counted from breadth, 52-week highs and lows, VIX, sector trends "
                   "and headlines",
    "Breadth": "the share of stocks above their 50- and 200-day averages",
    "DCF": "discounted cash flow: a company's value as the sum of its future cash flows in today's dollars",
    "Comps": "valuation by comparison with similar companies' multiples",
    "Forward P/E": "price divided by the next twelve months' expected earnings per share",
    "Beta": "how much the stock tends to move for each 1% move in the market",
    "Excess return": "the pitch's return minus the benchmark's over the same days",
    "Setup forming": "the partial bar would trigger a rule if the day closed here. Untested; it confirms only at the close",
    "Walk-forward": "testing a rule only on data that came after the data it was tuned on",
    "Base rate": "how the same rule has done across its replayed trades: how often it won, by how much, for how long",
}
MEMO = ("ATR", "R", "RSI", "EMA / SMA", "Momentum (12-1)", "Regime", "Reward:risk")
STATUS_WORDS = {
    "in_zone": "In the entry zone: still a buy at the plan",
    "above_zone": "Above the entry zone: don't chase",
    "below_zone": "Below the entry zone, still above the stop",
    "t1": "Reached the first target",
    "stopped": "Touched its stop: out",
    "missed": "Opened above the don't-chase line: skipped (untested refinement)",
    "gap_stop": "Opened at or below the stop: skipped",
    "no_quote": "No live quote right now",
    "new": "New pitch: enters at the next open",
    "active": "Active: entered, plan running",
    "closed": "Closed",
    "watching": "Watching: not triggered yet",
    "triggered": "Triggered: pitched at the last close",
    "in_play": "In play: the pitch is active",
    "exited": "Exited: the pitch closed",
    "dropped": "Dropped: nothing happened in time",
}


def memo_terms() -> list[tuple[str, str]]:
    return [(k, TERMS[k]) for k in MEMO]
