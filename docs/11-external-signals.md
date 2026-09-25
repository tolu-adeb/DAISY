# 11. External signals: interpret, track, decide, relay

This module takes trade ideas that other people post (a Discord signals channel, a newsletter, a
message you paste in) and handles them in five steps:

1. **Interprets** the message: ticker, direction, entry zone or trigger, stop, targets, timeframe.
2. **Validates** it against live market data. Missing stops and targets get sensible defaults, and
   typos or option-premium levels are rejected.
3. **Tracks** it for as long as it takes, whether days or weeks: approaching → entry → targets → stop / exit.
4. **Decides** at each step (enter, block a weak entry, take profit, move the stop, stop out, time
   exit) and **explains why** using the live analysis: trend, composite signal, RSI, ATR, reward:risk,
   the Monte Carlo odds of target-before-stop, the prediction model and news sentiment.
5. **Relays** every decision back to the signals channel as a rich Discord embed, and to the dashboard,
   desktop pop-ups and email.

All tracking is **paper**. Nothing here places orders.

```
 Discord channel(s) ──poll (bot token)──┐                          ┌──► Discord relay (webhook / threaded reply)
 dashboard "Signals" tab ───────────────┤                          ├──► NotificationHub: dashboard feed, desktop, email
 abg ext add "..." ─────────────────────┤                          │
                                        ▼                          │
                    parser.parse ──► ExtSignalTracker ──► commentary.explain (why / plan / risks)
                                        │  ▲                       │
               lifecycle.step (pure) ◄──┘  │ quotes (60 s), analysis + re-grade (15 min), daily-bar catch-up
                                        ▼
                         signals.sqlite3 (ideas · events timeline · messages · cursors)
```

## 11.1 Quick start

```bash
abg ext parse "$NVDA swing long entry zone 117.50-119, SL 112, TP1 130 TP2 138"   # preview, no network
abg ext add   "$NVDA swing long entry zone 117.50-119, SL 112, TP1 130 TP2 138" --author alpha-desk
abg ext list                      # open ideas: distance to entry, grade, R
abg ext show 1                    # full decision timeline with reasoning
abg ext add "TP1 hit on NVDA, moving stop to breakeven" --author alpha-desk   # updates apply to the open idea
abg ext stats                     # track record by source: win rate, avg R, profit factor
abg serve                         # or: abg monitor  -> tracks continuously + polls Discord
```

In the dashboard, open the **Signals** tab. Paste a message, click **Interpret** to preview it, and
click **Track it** to start tracking. Select any idea to see its timeline, set a new stop, trim, close
or cancel.

## 11.2 What the parser understands (`extsignals/parser.py`)

| Message | Interpreted as |
|---|---|
| `$NVDA swing long 🟢 entry zone 117.50-119, SL 112, TP1 130 TP2 138` | long, zone 117.50–119, stop 112, targets 130/138 |
| `BUY AAPL @ 180 - 182 \| Stop: 175 (daily close) \| Targets: 190 / 195 / 200` | long zone, **close-basis** stop, 3 targets |
| `Short SPY below 505, stop 512, target 490, 480` | short, breakdown below 505 |
| `TSLA breakout over 252 -> 265 / 280, invalidation 244` | long, breakout above 252 |
| `entries 101, 99.5, 98 on AMD, stop 94, pt 110` | long zone 98–101 (scale-in levels) |
| `NQ long 18250-18270 sl 18190 tp 18400` · `BTC long 60000-61000 sl 58000 tp 65000` | futures `NQ=F`, crypto `BTC-USD` |
| `AMD 170c 11/15 entry 3.20-3.50` | option: premium levels, **rejected** unless they're underlying prices |
| `TSLA long here sl 240 tp 280` | market entry at the live price |

