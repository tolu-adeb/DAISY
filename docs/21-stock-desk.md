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
| Research | `abg/stocks/research.py` | Ablations (§10.2 exp. 1-6), anchored walk-forward with trial registry, early-warning flag study, pass-bar verdict. **Written, not yet wired into the R&D desk** |
| Monitor | `abg/live/monitor.py` | Starts the stock desk when `ABG_STOCKS_ENABLED=true`; refuses to also start the retired swing desk |
| Tests | `tests/test_stocks.py` (21), `tests/test_tradebook_core.py` (9) | Full suite 331 passed, 3 skipped |

## 21.2 Still to do (pick up here)

1. **Retire the MNQ bot in code**: add `alpha_retired: bool = True`; monitor skips `AlphaBot` unless
   `ABG_ALPHA_RETIRED=false` (the PC's `.env` has `ABG_ALPHA_ENABLED=true`, so a default flip is not enough).
   Keep `abg/alpha/` and its data; MNQ dashboard tab becomes read-only history.
2. **R&D desk** (`abg/agents/desk.py`): gate the MNQ digests/lab refresh on `not alpha_retired`; add a
   Saturday job that runs `research.run_study` in a worker thread, saves to `<data_dir>/stocks/research/`,
   posts `research.embed` to `ABG_RD_WEBHOOK_URL`. A research smoke run is still needed (the first attempt,
   before the speed fix, timed out).
3. **Dashboard**: a Stocks tab (`abg/api/static/stocks.js` + `view-stocks` in `index.html`) showing today's
   candidates, memos, regime/flags, Hedge weights, scorecard; remove the Swing tab; R&D tab shows the
   research report.
4. Tests for research + the retire switch; full suite; commit.
5. On the PC: put the **rotated** webhook in `.env` as `ABG_STOCKS_WEBHOOK_URL`, set `ABG_STOCKS_ENABLED=true`,
   run `abg stocks universe --index sp500` then `abg stocks backfill`, then `abg stocks signals` (no post)
   to check before the first real post.

## 21.3 Rules that still hold

Paper only (`ABG_STOCKS_TAG=PAPER` on every post); webhooks only in `.env`; one live poster per channel;
findings are proposals, never automatic config changes; the seed universe is survivorship-biased and
every report says so.
