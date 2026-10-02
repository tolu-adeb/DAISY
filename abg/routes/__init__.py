"""Alert routing: turn a signal service's messages into per-account orders under your own rules.

Modelled on what Alerio does (parse an alert -> route -> per-account rules -> brackets -> orders, with a
dry-run mode, an execution replay and an activity log), plus the guards Alerio doesn't have and that
Oct 1 2026 showed we need:

* risk-based sizing   - contracts come from a $ risk budget and the stop distance, so a wide stop means
                        fewer contracts instead of a bigger loss (Alerio: fixed size cap only)
* pre-trade loss room - an entry whose stop could breach today's loss limit or the prop trailing
                        drawdown is cut down or skipped *before* it is placed (Alerio's DLL reacts after)
* max stop distance   - skip or tighten an alert whose stop is wider than you accept
* stale / chase guard - an alert that arrives late, or after price ran away from the entry, is skipped
* day rules           - stop after the first loss, blackout windows (TradingMind's 10:30-11:30 hole),
                        max trades, profit lock
* replay with P&L     - any length of exported history, with the $ result under your rules vs as-copied

Everything is dry-run: the engine returns order *plans*; nothing here talks to a futures broker.
"""
from .alerts import Alert, parse_alert, alert_from_alpha_event
from .engine import AccountState, Decision, RouteEngine
from .rules import Route, RouteRules, TrimRow, load_routes, save_routes

__all__ = ["Alert", "parse_alert", "alert_from_alpha_event", "AccountState", "Decision", "RouteEngine",
           "Route", "RouteRules", "TrimRow", "load_routes", "save_routes"]
