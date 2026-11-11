# 22. The stock-pitching desk, the research day and the new dashboard (Nov 10 2026)

The stock desk no longer runs a pretend portfolio. It researches the whole US market, pitches names
with a plan, and tracks how its pitches did. This chapter covers what changed on top of chapter 21
(the rule books, the pitch format and the top-5 board are unchanged and still documented there).

What never changes: **the rules decide**. Entries come from the daily close and the tested entry is
the next open. Everything in this chapter explains, ranks, alerts and monitors. Nothing here changes
which trades fire. Anything that looks like a new intraday entry is labelled **untested** and scored
apart.

## 22.0 For analysts: what is new and what a normal day looks like

Open the dashboard. The front page is **Today**. Every page opens simple; the **Deep dive** switch in
the top bar shows the technical layer in place, and remembers your choice.

| Time (ET) | What happens | Where you see it |
|---|---|---|
| 08:30 | The morning scan builds the watchlist: names close to a rule trigger, names the news put in play, IPOs | Watchlist |
| 08:45 | The watchlist digest is posted (when `ABG_STOCKS_WATCH_POST=true`) | Discord |
| 09:00 | "Today's top 5: buy at the open", with decks | Discord, Today |
| 09:30–16:00 | Live changes on the picks, active pitches and watchlist names: in or out of the entry zone, near or at the stop, targets, gaps, unusual volume, material news, theme shocks | Today → Live updates; Discord when `ABG_STOCKS_LIVE_ALERTS=true` |
| 12:00, 15:00 | Check-ins on the top 5, saying what changed since the last post | Discord, Today |
| 16:20 | The daily pass: the rules run on the close, new pitches are written, the whole market is re-screened | Pitches, Explore |
| Fri 16:45 | The weekly recap (when `ABG_STOCKS_WEEKLY_RECAP=true`) | Discord |

Where to find things: **Watchlist** for what to look at and what each name still needs; **Pitches**
for every idea and its memo, deck, plan and team notes; **Scorecard** for how the pitches have done;
**Explore** to search any US stock and read why it is or isn't pitched; **Research** for whether the
rules still earn their place; **Settings** for what is switched on. Hover or tab to any dotted word
for its definition; the full list is at `#/glossary`.

## 22.1 From paper desk to pitching desk

**Removed from everything users see** (dashboard, Discord, memos, decks, API): equity, P&L in
dollars, positions, drawdown, "the book holds X". The rule engine still replays history on every
pass; that replay is a research tool that gives base rates, the regime and which names the rules
pick. It is not "our book".

**Every pitch is an idea with a lifecycle**, stored in `<data_dir>/stocks/desk.sqlite3`
(`abg/stocks/pitchbook.py`, `deskdb.py`) with a full history of status changes:

| Status | Meaning | Becomes the next status when |
|---|---|---|
| new | Pitched at the close; enters at the next open | A session after the signal has traded |
| active | Entered; the plan is running | The earliest of: stop touched, T2 reached (or T1 when there is no T2), the time limit, a rule exit |
| closed | Done, with a reason (`stop`, `target`, `time`, `rule exit`) and a close price | — (the lifecycle only moves forward) |

A day that touches the stop and something else counts as the stop. A rule exit closes the pitch at
the open after the exit signal. Books with no stop (the momentum books) close on a rule exit, with a
252-session backstop.

**Ratings are decided against our own open pitches**, not replay holdings (`pitch_plan.rating`):
a new entry is BUY; on a name another book already pitched it is ADD; on a name the same book
already pitched it is HOLD (no second pitch). A rule exit is SELL, or TRIM when another book's pitch
stays open. A rule exit on a name we never pitched is shown in the deep dive and is not posted.

**Sizing is a share of the fund.** With `pitch.fund_equity` set, a pitch also gives shares and
dollars; without it, percent only. Risk-to-stop sizing, the conviction scale and the
`max_position_pct` cap are unchanged. The momentum books carry no stop and use the weight the
book's own rule gives the name.

**Fund fit compares against the fund's real holdings**: the list saved in Settings (kept in the
server's data folder, never in the repo) or `configs/fund_holdings.yaml`. With no list the section
says "holdings not provided" and assumes nothing. This repo is public: do not commit real holdings
unless they are public.

