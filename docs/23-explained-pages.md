# 23. Explained pages: deep Explore, analyst pitches, News, fund performance, Strategy (Nov 11 2026)

This chapter adds to chapter 22. The rule books, the pitch format, the morning watchlist and the live
layer are unchanged.

What never changes: **the rules decide**. Nothing here creates, removes or resizes a rule signal. An
analyst pitch is a person's idea and is always labelled as one. Anything the rules have not tested is
labelled **untested** and scored apart.

## 23.0 For analysts: what is new

- **Explore is now a full research page for one stock.** Type a ticker in the box in the top bar
  (or press `/`) from anywhere. The page reads top to bottom: a summary in plain sentences, the
  chart, key numbers, ratings with the reason for each, valuation, risk and outlook, news, and what
  the rules say. Turn on **Deep dive** for the indicator tables and the arithmetic.
- **Pitch this stock.** On any Explore page an admin can write a thesis with a stop and a target.
  DAISY builds the same memo, charts and deck as a rule pitch, checks the levels and files it as an
  **analyst pitch**. It shows on the Pitches page in its own table and on the Scorecard in its own
  row.
- **News** is a tab of its own, sorted into categories, with our holdings first and a short brief of
  the day at the top.
- **Portfolio** now shows how the fund has done against SPY, where the money is, what made and lost
  money, and news about the companies we hold. "What the log shows" lists observations, each with
  its evidence and one thing to test.
- **Strategy** (the old Research tab) explains each of the five rules with a small diagram, its
  steps, when it works and when it does not, its record in the replay and our own pitches from it.

## 23.1 Analyst pitches

`abg/stocks/analyst.py`, `InsightMixin.analyst_pitch` in `abg/stocks/insight.py`.

The analyst supplies: ticker, name, direction (buy, sell/avoid, watch), a thesis of at least 20
characters, and optionally entry, stop, first target, stretch target and a horizon in trading days.
DAISY supplies every number from data (price, ATR, valuation, grades, sentiment, the company
overview) and runs these checks on the analyst's levels:

| Check | What it states |
|---|---|
| Entry against the last close | Flagged if more than 5% away |
| A stop is given and sits below the entry | With the percentage |
| Stop distance in ATR | Flagged if under 1 ATR: noise could hit it |
| A target is given and sits above the entry | A stretch target must sit above the first |
| Reward to risk | Against `pitch.min_rr` |
| Earnings | Flagged if a report falls inside the holding window |
| Price and liquidity floor | The same eligibility floor the rules use |
| History | Flagged if the name is too new for the tested rules |
| Trend | Price against its 200-day average |

The level checks apply to buy pitches. Sell and watch pitches get the earnings, floor, history and
trend checks.

A failed check never blocks the pitch. It is printed in the memo and on the pitch page as a flag.

How it is kept apart from rule pitches:

- `book = "analyst"`, `kind = "analyst"`, the analyst's name on the row and in the lifecycle log.
- The memo opens with a banner: whose idea it is, that it is not a rule signal, and how many checks
  passed. The pitch page shows the same banner and the checks table above the plan.
