# 21. Stock desk (replaces the swing desk; MNQ bot retired)

> **Read chapter 22 next.** Since Nov 10 2026 the desk is a stock-pitching desk: it shows no paper
> portfolio. Where this chapter mentions the paper book, equity, positions or a `PAPER` tag, chapter 22
> (`docs/22-pitching-desk.md`, section 22.1) says what each became. The rule books, the pitch format
> (21.6) and the top-5 board (21.7) are unchanged.

The four-book rule system from the *Stocks Rule Book* (`tradebook`, vendored at `abg/tradebook/`),
run live on the terminal's own bar store, pitching stocks to Discord with a written thesis.

## 21.1 What is built (Nov 8 2026)

| Piece | File | State |
|---|---|---|
| Vendored engine | `abg/tradebook/` | Unchanged except: `Engine.run(live=True)` returns the last day's orders as `signals`; `Engine(ind=...)` shares indicators across runs; column-cached frames (`common/fastframe.py`, 3.5x faster, output bit-identical); `flag_study` no longer raises on an empty table |
| Rule file | `configs/stocks.yaml`, `configs/events.csv` | From the package; `.env` overrides in `abg/stocks/config.py` |
| Bar store | `abg/stocks/store.py` | SQLite at `<data_dir>/stocks.sqlite3`; builds the `Panel` directly |
| Universe | `abg/stocks/universe.py` | ETFs + gauges + a hand-written **seed** (survivorship-biased) until `abg stocks universe --index sp500` (FMP key) |
| Paged sweep | `abg/stocks/sweep.py` | 60 symbols / 30 min, cursor in the store, parks a symbol after 6 failures, prefers adjusted close |
| Live signals | `abg/stocks/live.py` | Candidates with the indicator values and gates behind each one; ranked by the book's own score |
| Thesis | `abg/stocks/thesis.py` | Markdown memo + Discord embed + daily digest. Four questions: what happened, why it pays, what we risk, what makes it wrong |
| Desk | `abg/stocks/desk.py` | Daily pass 16:20 ET weekdays, intraday sweep + near-stop watch, ledger of every candidate, pitch record, scorecard vs SPY |
| API / CLI | `abg/stocks/routes.py`, `abg/stocks/cli.py` | `/api/stocks/*` (10 endpoints), `abg stocks universe|backfill|sweep|signals|memo|score|rules|backtest` |
| Research | `abg/stocks/research.py` | Ablations (§10.2 exp. 1-6), anchored walk-forward with trial registry, early-warning flag study, pass-bar verdict. Run by the R&D desk Saturdays 09:00 ET, posted to the R&D channel; `GET/POST /api/stocks/research` |
| Monitor | `abg/live/monitor.py` | Starts the stock desk when `ABG_STOCKS_ENABLED=true`; refuses to also start the retired swing desk |
| MNQ bot | `abg/config.py` `alpha_retired=True` | Retired in code: the monitor never starts it while retired, whatever `ABG_ALPHA_ENABLED` says. Code and trade history kept; `ABG_ALPHA_RETIRED=false` brings it back |
| R&D desk | `abg/agents/desk.py` | MNQ digests and lab jobs only run while the MNQ bot is unretired; the stock study runs instead |
| Dashboard | `abg/api/static/stocks.js`, `index.html` | Stocks tab (Today, Thesis, Scorecard, Universe & data); R&D → Stock research; Swing tab removed from the nav; MNQ tab marked retired |
| First start | `StockDesk.first_start` | Loads the real S&P 500 from FMP when on the seed list (stable API, falls back to v3), then fills any missing history - resumes an interrupted backfill on the next start |
| Tests | `tests/test_stocks.py` (21), `tests/test_stocks_rd.py` (17), `tests/test_tradebook_core.py` (9) | Full suite 348 passed, 3 skipped |

## 21.2 Done on Nov 9, and what is left for you

Done overnight: MNQ retire switch, R&D desk rework, the Stocks dashboard tab, S&P 500 on first start,
resumable backfill, and these bugs found along the way:

- `refresh_index` called an `HttpClient.get` that does not exist, and an FMP path new keys can't use
- `^VIX`/`^VIX3M` were stored as `VIX`/`VIX3M` but looked up with the caret, so the VIX stress flags
  could never fire and the coverage table showed them empty
- the scorecard "marked" a pitch against its own signal-day close, reporting "beat SPY 0%" for trades
  that had not had a session yet; a pitch now counts only once a newer bar exists
