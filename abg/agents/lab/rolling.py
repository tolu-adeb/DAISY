"""Forward lab: test MNQ ideas daily on all the data, then paper-trade the survivors on sessions nobody has seen.

The Strategy Lab (docs/17) locks the last 6 months as a holdout and allows 6 looks; once they're spent nothing can be
confirmed until 2027-01-21.  The forward lab runs alongside it, on its own copy of the data, and never touches the
lab's ledger or holdout:

    every trading day after the close (ABG_FWD_TIME, 16:45 ET)
      1. extend its own copy of the proxy dataset by the new session(s)
      2. update every paper strategy on the sessions after it started (data no test has seen)
      3. test today's ideas (ABG_FWD_PER_DAY): systematic one-setting moves around the live settings, pairs of
         near-misses, and (with an Anthropic key) a few ideas from Claude
         - walk-forward on ALL sessions, split into ABG_FWD_FOLDS (6) consecutive periods: beat the live settings on
           win rate (+1 pt) and profit/drawdown (+5%) overall, keep >= 80% of the trades and >= 90% of the profit,
           win rate >= live in at least ABG_FWD_MIN_FOLD_WINS (5) of the 6 periods, no period flipping to a loss
         - a neighbouring value also passes (not a lone spike); ~0 edge on random-walk bars
      4. ideas that pass become paper strategies (at most ABG_FWD_MAX_ACTIVE), starting with the next session
      5. after ABG_FWD_MIN_SESSIONS (20) forward sessions and ABG_FWD_MIN_TRADES (15) trades, a strategy that is
         at least as good as the live settings on win rate AND net, better on one, and profitable is READY;
         one clearly behind, or still not ready after ABG_FWD_MAX_SESSIONS (60), is retired
      6. READY strategies wait for you: Approve writes alpha_params.json (the old one is backed up) and the bot
         uses it from the next session.  Nothing is approved automatically.

Testing the same data every day is a multiple-comparisons problem: with enough tries something passes by luck.
That's why passing the walk-forward only earns a paper slot - the forward sessions are the real test.
"""
from __future__ import annotations

import json
import logging
import random
import shutil
import time
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from abg.agents.lab.runner import TimeBudget, metrics, phash
from abg.agents.lab.space import KNOBS, Hypothesis
from abg.agents.lab.validate import Gate

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

TIME_CHOICES = {
    "entry_end": ["10:30", "11:00", "11:30", "12:00", "13:00", "14:00", "15:00"],
    "flat_time": ["11:30", "12:00", "12:30", "13:00", "14:00", "15:00", "15:45"],
    "orx_last_entry": ["", "10:00", "10:15", "10:30", "11:00"],
}
RETEST_AFTER = 20           # sessions before an idea that failed may be tried again on the longer history