**The scorecard is a pitch track record** (`PitchBook`, `desk.scorecard()["track"]`): each pitch's
return from the tested entry, the same days for SPY and for its sector ETF, hit rates for T1, T2 and
the stop, and cuts by book, conviction bucket, DAISY Rank, sector and (untested) news sentiment at
pitch time. Tested and untested refinements stay apart.

### Migration notes

| Old (paper) | Now |
|---|---|
| `pitches.json` | Carried into the `pitch` table once, on first start (`_migrate_pitches`). Every row becomes a pitch with its marks; its history says "carried over from pitches.json". The file is left in place. Proof: `scorecard()["migration"]` = `{rows_in_file, carried_over}`, shown on the Scorecard deep dive; `tests/test_pitchbook.py::test_pitches_json_is_carried_into_the_pitch_book_once` |
| `state.equity`, `drawdown`, `gross`, `budgets`, `positions`, `bench_vs` in `today.json` and the API | `null` (deprecated; kept so old readers do not break) |
| pack `shares`, `notional`, `weight`, `risk_pct`, `r_dollars` | `null`. The replay's own order is under `pack["replay"]` for the ledger a ranker learns from |
| pack `context.equity`, `context.drawdown`, `context.book_budget` | `null` |
| `plan.risk.equity` / `shares` / `notional` / `risk_dollars` | Set only when `pitch.fund_equity` is set; `pct_of_fund` and `risk_pct_of_fund` always |
| `plan.fit.already_held`, `corr_holdings`, `sector_before/after` | Against the fund's real holdings; `null` plus `holdings_provided: false` when there is no list |
| `plan.exit_review` (from the paper position) | From our open pitch on the name: tested entry, return, R. `pnl` and `shares` are `null` |
| Digest field "Book: N positions · equity …" | "Pitch book: N active · N new · N closed" |
| Tag `PAPER` | `RESEARCH` (`ABG_STOCKS_TAG`); "research, not advice" stays on every pitch and post |
| `ABG_STOCKS_INITIAL_EQUITY` | Still the capital the replay starts from, for base rates only |
| Intraday "paper positions near their stop" | The live layer watches active pitches (22.5) |

## 22.2 Research covers every US stock

**Coverage and eligibility are separate** (`abg/stocks/screen.py`). The screen has one row per
US-listed common stock and ADR (Polygon types `CS,ADRC`), built from the same indicator class the
rule books read, so a number on the dashboard is the number the rule would use. Every name is
scanned, ranked, graded and searchable. Only **eligible** names are pitched: `pitch.eligibility`
in `configs/stocks.yaml` (price ≥ $5, 20-day average dollar volume ≥ $20M, ≥ 60 sessions). A name
that is not eligible says why, on its Explore card and in `why_not`.

- `ABG_STOCKS_MARKET_MAX` now defaults to `0`, which means no cap: every liquid name is in the rule
  universe. A positive number still caps it.
- The list of listed names is re-read daily (`ABG_STOCKS_UNIVERSE_REFRESH_DAYS=1`), so a new listing
  appears the day after it starts trading. The cache keeps names and a first-seen date.
- Market history is `ABG_STOCKS_MARKET_SESSIONS=500` (about the two years the free Polygon plan
  allows), filled gradually: newest sessions first, `ABG_STOCKS_MARKET_BACKFILL_CALLS=25` older ones
  per day, so a deep backfill never holds up the day's posts.

**Two things kept the wider market out until Nov 11 2026, both fixed.** The list of listed names
was being cut to its first page (about 1,400 names instead of about 5,500): the paging link's cursor
was dropped by the HTTP library, so page two came back as an unfiltered first page. And Polygon's free
plan publishes a session's whole-market bar overnight, after the evening pass has run, so no name
outside the per-symbol S&P 500 core had a bar for the session and none could signal. The evening pass
now waits only until `ABG_STOCKS_MARKET_WAIT_UNTIL` (17:00), and a **morning catch-up pass**
(`ABG_STOCKS_MARKET_CATCHUP`, 06:30 ET, no post) fetches the bar and re-runs the rules, so the 08:30
watchlist and the 09:00 top 5 are picked from the whole market. `last_daily.market_bar` says whether a
pass saw the wider market's bar.

