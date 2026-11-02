"""One research history for the Strategy Lab and the forward lab (docs/17.10).

Ideas flow freely between the two labs; evidence doesn't.  An idea is identified by its setting changes (``key``),
and for each idea we keep the **seen-through date**: the last session any selection step looked at.  Only results
after that date count as out-of-sample:

* Strategy Lab trial, search window only   -> the session before its holdout starts
* Strategy Lab trial that looked at the holdout -> the day of the look (the holdout then ran to that date)
* forward lab trial (walk-forward on all data) -> the last session of the data it was tested on

Used by: the forward lab (skip what failed, test lab near-misses first, queue lab candidates), the Strategy Lab
(a holdout look counts only sessions after the seen-through date), both labs' Claude prompts, the dashboard.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path


def key(changes: dict) -> str:
    def norm(v):
        if isinstance(v, (list, tuple)):
            return ",".join(str(x) for x in v)
        if isinstance(v, float) and v.is_integer():
            return str(int(v)) if abs(v) >= 1 else str(v)
        return str(v)
    return json.dumps({k: norm(v) for k, v in sorted((changes or {}).items())}, sort_keys=True)


def _day_before(d: str) -> str:
    return str(date.fromisoformat(d) - timedelta(days=1))


# ---------------------------------------------------------------------- readers (read-only; never write either lab)
def lab_ledger(data_dir: Path) -> Path:
    return Path(data_dir).expanduser() / "lab" / "lab.sqlite3"


def lab_trials(data_dir: Path) -> list[dict]:
    p = lab_ledger(data_dir)
    if not p.exists():
        return []
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in c.execute("SELECT * FROM trials ORDER BY id")]
        peeks: dict[int, list] = {}
        for r in c.execute("SELECT trial_id, ts, holdout_start, passed FROM peeks ORDER BY id"):
            peeks.setdefault(r["trial_id"], []).append(dict(r))
    finally:
        c.close()
    out = []
    for r in rows:
        try:
            ch, sm = json.loads(r["changes"] or "{}"), json.loads(r["summary"] or "{}")
        except ValueError:
            continue
        pk = peeks.get(r["id"], [])
        seen = (str(datetime.fromtimestamp(pk[-1]["ts"]).date()) if pk
                else (_day_before(r["holdout_start"]) if r["holdout_start"] else None))
        out.append({"lab": "strategy", "id": r["id"], "at": r["ts"], "name": r["name"], "rationale": r["rationale"],
                    "changes": ch, "key": key(ch), "verdict": r["verdict"], "failed": sm.get("failed") or [],
                    "search": {k: sm.get(k) for k in ("trades", "win_rate", "net_usd", "max_dd_usd", "ratio")},
                    "base_phash": r["base_phash"], "holdout_start": r["holdout_start"],
                    "looks": [{"at": x["ts"], "passed": bool(x["passed"])} for x in pk], "seen_through": seen,
                    "source": r["source"]})
    return out


def forward_home(data_dir: Path) -> Path:
    return Path(data_dir).expanduser() / "lab" / "rolling"


def forward_trials(data_dir: Path) -> list[dict]:
    p = forward_home(data_dir) / "trials.jsonl"
    out = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for i, ln in enumerate(lines):
        try:
            t = json.loads(ln)
        except ValueError:
            continue
        out.append({"lab": "forward", "id": i + 1, "at": t.get("at"), "name": t.get("name"), "rationale": t.get("rationale"),
                    "changes": t.get("changes") or {}, "key": key(t.get("changes") or {}), "verdict": t.get("verdict"),
                    "failed": t.get("failed") or [], "search": t.get("search") or {}, "seen_through": t.get("data_to"),
                    "origin": t.get("origin")})
    return out


def forward_strategies(data_dir: Path) -> list[dict]:
    try:
        st = json.loads((forward_home(data_dir) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return st.get("strategies") or []


# ---------------------------------------------------------------------- the merged view
def registry(data_dir: Path) -> dict[str, dict]:
    """key -> {name, changes, events (both labs, oldest first), seen_through, strategy (forward), lab_ids, fwd_ids}"""
    reg: dict[str, dict] = {}
    for t in lab_trials(data_dir) + forward_trials(data_dir):
        e = reg.setdefault(t["key"], {"key": t["key"], "name": t["name"], "changes": t["changes"], "events": [],
                                      "seen_through": None, "lab_ids": [], "fwd_ids": [], "strategy": None})
        e["events"].append({"lab": t["lab"], "id": t["id"], "at": t.get("at"), "verdict": t["verdict"],
                            "failed": t["failed"], "seen_through": t["seen_through"], "looks": t.get("looks", []),
                            "base_phash": t.get("base_phash")})
        (e["lab_ids"] if t["lab"] == "strategy" else e["fwd_ids"]).append(t["id"])
        if t["seen_through"] and (e["seen_through"] is None or t["seen_through"] > e["seen_through"]):
            e["seen_through"] = t["seen_through"]
    for s in forward_strategies(data_dir):
        k = key(s.get("changes") or {})
        e = reg.setdefault(k, {"key": k, "name": s["name"], "changes": s.get("changes") or {}, "events": [],
                               "seen_through": s.get("passed_on"), "lab_ids": [], "fwd_ids": [], "strategy": None})
        e["strategy"] = {x: s.get(x) for x in ("id", "status", "fwd_start", "forward", "passed_on", "origin")}
    for e in reg.values():
        e["events"].sort(key=lambda x: x.get("at") or 0)
    return reg


def seen_through(data_dir: Path, changes: dict) -> str | None:
    e = registry(data_dir).get(key(changes))
    return e["seen_through"] if e else None


def counts(data_dir: Path) -> dict:
    lab, fwd = lab_trials(data_dir), forward_trials(data_dir)
    return {"strategy_lab": len(lab), "forward_lab": len(fwd), "total": len(lab) + len(fwd),
            "distinct_ideas": len({t["key"] for t in lab + fwd})}


def history_for_prompt(data_dir: Path, n: int = 40) -> list[dict]:
    """The most recent results from both labs, compact, for a Claude prompt or an agent tool."""
    rows = sorted(lab_trials(data_dir) + forward_trials(data_dir), key=lambda t: t.get("at") or 0)[-n:]
    return [{"lab": t["lab"], "name": t["name"], "changes": t["changes"], "verdict": t["verdict"], "failed": t["failed"],
             "win_rate": (t.get("search") or {}).get("win_rate"), "ratio": (t.get("search") or {}).get("ratio")}
            for t in rows]


# ---------------------------------------------------------------------- which ideas deserve the scarce holdout looks
def holdout_queue(data_dir: Path, holdout_start: str | None, min_unseen: int = 40, sessions: list[str] | None = None,
                  base_phash: str | None = None) -> list[dict]:
    """Lab candidates and forward strategies, best paper record first, each with how many holdout sessions it has
    NOT seen (a look counts only those).  ``sessions``: the lab dataset's holdout sessions (to count them)."""
    out = []
    for e in registry(data_dir).values():
        lab_cand = [x for x in e["events"] if x["lab"] == "strategy" and x["verdict"] == "candidate"]
        s = e["strategy"]
        if not lab_cand and not (s and s.get("status") in ("paper", "ready")):
            continue
        f = (s or {}).get("forward") or {}
        c, l = f.get("cand") or {}, f.get("live") or {}
        unseen = None
        if sessions is not None:
            unseen = sum(1 for d in sessions if not e["seen_through"] or d > e["seen_through"])
        from abg.alpha.strategy import AlphaParams
        blocker = None
        if any(not hasattr(AlphaParams(), k) for k in e["changes"]):
            blocker = "changes a setting the bot no longer has"
        elif lab_cand and base_phash and lab_cand[-1].get("base_phash") != base_phash and not s:
            blocker = "tested against older live settings: re-test it first (the forward lab does)"
        elif unseen is not None and unseen < min_unseen:
            blocker = f"not yet: seen through {e['seen_through']}"
        out.append({"key": e["key"], "name": e["name"], "changes": e["changes"], "lab_trial": lab_cand[-1]["id"] if lab_cand else None,
                    "forward_id": (s or {}).get("id"), "forward_status": (s or {}).get("status"),
                    "paper_sessions": f.get("sessions") or 0, "paper_win_rate": c.get("win_rate"), "live_win_rate": l.get("win_rate"),
                    "paper_net": c.get("net_usd"), "live_net": l.get("net_usd"), "seen_through": e["seen_through"],
                    "unseen_holdout_sessions": unseen, "blocker": blocker,
                    "eligible": blocker is None,
                    "edge": ((c.get("net_usd") or 0) - (l.get("net_usd") or 0)) if f else None})
    out.sort(key=lambda x: (not x["eligible"], -(x["paper_sessions"] >= 20), -(x["edge"] if x["edge"] is not None else -1e9)))
    return out
