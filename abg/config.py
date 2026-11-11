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
    alert_cross_rearm_pct: float = 0.2          # a repeating cross alert re-arms once price moves this % past the level

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

    # ---------------------------------------------------------------- MNQ alpha bot (docs/14)
    alpha_enabled: bool = False                 # run the MNQ signal bot inside the monitor
    alpha_retired: bool = True                  # docs/21: retired - the monitor won't start it even if
                                                # alpha_enabled is true. Code and trade history are kept;
                                                # set ABG_ALPHA_RETIRED=false to bring it back deliberately.
    stocks_research: str = "weekly"             # off | weekly: Saturday 09:00 ET stock-desk study (R&D desk)
    stocks_research_grid: str = ""              # "dotted.key=v1|v2;other.key=v1|v2"; empty = the built-in grid
    stocks_research_years: int = 10             # history the study replays (ablations + walk-forward)
    alpha_symbol: str = "MNQ=F"                 # futures bars for levels / direct feed
    alpha_feed: str = "proxy"                   # proxy (real-time QQQ x ratio) | direct (poll futures bars)
    alpha_proxy_symbol: str = "QQQ"
    alpha_webhook_url: str | None = None        # where signals go (or bot token + alpha_channel_id)
    alpha_channel_id: str | None = None
    alpha_mention_role_id: str | None = None    # role pinged on signals / targets / stops
    alpha_tag: str = "TEST"                     # shown on every message; set "" when you trust it
    alpha_contracts: float = 1.0                # micros, for the $ figures
    alpha_commission_rt: float = 1.24           # $ per micro round turn (backtests)
    alpha_post_ideas: bool = True               # post the "get ready" trade ideas
    alpha_post_brief: bool = True               # 09:25 pre-market brief
    alpha_stale_sec: int = 120                  # never post a trade message whose bar is older than this (restarts replay the morning); 0 = off
    alpha_auto_optimize: bool = True            # Sunday walk-forward re-fit (adopted only if it beats current)
    alpha_min_win_rate: float = 0.75            # re-fits only consider settings with at least this win rate

    # Strategy Lab (docs/17) - research agents; nothing here changes the live bot without `abg lab approve`
    lab_model: str | None = None                # Claude model for the research agents (default: anthropic_model)
    lab_max_tokens: int = 4000                  # per model call
    lab_rounds: int = 3                         # research rounds per `abg lab run`
    lab_per_round: int = 5                      # hypotheses per round
    lab_max_model_calls: int = 60               # hard cap on model calls per run (cost guard)
    lab_holdout_months: int = 6                 # the locked holdout: the most recent N months of the dataset
    lab_auto_run: str = "weekly"                # off | weekly: Saturday research run by the agents (only with an API key)
    lab_refresh_data: bool = True               # Saturday: append finished sessions to the lab dataset (QQQ proxy)
    # R&D desk digests (docs/17): daily / weekly / monthly findings -> Discord + the dashboard's R&D tab
    rd_enabled: bool = True                     # run the R&D desk inside the monitor
    rd_webhook_url: str | None = None           # Discord webhook for the R&D channel (keep it in .env only)
    rd_daily: bool = True                       # weekdays after the bot is flat (rd_daily_time ET)
    rd_daily_time: str = "12:20"
    rd_weekly: bool = True                      # Fridays 16:30 ET
    rd_monthly: bool = True                     # first weekday of the month, 08:00 ET (for the month just ended)
    rd_analyst_note: bool = True                # add a short Claude note when ABG_ANTHROPIC_API_KEY is set

    # ---- ported from the MNQ bot spin-off (c3.lab/mnq-bot, docs/20): reliability, forward lab, swing desk
    # Features that run on their own are off until switched on in .env (ABG_FWD_ENABLED, ABG_SWING_ENABLED).
    keep_awake: bool = True                     # Windows: don't sleep while the monitor runs, 08:55-16:35 ET trading days
    papers_enabled: bool = False                # needs the website link (above) and ABG_ANTHROPIC_API_KEY
    papers_model: str | None = None             # default: anthropic_model
    papers_max_tokens_per_day: int = 400_000    # Claude tokens (in + out) for paper analyses per New York day
    papers_max_tokens_per_paper: int = 90_000   # longer papers are cut to their key sections to fit
    papers_max_mb: int = 20
    alpha_refit_adopt: bool = False             # ... but only suggests (to the forward lab) unless this is true
    alpha_brief_until: str = "09:45"            # a morning brief later than this (a restart) stays on the dashboard
    parity_enabled: bool = True                 # live-vs-backtest check after the forward lab's daily data refresh
    fwd_enabled: bool = False                   # runs after the close on its own copy of the dataset (never the lab's holdout)
    fwd_time: str = "16:45"                     # ET, trading days
    fwd_per_day: int = 16                       # ideas tested per day (~10 s per backtest, ~3 backtests per idea)
    fwd_budget_min: float = 30.0                # stop starting new backtests after this; the cache resumes tomorrow
    fwd_workers: int = 1                        # backtest processes (1 is safest inside the monitor)
    fwd_folds: int = 6                          # walk-forward periods over all sessions
    fwd_min_fold_wins: int = 5                  # win rate >= live in at least this many periods
    fwd_max_active: int = 5                     # paper strategies at once
    fwd_min_sessions: int = 20                  # unseen sessions before a strategy can be READY
    fwd_min_trades: int = 15
    fwd_max_sessions: int = 60                  # retired if not ready by then
    fwd_agent: bool = True                      # + up to 4 ideas a day from Claude (needs an API key; one call)
    fwd_post: bool = True                       # post new paper / ready / retired to the R&D channel
    fwd_profit_route: bool = True               # also pass ideas that hold the win rate and clearly improve profit/drawdown:
    fwd_profit_route_wr_slack: float = 0.005    #   win rate no more than 0.5 pt below live
    fwd_profit_route_ratio: float = 0.15        #   net/max-drawdown at least 15% better
    fwd_profit_route_net: float = 0.10          #   net profit at least 10% higher
    # ---- swing desk: RETIRED (docs/21). The stock desk below replaced it as the stock signal source.
    # The settings stay so an existing .env still loads, and the module is still importable for its
    # history, but nothing starts it unless swing_enabled is turned back on deliberately.
    swing_enabled: bool = False                 # retired: the House of Traders intake and wide scanner
    swing_webhook_url: str | None = None        # Discord channel for swing alerts; unset = dashboard only
    swing_interval_min: int = 30                # intraday pass during market hours (prices, fills, in-zone nudges)
    swing_close_time: str = "16:20"             # after-close pass: final daily bars -> fills/exits, scanner, digest
    swing_scan: bool = True                     # after the close, look for the channel's own pattern (config/universe.txt)
    swing_research: str = "off"                 # "weekly" = Strands R&D agent on Sundays 11:00 ET (needs an API key; costs tokens)
    swing_pitch: bool = False                   # analyst agent writes a pitch for each idea the gate takes (API key; tokens)
    swing_ai_intake: bool = True                # pasted prose the parsers can't read goes to the intake agent (Haiku) if a key is set
    swing_use_twelvedata: bool = False          # daily bars/quotes from Twelve Data instead of yfinance (shares the MNQ bot's quota)
    swing_max_posts: int = 12                   # per pass; anything beyond is summarised in one message
    swing_wide: bool = True                     # runs only when ABG_POLYGON_API_KEY is set
    swing_wide_post: bool = False               # false = shadow: tracked on the dashboard only; true = best few to Discord
    swing_wide_time: str = "17:15"              # ET; retried every 30 min until 21:30 if the day's bars aren't out yet
    swing_wide_max_posts: int = 3               # per day, when posting
    swing_wide_min_price: float = 5.0
    swing_wide_min_dollar_vol_m: float = 20.0   # median 20-day dollar volume, $M
    swing_wide_min_breadth: float = 30.0        # % of liquid stocks above their 50-day average; below it nothing posts
    swing_wide_news_top: int = 12               # candidates whose headlines get read (Finnhub + Claude Haiku)
    swing_wide_earnings_sessions: int = 10      # earnings inside this many sessions = binary_event (half size)
    swing_wide_history: int = 130               # sessions of whole-market bars kept for the scan
    swing_wide_polygon_per_min: float = 5.0     # free plan limit
    swing_wide_finnhub_per_min: float = 50.0    # free plan allows 60
    swing_wide_rank: str = "entry"              # which taken setups post first: "entry" = closest to its entry (likeliest
    swing_wide_chase_atr: float = 1.0           # wide ideas only: if price closes above the zone by <= this many ATR before
    swing_wide_backfill_to: int = 400           # sessions of whole-market bars to build up, a little each night (~100 MB)
    swing_wide_backfill_per_night: int = 60     # Polygon calls per night for that (5/min: ~12 minutes)
    swing_wide_backtest_weekly: bool = True     # re-run the backtest on all stored history, Saturdays 09:00 ET
    swing_wide_post_min_trades: int = 40        # posting stays held until the backtest of what would post has this many
    swing_wide_post_min_win: float = 65.0       # closed trades, at least this win rate (%) and a positive total R

    # Pitch desk v1 (docs/20): screen -> valuation -> memo with sources -> scorecard vs SPY.  Off until enabled.
    pitch_enabled: bool = False
    pitch_webhook_url: str | None = None          # optional Discord channel for pitch memos (empty = dashboard only)
    pitch_universe: str = ""                       # comma list; empty = the built-in large-cap list (+ your watchlist)
    pitch_max_names: int = 3                       # memos per weekly batch
    pitch_weekday: int = 5                         # 0=Mon .. 6=Sun (default Saturday)
    pitch_time: str = "10:00"                      # ET
    pitch_risk_free: float = 0.042                 # discount rate = risk-free + beta x equity premium
    pitch_equity_premium: float = 0.05
    pitch_terminal_growth: float = 0.025
    pitch_ai_memo: bool = False                    # Claude write-up from the data pack only (API key; tokens)

    # ---------------------------------------------------------------- stock desk (docs/21)
    # The four-book rule system from the Stocks Rule Book, run live: it replaces the swing desk as the
    # terminal's stock signal source. Rules live in configs/stocks.yaml; only the knobs an operator
    # changes often are here.
    stocks_enabled: bool = False                   # run the stock desk inside the monitor (dashboard: Stocks)
    stocks_webhook_url: str | None = None          # Discord channel for the daily pitches; unset = falls back to
                                                   # ABG_PITCH_WEBHOOK_URL, and failing that the dashboard only
    stocks_tag: str = "RESEARCH"                   # shown on every post (the desk pitches; it places no orders)
    stocks_fund_holdings: str | None = None        # path to the fund's holdings list (default: configs/fund_holdings.yaml;
                                                   # the list saved from Settings wins and stays out of the repo)
    stocks_config: str | None = None               # path to the rule file (default: configs/stocks.yaml)
    stocks_daily_time: str = "16:20"               # ET, weekdays: the after-close pass that produces the pitches
    stocks_post_max: int = 5                       # detailed pitch embeds per post; the rest are in the digest
    stocks_charts: bool = True                     # price / valuation / peer PNGs next to each memo (needs matplotlib)
    stocks_post_charts: bool = True                # attach the price chart to each detailed Discord pitch
    stocks_board_size: int = 5                     # top picks posted after the close and checked during the day
    stocks_board_times: str = "09:00,12:00,15:00"  # ET, trading days: the open post and the check-ins (docs/21.7)
    stocks_post_decks: bool = True                 # attach the AIBG deck for each new pick (needs python-pptx)
    stocks_books: str = ""                         # comma list to limit which books run (a,b,c,d,e or their names)
    stocks_benchmark: str = "SPY"
    stocks_initial_equity: float = 100_000.0       # capital the historical replay starts from (base rates only;
                                                   # pitch sizes use pitch.fund_equity, or % of the fund)
    stocks_panel_start: str | None = "2015-01-01"  # earliest bar loaded into the panel (indicators warm up on it)
    stocks_min_bars: int = 250                     # a symbol with fewer bars than this is left out of the panel
    stocks_replay_years: int = 5                   # the live pass replays this far back to rebuild positions and
                                                   # Hedge weights (= the allocator's 60-month horizon); research
                                                   # studies use the rule file's own start instead
    # universe and data upkeep
    stocks_autofill: bool = True                   # backfill on first run when the store is empty
    stocks_backfill_period: str = "10y"
    stocks_backfill_concurrency: int = 3
    stocks_sweep_minutes: int = 30                 # paged refresh cadence while the market is open
    stocks_sweep_page: int = 60                    # symbols per pass; universe / page = passes for a full cycle
    stocks_sweep_concurrency: int = 4
    stocks_sweep_period: str = "6mo"               # history asked for on a refresh pass
    stocks_sweep_pause: float = 0.0                # seconds between calls, for a strict rate limit
    # the whole US market (docs/21.4): one Polygon "grouped daily" call per session covers every stock
    stocks_market: bool = True                     # on whenever ABG_POLYGON_API_KEY is set
    stocks_market_sessions: int = 500              # sessions kept (the free plan allows about two years)
    stocks_market_backfill_calls: int = 25         # older sessions fetched per day until that many are stored
    stocks_market_max: int = 0                     # cap on the rule universe; 0 = unlimited (every liquid name)
    stocks_universe_refresh_days: float = 1        # how often the list of US common stocks/ADRs is re-read
    # research around the rules (docs/22): screen, news, watchlist, live alerts. Posting anything new is off.
    stocks_news: bool = True                       # store headlines + filings for watched names (needs ABG_FINNHUB_API_KEY)
    stocks_news_minutes: int = 30                  # how often the live layer re-reads news for the watched names
    stocks_news_max_names: int = 40                # names per news pass (free-tier budget)
    stocks_edgar: bool = True                      # SEC EDGAR filings (8-K, S-1, 13D/G, Form 4) for watched names
    stocks_edgar_user_agent: str | None = None     # SEC asks for an app name here; default names DAISY, never a person
    stocks_themes: str | None = None               # path to the theme map (default: configs/themes.yaml)
    stocks_watch: bool = True                      # build the morning watchlist for the dashboard
    stocks_watch_scan_time: str = "08:30"          # ET, trading days
    stocks_watch_post: bool = False                # post the morning watchlist to Discord (off until you turn it on)
    stocks_watch_post_time: str = "08:45"          # ET
    stocks_live: bool = True                       # watch live quotes on picks, active pitches and the watchlist
    stocks_live_seconds: int = 120                 # how often (polling; Finnhub's free plan is 60 calls a minute)
    stocks_live_max_names: int = 40                # names watched per tick
    stocks_live_alerts: bool = False               # post live alerts to Discord (off until you turn it on)
    stocks_dashboard_url: str | None = None        # the dashboard's address, for "open the pitch" links in alerts
    stocks_weekly_recap: bool = False              # post the Friday recap to Discord (off until you turn it on)
    stocks_weekly_recap_time: str = "16:45"        # ET, Fridays
    stocks_market_replay_years: int = 2            # live replay length when the market tier is on (memory)
    stocks_market_wait_until: str = "21:30"        # ET: the daily pass waits for Polygon's bar until then
    # risk and allocation overrides (null = whatever configs/stocks.yaml says)
    stocks_allocation: str | None = None           # hedge | fixed
    stocks_vol_target: float | None = None
    stocks_max_gross: float | None = None
    stocks_max_name_weight: float | None = None
    stocks_dd_kill: float | None = None
    stocks_risk_per_trade: float | None = None
    stocks_max_positions: int | None = None
    stocks_slippage_bps: float | None = None
    stocks_commission_bps: float | None = None
    stocks_crisis: bool | None = None              # early-warning flags -> crisis mode
    stocks_ml: bool | None = None                  # walk-forward take/skip filter on the pullback book
    stocks_ml_threshold: float | None = None

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