class RollingLab:
    """The synchronous core (run in a worker thread).  All files live in ``data/lab/rolling/``."""

    def __init__(self, settings, data_dir: Path | None = None):
        self.s = settings
        self.data_dir = Path(data_dir or settings.data_dir).expanduser()
        self.home = self.data_dir / "lab" / "rolling"
        self.home.mkdir(parents=True, exist_ok=True)
        self.bars, self.trend = self.home / "mnq_proxy_5m.csv", self.home / "qqq_daily.csv"
        self.state_path, self.trials_path = self.home / "state.json", self.home / "trials.jsonl"
        self.origins: dict[int, str] = {}                 # id(hypothesis) -> where it came from (this run)

    # ------------------------------------------------------------------ files
    def ensure_data(self) -> bool:
        """First run: copy the bot's dataset (later runs extend this copy, never the lab's)."""
        from abg.agents.lab.dataset import default_paths
        src_bars, src_trend = default_paths(self.data_dir)
        if not self.bars.exists():
            if not Path(src_bars).exists():
                return False
            shutil.copy2(src_bars, self.bars)
        if not self.trend.exists() and src_trend and Path(src_trend).exists():
            shutil.copy2(src_trend, self.trend)
        return True

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"strategies": [], "runs": [], "tested": {}, "total_trials": 0, "next_id": 1}

    def save(self, st: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, default=str, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def _trial_log(self, rows: list[dict]) -> None:
        with open(self.trials_path, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, default=str) + "\n")

    def recent_trials(self, n: int = 60) -> list[dict]:
        try:
            lines = self.trials_path.read_text(encoding="utf-8").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    # ------------------------------------------------------------------ the lab objects
    def gate(self) -> Gate:
        g = replace(Gate(), min_block_wins=int(self.s.fwd_min_fold_wins), peek_budget=0)
        if getattr(self.s, "fwd_profit_route", True):
            g = replace(g, alt_ratio_gain=float(self.s.fwd_profit_route_ratio), alt_profit_gain=float(self.s.fwd_profit_route_net),
                        alt_wr_slack=float(self.s.fwd_profit_route_wr_slack))
        return g

    def open_lab(self):
        from abg.agents.lab import Lab, current_params
        from abg.agents.lab.dataset import load_dataset
        from abg.agents.lab.ledger import Ledger
        from abg.agents.lab.runner import Runner
        ds = load_dataset(self.bars, self.trend if self.trend.exists() else None, 0, n_blocks=int(self.s.fwd_folds))
        runner = Runner(self.bars, self.trend if self.trend.exists() else None, 0, self.home / "cache", ds.fingerprint,
                        workers=int(self.s.fwd_workers))
        return Lab(ds, runner, Ledger(self.home / "lab.sqlite3"), self.data_dir, current_params(self.data_dir), self.gate())

    # ------------------------------------------------------------------ ideas
    def systematic(self, base) -> list[Hypothesis]:
        out = []
        for k, kn in KNOBS.items():
            cur = getattr(base, k)
            vals: list = []
            if kn.kind in ("float", "int") and kn.step:
                for m in (-2, -1, 1, 2):
                    v = cur + m * kn.step
                    if (kn.lo is None or v >= kn.lo - 1e-9) and (kn.hi is None or v <= kn.hi + 1e-9):
                        vals.append(kn.coerce(v))
            elif kn.kind == "choice":
                vals = [c for c in kn.choices if c != cur]
            elif kn.kind == "bool":
                vals = [not cur]
            elif kn.kind == "time":
                vals = [t for t in TIME_CHOICES.get(k, []) if t != cur]
            elif kn.kind == "setups":
                have = list(cur)
                vals = [tuple(x for x in have if x != drop) for drop in have if len(have) > 1]
                vals += [tuple(have + [add]) for add in ("orx", "orb", "sweep", "vwap") if add not in have]
            for v in vals:
                if v == cur:
                    continue
                shown = ",".join(v) if isinstance(v, tuple) else (v if v != "" else "off")
                was = ",".join(cur) if isinstance(cur, tuple) else (cur if cur != "" else "off")
                try:
                    out.append(Hypothesis(name=f"{k} {was} -> {shown}"[:80],
                                          rationale=f"one-setting move around the live settings: {kn.help}"[:1200],
                                          changes={k: list(v) if isinstance(v, tuple) else v}))
                except ValueError:
                    continue
        return out

    def pairs(self, base, recent: list[dict], k: int = 6) -> list[Hypothesis]:
        """Combine single-setting near-misses (failed at most one check) on different settings."""
        singles = [t for t in recent if len(t.get("changes") or {}) == 1 and len(t.get("failed") or []) <= 1
                   and t.get("verdict") != "invalid"]
        singles.sort(key=lambda t: -(t.get("search", {}).get("ratio") or 0))
        out, seen = [], set()
        for i, a in enumerate(singles):
            for b in singles[i + 1:]:
                ka, kb = next(iter(a["changes"])), next(iter(b["changes"]))
                if ka == kb or (ka, kb) in seen:
                    continue
                seen.add((ka, kb))
                try:
                    out.append(Hypothesis(name=f"{a['name']} + {b['name']}"[:80],
                                          rationale="two near-misses combined: each alone came within one check of passing",
                                          changes={**a["changes"], **b["changes"]}))
                except ValueError:
                    continue
                if len(out) >= k:
                    return out
        return out

    def from_strategy_lab(self, st: dict) -> tuple[list[Hypothesis], set[str]]:
        """The Strategy Lab's ideas worth a forward test (docs/17.10), best first: its search-window candidates, the
        ones that failed only the holdout, then near-misses (one failed check).  Returns (ideas, keys it rejected
        outright - two or more failed checks - which the forward lab skips)."""
        from abg.agents.lab import shared
        rank = {"candidate": 0, "failed_holdout": 1, "rejected": 2}
        ideas, skip, bad = [], set(), {}
        for t in sorted(shared.lab_trials(self.data_dir), key=lambda t: (rank.get(t["verdict"], 9), -(t["search"].get("ratio") or 0))):
            if t["verdict"] == "rejected" and len(t["failed"]) >= 2:
                skip.add(t["key"])
                continue
            if t["verdict"] not in rank:
                continue
            try:
                h = Hypothesis(name=t["name"][:80], rationale=(f"from the Strategy Lab (trial #{t['id']}, {t['verdict'].replace('_', ' ')}"
                                                               f"{', failed: ' + ', '.join(t['failed']) if t['failed'] else ''}): "
                                                               + (t["rationale"] or ""))[:1200], changes=t["changes"])
            except Exception as e:
                errs = getattr(e, "errors", None)
                bad[str(t["id"])] = ("; ".join(str(x.get("msg")) for x in errs()) if callable(errs) else str(e))[:200]
                continue
            self.origins[id(h)] = f"strategy lab #{t['id']}"
            ideas.append(h)
        st["lab_import_skipped"] = bad                   # e.g. a setting the bot no longer has
        return ideas, skip

    def _on_gate_change(self, lab, st: dict) -> None:
        """When the pass rules change (e.g. the profit route, half-step neighbours), ideas they would now judge
        differently - the ones that failed only the win-rate and/or stability checks - are re-tested first."""
        import hashlib
        gk = hashlib.sha1((json.dumps(lab.gate.to_dict(), sort_keys=True) + "|neighbours-v2").encode()).hexdigest()[:10]
        if st.get("gate_key") == gk:
            return
        retry = []
        for t in self.recent_trials(500):
            f = set(t.get("failed") or [])
            if f and f <= {"win_rate", "stability", "consistency"} and len(f) <= 2 and t.get("changes"):
                if t["changes"] not in [r["changes"] for r in retry]:
                    retry.append({"name": t.get("name"), "changes": t["changes"], "failed": sorted(f)})
        for r in retry:
            try:
                k = phash(Hypothesis(name=r["name"] or "retry", rationale="re-test under the new rules",
                                     changes=r["changes"]).apply(lab.base))
                st.get("tested", {}).pop(k, None)
            except Exception:
                continue
        st["retry_first"] = retry[:12]
        st["gate_key"] = gk

    def from_retry(self, st: dict) -> list[Hypothesis]:
        out = []
        for r in st.get("retry_first") or []:
            try:
                h = Hypothesis(name=(r.get("name") or "retry")[:80], rationale=(
                    f"re-tested under the new pass rules (it failed only {', '.join(r.get('failed') or [])} before)"),
                    changes=r["changes"])
            except Exception:
                continue
            self.origins[id(h)] = "re-test (new rules)"
            out.append(h)
        return out

    def from_refit(self) -> list[Hypothesis]:
        """The Sunday re-fit's suggestion (it no longer adopts by itself): tested here like any other idea."""
        try:
            r = json.loads((self.data_dir / "refit.json").read_text())
        except (OSError, ValueError):
            return []
        ch = r.get("suggest") or {}
        if not ch:
            return []
        try:
            h = Hypothesis(name=("weekly re-fit: " + ", ".join(f"{k}={v}" for k, v in ch.items()))[:80],
                           rationale=f"the Sunday walk-forward re-fit ({r.get('at')}) preferred these on the last ~60 days; "
                                     "tested on all the data before anything changes", changes=ch)
        except Exception:
            return []
        self.origins[id(h)] = "weekly re-fit"
        return [h]

    def pick(self, lab, st: dict, agent: list[Hypothesis] | None = None,
             papers: list[Hypothesis] | None = None) -> list[Hypothesis]:
        from abg.agents.lab import shared
        from abg.agents.lab.space import strip_noops
        n_today = len(lab.ds.sessions)
        tested = st.get("tested", {})
        live = {s["phash"] for s in st["strategies"] if s["status"] in ("paper", "ready", "queued")}
        lab_ideas, lab_rejected = self.from_strategy_lab(st)
        picked: set[str] = set()
        st["noops_dropped"] = st.get("noops_dropped", 0)

        def fresh(h: Hypothesis) -> bool:
            k = phash(h.apply(lab.base))
            if k == phash(lab.base) or k in live or k in picked:
                return False
            t = tested.get(k)
            return t is None or n_today - t >= RETEST_AFTER

        def clean(hs):
            for h in hs or []:
                h2 = strip_noops(h, lab.base)
                if h2 is not h:
                    st["noops_dropped"] += 1
                if h2 is None:
                    continue
                if h2 is not h and id(h) in self.origins:
                    self.origins[id(h2)] = self.origins[id(h)]
                yield h2

        per_day = int(self.s.fwd_per_day)
        out: list[Hypothesis] = []

        def add(h: Hypothesis) -> None:
            out.append(h)
            picked.add(phash(h.apply(lab.base)))

        for h in clean(papers):                         # ideas from research papers you chose to test, first
            if len(out) < per_day and fresh(h):
                add(h)
        for h in clean(self.from_refit()):
            if fresh(h):
                add(h)
        for h in clean(self.from_retry(st)):           # near-misses the new rules may pass (up to a quarter of the day)
            if len(out) >= max(1, per_day // 4):
                break
            if fresh(h):
                add(h)
        lab_ideas = list(clean(lab_ideas))
        agent = list(clean(agent))
        for h in lab_ideas:                              # the Strategy Lab's best first (up to half the day)
            if len(out) >= per_day // 2:
                break
            if fresh(h):
                add(h)
        for h in agent[: max(0, per_day // 4)]:
            if fresh(h) and shared.key(h.changes) not in lab_rejected:
                add(h)
        for h in clean(self.pairs(lab.base, self.recent_trials(200), k=max(1, per_day // 4))):
            if len(out) < per_day // 2 and fresh(h):
                add(h)
        pool = [h for h in self.systematic(lab.base) if fresh(h) and shared.key(h.changes) not in lab_rejected]
        random.Random(n_today).shuffle(pool)          # a different slice each day, reproducible for a given dataset
        for h in pool:
            if len(out) >= per_day:
                break
            if fresh(h):
                add(h)
        return out

    # ------------------------------------------------------------------ forward (unseen sessions)
    def forward(self, lab, st: dict) -> list[dict]:
        """Update paper strategies on sessions after their start; returns events (ready / retired / approved)."""
        events = []
        act = [s for s in st["strategies"] if s["status"] in ("paper", "ready")]
        base_hash = phash(lab.base)
        for s in act:
            if s["phash"] == base_hash:
                s["status"], s["closed_at"] = "approved", time.time()
                events.append({"kind": "approved", "id": s["id"], "name": s["name"]})
        act = [s for s in act if s["status"] in ("paper", "ready")]
        if not act:
            return events
        from abg.alpha.strategy import AlphaParams
        params = [AlphaParams.from_dict(s["params"]) for s in act]
        runs = lab.runner.run_many([lab.base] + params)
        base_run = runs[0]
        for s, run in zip(act, runs[1:]):
            days = [str(d) for d in lab.ds.sessions if str(d) >= s["fwd_start"]]
            c, b = metrics(run["trades"], days), metrics(base_run["trades"], days)
            s["forward"] = {"sessions": len(days), "from": s["fwd_start"], "to": days[-1] if days else None,
                            "cand": c, "live": b, "updated": time.time()}
            s.setdefault("history", []).append({"to": days[-1] if days else None, "sessions": len(days),
                                                "trades": c["trades"], "win_rate": c["win_rate"], "net_usd": c["net_usd"],
                                                "live_win_rate": b["win_rate"], "live_net_usd": b["net_usd"]})
            s["history"] = s["history"][-120:]
            enough = len(days) >= int(self.s.fwd_min_sessions) and c["trades"] >= int(self.s.fwd_min_trades)
            if s.get("route") == "profit":             # passed on profit/drawdown with the win rate held: judged the same way
                better = (c["net_usd"] > b["net_usd"] and c["net_usd"] > 0 and c["win_rate"] >= b["win_rate"] - 0.02
                          and (c.get("ratio") or 0) >= (b.get("ratio") or 0))
                behind = c["net_usd"] < b["net_usd"] - 150 or c["win_rate"] < b["win_rate"] - 0.04
            else:
                better = (c["win_rate"] >= b["win_rate"] and c["net_usd"] >= b["net_usd"] and c["net_usd"] > 0
                          and (c["win_rate"] > b["win_rate"] or c["net_usd"] > b["net_usd"]))
                behind = c["win_rate"] < b["win_rate"] - 0.02 and c["net_usd"] < b["net_usd"] - 150
            if enough and better and s["status"] == "paper":
                s["status"], s["ready_at"] = "ready", time.time()
                events.append({"kind": "ready", "id": s["id"], "name": s["name"], "forward": s["forward"]})
            elif s["status"] == "ready" and enough and not better:
                s["status"] = "paper"                      # slipped back: keep watching
            elif (enough and behind) or (len(days) >= int(self.s.fwd_max_sessions) and s["status"] == "paper"):
                s["status"], s["closed_at"] = "retired", time.time()
                s["retired_why"] = "clearly behind the live settings" if behind else \
                    f"not ready after {len(days)} forward sessions"
                events.append({"kind": "retired", "id": s["id"], "name": s["name"], "why": s["retired_why"]})
        return events

    def _activate_queued(self, st: dict, first_unseen: str) -> list[dict]:
        events = []
        active = sum(1 for s in st["strategies"] if s["status"] in ("paper", "ready"))
        for s in sorted([x for x in st["strategies"] if x["status"] == "queued"], key=lambda x: -(x["wf"].get("ratio") or 0)):
            if active >= int(self.s.fwd_max_active):
                break
            s["status"], s["fwd_start"] = "paper", first_unseen
            active += 1
            events.append({"kind": "paper", "id": s["id"], "name": s["name"], "changes": s.get("changes")})
        return events

    # ------------------------------------------------------------------ the daily run
    def run(self, agent_ideas: list[Hypothesis] | None = None, deadline: float | None = None,
            papers: list[Hypothesis] | None = None) -> dict:
        t0 = time.time()
        if not self.ensure_data():
            return {"ok": False, "why": "no dataset yet (data/mnq_proxy_5m.csv)"}
        lab = self.open_lab()
        st = self.state()
        self._on_gate_change(lab, st)
        last = str(lab.ds.sessions[-1])
        first_unseen = _next_session(lab.ds.sessions[-1])
        events = self.forward(lab, st)
        for h in papers or []:
            self.origins[id(h)] = "paper"
        hyps = self.pick(lab, st, agent_ideas, papers)
        results, timed_out = [], False
        if hyps:
            try:
                results = lab.test_many(hyps, source="forward-lab", deadline=deadline)
            except TimeBudget as e:
                timed_out = True
                log.info("forward lab: %s", e)
        rows = []
        done_changes = []
        for h, r in zip(hyps, results):
            k = phash(h.apply(lab.base))
            st["tested"][k] = len(lab.ds.sessions)
            done_changes.append(h.changes)
            row = {"at": time.time(), "data_to": last, "name": r.get("name"), "changes": r.get("changes", h.changes),
                   "rationale": h.rationale, "phash": k,
                   "origin": self.origins.get(id(h)) or ("claude" if "(from Claude)" in h.rationale else "forward lab"),
                   "verdict": r.get("verdict"), "failed": r.get("failed", []), "route": r.get("route"),
                   "search": {kk: (r.get("search") or {}).get(kk) for kk in ("trades", "win_rate", "net_usd", "max_dd_usd", "ratio")},
                   "live": {kk: (r.get("baseline") or {}).get(kk) for kk in ("trades", "win_rate", "net_usd", "max_dd_usd", "ratio")},
                   "folds": next((c.get("blocks") for c in r.get("checks", []) if c.get("check") == "consistency"), None),
                   "checks": [{kk: c.get(kk) for kk in ("check", "pass", "detail")} for c in r.get("checks", [])]}
            rows.append(row)
            if r.get("verdict") == "candidate":
                sid = st["next_id"]
                st["next_id"] += 1
                st["strategies"].append({
                    "id": sid, "name": h.name, "changes": r["changes"], "rationale": h.rationale, "phash": k,
                    "origin": row["origin"], "route": r.get("route", "win rate"),
                    "params": h.apply(lab.base).to_dict(), "base_phash": phash(lab.base), "passed_on": last,
                    "status": "queued", "fwd_start": None, "created": time.time(),
                    "wf": row["search"], "wf_live": row["live"], "folds": row["folds"], "checks": row["checks"],
                    "trials_before": st["total_trials"] + len(rows)})
        st["total_trials"] += len(rows)
        from abg.agents.lab import shared as _sh
        done_keys = {_sh.key(c) for c in done_changes}
        st["retry_first"] = [r for r in st.get("retry_first") or [] if _sh.key(r["changes"]) not in done_keys]
        st["lab_ideas_waiting"] = sum(1 for h in self.from_strategy_lab(st)[0]
                                      if phash(h.apply(lab.base)) not in st["tested"])
        self._trial_log(rows)
        events += self._activate_queued(st, first_unseen)
        run = {"at": time.time(), "data_to": last, "sessions": len(lab.ds.sessions), "tested": len(rows),
               "passed": sum(1 for r in rows if r["verdict"] == "candidate"), "events": events, "timed_out": timed_out,
               "seconds": round(time.time() - t0), "backtests": lab.runner.runs,
               "live": {k: v for k, v in metrics(lab.runner.get(lab.base)["trades"], [str(d) for d in lab.ds.sessions]).items()
                        if k in ("trades", "win_rate", "net_usd", "max_dd_usd", "ratio")} if lab.runner.get(lab.base) else None}
        st["runs"] = (st.get("runs") or [])[-59:] + [run]
        if lab.runner.get(lab.base):
            st["breakdown"] = breakdown(lab.runner.get(lab.base)["trades"])
        st["data"] = lab.ds.summary()
        self.save(st)
        self._prune_cache(lab.ds.fingerprint)
        return {"ok": True, **run}

    def _prune_cache(self, keep_fp: str) -> None:
        """Each dataset version gets its own backtest cache; keep this one and the previous one."""
        root = self.home / "cache"
        dirs = sorted([d for d in root.iterdir() if d.is_dir()], key=lambda d: d.stat().st_mtime) if root.exists() else []
        for d in dirs[:-2]:
            if d.name != keep_fp:
                shutil.rmtree(d, ignore_errors=True)

    # ------------------------------------------------------------------ human decisions
    def approve(self, sid: int, note: str = "") -> Path:
        st = self.state()
        s = next((x for x in st["strategies"] if x["id"] == sid), None)
        if not s or s["status"] != "ready":
            raise ValueError(f"forward strategy #{sid} is not ready")
        from abg.alpha.strategy import AlphaParams
        f = self.data_dir / "alpha_params.json"
        if f.exists():
            shutil.copy2(f, f.with_suffix(f".json.bak-{int(time.time())}"))
        params = AlphaParams.from_dict(s["params"]).to_dict()
        f.write_text(json.dumps({"params": params, "source": f"forward lab #{sid} ({s['name']})",
                                 "approved": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note,
                                 "forward": s.get("forward"), "walk_forward": s.get("wf")}, indent=1, default=str))
        s["status"], s["closed_at"], s["note"] = "approved", time.time(), note
        for x in st["strategies"]:                     # the others were measured against the old live settings
            if x["status"] in ("paper", "ready", "queued") and x["id"] != sid:
                x["rebased"] = True
        self.save(st)
        return f

    def retire(self, sid: int, why: str = "retired by you") -> None:
        st = self.state()
        s = next((x for x in st["strategies"] if x["id"] == sid), None)
        if not s or s["status"] not in ("paper", "ready", "queued"):
            raise ValueError(f"forward strategy #{sid} is not active")
        s["status"], s["closed_at"], s["retired_why"] = "retired", time.time(), why
        self.save(st)

    # ------------------------------------------------------------------ view
    def view(self) -> dict:
        st = self.state()
        order = {"ready": 0, "paper": 1, "queued": 2, "approved": 3, "retired": 4}
        strategies = sorted(st["strategies"], key=lambda s: (order.get(s["status"], 9), -s["id"]))
        from abg.agents.lab import shared
        reg = shared.registry(self.data_dir)
        for s in strategies:
            e = reg.get(shared.key(s.get("changes") or {}))
            s["journey"] = journey(e, s) if e else []
        return {"data": st.get("data"), "total_trials": st.get("total_trials", 0), "runs": (st.get("runs") or [])[-14:][::-1],
                "strategies": strategies[:40], "trials": self.recent_trials(40), "shared": shared.counts(self.data_dir),
                "lab_ideas_waiting": st.get("lab_ideas_waiting"), "lab_import_skipped": st.get("lab_import_skipped") or {},
                "retry_first": st.get("retry_first") or [], "noops_dropped": st.get("noops_dropped", 0),
                "counts": {k: sum(1 for s in st["strategies"] if s["status"] == k) for k in order}}


def breakdown(trades: list[dict]) -> dict:
    """Where the live settings win and lose (all sessions): by setup, regime, side, entry time and exit - what an
    idea-writer needs to propose structural changes instead of nudging one number."""
    def grp(keyf):
        g: dict[str, list] = {}
        for t in trades:
            g.setdefault(str(keyf(t)), []).append(t)
        return {k: {"n": len(v), "win_rate": round(sum(1 for x in v if (x.get("pts") or 0) > 0.5) / len(v), 3),
                    "net_pts": round(sum(x.get("pts") or 0 for x in v), 1), "avg_r": round(sum(x.get("r") or 0 for x in v) / len(v), 3)}
                for k, v in sorted(g.items())}
    def slot(t):
        hm = str(t.get("opened") or "")[11:16]
        return "09:30-09:59" if hm < "10:00" else "10:00-10:29" if hm < "10:30" else "10:30-11:29" if hm < "11:30" else "11:30+"
    return {"trades": len(trades), "by_setup": grp(lambda t: t.get("setup")), "by_regime": grp(lambda t: t.get("regime")),
            "by_side": grp(lambda t: {1: "long", -1: "short"}.get(t.get("side"), t.get("side"))), "by_entry_time": grp(slot),
            "by_exit": grp(lambda t: t.get("exit_reason"))}


def journey(e: dict, s: dict | None = None) -> list[dict]:
    """One idea's path through both labs, oldest first; ``oos`` marks where its out-of-sample record starts."""
    steps = []
    for ev in e.get("events", []):
        if ev["lab"] == "strategy":
            steps.append({"at": ev["at"], "what": f"Strategy Lab #{ev['id']}: search window {('passed' if ev['verdict'] in ('candidate', 'confirmed', 'failed_holdout') else 'failed')}"
                          + (f" ({', '.join(ev['failed'])})" if ev["failed"] and ev["verdict"] == "rejected" else ""), "seen": ev["seen_through"]})
            for lk in ev.get("looks") or []:
                steps.append({"at": lk["at"], "what": f"Strategy Lab #{ev['id']}: holdout look {'passed' if lk['passed'] else 'failed'}",
                              "seen": ev["seen_through"]})
        else:
            steps.append({"at": ev["at"], "what": f"Forward lab: walk-forward {'passed' if ev['verdict'] == 'candidate' else 'failed'}"
                          + (f" ({', '.join(ev['failed'])})" if ev["failed"] else ""), "seen": ev["seen_through"]})
    if s and s.get("fwd_start"):
        f = s.get("forward") or {}
        steps.append({"at": s.get("created"), "what": f"Paper trading from {s['fwd_start']}"
                      + (f": {f.get('sessions')} unseen sessions so far" if f else ""), "oos": s["fwd_start"]})
    steps.sort(key=lambda x: x.get("at") or 0)
    return steps


def _next_session(d) -> str:
    from datetime import timedelta

    from abg.live.market_hours import is_trading_day
    x = d + timedelta(days=1)
    while not is_trading_day(x):
        x += timedelta(days=1)
    return str(x)


# ====================================================================== the monitor component
class ForwardDesk:
    """Runs the forward lab once per trading day after the close, inside the monitor."""

    def __init__(self, monitor):
        import asyncio
        self.m, self.s, self.http = monitor, monitor.s, monitor.engine.http
        self.lab = RollingLab(self.s)
        self.busy: str | None = None
        self.errors: list[str] = []
        self._stop = asyncio.Event()
        self._task = None
        self._tried: dict[str, float] = {}

    def stop(self) -> None:
        self._stop.set()

    def _done_today(self, today: str) -> bool:
        runs = self.lab.state().get("runs") or []
        return bool(runs) and runs[-1].get("ran_for") == today

    def due(self, now: datetime) -> bool:
        from abg.live.market_hours import is_trading_day
        if not is_trading_day(now.date()):
            return False
        h, m = (self.s.fwd_time or "16:45").split(":")
        today = str(now.date())
        return now.hour * 60 + now.minute >= int(h) * 60 + int(m) and not self._done_today(today) \
            and (self._task is None or self._task.done()) and time.time() - self._tried.get(today, 0) >= 1800

    async def run(self) -> None:
        import asyncio
        self._stop.clear()
        while not self._stop.is_set():
            try:
                if self.due(datetime.now(NY)):
                    await self.run_once(datetime.now(NY).date())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("forward lab failed")
                self.errors = (self.errors + [f"{type(e).__name__}: {e}"[:200]])[-5:]
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    def start(self, day: date | None = None) -> bool:
        import asyncio
        if self._task is not None and not self._task.done():
            return False
        self._task = asyncio.ensure_future(self.run_once(day or datetime.now(NY).date()))
        return True

    async def run_once(self, day: date) -> dict:
        import asyncio
        if self.busy:
            return {"ok": False, "why": f"busy: {self.busy}"}
        self._tried[str(day)] = time.time()
        try:
            self.busy = "extending the dataset"
            refresh = None
            if self.lab.ensure_data():
                try:
                    from abg.agents.lab.dataset import refresh_proxy_csv
                    refresh = await refresh_proxy_csv(self.m.engine, self.lab.bars, self.lab.trend if self.lab.trend.exists() else None)
                except Exception as e:
                    self.errors = (self.errors + [f"refresh: {type(e).__name__}: {e}"[:200]])[-5:]
                if getattr(self.s, "parity_enabled", True):
                    await self.run_parity(inside=True)
            ideas = []
            if getattr(self.s, "fwd_agent", True) and self.s.anthropic_api_key:
                self.busy = "asking Claude for ideas"
                try:
                    ideas = await self.agent_ideas()
                except Exception as e:
                    self.errors = (self.errors + [f"ideas: {type(e).__name__}: {e}"[:200]])[-5:]
            from abg.papers import queue as paper_queue
            papers = paper_queue.take(self.s.data_dir)
            self.busy = "backtesting"
            deadline = time.time() + 60 * float(self.s.fwd_budget_min)
            res = await asyncio.to_thread(self.lab.run, ideas, deadline, papers)
            st = self.lab.state()
            if st.get("runs"):
                st["runs"][-1].update(ran_for=str(day), refresh=refresh, ideas_from_claude=len(ideas), ideas_from_papers=len(papers))
                self.lab.save(st)
            try:
                paper_queue.settle(self.s.data_dir, st, self.lab.recent_trials(500))
            except Exception as e:
                self.errors = (self.errors + [f"papers: {type(e).__name__}: {e}"[:200]])[-5:]
            await self.post(res)
            hub = getattr(self.m, "hub", None)
            if hub is not None:
                hub.broadcast("alpha", {"type": "forward_lab", "tested": res.get("tested")})
            return res
        finally:
            self.busy = None

    async def run_parity(self, inside: bool = False) -> list[dict]:
        """Live vs backtest for the last 5 sessions (docs/14.12) on this lab's freshly extended dataset."""
        import asyncio
        from abg.agents.lab import parity
        if not inside and self.busy:
            return []
        prev = self.busy
        self.busy = "checking live vs backtest"
        try:
            if not self.lab.ensure_data():
                return []
            return await asyncio.to_thread(parity.run_recent, self.lab.data_dir, self.lab.bars,
                                           self.lab.trend if self.lab.trend.exists() else None, 5, getattr(self.m, "uptime", None))
        except Exception as e:
            self.errors = (self.errors + [f"parity: {type(e).__name__}: {e}"[:200]])[-5:]
            return []
        finally:
            self.busy = prev if inside else None

    async def agent_ideas(self) -> list[Hypothesis]:
        """One Claude call: up to 4 hypotheses from the search space and the recent results (validated; bad ones dropped)."""
        from abg.agents.lab import current_params
        from abg.agents.lab.space import describe_space
        base = current_params(Path(self.s.data_dir).expanduser())
        from abg.agents.lab import shared
        recent = shared.history_for_prompt(Path(self.s.data_dir).expanduser(), 40)
        bd = self.lab.state().get("breakdown") or {}
        prompt = (
            "You propose hypotheses for an intraday MNQ futures strategy's settings.  Each is a JSON object "
            '{"name": short, "rationale": one or two sentences on why it could raise the win rate AND the '
            'profit-to-drawdown ratio (or hold the win rate and clearly raise profit/drawdown), "changes": {setting: value}} '
            "with 1 to 3 changes, only from this search space (values within the ranges/choices; 'current' is the live "
            "value - never propose a value equal to it):\n" + json.dumps(describe_space(base)) +
            "\n\nWhere the live settings win and lose over all sessions (by setup, regime, side, entry time, exit):\n" +
            json.dumps(bd)[:3500] +
            "\n\nRecent results from both labs (the Strategy Lab and this forward lab; verdict and failed checks):\n" +
            json.dumps(recent)[:6000] +
            "\n\nMost recent ideas only nudged one target or length and failed.  Prefer ideas that change WHICH trades "
            "are taken or how losers are cut: day filters (min_trend_atr, max_atr_ratio), opening-range filters "
            "(orx_min_drive, orx_max_ext, orx_or_min_atr, orx_or_max_atr, orx_vwap_confirm, orx_gap_mode), the trend "
            "rule (orx_leg, orx_grade_min), the setups list, and the entry/exit times - aimed at the weak groups in the "
            "breakdown.  Avoid repeating what already failed.  Reply with a JSON array of up to 4 objects, nothing else.")
        model = self.s.lab_model or self.s.anthropic_model
        resp = await self.http.request(
            "POST", (self.s.anthropic_base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages",
            provider="anthropic", timeout=120,
            headers={"x-api-key": self.s.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]})
        import re
        text = "".join(b.get("text", "") for b in (resp.json().get("content") or []) if b.get("type") == "text")
        m = re.search(r"\[.*\]", text, re.S)
        out = []
        for x in json.loads(m.group(0)) if m else []:
            try:
                out.append(Hypothesis(name=str(x.get("name"))[:80], rationale=(str(x.get("rationale")) + " (from Claude)")[:1200],
                                      changes=x.get("changes") or {}))
            except Exception:
                continue
        return out[:4]

    async def post(self, res: dict) -> None:
        url = self.s.rd_webhook_url
        ev = [e for e in res.get("events") or [] if e["kind"] in ("ready", "paper", "retired", "approved")]
        if not url or not getattr(self.s, "fwd_post", True) or not ev:
            return
        lines = []
        for e in ev:
            if e["kind"] == "ready":
                f = e["forward"]
                lines.append(f"✅ **Ready for your approval**: #{e['id']} {e['name']} - {f['sessions']} unseen sessions, "
                             f"{f['cand']['trades']} trades, {f['cand']['win_rate']:.0%} win rate vs {f['live']['win_rate']:.0%} live, "
                             f"net ${f['cand']['net_usd']:,.0f} vs ${f['live']['net_usd']:,.0f}")
            elif e["kind"] == "paper":
                lines.append(f"📝 Paper trading starts: #{e['id']} {e['name']}")
            elif e["kind"] == "retired":
                lines.append(f"🗄️ Retired: #{e['id']} {e['name']} ({e['why']})")
            else:
                lines.append(f"🟢 Now live: #{e['id']} {e['name']}")
        body = {"username": "ABG R&D", "allowed_mentions": {"parse": []},
                "embeds": [{"title": "🧭 Forward lab", "color": 0x10B981,
                            "description": "\n".join(lines)[:3900] + f"\n\n{res.get('tested', 0)} ideas tested today on "
                                           f"{res.get('sessions')} sessions; {res.get('passed', 0)} passed the walk-forward.",
                            "footer": {"text": "ABG forward lab · paper only · approve on the dashboard"}}]}
        try:
            await self.http.request("POST", url + ("&" if "?" in url else "?") + "wait=true", provider="discord", json=body)
        except Exception as e:
            self.errors = (self.errors + [f"post: {type(e).__name__}"])[-5:]

    def status(self) -> dict:
        runs = self.lab.state().get("runs") or []
        return {"enabled": True, "busy": self.busy, "time": self.s.fwd_time, "per_day": self.s.fwd_per_day,
                "agent": bool(getattr(self.s, "fwd_agent", True) and self.s.anthropic_api_key),
                "post": bool(self.s.rd_webhook_url and getattr(self.s, "fwd_post", True)),
                "last_run": runs[-1] if runs else None, "errors": self.errors[-3:],
                "rules": {"profit_route": {"wr_slack": self.s.fwd_profit_route_wr_slack, "ratio": self.s.fwd_profit_route_ratio,
                                           "net": self.s.fwd_profit_route_net} if getattr(self.s, "fwd_profit_route", True) else None,
                          "folds": self.s.fwd_folds, "min_fold_wins": self.s.fwd_min_fold_wins,
                          "min_sessions": self.s.fwd_min_sessions, "min_trades": self.s.fwd_min_trades,
                          "max_sessions": self.s.fwd_max_sessions, "max_active": self.s.fwd_max_active}}