- No DAISY Rank (the rank is the rules' word). Conviction is still computed and labelled as DAISY's.
- Never in the top-5 board, never in the rules' ratings of open pitches.
- The Scorecard's "Rules against analysts" table (`track.by_kind`) measures both the same way: from
  the open after the pitch was filed.
- Sell and watch pitches are tracked on time only (the stop/target walk is a long's).

Filing needs the admin token. It posts to Discord if the form's box is ticked, or always when
`ABG_STOCKS_POST_ANALYST=true`; both use the one stock webhook and the one poster.

## 23.2 The Explore page

`InsightMixin.profile` builds the read and caches it per name per screen day in
`stocks/profiles/<SYM>.json`, so reopening a page spends no API calls.

| Section | Source | Notes |
|---|---|---|
| Summary sentences | `insight.summarize` | Each sentence is built from named numbers and carries them in `numbers` |
| Chart | `/api/stocks/bars/{symbol}` | Candles, volume, 50- and 200-day EMA, pitch levels. Toggles: Bollinger bands, RSI-14, MACD histogram, compare with SPY (switches the scale to percent) |
| Key numbers | Fundamentals from the provider chain | Market cap, P/E, EPS, margins, dividend, beta, 52-week range, dollar volume |
| Ratings | DAISY Rank, the five grades with `grade_reasons`, the indicator blend, the forecast model, Finnhub analyst recommendations | The third-party view is labelled as such. Analyst price targets are not on the free Finnhub plan |
| Valuation | `abg/pitch/valuation.py` | Ranges drawn against the price: 52-week, DCF bear to bull, peer multiples, blended fair value |
| Risk and outlook | Engine statistics and the forecast ensemble | Each figure says the stretch it covers ("past two years") |
| News | Stored headlines and filings with their sentiment score | |
| Rules | Setups, rule-by-rule checks, every pitch on the name | |

A ticker outside the research screen (an ETF, a foreign listing, a name too new for the screen)
still opens: the page says the rules have no read on it and shows what the analysis engine has.

Not in data on the free plans, and shown as "not in data": short interest, analyst price targets,
insider and institutional holdings.

## 23.3 News

`news.categorize`, `news.brief`, `InsightMixin.newsfeed`.

Categories are assigned by rule, never by a model: our holdings, our pitches and watchlist, markets
and macro, earnings, mergers and deals, IPOs, AI and tech, SEC filings. An item can sit in several.
The order is holdings first, then names on our lists, then newest first. The brief at the top is
built from the mood inputs, sector moves, new highs and lows and the count of stored items; every
figure in it comes from the screen.

## 23.4 Fund performance

`portfolio.performance`, `portfolio.breakdown`, `portfolio.review`, `InsightMixin.portfolio_full`.

- **The return is time-weighted.** Each day's return is the change in value of what was held, so a
  deposit is not a gain and a withdrawal is not a loss. Log deposits and withdrawals in the form
  under the positions.
- **It is rebuilt from the log**, not stored: positions with their dates, sold positions with their
  exit, the cash figure and the cash flows, priced at stored daily closes.
- **Assumptions are printed under the chart.** If the cash log cannot explain every purchase (cash
  would have gone negative), the page says so and asks for the missing deposits.
- **Against SPY** over the same days, with the worst drop, volatility and beta.
- **Where the money is**: weight by sector, gain or loss by holding in dollars, the largest position
  and the top three, and anything above the pitch size cap.
- **What the log shows**: observations only. Each has a finding, its evidence and one thing to test.
  None is an instruction and none changes a rule.
- **News on our holdings**: only companies the fund holds, grouped by company, with risk flags
  (new SEC filings, red-flag or sharply negative headlines, a drop in sentiment, a theme shock)
  pulled to the top, each with its source link.

The fund's real positions live in the server's database (`desk.sqlite3`). They are never written to
the repository.

## 23.5 The Strategy page

`InsightMixin.strategy`, `BOOK_STORY` and `TIMELINE` in `abg/stocks/insight.py`.

Per rule book: what it buys and why that has worked, a diagram of the setup, the steps, where it
works and struggles, which market regimes it may trade in and at what size, its replay record (win
rate, average win and loss in R, expectancy) and our own pitches from it. Then: how the rules
compare, the market filter table, how a conviction score is built from its inputs (drawn as a
waterfall for a real pitch), what DAISY Rank reads off, the weekly study's ablations and pass bars,
known limits, and a timeline of how the system got here. The full study is under Deep dive.

Today's page also explains why the rules' regime and the day's market mood can disagree.

## 23.6 Research ideas from the market scan

`watchlist.find_ideas`. The morning scan adds up to `watchlist.ideas` (default 5) names the rules
**cannot** pitch (below the eligibility floor or too new) that are up strongly over a month, above
their 50-day average and above a lower floor (`idea_min_adv_usd`, `idea_min_price`). They appear in
their own section of the Watchlist, labelled "Research idea (untested)", and never in the top picks.
They are starting points for an analyst pitch.

The Watchlist page now shows today's scan first. Names carried over from earlier scans are folded
under "Still on the clock".

## 23.7 Settings and API

| Setting | Default | What |
|---|---|---|
| **`ABG_STOCKS_POST_ANALYST`** | **`false`** | Post an analyst pitch to Discord when it is filed |

`configs/stocks.yaml` `watchlist:` gains `ideas`, `idea_min_adv_usd`, `idea_min_price`.

New under `/api/stocks`:

| Route | What |
|---|---|
| `GET profile/{symbol}` | The deep Explore read (`?refresh=true` rebuilds it) |
| `POST analyst-pitch` | File an analyst pitch (admin) |
| `GET newsfeed` | `?category=&symbol=&q=&days=&limit=` |
| `GET strategy` | The Strategy page's data |
| `GET portfolio` | Now also returns `performance`, `breakdown`, `review`, `news`, `flows` |
| `POST portfolio/flow` | `{day, amount, note}` or `{delete: id}` (admin) |

`GET desk` adds `regime_note`; `GET pitch/{key}` adds `kind` and `analyst` to `pack`; the scorecard's
`track` adds `by_kind`. Existing keys are unchanged.

## 23.9 News on view

`DayMixin.name_news`, `GET /api/stocks/name/{symbol}/news`.

Opening a stock on Explore fetches its headlines, scores them with the same finance word list, stores
them and updates its daily sentiment. The page shows what is stored first, then fills in.

- **First view reaches back 30 days** (`ABG_STOCKS_NEWS_BACKFILL_DAYS`), so the sentiment line and
  the 20-day baseline exist at once. Later views read the last three days.
- **Cooldown**: one fetch per name per 30 minutes (`ABG_STOCKS_NEWS_VIEW_MINUTES`). Two people opening
  the same name share one fetch. A failed fetch is retried on the next view.
- **Keeps collecting**: a viewed name is added to the scheduled news passes for five sessions
  (`ABG_STOCKS_NEWS_FOLLOW_SESSIONS`), after holdings, pitches and the watchlist, inside the
  40-names-per-pass budget.
- **Sources**: Finnhub first. If it returns fewer than three items, Polygon's ticker news is asked
  (`ABG_STOCKS_NEWS_POLYGON`, on by default). A source the key's plan does not cover is reported in
  the status line and skipped. Tiingo News is a paid add-on and is not used.
- **One story once**: the same headline for the same name from two sources is stored once.
- Opening a page never posts to Discord and never fires an alert. `ABG_STOCKS_NEWS_ON_VIEW=false`
  turns the whole thing off. Sentiment stays untested as a trading signal.

## 23.8 Known limits

- The forecast and the valuation on the Explore page are model estimates. They explain; they do not
  gate a pitch.
- Fund performance is only as good as the log: a position logged late, or a deposit not logged, moves
  the line.
- SPY's sector weights are not in data, so the sector chart shows the fund alone.
- An analyst pitch has no back-test behind it. Its record on the Scorecard is the only evidence.
- One old test (`test_extsignals.py::test_multi_message_and_earnings_blackout`) depends on the
  calendar: its sample message says "earnings 11/12", so it fails when run on that date (seen on
  November 12 2026, UTC). It is unrelated to this chapter and passes on other days.
