"""Strategy Lab (docs/17): search space, gate, ledger, holdout budget, approval, research loop and agent wiring.

The lab's numbers come from the real backtester in normal use; here a fake runner produces synthetic trades so
the protocol itself can be tested quickly and exactly.
"""
import json
import random
from dataclasses import dataclass, field

import pytest

from abg.agents.lab import Hypothesis, Lab
from abg.agents.lab.ledger import Ledger
from abg.agents.lab.runner import metrics, phash
from abg.agents.lab.validate import Gate
from abg.alpha.strategy import SAFETY_KEYS, AlphaParams, AlphaStrategy, load_saved_params

from test_alpha import _ctx, _orb_day, _run, _warm


# --------------------------------------------------------------------------- fakes
@dataclass
class FakeDS:
    sessions: list
    search: list
    holdout: list
    blocks: list
    fingerprint: str = "fakefp000001"
    source: str = "fake"

    @property
    def holdout_start(self):
        return str(self.holdout[0])

    def summary(self):
        return {"sessions": len(self.sessions), "fingerprint": self.fingerprint}


def make_ds(n=400, hold=100):
    from datetime import date, timedelta
    d, days = date(2024, 1, 1), []
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    search, holdout = days[:-hold], days[-hold:]
    size = -(-len(search) // 4)
    return FakeDS(days, search, holdout, [search[i:i + size] for i in range(0, len(search), size)])


class FakeRunner:
    """One trade a day; win probability falls as t1_r rises (like the real bot), common random numbers so the
    comparison is about the setting, not the dice.  ``holdout_wr_shift`` lets a test make the holdout disagree."""

    def __init__(self, ds, holdout_wr_shift=0.0):
        self.ds, self.shift, self.mem, self.runs = ds, holdout_wr_shift, {}, 0
        rng = random.Random(3)
        self.u = [rng.random() for _ in ds.sessions]

    def _make(self, p: AlphaParams) -> dict:
        wr = 0.62 + (0.7 - p.t1_r) * 0.25
        hold = set(map(str, self.ds.holdout))
        trades = []
        for i, d in enumerate(self.ds.sessions):
            w = wr + (self.shift * (0.7 - p.t1_r) if str(d) in hold else 0.0)
            win = self.u[i] < w
            trades.append({"date": str(d), "pts": 30.0 if win else -40.0, "r": 0.6 if win else -1.0, "setup": "orx",
                           "side": 1, "exit_reason": "final" if win else "stopped", "opened": f"{d}T09:50:00", "regime": "trend"})
        return {"trades": trades, "days": [str(d) for d in self.ds.sessions]}

    def get(self, p):
        return self.mem.get(phash(p))

    def run_many(self, ps, deadline=None):
        for p in ps:
            if phash(p) not in self.mem:
                self.mem[phash(p)] = self._make(p)
                self.runs += 1
        return [self.get(p) for p in ps]

    def random_walk(self, p, deadline=None):
        return {"trades": 300, "avg_r": 0.01}


@pytest.fixture
def lab(tmp_path):
    ds = make_ds()
    return Lab(ds, FakeRunner(ds), Ledger(tmp_path / "lab.sqlite3"), tmp_path, base=AlphaParams(t1_r=0.7, scale_frac=0.5),
               gate=Gate(min_trades=100))


# --------------------------------------------------------------------------- search space
def test_hypothesis_whitelist_and_loss_controls():
    for k in SAFETY_KEYS:
        with pytest.raises(ValueError):
            Hypothesis(name="loosen", rationale="should never be allowed", changes={k: 1})
    with pytest.raises(ValueError):
        Hypothesis(name="unknown", rationale="not a real setting", changes={"leverage": 10})
    with pytest.raises(ValueError):
        Hypothesis(name="too many", rationale="five knobs is fitting noise",
                   changes={"t1_r": 0.6, "orx_k": 0.7, "trail_atr": 1.0, "min_score": 60, "max_trades": 1})
    with pytest.raises(ValueError):
        Hypothesis(name="out of range", rationale="t1 at 5R is outside the space", changes={"t1_r": 5})
    h = Hypothesis(name="coerce", rationale="coerces strings", changes={"t1_r": "0.6", "setups": "orx", "orx_last_entry": "10:5"})
    assert h.changes == {"t1_r": 0.6, "setups": ("orx",), "orx_last_entry": "10:05"}
    assert h.apply(AlphaParams()).t1_r == 0.6


def test_metrics_ratio_and_drawdown():
    tr = [{"date": "2026-01-02", "pts": 10, "r": 0.5}, {"date": "2026-01-05", "pts": -20, "r": -1},
          {"date": "2026-01-06", "pts": 30, "r": 1.5}]
    m = metrics(tr, ["2026-01-02", "2026-01-05", "2026-01-06"], commission_rt=0.0)
    assert m["net_usd"] == 40.0 and m["max_dd_usd"] == 40.0 and m["ratio"] == 1.0
    assert m["win_rate"] == pytest.approx(2 / 3, abs=1e-4)


# --------------------------------------------------------------------------- the gate
def test_better_setting_becomes_a_candidate_with_stability_and_random_walk(lab):
    r = lab.test(Hypothesis(name="lower T1", rationale="earlier T1 turns more trades into wins", changes={"t1_r": 0.5}))
    assert r["verdict"] == "candidate", r["checks"]
    names = [c["check"] for c in r["checks"]]
    assert {"sample", "frequency", "profit", "win_rate", "risk_to_profit", "consistency", "stability", "random_walk"} <= set(names)
    assert r["search"]["win_rate"] > r["baseline"]["win_rate"] and r["search"]["ratio"] > r["baseline"]["ratio"]


def test_worse_setting_is_rejected_and_recorded(lab):
    r = lab.test(Hypothesis(name="higher T1", rationale="bigger first target", changes={"t1_r": 1.0}))
    assert r["verdict"] == "rejected" and "win_rate" in r["failed"]
    assert lab.ledger.count_trials(lab.ds.fingerprint) == 1          # losers are counted too


def test_no_change_is_invalid(lab):
    r = lab.test(Hypothesis(name="same", rationale="current value restated", changes={"t1_r": 0.7}))
    assert r["verdict"] == "invalid"


# --------------------------------------------------------------------------- holdout
def test_only_candidates_may_see_the_holdout_and_looks_are_counted(lab):
    bad = lab.test(Hypothesis(name="higher T1", rationale="bigger first target", changes={"t1_r": 1.0}))
    with pytest.raises(ValueError, match="only search-window candidates"):
        lab.confirm(bad["trial_id"])
    good = lab.test(Hypothesis(name="lower T1", rationale="earlier T1 turns more trades into wins", changes={"t1_r": 0.5}))
    out = lab.confirm(good["trial_id"])
    assert out["pass"] and out["peeks_used"] == 1 and out["proposal_id"]
    assert lab.ledger.trial(good["trial_id"])["verdict"] == "confirmed"


def test_holdout_disagreement_blocks_the_proposal(tmp_path):
    ds = make_ds()
    lab = Lab(ds, FakeRunner(ds, holdout_wr_shift=-1.2), Ledger(tmp_path / "l.sqlite3"), tmp_path,
              base=AlphaParams(t1_r=0.7, scale_frac=0.5), gate=Gate(min_trades=100))
    good = lab.test(Hypothesis(name="lower T1", rationale="earlier T1 turns more trades into wins", changes={"t1_r": 0.5}))
    assert good["verdict"] == "candidate"
    out = lab.confirm(good["trial_id"])
    assert not out["pass"] and "proposal_id" not in out
    assert lab.ledger.trial(good["trial_id"])["verdict"] == "failed_holdout"
    assert lab.ledger.proposals() == []


def test_holdout_budget_is_enforced(lab):
    lab.gate.peek_budget = 1
    a = lab.test(Hypothesis(name="lower T1", rationale="earlier T1 turns more trades into wins", changes={"t1_r": 0.5}))
    b = lab.test(Hypothesis(name="lower T1 b", rationale="earlier T1 turns more trades into wins", changes={"t1_r": 0.4}))
    lab.confirm(a["trial_id"])
    with pytest.raises(ValueError, match="used up"):
        lab.confirm(b["trial_id"])


def test_sliding_the_window_does_not_buy_a_fresh_holdout(tmp_path):
    led = Ledger(tmp_path / "l.sqlite3")
    led.add_peek(1, "2026-04-22", "fp", False, {"window": ["2026-04-22", "2026-10-21"]})
    assert led.peeks("2026-05-01", "2026-10-30") == 1                 # mostly the same sessions -> still counted
    assert led.peeks("2027-01-04", "2027-07-02") == 0                 # a genuinely new window starts fresh


# --------------------------------------------------------------------------- approval
def test_approve_writes_params_backs_up_and_keeps_loss_controls(lab, tmp_path):
    (tmp_path / "alpha_params.json").write_text(json.dumps({"params": {"orx_policy": "trend", "t1_r": 0.7}}))
    good = lab.test(Hypothesis(name="lower T1", rationale="earlier T1 turns more trades into wins", changes={"t1_r": 0.5}))
    pid = lab.confirm(good["trial_id"])["proposal_id"]
    f = lab.approve(pid, "test")
    saved = json.loads(f.read_text())
    assert saved["params"]["t1_r"] == 0.5 and "strategy lab proposal" in saved["source"]
    assert list(tmp_path.glob("alpha_params.json.bak-*"))
    p = load_saved_params(saved)
    assert p.t1_r == 0.5 and p.max_risk_pts == AlphaParams().max_risk_pts
    with pytest.raises(ValueError):
        lab.approve(pid)                                              # not pending any more


# --------------------------------------------------------------------------- research loop (scripted roles)
def _scripted(session, script):
    """Roles that call the bound Strands tools directly, like an agent would."""
    from abg.agents.graph import Roles
    from abg.agents.tools import make_lab_tools
    tools = {t.tool_name: t for ts in make_lab_tools(session).values() for t in ts}

    def researcher(prompt):
        session.trajectory.append("researcher:submit_hypotheses")
        tools["submit_hypotheses"](hypotheses=script["hyps"][session.round])
        return "submitted"

    def critic(prompt):
        session.trajectory.append("critic:decide")
        attempts = script["decide"][session.round - 1]
        for dec in (attempts if isinstance(attempts, list) else [attempts]):   # an agent retries after a refusal
            tid = None
            if dec == "confirm":
                tid = next(r["trial_id"] for r in session.results if r["verdict"] == "candidate")
            elif isinstance(dec, tuple):
                dec, tid = dec
            res = tools["decide"](decision=dec, reason="scripted", trial_id=tid)
            if "recorded" in res:
                return dec
        return "none"
    return Roles(researcher, critic, None, "scripted")


def test_research_loop_code_evaluates_and_confirms_only_candidates(lab, tmp_path):
    pytest.importorskip("strands")
    from abg.agents.graph import run_research
    script = {"hyps": [[{"name": "higher T1", "rationale": "bigger first target", "changes": {"t1_r": 1.0}}],
                       [{"name": "lower T1", "rationale": "earlier T1 turns more trades into wins", "changes": {"t1_r": 0.5}}]],
              "decide": [[("confirm", 1), "revise"], "confirm"]}
    out = run_research(lab, lambda s: _scripted(s, script), rounds=2, report_dir=tmp_path / "reports")
    s = out["session"]
    # round 1: critic tried to confirm a rejected trial -> the decide tool refused, nothing was looked at
    assert s["evaluated"][0]["verdict"] == "rejected"
    assert lab.ledger.peeks(lab.ds.holdout_start) == 1               # only the round-2 candidate used a look
    assert s["confirmations"][0]["pass"] and s["confirmations"][0]["proposal_id"]
    assert out["report_path"] and "proposal" in out["report"]
    assert s["trajectory"] == ["researcher:submit_hypotheses", "critic:decide"] * 2


def test_duplicate_hypotheses_are_refused_and_rounds_are_capped(lab):
    pytest.importorskip("strands")
    from abg.agents.graph import run_research
    h = {"name": "lower T1", "rationale": "earlier T1 turns more trades into wins", "changes": {"t1_r": 0.5}}
    out = run_research(lab, lambda s: _scripted(s, {"hyps": [[h], [h], [h]], "decide": ["revise", "revise", "revise"]}),
                       rounds=3)
    assert len(out["session"]["evaluated"]) == 1                     # the repeat was rejected by submit_hypotheses
    assert any("no valid hypotheses" in n for n in out["session"]["notes"])


def test_strands_agents_are_wired_with_read_only_tools(lab):
    pytest.importorskip("strands")
    from strands.models.anthropic import AnthropicModel

    from abg.agents.graph import strands_roles
    from abg.agents.tools import LabSession
    from abg.config import Settings
    session = LabSession(lab)
    model = AnthropicModel(client_args={"api_key": "test-not-a-key"}, model_id="claude-test", max_tokens=100)
    roles = strands_roles(session, Settings(anthropic_api_key=None), model=model)
    assert roles.researcher and roles.critic and roles.reporter
    from abg.agents.tools import make_lab_tools
    names = {k: {t.tool_name for t in v} for k, v in make_lab_tools(session).items()}
    assert "submit_hypotheses" in names["researcher"] and "decide" in names["critic"]
    for role, ns in names.items():                                   # no tool can confirm, approve or edit settings
        assert not ns & {"confirm", "approve", "write_params"}, role


def test_sops_follow_the_agent_sop_format():
    from abg.agents.graph import load_sop
    for n in ("strategy-research", "strategy-critic", "strategy-report"):
        s = load_sop(n)
        assert s.startswith("# ") and "## Overview" in s and "## Steps" in s and "MUST" in s


# --------------------------------------------------------------------------- the new (default-off) bot filters
def test_lab_filters_are_off_by_default_and_block_when_set():
    p0 = AlphaParams(setups=("orx",), orx_final_mode="fixed")
    s = AlphaStrategy(p0)
    s.start_day(_ctx(trend_long=1, trend_mid=1, trend_d=1), _warm())
    assert any(e["type"] == "signal" for e in _run(s, _orb_day("up").bars))
    for kw in ({"orx_max_ext": 0.01}, {"orx_leg": "fade"}, {"orx_last_entry": "09:46"}):
        s = AlphaStrategy(AlphaParams(setups=("orx",), orx_final_mode="fixed", **kw))
        s.start_day(_ctx(trend_long=1, trend_mid=1, trend_d=1), _warm())
        assert not any(e["type"] == "signal" for e in _run(s, _orb_day("up").bars)), kw


def test_critic_may_confirm_an_open_candidate_from_an_earlier_run(lab):
    pytest.importorskip("strands")
    from abg.agents.tools import LabSession, make_lab_tools
    old = lab.test(Hypothesis(name="lower T1", rationale="earlier T1 turns more trades into wins", changes={"t1_r": 0.5}))
    bad = lab.test(Hypothesis(name="higher T1", rationale="bigger first target", changes={"t1_r": 1.0}))
    session = LabSession(lab)
    tools = {t.tool_name: t for ts in make_lab_tools(session).values() for t in ts}
    assert [c["trial_id"] for c in session.open_candidates()] == [old["trial_id"]]
    assert "error" in tools["decide"](decision="confirm", reason="x", trial_id=bad["trial_id"])
    assert tools["decide"](decision="confirm", reason="x", trial_id=old["trial_id"])["recorded"]["trial_id"] == old["trial_id"]


def test_a_paused_run_resumes_without_redoing_or_double_counting(lab, tmp_path):
    pytest.importorskip("strands")
    import time as _t

    from abg.agents.graph import run_research_session
    from abg.agents.lab import TimeBudget
    from abg.agents.tools import LabSession

    real = lab.runner.run_many
    calls = {"n": 0}

    def flaky(ps, deadline=None):                       # the first evaluate runs out of time half-way
        calls["n"] += 1
        if calls["n"] == 1 and deadline is not None:
            real(ps[:1])
            raise TimeBudget("2 backtests left")
        return real(ps, deadline)
    lab.runner.run_many = flaky

    script = {"hyps": [[{"name": "lower T1", "rationale": "earlier T1 turns more trades into wins", "changes": {"t1_r": 0.5}}]],
              "decide": ["confirm"]}
    state = tmp_path / "run.json"
    s1 = LabSession(lab, max_rounds=1)
    out = run_research_session(lab, _scripted(s1, script), s1, state_path=state, deadline=_t.time() + 10_000)
    assert out["status"] == "paused" and out["stage"] == "evaluate"
    assert lab.ledger.count_trials(lab.ds.fingerprint) == 0          # nothing half-written to the ledger
    s2 = LabSession(lab).load_state(json.loads(state.read_text()))
    assert [h.name for h in s2.pending] == ["lower T1"] and s2.trajectory == ["researcher:submit_hypotheses"]
    out = run_research_session(lab, _scripted(s2, script), s2, state_path=state, report_dir=tmp_path)
    assert out["status"] == "done" and out["session"]["confirmations"][0]["pass"]
    assert lab.ledger.count_trials(lab.ds.fingerprint) == 1 and lab.ledger.peeks(lab.ds.holdout_start) == 1
    assert json.loads(state.read_text())["stage"] == "done"


def test_critic_eval_cases_come_from_the_ledger(lab):
    from abg.agents.evals import build_cases
    for v in (1.0, 0.9, 0.8):
        lab.test(Hypothesis(name=f"higher T1 {v}", rationale="bigger first target", changes={"t1_r": v}))
    a = lab.test(Hypothesis(name="lower T1", rationale="earlier T1 turns more trades into wins", changes={"t1_r": 0.5}))
    b = lab.test(Hypothesis(name="lower T1 b", rationale="earlier T1 turns more trades into wins", changes={"t1_r": 0.4}))
    lab.ledger.set_verdict(a["trial_id"], "failed_holdout")
    cases = {c["name"]: c for c in build_cases(lab)}
    assert cases["only-rejected"]["allowed"] == ["revise", "stop"]
    assert b["trial_id"] in cases["candidate-among-rejects"]["show"]
    look = cases[f"lookalike-of-failed-{a['trial_id']}"]               # same knob as a holdout failure -> don't confirm
    assert look["never_confirm"] == [b["trial_id"]] and look["results"][0]["verdict"] == "candidate"