- a memo showed risk as "+0.43% of the book"; the research verdict printed an R multiple as a percent
- the autofill only ran on an empty store, so a backfill cut off halfway never resumed
- a warning pointed at an `abg stocks membership` command that does not exist
- research reports carried `date` objects and failed to serialise

Left for you (needs your PC or a decision):

1. Restart the monitor (close and reopen `Start Monitor.bat`) so the `.env` switch-over applies:
   stock desk on, swing desk and MNQ bot off. First start loads the S&P 500 list and backfills ~10 years
   of daily bars in the background (rate-limited; expect it to take a while).
2. Check `Stocks → Today` after the next close. Nothing posts to Discord until you add
   `ABG_STOCKS_WEBHOOK_URL` (rotate the one shared in chat first).
3. The S&P 500 list now comes from GitHub with no key (21.5); FMP is only a fallback.
4. Dated membership loads with the list (21.5). Delisted companies still have no price data on
   free providers, so some survivorship bias remains.

## 21.4 The whole US market (market tier)

Per-symbol requests can't cover ~5,000 stocks on a free plan. Polygon's *grouped daily* call returns
every US stock's bar for one session, so the market costs **one call per trading day**.

- `abg/stocks/market.py`: syncs missing sessions newest-first into `<data_dir>/swing/wide/bars/` (the old
  wide scanner's day files are reused: 221 sessions were already there), takes Polygon's list of common
  stocks **and ADRs** (TSM, ASML, BABA, NVO, ARM would otherwise be lost; ETFs, warrants, units and
  preferreds are excluded), keeps the names that clear the books' own floor ($5, median 20-day dollar
  volume $20M) and loads their bars into the bar store. On your data about 2,670 tickers clear the floor
  before the common-stock filter.
