"""Route and rule definitions (saved as JSON in ``ABG_DATA_DIR/routes.json``).

A *route* = one alert source -> one or more accounts, with one rule set.  Field names follow Alerio's
route editor where there is an equivalent, so settings can be copied across; the extra guards are
marked NEW.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

POINT_VALUE = {"MNQ": 2.0, "NQ": 20.0, "MES": 5.0, "ES": 50.0, "MYM": 0.5, "YM": 5.0, "M2K": 5.0, "RTY": 50.0,
               "MGC": 10.0, "GC": 100.0, "MCL": 100.0, "CL": 1000.0}


@dataclass
class TrimRow:
    at_r: float | None = None           # target as a multiple of the alert's risk ...
    at_pts: float | None = None         # ... or as points from entry
    pct: float = 0.5                    # share of the ORIGINAL position closed here
    sl_after: float | None = None       # after this fill: 0 = stop to break-even, N = lock N points, None = leave

    @classmethod
    def from_dict(cls, d: dict) -> "TrimRow":
        return cls(**{k: v for k, v in d.items() if k in {f.name for f in fields(cls)}})


@dataclass
class RouteRules:
    # ---- sizing
    sizing: str = "risk"                # fixed | risk  (NEW: risk = contracts from a $ budget and the stop distance)
    contracts: int = 1                  # fixed size, and the size used when sizing="risk" can't compute (no stop)
    max_contracts: int = 8              # hard cap per position (Alerio "size cap")
    risk_per_trade_usd: float = 200.0   # NEW: max $ a single trade may lose at its stop
    # ---- stops / brackets
    require_stop: bool = True           # no stop in the alert -> use default_stop_pts, or skip if that is 0
    default_stop_pts: float = 0.0
    max_stop_pts: float = 60.0          # NEW: a wider alert stop is skipped (or tightened, see stop_cap_mode)
    stop_cap_mode: str = "skip"         # skip | tighten
    alert_override: str = "merge"       # ignore | override | merge  (Alerio "Alert Override")
    trims: list[TrimRow] = field(default_factory=lambda: [TrimRow(at_r=0.7, pct=0.5, sl_after=0.0)])
    runner_target_r: float = 2.0        # used when the alert has no final target
    # ---- entry
    entry_type: str = "market"          # market | limit | smart (market inside the zone, else a limit at the optimal
                                        # price good for limit_expiry_min).  On TradingMind's Aug-Oct signals limits
                                        # missed the runners and filled the losers, so market + chase guard is the default
    limit_offset_ticks: int = 0
    limit_expiry_min: int = 10
    half_on_chase_pts: float = 0.0      # NEW: market entry this far past the optimal -> half size (0 = off)
    skip_reached_targets: bool = True   # NEW: drop alert targets the fill is already at/through (Oct 1: TP1 = fill)
    follow_management: bool = True      # act on "stop to BE", "move stop", "close" replies (Alerio allow_* were all off)
    stale_sec: int = 60                 # NEW: ignore entries older than this when they reach us
    max_chase_pts: float = 30.0         # NEW: skip a MARKET entry when price already ran this far past the entry
    max_chase_r: float = 0.6            # NEW: ... or this share of the risk, whichever is smaller
    # ---- session
    entry_windows: list[list[str]] = field(default_factory=lambda: [["09:30", "16:00"]])   # ET, new entries only
    blackout_windows: list[list[str]] = field(default_factory=lambda: [["10:30", "11:30"]])  # NEW (TradingMind's hole)
    exclude_events: list[str] = field(default_factory=lambda: ["CPI", "FOMC", "NFP"])     # Alerio "Entry Exclusions"
    event_before_min: int = 5
    event_after_min: int = 15
    auto_close: str = "15:55"           # flatten time ("" = off)
    # ---- day limits
    max_trades_per_day: int = 3
    stop_after_first_loss: bool = True  # NEW: the single most protective rule on TradingMind's own history
    daily_loss_limit_usd: float = 500.0  # Alerio DLL - but also checked BEFORE entry against the stop
    daily_profit_target_usd: float = 0.0  # stop taking entries once the day is up this much (0 = off)
    # ---- prop-firm room (NEW)
    trailing_drawdown_usd: float = 0.0  # e.g. Lucid's trailing max drawdown; entries must fit 90% of what's left
    # ---- symbols
    allowed_symbols: list[str] = field(default_factory=lambda: ["NQ", "MNQ"])
    contract_map: dict = field(default_factory=lambda: {"NQ": "MNQ"})   # trade MNQ off NQ alerts

    @classmethod
    def from_dict(cls, d: dict | None) -> "RouteRules":
        d = dict(d or {})
        names = {f.name for f in fields(cls)}
        if "trims" in d:
            d["trims"] = [TrimRow.from_dict(x) if isinstance(x, dict) else x for x in d["trims"]]
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Route:
    name: str
    source: str = "discord"             # discord | telegram | webhook | alpha
    channel: str = ""                   # channel id / name filter
    mode: str = "dry_run"               # disabled | dry_run | live   (live is refused until a broker adapter exists)
    accounts: list[str] = field(default_factory=lambda: ["lucid"])
    rules: RouteRules = field(default_factory=RouteRules)

    @classmethod
    def from_dict(cls, d: dict) -> "Route":
        d = dict(d)
        d["rules"] = RouteRules.from_dict(d.get("rules"))
        return cls(**{k: v for k, v in d.items() if k in {f.name for f in fields(cls)}})

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rules"] = self.rules.to_dict()
        return d


def routes_path(data_dir) -> Path:
    return Path(data_dir).expanduser() / "routes.json"


def load_routes(data_dir) -> dict[str, Route]:
    p = routes_path(data_dir)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {r["name"]: Route.from_dict(r) for r in raw.get("routes", [])}


def save_routes(data_dir, routes: dict[str, Route]) -> Path:
    p = routes_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"routes": [r.to_dict() for r in routes.values()]}, indent=1), encoding="utf-8")
    return p
