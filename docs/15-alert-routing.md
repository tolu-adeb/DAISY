# 15. Alert routing (Alerio-style, dry run)

`abg/routes/` turns a signal service's Discord/Telegram/webhook messages into per-account order plans, under
your own rules. It is modelled on [Alerio](https://alerio.dev): a source feeds a route, the route's rules apply
to each account, and brackets become orders. Like Alerio it has a dry-run mode, an execution replay and an
activity log. It also has the guards Alerio lacks, which is what Oct 1, 2026 showed we need.

**Nothing here places a futures order.** Routes run in `dry_run`. `live` is refused until a Tradovate/Rithmic
adapter exists.

## 15.1 How Alerio works (from its docs, Oct 2026)

- **Sources:** Discord channels, Telegram, TradingView webhooks and generic webhooks.
- **Parser:**
  - The AI parser is the default. It reads entry, trim, move-stop, break-even, close and cancel messages, and
    by default it treats "+40 pts"-style P&L updates as trims.
  - Regex mode is strict, moderate or loose.
  - It has keywords and contract mapping.
- **Route settings:**
  - Execution mode: disabled, dry run or live. Target accounts.
  - Allowed contracts and multiple positions.
  - Market/limit entry with tick offsets.
  - Up to 10 trim rows, each with "SL after fill" (0 = break-even, N = lock N ticks).
  - A runner, fixed or trailing stop, and Alert Override (ignore, override or merge the alert's SL/TP).
- **Session rules:** an entry window; CPI/FOMC/PMI/OPEX entry exclusions; auto close.
- **Risk controls:** size caps, daily loss and profit limits.
- **Execution replay:** 30 days, Discord routes only.
- **Logs:** an activity log with parse time and routing duration.
- **Speed:** under 200 ms on Core, under 50 ms on Max.
- **Price:** $59.99–89.99 a month.

## 15.2 What it doesn't protect against, and what we added

| Gap in a plain copier | Here |
|---|---|
| A fixed size copies a wide stop at full size. On Oct 1, 8 MNQ × 83 pts was a −$1,330 loss. | `sizing="risk"`: contracts = $ budget ÷ (stop pts × $/pt). A wide stop gets fewer contracts. |
| The daily loss limit reacts after the loss. | Before entry, the size must fit the per-trade budget, today's remaining loss room and 90% of the prop trailing-drawdown room. Whichever is smallest wins. |
| No limit on stop distance. | `max_stop_pts` with `skip` or `tighten`. |
| Late or replayed alerts are followed. | `stale_sec`: alerts older than this are ignored. |
| Price already ran past the entry. | Chase guard: `max_chase_pts` / `max_chase_r`. |
| A "move stop" message can widen the stop. | Updates may only tighten. |
| No rules from the service's own track record. | `stop_after_first_loss` and `blackout_windows` (10:30–11:30 by default). On the TradingMind journal these cut max drawdown from 303 to 89 pts. |
| Replay covers 30 days and shows parsing only. | `abg route replay` takes any length of export, fills on bars and shows $ next to "as copied". |

## 15.3 Commands

```
abg route add lucid --contracts 8                        # guarded defaults, dry run
abg route add plain --preset copy --contracts 8          # what a plain copier does, for comparison
abg route set lucid stop_cap_mode tighten                # change one rule (JSON values)
abg route set lucid trailing_drawdown_usd 2000
abg route test lucid "short NQ 30709 sl 30792 tp 30651 30543" --at "2026-10-01 09:50"
abg route replay lucid tradingmind_export.json --bars data/mnq_5m.csv --trades
abg route log -n 50
```

API: `GET /api/routes`, `GET /api/routes/log`, `POST /api/routes/test {"route", "text", "price"}`.

## 15.4 Rules (`ABG_DATA_DIR/routes.json`)

| Rule | Default | |
|---|---|---|
| `sizing` | risk | `fixed` uses `contracts` |
| `max_contracts` | 8 | |
| `risk_per_trade_usd` | 200 | |
| `max_stop_pts` / `stop_cap_mode` | 60 / skip | |
| `alert_override` | merge | ignore / override / merge |
| `trims` | `[{"at_r": 0.7, "pct": 0.5, "sl_after": 0}]` | |
| `runner_target_r` | 2 | |
| `entry_type` | market | |
| `stale_sec` | 60 | |
| `max_chase_pts` / `max_chase_r` | 15 / 0.35 | |
| `entry_windows` | 09:30–16:00 | |
| `blackout_windows` | 10:30–11:30 | |
| `exclude_events` | CPI, FOMC, NFP | ±5/15 min |
| `auto_close` | 15:55 | |
| `max_trades_per_day` | 3 | |
| `stop_after_first_loss` | true | |
| `daily_loss_limit_usd` / `daily_profit_target_usd` | 500 / 0 | |
| `trailing_drawdown_usd` | 0 | |
| `contract_map` | NQ→MNQ | |

Every decision lists each check with ✓/✗ and the reason. The activity log is `ABG_DATA_DIR/routes_log.jsonl`.

## 15.5 Next steps

- A live listener: feed messages from the external-signals Discord gateway (docs/11) into a route.
- A broker adapter (Tradovate) behind the same `Decision.orders`.
- Keep Alerio pointed at TradingMind until replays of their history show the guarded route beats plain copying.