- `Universe.daily()` = S&P 500 core + market tier. The research study stays on the core (it needs years
  of history; Polygon's free plan keeps two).
- With the market tier on, the live replay is 2 years (`ABG_STOCKS_MARKET_REPLAY_YEARS`) and the panel
  starts at the replay window plus warm-up - ~2,500 names over 5 years would need ~1.5 GB of indicators.
- The daily pass **waits for Polygon's bar** (rechecking every 10 min) until `ABG_STOCKS_MARKET_WAIT_UNTIL`
  (21:30 ET): run before it is published, every non-S&P name would miss today's close and none could signal.
- `abg stocks market [--max-calls N]` / `POST /api/stocks/market`. First fill tops up to 300 sessions at
  5 calls a minute (~16 min from what was already stored).
- Limit: the common-stock list is today's, so delisted non-S&P names are absent (survivorship, as before).

## 21.5 Index list without FMP

`abg/stocks/index_sources.py` reads the `fja05680/sp500` dataset (MIT, GitHub): every S&P 500 membership
snapshot since 1996. It gives today's list *and* dated membership, so backtests no longer buy a stock
before it joined (Tesla before Dec 2020). Ticker renames are folded into today's ticker only when the
data confirms them (old ticker leaves the day the new one joins): FB->META, UTX->RTX, FISV->FI->FISV,
SYMC->NLOK->GEN and 12 more. FMP stays as a fallback. Re-checked weekly.

## 21.6 AIBG-format pitches (recommendation, trade plan, charts, deck)

Every stock-desk pitch now reads like an AIBG Round 1 pitch, and goes further where the human
pitches were vague: a dated entry, a stop, targets with their basis, a share count, and the
questions the team should argue about. **The rule still decides.** Nothing here changes which
trades fire or how big the book sizes them; the plan explains and sizes for the fund.

**Order of a memo:** header line (rating, conviction, 12-month target, trade levels) → "views
disagree" banner when the rule and the 12-month valuation point opposite ways → TL;DR →
01 Company Overview → 02 Investment Thesis (core statement, bull/bear, valuation with every
line's arithmetic, events and risks) → 03 Data (charts, the book's base rates) → 04
Recommendation (rating, method, the trade plan table, untested refinements, why we're exiting
for a SELL/TRIM, what would make this wrong, fund fit, questions, conviction breakdown) → market
context, gates, sources, glossary, caveats.

| Piece | Where | What it is |
|---|---|---|
| Rating | `pack.rec` | BUY / ADD / HOLD / TRIM / SELL from the book's order against that book's holding; the pitch desk maps LONG/WATCH/PASS to BUY/WATCH/AVOID |
| Entry | `pack.plan.entry` | The tested entry is the **next open** (holidays skipped). Zone = close ± 0.5 ATR. "Don't chase", "gap below the stop" and "wait for earnings" are labelled *untested refinements* |
| Targets | `pack.plan.targets` | Pullback: T1/T2 = this book's median / 75th-percentile winning exit in R. Mean reversion: T1 = the 5-day-average rule exit, T2 only if above it. Momentum: no price target, a month-end review date. Levels out of order → no targets, said so |
| 12-month view | `pack.plan.investment` | Bear / base / bull earnings DCF (each recomputed and checked), peer comps, street target (reference), 25/50/25 probability-weighted target, total return with the dividend, implied growth |
| Conviction | `pack.rec.conviction` | 50 ± listed inputs (gates, ML odds, valuation, regime, flags, earnings, sample size), clamped 0-100; High ≥ 70, Medium ≥ 45 |
| Size | `pack.plan.risk` | risk_per_trade × conviction scale of the fund to the stop, capped at max_position_pct; momentum uses the book's weight; a $10k-account line |
| Fit | `pack.plan.fit` | Sector weight before/after, 60-day correlation to holdings and SPY, beta, AI-infrastructure tag |
| Charts | `pack.charts` | `charts.py`: price + EMA50/200 + entry zone/stop/T1/T2 + 12-month strip; valuation football field; peer table. PNGs next to the memo, inline on the dashboard, the price chart attached to each Discord pitch |
| Write-up | `pack.writeup` | Optional (`ABG_PITCH_AI_MEMO=true` + Anthropic key): Claude rewrites the prose from the pack only; any number, ticker or date not in the pack, or hype words, drops that field (logged in `pack.writeup_log`) |
| Deck | `abg pitch deck SYMBOL` | `deck.py` fills `abg/assets/aibg_pitch_template.pptx` (the Round 1 design, content removed) from the saved pack; also the "Download deck" button on a memo. Refuses to save with a `{{placeholder}}` left |
| Scorecard | Stocks → Scorecard | The plan is frozen per pitch; T1/T2/stop/time and the 12-month target are marked from the next open, by book and by conviction bucket, tested vs the untested refinement |

**Settings.** Pitch tunables live under `pitch:` in `configs/stocks.yaml` (every key commented).
`.env`: `ABG_STOCKS_CHARTS` (default true), `ABG_STOCKS_POST_CHARTS` (default true),
`ABG_PITCH_AI_MEMO` (default false). Optional installs: `pip install .[charts]` (matplotlib) and
`pip install .[deck]` (python-pptx); without them the memo says so and nothing else breaks.

**Not possible with the free data we have** (shown as "not in data", never guessed): revenue
(TTM), free cash flow, net debt/EBITDA, revenue segments, market share or patents (the moat box).
Earnings dates come from Finnhub when a key is set, otherwise "not in data". The DCF discounts
EPS as an owner-earnings proxy because no provider gives cash-flow statements.

**Samples:** `docs/samples/` — a pullback BUY, a mean-reversion BUY, a SELL, the charts, and the
deck for the first. They use the synthetic test panel and invented company facts.

## 21.7 The day's top picks on Discord (after the close, 9:00, 12:00, 15:00 ET)

| When (ET, trading days) | What is posted |
|---|---|
| After the close (16:20, later if Polygon is late) | The digest, the **top 5 buys by conviction** (single-stock books) as full pitches with price charts, then a message with their 5 AIBG decks |
| 9:00 | "Today's top 5 — buy at the 9:30 open": entry, zone, stop, T1/T2, size, plus the 5 decks again |
| 12:00 and 15:00 | Check-in on the same picks from live quotes: in the zone / above it / below it / reached T1 / stopped / skipped (opened above the don't-chase line or below the stop). A pick that drops out is replaced by the next-best reserve still in play, marked *new*, and only its deck is sent |

Picks are never made midday: the signals come from the close and the tested entry is the open, so the
check-ins track the morning's list. The board lives in `<data_dir>/stocks/board.json` (picks, reserves,
what was dropped, which posts went out). A slot posts once, within 45 minutes of its time; a restart
inside that window still posts it, later than that it is skipped.

`.env`: `ABG_STOCKS_BOARD_SIZE` (5), `ABG_STOCKS_BOARD_TIMES` ("09:00,12:00,15:00"), `ABG_STOCKS_POST_DECKS`
(true; needs python-pptx, which `[all]` installs on the server).

## 21.3 Rules that still hold

Research only (`ABG_STOCKS_TAG=RESEARCH` on every post); webhooks only in `.env`; one live poster per channel;
findings are proposals, never automatic config changes; the seed universe is survivorship-biased and
every report says so.