**IPOs and new listings have their own track.** The momentum and trend rules need about a year of
history, so a name with fewer than `too_new_sessions` (252) bars is marked **too new for the tested
rules**: watched and researched, not rule-pitched. Upcoming listings come from Finnhub's IPO
calendar, so the desk knows about an IPO before day one; recent listings also come from the screen.
They are on Explore and on the morning watchlist. Lock-up expiry is not in a free source: not in
data.

**Scale.** The design is still one Polygon call per session. The screen pass is vectorised: on a
synthetic panel of 6,000 names × 500 sessions it builds in about 4 s and works out every eligible
name's setups in about 20 s, at under 1 GB peak (measured in development). On the server the real
numbers are recorded on every pass: `last_daily.screen` (covered, eligible, too new, seconds),
`last_daily.seconds` and `last_daily.memory_mb`, shown on Today and Settings in the deep dive.

**Survivorship.** Outside the dated S&P 500 core, the list is today's listed names. Delisted
companies are missing, which flatters any backtest on those names. The Research page says so.

## 22.3 News and market sentiment

Sources, all free-tier and rate-limited (`abg/stocks/feeds.py`): Finnhub market news (general and
merger), Finnhub company news for watched names, the day's movers and every name with an active
pitch, Finnhub's IPO and earnings calendars, and SEC EDGAR filings (8-K, S-1/F-1, 13D/G, Form 4)
from the free JSON API with a User-Agent that names the app (`ABG_STOCKS_EDGAR_USER_AGENT`).
Analyst rating changes are not on Finnhub's free plan: not in data. Articles are deduplicated,
timestamped, source-linked and stored in SQLite.

Scoring is code, not an LLM (`abg/stocks/news.py`):

- **Per article:** sentiment in [-1, 1] from a finance word list with negation, and relevance (1.0
  the headline names the company, 0.6 only the summary does, 0.3 tagged by the feed).
- **Per name:** level (last 3 days), change against its 20-day baseline, article count against
  normal.
- **Market mood:** risk-on, neutral or risk-off as a count of named signals: the share of eligible
  names above their 50- and 200-day averages, 52-week highs against lows, VIX and VIX over 3-month
  VIX, sector ETFs above their 50-day average, and the market headlines' score. The sentence on
  Today lists the signals that fired.

**Themes and theme shocks.** `configs/themes.yaml` maps related names (AI compute, AI power and
data centres, AI models and software, and other groups). Each day, names 60-day correlated with a
theme's basket are added and shown apart. A **theme shock** is reported when enough members move
the same way, when one name moves hard and others follow, or when the day's headlines match a
theme's keywords. Keywords are how a company with no ticker reaches the desk: "anthropic" sits on
the AI-models theme next to the listed names it touches. A shock links its source and lists each
affected name with its move; it appears on Today, in the morning watchlist and in live alerts.

**How news affects pitches:**

| Use | What it does |
|---|---|
| Discovery | News-heavy and theme-shock names join the watchlist research queue the same day |
| Risk flags | A negative or red-flag headline, a filing, a sentiment collapse or a gap on news is listed in the memo with its source link |
| Conviction | Two documented, configurable terms: the name's news sentiment and the market mood (`pitch.conviction.news_*`, `market_risk_*`), shown in the conviction breakdown |
| Gates | None. A sentiment rule may gate an entry only after the R&D study shows it helps out of sample. Until then every pitch records its sentiment bucket, and the Scorecard cuts results by it under "untested" |

The pullback book's old optional `sentiment_floor` reads a different table and is untouched: the
new scores are never written where a rule could read them.

## 22.4 The morning watchlist

Built at `ABG_STOCKS_WATCH_SCAN_TIME` (08:30) from the screen, the news store and the calendars
(`abg/stocks/watchlist.py`). Each card has:

**DAISY Rank** (`abg/stocks/rank.py`), a plain mapping anyone can check:

| Rank | Means | When |
|---|---|---|
| 1 Strong buy | The rule triggered | Conviction is High (≥ 70) |
| 2 Buy | The rule triggered | Conviction is Medium (≥ 45) |
| 3 Hold / near trigger | — | Triggered with Low conviction; an open pitch we are holding; or one step from a trigger |
| 4 Watch | — | Two steps from a trigger, in the news or theme queue, an IPO, or a trim |
| 5 Avoid | — | A rule exit, a closed pitch, or dropped from the list |

