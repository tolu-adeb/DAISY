# 21. Stock desk (replaces the swing desk; MNQ bot retired)

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

## 21.3 Rules that still hold

Paper only (`ABG_STOCKS_TAG=PAPER` on every post); webhooks only in `.env`; one live poster per channel;
findings are proposals, never automatic config changes; the seed universe is survivorship-biased and
every report says so.