The parser first normalises phrases ("take profit" → tp, "stop loss" → sl, arrows → targets) and
strips emoji, markdown, dates, percentages, times, option strikes and holding periods. A small
tokenizer then assigns each number to the current role (entry / stop / target). Modifiers such as
*above / below / breakout / market / here* pick the entry trigger, and the direction comes from
words, from option side, or from where the stop sits. Every result has a `confidence` and
`warnings` (for example "stop is on the wrong side of the entry").

**Updates** from the source are recognised and applied to the right idea. A Discord reply to the
original call matches that exact idea. Otherwise the terminal uses the latest open idea for that
ticker from the same author/channel. With no ticker, it uses that source's only open idea, if it has exactly one:

| Update | Effect |
|---|---|
| `TP1 hit on NVDA, moving stop to breakeven` | take the TP1 slice (if our data hasn't already), stop → entry |
| `NVDA: raise stop to 121` / `move stop to 121` | stop moved (ignored if it would stop out instantly) |
| `stop to BE` | breakeven |
| `trim half AMD` | close 50% of what's left |
| `closing TSLA here` / `AAPL stopped out` | close the paper position at the live price |
| `cancel the SPY short` | cancel a pending idea, or close an active one |

## 11.3 Validation and defaults (`tracker._prepare`)

| Check | Result |
|---|---|
| entry more than `ABG_EXT_MAX_ENTRY_DISTANCE_PCT` (35%) from the live price | **rejected** (typo / wrong ticker) |
| option idea whose levels look like premiums | **rejected** with an explanation |
| stop on the wrong side of the entry | **rejected** |
| no stop | 2 × ATR(14) beyond the entry edge (flag `default_stop`) |
| no targets | 2R and 3R (flag `default_targets`) |
| duplicate (same author, symbol, direction, entry and stop) | ignored, points to the existing idea |
| entry expiry | swing `ABG_EXT_ENTRY_EXPIRY_DAYS` (45), position ×2, day 2 days, scalp 1 day |
| max hold after entry | swing `ABG_EXT_MAX_HOLD_DAYS` (90), position ×3; a stated holding period ×1.5 |
| paper size | `ABG_EXT_ACCOUNT_SIZE × ABG_EXT_RISK_PCT% / (entry edge − stop)` |

"Entry edge" is the worst price of the zone: the top for a long, the bottom for a short. R:R and
sizing are planned from it, so filling lower in the zone only improves the trade.

## 11.4 The lifecycle (`extsignals/lifecycle.py`)

`step(idea, obs)` is a **pure** state machine, fully unit-tested. `obs` is either a live quote
(high = low = price) or a daily bar during catch-up.

```
pending ─► approaching (once, within ABG_EXT_APPROACH_PCT)
   │   ─► entry_blocked (grade below ABG_EXT_MIN_ENTRY_GRADE; re-graded every ABG_EXT_REGRADE_SECONDS)
   │   ─► invalidated (stop traded, or gapped through it, before entry)
   │   ─► missed      (TP1 reached, or the fill would already be past TP1)
   │   ─► expired     (entry never reached in time)
   └─► entry ─► target_hit (equal slice per target) ─► stop_moved (→ breakeven after TP1, → prior target after TP2+)
             ├─► stop_hit / breakeven_stop / trailing_stop
             ├─► time_exit (max hold)
             └─► exit / trim (source or user)
```

**Entry triggers**

| Type | Fills when | Fill price |
|---|---|---|
| zone (long) | price trades at or below the top of the zone | the open if it opened inside/below the zone, else the zone top |
| zone (short) | price trades at or above the bottom of the zone | mirror |
| breakout_above / limit_above | high ≥ level | max(level, open) (gaps fill at the open) |
| breakdown_below / limit_below | low ≤ level | min(level, open) |
| market | immediately | live price |

**Rules that keep paper results honest**

- If a daily bar touches both the stop and a target, the **stop is assumed first**, because the
  intrabar order is unknown.
- Gaps through a stop fill at the open, not at the stop.
- Targets are resting limits. On live quotes they fill **at the target**. On a daily bar that gaps
  past a target they fill at the better open.
- **Close-basis stops** ("stop on a daily close below…") ignore intraday pokes and are judged on the
  session close. The tracker runs one bar-type check after 16:00 ET on trading days.

## 11.5 Grading entries (`commentary.grade`)

When the price reaches the entry, the setup is graded **A–D** from the live analysis:

| Factor | + | − |
|---|---|---|
| trend (regime) | with the trend (+1) | counter-trend (−1) |
| composite signal | agrees ≥ 20 (+1) | disagrees ≤ −20 (−1) |
| simulated P(TP1 before stop) | ≥ 45% (+1) | < 30% (−1) |
| reward:risk to TP1 | ≥ 1.5 (+1) | < 1.0 (−1) |
| RSI | room to run (+0.5) | stretched > 75 / < 25 (−0.5) |
| stop distance in ATR | | < 0.5 × ATR (noise) or > 4 × ATR (−0.5) |
| baseline risk level | | High / Extreme (−1) |
| prediction model view | agrees (+0.5) | opposes (−0.5) |
| news sentiment (≥ 3 articles) | agrees (+0.5) | opposes (−0.5) |

Scores map to grades: A ≥ 3, B ≥ 1.5, C ≥ 0, D < 0. With the default `ABG_EXT_MIN_ENTRY_GRADE=C`, a
**D** setup is not entered. You get an `entry_blocked` message with the reasons, the idea is
re-graded while it stays pending, and it enters if conditions improve. Set the gate to `none` to take
every trigger, or `B` to be pickier.

The **simulated odds** come from the same Monte Carlo ensemble as the prediction engine (docs/10):
GBM with Student-t shocks plus filtered historical simulation, 1,500 paths. They are run on the
idea's own stop and targets, starting from the zone midpoint (pending) or the live price (active),
over a horizon set by the timeframe (swing ≈ 30 trading days).

**Advisories.** On every analysis sweep, active trades are re-checked. At least two of these
conditions produce one `advisory`, with a suggested 1.5 × ATR stop: the trend turned against the
trade, the signal opposes it by 30 or more, the model flipped, or risk is High/Extreme. The same
advisory repeats at most once a day. An advisory is a warning, not an automatic exit.

## 11.6 Discord: reading signal channels

The terminal reads Discord with a **bot you create**. A bot can only read servers it has been added
to, and you can only add a bot to a server where you have *Manage Server*. For a signals server you
don't run (a paid group, a public signals Discord), the plan is:

```
 Signals server (not yours)            Your own server (free, private)                 ABG Terminal
 ┌─────────────────────────┐   Follow   ┌──────────────────────────────┐   bot reads   ┌───────────────┐
 │ #nq-signals (announce.) │ ─────────► │ #signals-in   (copies arrive) │ ───────────► │ parse · track │
 └─────────────────────────┘ or Forward │ #abg-decisions (our relays)   │ ◄─────────── │ decide · relay│
                                        └──────────────────────────────┘   webhook     └───────────────┘
```

You only need to do this once. It takes about 15 minutes.

### Step 1: make your own server and two channels

1. In Discord, click **+** (Add a Server) at the bottom of the server list, then **Create My Own**, then
   **For me and my friends**. Name it, e.g. "ABG Signals".
2. Create a text channel **#signals-in**. The copies of the calls will arrive here.
3. Create a second text channel **#abg-decisions**. The terminal's entries, targets, stops and
   reasoning will be posted here.
   Keeping them separate makes #signals-in an exact copy of the source.
   **If you copy trades automatically** (for example with a copier that reads a Discord channel), do
   not point the copier at #abg-decisions. Our posts say "ENTRY", "STOP", "TP1" and could be read as
   new trades.
4. Turn on **Developer Mode**: User Settings → Advanced → Developer Mode. You need it to copy IDs.

### Step 2: get the calls into #signals-in

Pick the first method that works for the channel you want to follow.

**A. Follow the channel (automatic, best).** This only works when the signals channel is an
*Announcement* channel. It has a megaphone icon instead of `#`.

1. Open the signals server and go to the signals channel.
2. Click **Follow** at the top of the channel. If there's no button, click the channel name → **Follow**.
3. Choose your server **ABG Signals** and the channel **#signals-in**, then click **Follow**.
4. From now on, every call the server *publishes* appears in #signals-in within seconds. It shows the
   source server's name and a `SERVER` tag.

Checks and caveats:

- If you can't see a Follow option, the channel isn't an announcement channel. Use B or C.
- If you can't select your server, you need *Manage Webhooks* there. You have it as the owner.
- Only *published* posts are copied. Most signal servers publish every call, but some don't publish
  updates. Watch for the first update, like "TP1 hit". If it doesn't arrive, send updates in by hand
  (see C) or accept that the terminal will manage the trade by its own rules.
- Replies aren't linked in the copy. The terminal still matches updates: "stop to BE on NQ" goes to the
  latest open NQ idea from that feed. An update with no ticker goes to the feed's open idea if there is
  exactly one; otherwise it's logged as `unmatched`.

**B. Ask the server's staff.** Many signal groups will add a bot or give you a webhook if you ask.
They can add your bot to their channel with read-only permissions. If they agree, skip to Step 3 and
use their channel ID instead.

**C. Forward the calls yourself (manual fallback).** Hover over a call in the signals channel → **⋯**
→ **Forward**, then pick **#signals-in**. Forwarded messages are read the same way as normal ones,
including embeds. You can also paste the text into the dashboard's **Signals** tab, or run
`abg ext add "…"`.

**Don't** use your own account's login (a "user token") or a "self-bot" to read the server
directly. That breaks Discord's Terms of Service and can get your account banned, including the
account you use for your funded-trading or copier setup. The terminal only supports bot tokens.

### Step 3: create the bot that reads #signals-in

1. Go to <https://discord.com/developers/applications> → **New Application** and name it, e.g. "ABG Reader".
2. **Bot** tab:
   - Click **Reset Token**, then copy the token. It's shown once. Treat it like a password.
   - Under *Privileged Gateway Intents*, turn on **Message Content Intent** and click **Save**.
     Without this intent, the bot sees empty messages.
3. **OAuth2 → URL Generator**:
   - Scopes: tick **bot**.
   - Bot permissions: tick **View Channels** and **Read Message History**. If you'll use reply mode,
     also tick **Send Messages** and **Embed Links**.
   - Open the generated URL and add the bot to **ABG Signals**.
4. In Discord, right-click **#signals-in** → **Copy Channel ID**.

### Step 4: create the relay webhook in #abg-decisions

1. Right-click #abg-decisions → **Edit Channel** → **Integrations** → **Webhooks** → **New Webhook**.
2. Name it "ABG Signal Tracker", then click **Copy Webhook URL**.

### Step 5: add it to `.env` and test

```
ABG_DISCORD_BOT_TOKEN=paste-the-bot-token
ABG_EXT_DISCORD_CHANNEL_IDS=123456789012345678        # #signals-in (comma-separate several)
ABG_EXT_RELAY_MODE=webhook
ABG_EXT_RELAY_WEBHOOK_URL=https://discord.com/api/webhooks/...   # #abg-decisions
# optional
ABG_EXT_BACKFILL_MESSAGES=20        # also read the last 20 calls on the first start
ABG_EXT_RELAY_KINDS=                # empty = every decision; e.g. entry,target_hit,stop_hit,exit,advisory
```

Then:

```bash
abg ext discord-test          # bot name + "ok: #signals-in" for each channel
abg ext discord-test --send   # posts a test embed in #abg-decisions
abg ext poll                  # reads new messages once, so you can check parsing
abg serve                     # or Start Dashboard.bat: tracks and polls every 20 s from now on
```

In the dashboard, the **Signals** tab status bar should show `discord in · 1 ch` and `relay · webhook`.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `token rejected` | Token copied wrong or reset since. Reset it again and paste the new one |
| `error: HTTP 403` on the channel | The bot isn't in that server, or can't see the channel. Check the channel's permissions for the bot's role |
| `latest message has no readable text` | Turn on **Message Content Intent** (Step 3.2) and restart |
| calls arrive but show `ignored` | Open `/api/ext/status` (see the `messages` list), or paste one call into **Interpret** to see how it's read. Unusual formats may need editing |
| updates like "TP1 hit" don't apply | With Follow, replies aren't linked. An update without a ticker only applies when that feed has exactly one open idea. Also, only published posts are copied |
| nothing is copied into #signals-in | Check the Follow in #signals-in → Edit Channel → Integrations → *Channels Followed*. The source may not publish every post |

How the poller behaves:

- **Polling**: every `ABG_EXT_POLL_SECONDS` (20) the poller calls `GET /channels/{id}/messages?after=<cursor>`.
  Cursors are saved, so a restart continues where it left off. On first start it begins from *now*,
  unless `ABG_EXT_BACKFILL_MESSAGES` is set.
- Embeds, forwarded messages and followed-channel copies are flattened to text. A cheap keyword
  filter skips chatter before full parsing.
- Replies to a call (`message_reference` in the same channel) are applied to that exact idea.
- Our own relay messages are recognised (by webhook id and bot id) and never re-ingested.
- Rate limits (HTTP 429) are honoured using `retry_after`. Errors back off up to 10 minutes and show in
  the Signals status bar.

## 11.7 Discord: relaying decisions back

| `ABG_EXT_RELAY_MODE` | Where decisions go |
|---|---|
| `webhook` (default) | `ABG_EXT_RELAY_WEBHOOK_URL`, else `ABG_DISCORD_WEBHOOK_URL`. For a server you don't own, this is a channel in *your* server (e.g. #abg-decisions, §11.6). You can't post into someone else's channel without their webhook |
| `reply` | the bot replies **to the original call** in the channel it read it from, threaded under it (needs Send Messages + Embed Links). With Follow, that's your #signals-in copy |
| `none` | no relay; decisions still go to the dashboard, desktop and email |

Each relay is an embed with the title (for example `🟢 ENTRY NVDA LONG @ 118.20 (paper) · grade B`),
a summary, **Why** (context line, aligned evidence, simulated odds, model view, grade reasons),
**Plan** (stop in ATR terms, each target with its R multiple, trade management), **Risks** (opposing
evidence) and fields for price / status / grade / stop / targets / trade R / P(TP1 first). Mentions
are disabled, so relays never ping anyone. `ABG_EXT_RELAY_KINDS=entry,target_hit,stop_hit,exit`
limits which events are relayed, and `ABG_EXT_RELAY_ACK=false` turns off the "tracking" confirmation.

When the relay is on, the generic portfolio Discord embed is skipped for these events
(`hub.publish(skip={"discord"})`), so nothing is posted twice.

## 11.8 Running it

- `abg serve` or `abg monitor` does everything. The monitor already running for your portfolio also:
  - quotes every tracked idea's symbol on each quote sweep (60 s in market hours);
  - re-grades and reviews on each analysis sweep (15 min);
  - replays missed **daily bars** at start-up (`catch_up`), so a week with the PC off still records
    the entries, stops and targets that happened. Those events say "from the 2026-09-18 daily bar,
    while the monitor was offline".
- The Discord poller only runs inside the process that holds the monitor lock, so two windows never
  ingest the same message twice.
- The `messages` table records every message and what happened to it (tracking / updated / ignored /
  rejected / duplicate / unmatched). The dashboard status endpoint shows the last 20.
- `abg ext update` runs one tracking pass by hand (catch-up + quotes + review) without the monitor.

## 11.9 Storage (`signals.sqlite3`, next to `portfolio.sqlite3`)

| Table | Contents |
|---|---|
| `ideas` | the `Idea` as JSON plus indexed status / symbol / channel / message id (unique per message) |
| `events` | the timeline: type, price, title, full explanation text, the structured explanation and a snapshot of the idea, plus per-channel relay status |
| `messages` | every ingested message and its outcome |
| `kv` | Discord cursors |

External-signal events are also saved in the portfolio's signal history (kinds `ext_*`), so they
appear in the Portfolio tab's live feed and pass through the same notification channels and
severities: entries and targets are *warning*, stop-outs are *critical*.

## 11.10 REST API

| Method | Path | |
|---|---|---|
| POST | `/api/ext/parse` `{text}` | interpretation preview |
| POST | `/api/ext/ingest` `{text, author?, source?}` | interpret and track / apply update |
| GET | `/api/ext/ideas?status=open\|all\|final\|pending\|active\|closed&symbol=` | ideas + stats + status |
| GET | `/api/ext/ideas/{id}` | idea + full event timeline |
| POST | `/api/ext/ideas/{id}/edit` `{stop?, targets?, entry_low?, entry_high?, stop_basis?}` | change the plan |
| POST | `/api/ext/ideas/{id}/close` `{price?, fraction?}` | close / trim an active idea |
| POST | `/api/ext/ideas/{id}/cancel` | cancel |
| GET | `/api/ext/events?limit&since` · `/api/ext/stats` · `/api/ext/status` · `/api/ext/discord/check` | |

Live updates go out as the SSE event `ext` on `/api/signals/stream`.

## 11.11 Settings

| Variable | Default | |
|---|---|---|
| `ABG_EXT_ENABLED` | true | track external ideas in the monitor |
| `ABG_DISCORD_BOT_TOKEN` | — | bot token for reading channels / reply relays |
| `ABG_EXT_DISCORD_CHANNEL_IDS` | — | comma-separated channel ids |
| `ABG_EXT_POLL_SECONDS` | 20 | Discord poll interval |
| `ABG_EXT_BACKFILL_MESSAGES` | 0 | parse this many older messages on first start |
| `ABG_EXT_RELAY_MODE` | webhook | webhook · reply · none |
| `ABG_EXT_RELAY_WEBHOOK_URL` | — | falls back to `ABG_DISCORD_WEBHOOK_URL` |
| `ABG_EXT_RELAY_ACK` | true | post the "tracking" confirmation |
| `ABG_EXT_RELAY_KINDS` | (all) | e.g. `entry,target_hit,stop_hit,exit,advisory` |
| `ABG_EXT_ACCOUNT_SIZE` / `ABG_EXT_RISK_PCT` | 10000 / 1.0 | paper sizing |
| `ABG_EXT_ENTRY_EXPIRY_DAYS` / `ABG_EXT_MAX_HOLD_DAYS` | 45 / 90 | swing time limits |
| `ABG_EXT_APPROACH_PCT` | 1.5 | heads-up distance |
| `ABG_EXT_MIN_ENTRY_GRADE` | C | A · B · C · D · none |
| `ABG_EXT_REGRADE_SECONDS` | 900 | re-grade interval for pending / blocked ideas |
| `ABG_EXT_MOVE_STOP_TO_BREAKEVEN` | true | stop → entry after TP1 |
| `ABG_EXT_MAX_ENTRY_DISTANCE_PCT` | 35 | typo guard |

## 11.12 Limitations

- Paper tracking uses quotes every 60 s plus daily bars. It does not use tick data, so fills are
  approximations with conservative tie-breaks (§11.4). Crypto and futures are tracked only if a
  configured provider serves those symbols. Yahoo does (`BTC-USD`, `NQ=F`).
- Options are not priced. Option calls are tracked on the underlying only when their levels are
  underlying prices.
- The parser is rule-based. Unusual formats may be missed, and those show up as `ignored` in the
  message log. Use **Interpret** to check a format, and edit the idea when needed.
- Educational analysis tool, not investment advice.
