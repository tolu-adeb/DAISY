"""Read another signal service's track record (DiscordKit JSON exports) and test simple rules on it.

``parse_journal``  daily journal embeds ("📒 Trading Journal — Tue Jul 07, 2026") -> one row per trade:
                   date, time, side, setup, entry, points, outcome.  "No qualifying setups" days are kept
                   as flat days.
``parse_signals``  the live channel's "SIGNAL VALIDATED" embeds -> entry zone, stop, targets, price at the
                   alert, and the outcome from the replies (Target 1 / stopped / break-even / final).
``rules_report``   replays the trade list under day rules (stop after the first loss, a time cut-off,
                   max trades) - fitted on the first two thirds of the dates, checked on the rest.

This works on what the service *published*; their fills may be optimistic (one of their own corrections
says so), so treat it as their best case.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

TITLE_J = re.compile(r"Trading Journal\s+[—-]\s+\w{3}\s+(\w{3}\s+\d{1,2},\s+\d{4})")
LINE_J = re.compile(r"\*\*(\d{1,2}:\d{2}) ET · (?:🟥|🟩)?\s*(SHORT|LONG)(?: · ([A-Z_]+))?\*\* @ ([\d,\.]+) → \*\*([+-]?[\d\.]+) pts\*\*"
                    r"(?: \(([^)]+)\))?")
NUM = r"([\d,]+(?:\.\d+)?)"


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _embeds(path) -> tuple[dict, list[dict]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("channel") or {}, data.get("messages") or []


def parse_journal(path) -> tuple[pd.DataFrame, list[str]]:
    _, msgs = _embeds(path)
    rows, flat = [], []
    for m in msgs:
        for e in m.get("embeds") or []:
            t = TITLE_J.search(e.get("title") or "")
            if not t:
                continue
            day = datetime.strptime(t.group(1).replace("  ", " "), "%b %d, %Y").date().isoformat()
            desc = e.get("description") or ""
            found = LINE_J.findall(desc)
            if not found and "No qualifying setups" in desc:
                flat.append(day)
            for tm, side, setup, entry, pts, outcome in found:
                rows.append({"date": day, "time": tm.zfill(5), "dir": "L" if side == "LONG" else "S",
                             "setup": setup or "NA", "entry": _num(entry), "pts": float(pts), "outcome": (outcome or "").lower()})
    df = pd.DataFrame(rows).sort_values(["date", "time"]).reset_index(drop=True) if rows else pd.DataFrame(
        columns=["date", "time", "dir", "setup", "entry", "pts", "outcome"])
    return df, sorted(set(flat))


SIG_TITLE = re.compile(r"SIGNAL VALIDATED #(\d+)\s+[—-]\s+(SHORT|LONG)")


def parse_signals(path) -> pd.DataFrame:
    _, msgs = _embeds(path)
    by_id: dict[str, dict] = {}
    for m in msgs:
        if "DEMO" in (m.get("content") or ""):
            continue
        for e in m.get("embeds") or []:
            title, desc = e.get("title") or "", e.get("description") or ""
            s = SIG_TITLE.search(title)
            if s:
                g = lambda pat: (lambda r: _num(r.group(1)) if r else None)(re.search(pat, desc))  # noqa: E731
                zone = re.search(rf"Entry zone:\*\* {NUM} [–-] {NUM}", desc)
                by_id[m["id"]] = {
                    "ts": m["timestamp"], "num": int(s.group(1)), "side": s.group(2),
                    "zone_lo": _num(zone.group(1)) if zone else None, "zone_hi": _num(zone.group(2)) if zone else None,
                    "optimal": g(rf"optimal \*\*{NUM}"), "stop": g(rf"Stop loss:\*\* {NUM}"),
                    "t1": g(rf"Target 1:\*\* {NUM}"), "final": g(rf"Final target:\*\* {NUM}"),
                    "price": g(rf"Price now:\*\* {NUM}"), "direct": "Direct signal" in desc,
                    "chase": "past the entry" in desc or "already reached Target 1" in desc, "outcome": None, "pts": None}
                continue
            ref = ((m.get("reference") or {}).get("messageId"))
            if ref in by_id:
                pts = re.search(r"\*\*([+-]?[\d\.]+) pts\*\*", desc)
                for key, label in (("STOPPED OUT", "stopped"), ("BREAK-EVEN", "breakeven"), ("FINAL TARGET", "final"),
                                   ("TRADE CLOSED", "closed"), ("TARGET 1 HIT", "t1")):
                    if key in title:
                        rec = by_id[ref]
                        if label == "t1":
                            rec["t1_hit"] = True
                        elif pts:
                            rec["outcome"], rec["pts"] = label, float(pts.group(1))
                        break
    df = pd.DataFrame(by_id.values())
    if len(df):
        sgn = df["side"].map({"LONG": 1, "SHORT": -1})
        df["risk"] = (df["optimal"] - df["stop"]) * sgn
        df["t1_r"] = (df["t1"] - df["optimal"]) * sgn / df["risk"]
        df["final_r"] = (df["final"] - df["optimal"]) * sgn / df["risk"]
        df["past_entry"] = (df["price"] - df["optimal"]) * sgn
    return df


def load_trades(path) -> tuple[pd.DataFrame, list[str]]:
    """A DiscordKit journal export (.json) or a CSV with date,time,dir,pts columns."""
    p = Path(path)
    if p.suffix.lower() == ".json":
        return parse_journal(p)
    df = pd.read_csv(p)
    return df, []


def apply_rules(t: pd.DataFrame, cut: tuple[int, int] | None = None, stop_after_loss: bool = False,
                max_n: int | None = None) -> pd.DataFrame:
    keep = []
    for _, g in t.groupby("date", sort=True):
        n, lost = 0, False
        for _, r in g.sort_values("time").iterrows():
            hh, mm = str(r["time"]).split(":")
            m = int(hh) * 60 + int(mm)
            if cut and cut[0] <= m < cut[1]:
                continue
            if (stop_after_loss and lost) or (max_n and n >= max_n):
                break
            keep.append(r)
            n += 1
            lost = lost or r["pts"] < 0
    return pd.DataFrame(keep, columns=t.columns)


def stats(x: pd.DataFrame) -> dict:
    if not len(x):
        return {"n": 0}
    daily = x.groupby("date")["pts"].sum().cumsum()
    neg = -x.loc[x.pts < 0, "pts"].sum()
    return {"n": int(len(x)), "win_rate": round(float((x.pts > 0).mean()), 3), "net_pts": round(float(x.pts.sum()), 1),
            "avg_pts": round(float(x.pts.mean()), 1), "max_dd_pts": round(float((daily.cummax() - daily).max()), 1),
            "profit_factor": round(float(x.loc[x.pts > 0, "pts"].sum() / neg), 2) if neg > 0 else None}


RULES = {
    "as published": {},
    "stop after the first loss": {"stop_after_loss": True},
    "no new entries 10:30-11:30": {"cut": (630, 690)},
    "max 2 trades a day": {"max_n": 2},
    "first loss stop + no 10:30-11:30": {"stop_after_loss": True, "cut": (630, 690)},
    "entries before 10:30 only": {"cut": (630, 24 * 60)},
}


def rules_report(t: pd.DataFrame, split: float = 0.67) -> dict:
    t = t.copy()
    t["time"] = t["time"].astype(str).str.zfill(5)
    dates = sorted(t["date"].unique())
    cut_date = dates[int(len(dates) * split)] if len(dates) > 3 else dates[-1]
    early, late = t[t.date < cut_date], t[t.date >= cut_date]
    out = {"split_date": cut_date, "rules": {}}
    for name, kw in RULES.items():
        out["rules"][name] = {"fit": stats(apply_rules(early, **kw)), "check": stats(apply_rules(late, **kw)),
                              "all": stats(apply_rules(t, **kw))}
    mins = t["time"].str[:2].astype(int) * 60 + t["time"].str[3:].astype(int)
    buckets = pd.cut(mins, [0, 600, 630, 690, 24 * 60], labels=["before 10:00", "10:00-10:30", "10:30-11:30", "after 11:30"])
    out["by_time"] = {str(k): stats(g) for k, g in t.groupby(buckets, observed=True)}
    t["nth"] = t.groupby("date").cumcount() + 1
    out["by_trade_number"] = {f"#{int(k)}{'+' if k == 3 else ''}": stats(g) for k, g in t.groupby(t["nth"].clip(upper=3))}
    prev = t.groupby("date")["pts"].shift().fillna(0) < 0
    out["after_a_loss"] = {"after a loss": stats(t[prev]), "otherwise": stats(t[~prev])}
    out["by_side"] = {k: stats(g) for k, g in t.groupby("dir")}
    return out