**Style grades A–F.** Momentum (12-1 momentum over volatility) and Trend (distance from the 200-day
average) are graded against the eligible universe: top fifth A, bottom fifth F. Value is forward P/E
against the sector peers' median (≤ 0.80× A … > 1.30× F). Quality is operating margin (≥ 25% A, ≥ 15%
B, ≥ 8% C, ≥ 0 D, below F). News/Sentiment is the name's 3-day level (≥ 0.35 A … < −0.35 F) and needs
`news.min_articles`. A grade that cannot be computed is "—", never a guess.

**The strategy to follow:** the book, the exact condition still needed (for example "a close above
$101.00 (today's high) and the MACD histogram turning up", or "RSI(2) below 10 at the close: a close
under about $357"), and the stop it would use. **The timeline:** earliest trigger, expected hold from
the book's replayed trades, next review, earnings date, and the date it leaves the list
(`watchlist.watch_sessions`, 10 sessions).

**Lifecycle:** watching → triggered (a pitch) → in play → exited, or dropped. Stored with history.
Every morning the list leads with **what changed**: added, removed, rank up or down, triggered
overnight. The post (`ABG_STOCKS_WATCH_POST=true`, 08:45) shows up to `watchlist.post` names grouped
by rank and a **Name of the day**, written from the card's own numbers (no LLM, so nothing to
validate). It is tested against Discord's limits.

**Scoring the watchlist:** `watch_score()` on the Scorecard and Watchlist deep dive: by the rank a
name joined with, how often a pitch followed within 15 days, and its 10-session return against SPY.

## 22.5 The live layer

`abg/stocks/alerts.py`. It watches the day's top picks, every active pitch and the watchlist's
near-trigger names (up to `ABG_STOCKS_LIVE_MAX_NAMES`) every `ABG_STOCKS_LIVE_SECONDS` during the
session, and reports **changes of state**, never a level that is merely still true:

zone entered or left · within `near_stop_atr` of the stop · stop touched · T1 or T2 reached · a gap
over `gap_atr` at the open · volume over `rel_volume` × the 20-day pace for the time of day · a
material headline or filing · a sentiment swing · a theme shock touching a watched name · and
**setup forming (untested)**: the partial bar would trigger a rule if the day closed here. That one
is never a buy; its confirmation rate is on the Scorecard.

Each alert is one plain sentence, the numbers, what the plan says to do, and a link to the pitch
(`ABG_STOCKS_DASHBOARD_URL`). Events in the same tick go out as one post. The gate gives one alert
per name per `cooldown_min` (stops and targets always go), one per (name, event, window), and caps
per tick and per hour, so a reconnect storm cannot flood Discord. With `quiet: true` nothing posts
outside 9:30–16:00. State is kept in the database, so a restart re-sends nothing.

The 9:00, 12:00 and 15:00 posts use fresh quotes and now say what changed since the last post.

Reliability: the heartbeat is in `/api/health` (`stocks_live`) and on Settings. If quotes fail for
half the watched names, the next post says so, and after three failed looks one notice is posted.

Quotes are polled, not streamed. Finnhub's free websocket would cut the delay, but polling 40 names
every two minutes sits inside the free 60 calls a minute and has no reconnect logic to get wrong.

## 22.6 The dashboard

`/` is the pitching desk (`abg/api/static/daisy.html`, `daisy/`). The earlier terminal (single-name
analysis, forecasts, the retired MNQ and swing desks' records) is at `/classic`, out of the main
navigation.

**Look.** The feel is borrowed from vandstrom.com, measured from its computed styles: dark green
`#213438`, green `#446266`, light green `#5d7f80`, ivory `#e4d7d0`, dark ivory `#8c8683`, 15%-alpha
hairlines, a floating translucent pill nav, a 9-column grid with 20px gaps, 24px and 12px radii, no
shadows, alternating sections, an editorial serif for headings and wide caps for labels. None of
their assets or licensed fonts are used: the fonts are Fraunces, Unbounded and Inter, self-hosted
under the SIL Open Font License (`daisy/fonts/LICENSES.txt`), so the page works with no CDN.
Everything is a token in `daisy.css`. Dark (green) is the default; light (ivory) is the alternate;
the choice is remembered per viewer. Up, down and warning colours are colour-blind-safe (blue and
orange families) and always come with an arrow and a sign. The pitch charts use the same tokens in
both themes, and their series colours pass the dataviz validator on each surface.

Dense data stays compact: tables at 13px, hairline rows, no stripes, right-aligned tabular figures.
The rolling nav hover and the soft parallax are kept; the custom cursor is not. All motion is off
under `prefers-reduced-motion`.

**Destinations** (each opens simple; Deep dive adds the right column in place):

| Destination | Simple | Deep dive |
|---|---|---|
| Today | Mood in a sentence, theme shocks, sector and theme heat strip, the top 5 with plain-English status, watchlist preview, live updates, calendar | Regime, flags, rule weights, every candidate, the run log |
| Watchlist | Cards: rank, grades, the plan in one line, timeline, why, latest status; name of the day | Passing and missing checks, conviction, whether the rank has meant anything |
| Pitches | Open and closed pitches; one pitch: plan as a timeline, flags, lifecycle, memo with charts, deck download, team notes and votes | Conviction breakdown, valuation arithmetic, comps, write-up validator log, pack JSON |
| Scorecard | "How have our pitches done?" in a sentence; by conviction, rank, book, sector; the watchlist's score | Plan marks, tested against untested, every pitch |
| Explore | Search any US stock: verdict, rank, grades, closest setups, news and sentiment; IPOs and new listings | Rule-by-rule checks, every indicator, the sentiment series |
| Research | The latest study's verdict in plain words; pass bars; known limits | The full report |
| Settings | Switches, schedule, which keys are present (yes or no, never a value), coverage; admins edit fund holdings and run things | Raw status |

**Accessibility.** Semantic landmarks, headings in order, real tables with captions, a skip link,
visible focus, the whole app by keyboard, `aria-live` for toasts and live updates, alt text on charts
and sparklines, tooltips that open on focus as well as hover. Text contrast is AA in both themes
(ivory on green 9.3:1, muted text 6.2:1 on the page and 5.0:1 on cards; dark green on ivory 9.3:1, muted
text 5.2:1; up, down and warning colours 4.8:1 or better on the page and on cards).

**Weight and QA.** Vanilla ES modules, no build step, about 83 KB of HTML, CSS and JS before
compression. Every destination was rendered with Playwright at 375px and 1440px, in both themes and
both depths (72 renders): no console errors and no horizontal scroll. A set is in
`docs/samples/screens/`.

## 22.7 Level-ups shipped, and not

Four shipped, chosen for daily use without a new page:

1. **Team notes and votes** on a pitch (display name, 👍 / 👎 / hold, a short note; viewers may post).
   It builds the fund's decision log next to the idea it is about.
2. **Earnings, IPO and review calendar** for the watched names, on Today.
3. **Sector, theme and market heat strip**, one line on Today.
4. **Weekly recap** on Fridays: five bullets from the track record (`ABG_STOCKS_WEEKLY_RECAP`).

Not shipped: **Ask DAISY** (an LLM answering from one pack needs its own validator work and costs
tokens per question) and **personal alert filters** (a preference store per person for a small
gain while the team is four people).

## 22.8 Settings

`.env` (defaults in `abg/config.py`). Everything that posts somewhere new starts off.

| Setting | Default | What |
|---|---|---|
| `ABG_STOCKS_TAG` | `RESEARCH` | Shown on every post |
| `ABG_STOCKS_FUND_HOLDINGS` | unset | Path to a holdings file (default `configs/fund_holdings.yaml`); the Settings list wins |
| `ABG_STOCKS_MARKET_MAX` | `0` | Cap on the rule universe; 0 = unlimited |
| `ABG_STOCKS_MARKET_SESSIONS` | `500` | Whole-market sessions kept |
| `ABG_STOCKS_MARKET_BACKFILL_CALLS` | `25` | Older sessions fetched per day until that many are stored |
| `ABG_STOCKS_UNIVERSE_REFRESH_DAYS` | `1` | How often the list of listed names is re-read |
| `ABG_STOCKS_MARKET_WAIT_UNTIL` | `17:00` | How long the evening pass waits for the whole-market bar |
| `ABG_STOCKS_MARKET_CATCHUP` | `06:30` | Morning catch-up pass once that bar is out (no post); empty = off |
| `ABG_STOCKS_NEWS` | `true` | Store headlines and filings (needs `ABG_FINNHUB_API_KEY`) |
| `ABG_STOCKS_NEWS_MINUTES` / `_MAX_NAMES` | `30` / `40` | News re-read cadence and names per pass |
| `ABG_STOCKS_EDGAR` / `_EDGAR_USER_AGENT` | `true` / app name | SEC filings and the User-Agent sent to the SEC |
| `ABG_STOCKS_THEMES` | unset | Path to the theme map (default `configs/themes.yaml`) |
| `ABG_STOCKS_WATCH` / `_WATCH_SCAN_TIME` | `true` / `08:30` | Build the morning watchlist |
| **`ABG_STOCKS_WATCH_POST`** / `_WATCH_POST_TIME` | **`false`** / `08:45` | Post it to Discord |
| `ABG_STOCKS_LIVE` / `_LIVE_SECONDS` / `_LIVE_MAX_NAMES` | `true` / `120` / `40` | Live watch for the dashboard |
| **`ABG_STOCKS_LIVE_ALERTS`** | **`false`** | Post live alerts to Discord |
| `ABG_STOCKS_DASHBOARD_URL` | unset | Base address for "open the pitch" links |
| **`ABG_STOCKS_WEEKLY_RECAP`** / `_TIME` | **`false`** / `16:45` | Post the Friday recap |

`configs/stocks.yaml`: `pitch.fund_equity`, `pitch.eligibility`, `pitch.conviction.news_positive /
news_negative / market_risk_on / market_risk_off`, and the new `news:`, `watchlist:` and `alerts:`
blocks, each key commented. `configs/themes.yaml` and `configs/fund_holdings.yaml` are new.

All posts use the one stock webhook and the one poster (`StockDesk._post_to`).

## 22.9 API

New under `/api/stocks`: `desk`, `watchlist` (+ `POST watchlist/scan`, `watchlist/{symbol}/history`),
`pitches`, `pitch/{key}`, `scorecard`, `explore`, `name/{symbol}`, `news` (+ `POST news/refresh`),
`live` (+ `POST live/tick`), `notes/{id}` (GET, POST), `holdings` (GET, POST), `glossary`,
`settings`, `recap` (GET, POST). No route that a page can click posts to Discord by default. A
viewer token may POST notes and nothing else; every other change needs the admin token.
`/api/health` adds `stocks_live`.

## 22.10 What the owner needs to do

1. Push `main`, then on the server: `bash ~/abg/deploy/update.sh`.
2. To turn the new posts on, add to `~/abg/.env` and restart: `ABG_STOCKS_WATCH_POST=true`,
   `ABG_STOCKS_LIVE_ALERTS=true`, `ABG_STOCKS_WEEKLY_RECAP=true`. They use the existing stock
   channel; make a second channel and webhook only if the team wants alerts kept apart.
3. Optional: `ABG_STOCKS_DASHBOARD_URL=https://<your-tailnet-address>` so alerts link to pitches.
4. Optional: enter the fund's holdings in Settings, and set `pitch.fund_equity` if share counts are
   wanted.
5. No new API keys. News, calendars and quotes use the Finnhub key already set; without it those
   parts say "not in data".
6. The first daily pass after the update takes longer while market history backfills toward 500
   sessions (25 older sessions a day). Check Settings → Deep dive for covered and eligible counts,
   the pass time and memory.

## 22.11 Known limits

- Polling, not streaming, for live quotes (22.5). Free quotes can be delayed.
- The free Polygon plan gives about two years of whole-market history, so names outside the S&P 500
  core replay over two years.
- Value and Quality grades exist only for names whose fundamentals were fetched (the day's best
  candidates); elsewhere they are "—".
- Relative volume needs a quote that carries volume; without it that alert does not fire.
- The sentiment word list is deliberately simple and auditable. It will miss sarcasm and context.
  It informs; it never gates.
- Lock-up expiries and analyst rating changes are not in a free source: not in data.
- `docs/samples/` uses a synthetic panel. Tickers there are not real companies.
