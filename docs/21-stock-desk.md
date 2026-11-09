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
3. If FMP refuses the constituent list on your plan, load a list with
   `abg stocks universe --file sp500.txt`.
4. Point-in-time membership is still not loaded, so backtests and the weekly study carry survivorship
   bias until it is (`BarStore.set_membership`).

## 21.3 Rules that still hold

Paper only (`ABG_STOCKS_TAG=PAPER` on every post); webhooks only in `.env`; one live poster per channel;
findings are proposals, never automatic config changes; the seed universe is survivorship-biased and
every report says so.
