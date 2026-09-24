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

    def extra_risk_models(self) -> list[str]:
        return [m.strip() for m in self.risk_models.split(",") if m.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
