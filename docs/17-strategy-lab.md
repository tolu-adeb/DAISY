# 17. Strategy Lab and research agents

The Strategy Lab tests ideas for improving the MNQ bot (docs/14) honestly. Every idea is backtested and gated
by code, every test is recorded, and the most recent six months of data stay locked until an idea has passed
everything else. Research agents built on [Strands Agents](https://github.com/strands-agents) and Claude
propose and review ideas. They never decide.

**Rule of the house: agents propose, code decides, a person approves.**
- No agent or tool can place a trade, loosen a loss control, or change the live bot's settings.
- A change reaches the bot only through `abg lab approve`, which writes `alpha_params.json` and backs up the
  old file. Restart the monitor to load it.

## 17.1 How a hypothesis is judged

A hypothesis is a name, a mechanism ("why the market would pay for this"), and 1–4 changes to whitelisted
settings (`abg lab space`). Loss controls (`max_risk_pts`, stop-after-loss, the daily loss cap, the
no-follow-through exit) are not in the search space at all.

**Data split** (`data/mnq_proxy_5m.csv` + `data/qqq_daily.csv` for the trend history):
- **Search window:** Jul 1, 2024 – Apr 21, 2026, 453 sessions, judged in four sub-periods.
- **Locked holdout:** the most recent 6 months, Apr 22 – Oct 21, 2026, 124 sessions.

Every backtest runs all sessions with a fresh adaptive book, exactly like the live bot. What an agent may
*see* is limited to the search window.

**Search-window gate.** Code compares the hypothesis with the current settings. All of these must pass:

| Check | Rule |
|---|---|
| sample | at least 250 trades |
| frequency | at least 80% of the current trade count |
| profit | at least 90% of the current net profit |
| **win rate** | at least +1.0 point better |
| **risk to profit** | net $ / max drawdown $ at least 5% better |
| consistency | win rate ≥ current in 3 of 4 sub-periods, and no sub-period flips from profit to loss |
| stability | a neighbouring value of a changed setting also beats the current win rate and net/DD (no lone spike) |
| random walk | no edge on structureless random-walk bars |

**Confirmation** (`abg lab confirm ID`) is one *counted* look at the holdout:
- On the holdout, the win rate and net/DD must not be worse than the current settings, and net profit must
  be positive.
- Over all sessions, both must be strictly better.
- Each holdout window allows **6 looks**. Sliding the window forward a few days doesn't reset the count; a
  window counts as fresh only once it overlaps the old one by less than half.
- A pass queues a **proposal**. Nothing changes until a person runs `abg lab approve`.

**Ledger** (`data/lab/lab.sqlite3`): every trial (winners and losers), every holdout look and every proposal.
The number of tries behind any "winner" is always visible.

## 17.2 The research agents (`abg lab run`)

```
researcher (Claude) --submit_hypotheses--> evaluate (code) --> critic (Claude) --decide-->
   revise -> next round | confirm -> holdout look (code) | stop  -->  reporter (Claude) -> data/lab/reports/
```

**What each role has:**
- **The researcher's tools:** `overview`, `baseline`, `past_trials`, `research_log` (docs/16),
  `search_space`, and `submit_hypotheses`. Submissions are validated against the whitelist; duplicates and
  loss controls are refused.
- **The critic's tools:** `round_results`, `trial_detail`, `past_trials`, `research_log`, and `decide`. It can
  only *request* a holdout look, and only for a trial code already marked a candidate (from this run, or an
  open one from earlier). Code performs the look and counts it.
- **Instructions** are Agent SOP files (`abg/agents/sops/*.sop.md`: overview, parameters, steps with
  MUST/SHOULD rules). The same files load into Claude Code as skills.

**Guard rails:**
- A Strands hook records every tool call (the run's trajectory), caps tool calls per step, and enforces the
  run's model-call budget (`ABG_LAB_MAX_MODEL_CALLS`).
- The routing between steps is plain Python, so it is deterministic and testable.

**Resumable runs:** `--max-seconds N` pauses cleanly before work it can't finish, and `--resume` continues.
- Finished backtests are cached.
- Agent conversations persist through a Strands `FileSessionManager`.
- The ledger is only written once a whole batch is evaluated.
- This makes runs safe on GitHub Actions, short shells, or a laptop that sleeps.

**Evals** (`abg lab eval`): a Strands Evals suite for the critic, built from real ledger trials. Each case
checks that the critic:
- never confirms a rejected trial
- doesn't confirm a look-alike of a holdout failure
- reads the results before it decides, and decides exactly once

It runs against the API only; it never looks at the holdout.

**MCP** (`abg mcp`): the same read-only tools, plus `lab_test` (search window only), over stdio for Claude,
Cowork, or Claude Code. For example: `claude mcp add abg-lab -- abg mcp`.

## 17.3 Commands

```powershell
pip install -e .[agents]                  # strands-agents[anthropic], strands-agents-evals, mcp
abg lab info                              # windows, gate, trials so far, holdout looks used
abg lab space                             # what a hypothesis may change
abg lab baseline                          # current settings on the search window + sub-periods
abg lab test "Name" "Why it should work" t1_r=0.6 orx_max_ext=0.4
abg lab trials ; abg lab trial 55         # the ledger
abg lab confirm 55                        # ONE counted holdout look for a candidate
abg lab run --rounds 3 --max-seconds 900  # research agents (needs ABG_ANTHROPIC_API_KEY); --resume to continue
abg lab eval                              # critic regression suite
abg lab proposals ; abg lab approve 3 ; abg lab reject 3
abg mcp                                   # tools over MCP
```

| Setting | Default | |
|---|---|---|
| `ABG_ANTHROPIC_API_KEY` | — | needed only for `lab run` / `lab eval` |
| `ABG_LAB_MODEL` | `ABG_ANTHROPIC_MODEL` | Claude model for the agents |
| `ABG_LAB_ROUNDS` / `ABG_LAB_PER_ROUND` | 3 / 5 | research rounds, hypotheses per round |
| `ABG_LAB_MAX_MODEL_CALLS` | 60 | hard cost guard per run |
| `ABG_LAB_MAX_TOKENS` | 4000 | per model call |
| `ABG_LAB_HOLDOUT_MONTHS` | 6 | size of the locked holdout |

## 17.4 First results (Oct 22, 2026): no change adopted

**57 trials:** 47 rejected on the search window, 10 candidates, 6 holdout looks (the full budget), 0 passed.

| Current settings | Trades | Win rate | Net $ | Max DD $ | Net/DD |
|---|---|---|---|---|---|
| search window (21 months) | 442 | 67.2% | 4,734 | 584 | 8.11 |
| holdout (last 6 months) | 124 | 59.7% | 744 | 568 | 1.31 |

| Candidate (trial) | Search: win / net/DD | Holdout: win / net/DD / net $ | Verdict |
|---|---|---|---|
| T1 0.55R (#35) | 73.0% / 9.30 | **63.5%** / 0.85 / 495 | failed: net/DD |
| T1 0.5R + faint-trend filter (#38) | 75.2% / 10.12 | **65.1%** / 0.69 / 408 | failed: net/DD |
| T1 0.6R + faint-trend filter (#39) | 71.5% / 10.94 | **61.9%** / 0.56 / 320 | failed: net/DD |
| T1 0.55R + opening-drive filter (#51) | 74.8% / 10.42 | **64.1%** / 0.74 / 370 | failed: net/DD |
| Exhaustion filter 0.4 + lock 25% (#55) | 68.7% / 9.75 | 58.2% / 0.62 / 364 | failed: both |
| Faint-trend day off + lock 25% (#53) | 68.8% / 8.86 | 57.8% / −0.01 / −4 | failed: both |

**What it means:**
- **Every change that looked better over the 21-month search window did worse in the last six months.** That
  includes #55: +28% profit and +20% net/DD in-sample, worse than doing nothing on the holdout. Without the
  holdout, it would have been adopted.
- **A lower Target 1 does buy win rate on recent data too** (62–65% vs 60%), but it costs too much profit per
  unit of drawdown. If you ever decide win rate matters more than net/DD, #35 (T1 0.55R) is the cleanest
  version. That is a judgment call for you, not something the gate allows.
- **The current rule is weaker recently:** 59.7% wins and net/DD 1.31 over the last six months, against
  67.2% and 8.11 before. Keep it in paper mode, and keep the copier (Alerio) off it.

**Disclosure:**
- The trend rule itself (docs/14.9) was chosen while looking at all 577 sessions, including this holdout.
- Trials 1–52 came from an earlier session on the same data; trials 53–57 and looks 5–6 from this one.
- None of the trials so far were proposed by the research agents: a person (Claude, in this session) acted
  as researcher and critic, because no API key was configured. The agents run the same pipeline once
  `ABG_ANTHROPIC_API_KEY` is set.

**The holdout is used up** until enough new sessions are appended to `data/mnq_proxy_5m.csv` that a 6-month
window overlaps the current one by less than half: about late January 2027. Until then the lab and the agents
can keep testing on the search window, but nothing can be confirmed.

## 17.5 Limits

- **Proxy data:** QQQ × ratio, not MNQ fills, with slippage of 1 tick.
- **Holdout size:** the holdout is one 6-month regime. "Not worse there" is a minimum bar, not proof.
- **Search space:** the lab only searches existing settings. A genuinely new setup needs code and its own tests
  first.
- **Model behavior:** the agents use whichever Claude model you configure. Re-run `abg lab eval` when you
  change it.
