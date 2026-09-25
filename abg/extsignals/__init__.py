"""External signals: parse trade ideas from messages / Discord channels, track them against live
market data, decide entries / exits, explain why, and relay the decisions back (docs/11)."""
from .lifecycle import FINAL_STATES, OPEN_STATES, Idea, Obs, step, summary_stats
from .parser import ParsedSignal, looks_like_signal, parse
from .store import ExtSignalStore
from .tracker import ExtSignalTracker

__all__ = ["ExtSignalStore", "ExtSignalTracker", "FINAL_STATES", "Idea", "OPEN_STATES", "Obs", "ParsedSignal",
           "looks_like_signal", "parse", "step", "summary_stats"]
