"""Runtime configuration.

All settings come from (highest priority first):
  1. keyword arguments passed to ``Settings(...)`` (used by tests and the CLI flags)
  2. environment variables prefixed ``ABG_`` (e.g. ``ABG_POLYGON_API_KEY``)
  3. a ``.env`` file in the working directory
  4. the defaults below

Nothing here is required: with zero configuration the terminal uses the free,
key-less sources (Yahoo, yfinance, Stooq) and local CSVs.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Order in which providers are tried for each capability.  Providers that are not
# configured (missing API key) or do not support a capability are skipped.
DEFAULT_PROVIDER_ORDER = "yahoo,polygon,tiingo,fmp,twelvedata,alphavantage,finnhub,yfinance,stooq,csv,synthetic"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ABG_", env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore", populate_by_name=True)

    # ---------------------------------------------------------------- API keys (all optional)
    alphavantage_api_key: str | None = None
    finnhub_api_key: str | None = None
    polygon_api_key: str | None = None
    tiingo_api_key: str | None = None
    fmp_api_key: str | None = None
    twelvedata_api_key: str | None = None
    anthropic_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("ABG_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "anthropic_api_key"))

    # ---------------------------------------------------------------- provider routing
    provider_order: str = DEFAULT_PROVIDER_ORDER
    # Optional per-capability overrides (priority + allow-list for that capability only), e.g.
    # ABG_QUOTE_ORDER=finnhub,twelvedata,yahoo  - lets real-time sources win for quotes while
    # long adjusted histories come from Tiingo.
    history_order: str = ""
    quote_order: str = ""
    news_order: str = ""
    options_order: str = ""
    fundamentals_order: str = ""
    disabled_providers: str = ""
    tiingo_news_enabled: bool = False            # Tiingo News API is a paid add-on (free keys get 403)
    polygon_base_url: str = "https://api.polygon.io"
    csv_dir: Path | None = None                 # folder of <SYMBOL>.csv files used by the csv provider
    allow_synthetic: bool = False               # demo mode: deterministic simulated data as last resort

    # ---------------------------------------------------------------- networking / resilience
    http_timeout: float = 8.0                   # total per-request timeout (s)
    connect_timeout: float = 4.0
    max_retries: int = 1                        # retries *within* one provider before failing over
    retry_base_delay: float = 0.25
    hedge_delay: float = 1.5                    # start the next provider if the current one is this slow (0 = off)
    max_connections: int = 32
    breaker_failures: int = 3                   # consecutive failures that open a provider's circuit
    breaker_cooldown: float = 60.0              # seconds before a half-open probe is allowed
    user_agent: str = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 ABGTerminal/3.0")

    # ---------------------------------------------------------------- cache (seconds)
    cache_enabled: bool = True
    cache_dir: Path = Field(default_factory=lambda: Path.home() / ".cache" / "abg-terminal")
    memory_cache_entries: int = 512
    ttl_quote: float = 15
    ttl_history_intraday: float = 60
    ttl_history_daily: float = 900
    ttl_news: float = 600
    ttl_options: float = 120
    ttl_fundamentals: float = 86_400
    ttl_ai: float = 3_600
    max_stale: float = 7 * 86_400               # serve stale cache up to this age when every provider fails

    # ---------------------------------------------------------------- analysis
    risk_free_rate: float = 0.04
    dividend_yield_default: float = 0.0
    benchmark: str = "SPY"
    warmup_days: int = 420                      # extra history fetched so SMA200 etc. are valid on day 1
    news_limit: int = 20

    # ---------------------------------------------------------------- AI insight
    ai_enabled: bool = True
    anthropic_model: str = "claude-sonnet-4-5"
    anthropic_base_url: str = "https://api.anthropic.com"
    ai_timeout: float = 30.0
    ai_max_tokens: int = 1200

    # ---------------------------------------------------------------- risk plug-ins
    risk_models: str = ""                       # extra models: "pkg.module:ClassName,other.mod:Factory"

    # ---------------------------------------------------------------- prediction (Monte Carlo)
    forecast_paths: int = 5000                  # simulated paths (2,500 GBM-t + 2,500 filtered historical)
    forecast_horizon: int = 63                  # primary horizon for the recommendation (trading days, ~3 months)
    forecast_equity_premium: float = 0.05       # long-run equity risk premium used in the drift
    forecast_signal_tilt: float = 0.25          # drift tilt at |signal| = 100, in units of annual vol

    # ---------------------------------------------------------------- saved portfolio
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".abg-terminal")   # portfolio.sqlite3 lives here
    default_portfolio: str = "main"

    # ---------------------------------------------------------------- live monitor
    monitor_on_serve: bool = True               # `abg serve` also runs the monitor (if no other monitor is running)
    monitor_quote_interval: float = 60          # seconds between live-quote sweeps while the market is open
    monitor_analysis_interval: float = 900      # seconds between full re-analyses (indicators/setups/risk)
    monitor_offhours_interval: float = 1800     # quote sweep interval when the market is closed
    monitor_market_hours_only: bool = True      # slow down outside NYSE hours
    monitor_history_ttl: float = 21_600         # daily history reuse inside the monitor (live quote spliced in)
    signal_cooldown: float = 14_400             # seconds before the same signal can fire again for a symbol

    # signal thresholds
    price_move_pct: float = 3.0                 # alert every +/-3% band of intraday move
    position_loss_pct: float = 8.0              # alert every -8% band a holding falls below cost
    portfolio_move_pct: float = 2.0             # alert every +/-2% band of portfolio day change
    concentration_pct: float = 35.0             # alert when one holding exceeds this % of the portfolio
    setup_min_confidence: float = 0.75          # only announce trade setups at/above this confidence

    # ---------------------------------------------------------------- notifications
    notify_desktop: bool = True                 # native pop-ups (needs `pip install plyer`); dashboard pop-ups need no install
    notify_desktop_min_severity: str = "warning"
    discord_webhook_url: str | None = None
    notify_discord_min_severity: str = "info"
    smtp_host: str | None = None                # e.g. smtp.gmail.com
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None            # Gmail: an App Password, not your normal password
    smtp_ssl: bool = False                      # True for port 465 (implicit TLS); False = STARTTLS on 587
    email_from: str | None = None
    email_to: str = ""                          # comma-separated recipients
    notify_email_min_severity: str = "warning"
    email_batch_seconds: float = 120            # bundle non-critical signals into one email per window

    # ---------------------------------------------------------------- external signals (docs/11)
    ext_enabled: bool = True                    # track external trade ideas inside the monitor
    discord_bot_token: str | None = None        # bot token (NOT a user token) to read signal channels
    ext_discord_channel_ids: str = ""           # comma-separated channel ids to read ideas from
    ext_poll_seconds: float = 20                # how often to poll those channels
    ext_backfill_messages: int = 0              # on first start, also parse this many older messages per channel
    ext_relay_mode: str = "webhook"             # webhook | reply | none : how decisions go back to the channel
    ext_relay_webhook_url: str | None = None    # webhook for relays (falls back to ABG_DISCORD_WEBHOOK_URL)
    ext_relay_ack: bool = True                  # post an "interpreted & tracking" message when an idea is ingested
    ext_relay_kinds: str = ""                   # comma-separated event kinds to relay (empty = all)
    ext_account_size: float = 10_000            # paper account used to size tracked ideas
    ext_risk_pct: float = 1.0                   # % of the paper account risked per idea
    ext_entry_expiry_days: float = 45           # pending swing ideas expire if the entry isn't reached
    ext_max_hold_days: float = 90               # active swing ideas are closed after this long
    ext_approach_pct: float = 1.5               # "approaching entry" heads-up distance
    ext_min_entry_grade: str = "C"              # grade gate for paper entries (A/B/C/D, or "none")
    ext_regrade_seconds: float = 900            # re-grade a blocked / pending idea at most this often
    ext_move_stop_to_breakeven: bool = True     # after TP1, stop -> entry
    ext_max_entry_distance_pct: float = 35      # reject ideas whose entry is further than this from the price
    ext_scale_in: bool = True                   # zones: split the entry between the zone edge and midpoint
    ext_scale_in_split: float = 0.5             # fraction bought at the zone edge
    ext_scale_out: bool = True                  # single far target: add a partial exit before it
    ext_scale_out_min_r: float = 1.5            # ... only when the target is at least this many R away
    ext_earnings_warn_days: int = 5             # warn this many days before a known earnings date
    ext_earnings_blackout_days: int = 1         # no new entries within this many days of earnings

    # ---------------------------------------------------------------- portfolio-level risk across tracked ideas
    ext_max_open_positions: int = 8             # concurrent active ideas
    ext_max_heat_pct: float = 6.0               # sum of open risk (to the stops) as % of the account
    ext_max_correlated: int = 2                 # active ideas in one correlation cluster (|rho| >= threshold)
    ext_correlation_threshold: float = 0.7      # 90-day daily-return correlation that counts as "the same bet"
    ext_max_sector: int = 3                     # active ideas in one sector / futures group
    ext_resize_to_fit: bool = True              # shrink a new entry to fit the remaining heat / loss budget

    # ---------------------------------------------------------------- prop-firm / funded-account guardrails
    prop_enabled: bool = False
    prop_account_size: float = 50_000           # starting balance of the evaluation / funded account
    prop_daily_loss_limit: float = 0            # $ max loss per trading day (0 = off)
    prop_max_drawdown: float = 0                # $ trailing drawdown from the equity high (0 = off)
    prop_drawdown_mode: str = "eod"             # eod (trails end-of-day highs) | intraday (trails every new high)
    prop_drawdown_lock: bool = True             # trailing stops rising once it reaches the starting balance
    prop_max_contracts: int = 0                 # max contracts per position (0 = no cap)
    prop_warn_pct: float = 70                   # warn when this % of a limit is used

    # ---------------------------------------------------------------- real-time prices
    ext_realtime: bool = True                   # Finnhub websocket stream (needs ABG_FINNHUB_API_KEY)
    ext_stream_flush_seconds: float = 1.0       # batch ticks into one tracking step per symbol at most this often
    ext_fast_poll_seconds: float = 15           # symbols the stream can't serve (futures, yields) are polled this often

    # ---------------------------------------------------------------- calendar & market filter
    ext_event_blackout_before_min: int = 30     # no new entries this long before a high-impact macro release
    ext_event_blackout_after_min: int = 15
    ext_event_blackout_classes: str = "future,etf,index,fx,stock"  # which instruments macro blackouts apply to
    ext_market_filter: bool = True              # grade longs down in a risk-off tape (SPY/QQQ trend, VIX)
    ext_auto_earnings: bool = True              # look up earnings dates (Finnhub) for tracked symbols

    # ---------------------------------------------------------------- entry confirmation, AI, charts
    ext_confirm_timeframe: str = "1h"           # zone entries wait for a reversal bar on this timeframe (none|15m|1h)
    ext_confirm_max_wait_hours: float = 24      # after this long in the zone without confirmation, enter anyway
    ext_ai_parse: bool = True                   # Claude reads posts the rule parser can't (needs ANTHROPIC_API_KEY)
    ext_ai_narrative: bool = True               # 2-3 sentence plain-English read on key Discord messages
    ext_relay_charts: bool = True               # attach a chart image to key Discord messages (needs matplotlib)
    ext_grade_model: str = "auto"               # auto (learned grade when validated) | learned | rules

    # ---------------------------------------------------------------- Discord commands & scheduled posts
    discord_commands: bool = True               # /abg slash commands through the bot (gateway)
    discord_admin_ids: str = ""                 # user ids allowed to change things via commands (comma-separated)
    discord_guild_id: str | None = None         # register commands in this server (default: the signal channel's server)
    ext_weekly_recap: bool = True               # Friday after the close: performance recap
    ext_week_ahead: bool = True                 # Sunday evening: macro calendar + earnings for tracked symbols

    # ---------------------------------------------------------------- operations
    admin_token: str | None = None              # required for any change through the dashboard/API when set
    view_token: str | None = None               # when set, even viewing needs a token (view or admin)
    healthcheck_url: str | None = None          # dead-man's switch ping (e.g. healthchecks.io) every 5 min
    backup_hour: int = 2                        # nightly SQLite backups at this NY hour (-1 = off)
    backup_keep: int = 14                       # days of backups to keep
    broker: str = "none"                        # none | alpaca_paper
    alpaca_key_id: str | None = None
    alpaca_secret_key: str | None = None
    alpaca_live: bool = False                   # never on unless you explicitly set it

    log_level: str = "WARNING"

    # ---------------------------------------------------------------- helpers
    def provider_list(self) -> list[str]:
        disabled = {p.strip().lower() for p in self.disabled_providers.split(",") if p.strip()}
        return [p.strip().lower() for p in self.provider_order.split(",")
                if p.strip() and p.strip().lower() not in disabled]

    def capability_order(self, capability: str) -> list[str] | None:
        """Per-capability order if configured, else None (fall back to provider_order)."""
        raw = getattr(self, f"{capability}_order", "") or ""
        disabled = {p.strip().lower() for p in self.disabled_providers.split(",") if p.strip()}
        names = [p.strip().lower() for p in raw.split(",") if p.strip() and p.strip().lower() not in disabled]
        return names or None

    def ext_channel_list(self) -> list[str]:
        return [c.strip() for c in self.ext_discord_channel_ids.split(",") if c.strip()]

    def extra_risk_models(self) -> list[str]:
        return [m.strip() for m in self.risk_models.split(",") if m.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
