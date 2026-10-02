"""Alerio integration: pull what Alerio did, audit it, and replay the same signals under better rules.

Data comes from Alerio's own web API (the one its dashboard uses) with your session cookie, read-only:

    GET /api/logs/feed          every message the route received + how it was parsed and executed
    GET /api/user/trades        Alerio's trade records per account (fills, P&L, how it closed)
    GET /api/user/accounts/metrics   account cash / status
    GET /api/user/workspace     the route's settings

``sync()`` turns that into one *snapshot* (``ABG_DATA_DIR/alerio/snapshot.json``): the route settings, the
accounts and one row per signal with the service's own follow-up messages and what Alerio actually did.
``abg alerio import FILE`` loads a snapshot saved any other way.  Nothing here can place, change or
cancel an order in Alerio or at the broker.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .alerts import Alert
from .engine import NY, AccountState
from .rules import Route, RouteRules, TrimRow

BASE = "https://app.alerio.dev"
MNQ_PV = 2.0


def snapshot_path(data_dir) -> Path:
    return Path(data_dir).expanduser() / "alerio" / "snapshot.json"


def load_snapshot(data_dir_or_file) -> dict:
    p = Path(data_dir_or_file)
    if p.is_dir() or not p.suffix:
        p = snapshot_path(p)
    d = json.loads(p.read_text(encoding="utf-8"))
    if d.get("kind") != "abg-alerio-snapshot":
        raise ValueError(f"{p} is not an Alerio snapshot (kind={d.get('kind')!r})")
    return d


def save_snapshot(data_dir, snap: dict) -> Path:
    p = snapshot_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap, indent=1), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- building a snapshot from the API
_N = r"([\d,]+(?:\.\d+)?)"


def _num(t: str, pat: str) -> float | None:
    m = re.search(pat, t)
    return float(m.group(1).replace(",", "")) if m else None


def parse_signal_text(text: str) -> dict | None:
    s = re.search(r"SIGNAL VALIDATED #(\d+) — (LONG|SHORT)", text)
    if not s:
        return None
    z = re.search(rf"Entry zone:\*\* {_N} – {_N}", text)
    return {"num": int(s.group(1)), "side": s.group(2), "opt": _num(text, rf"optimal \*\*{_N}"),
            "zlo": float(z.group(1).replace(",", "")) if z else None, "zhi": float(z.group(2).replace(",", "")) if z else None,
            "stop": _num(text, rf"Stop loss:\*\* {_N}"), "t1": _num(text, rf"Target 1:\*\* {_N}"),
            "fin": _num(text, rf"Final target:\*\* {_N}"), "px": _num(text, rf"Price now:\*\* {_N}"),
            "past": _num(text, r"Price is (\d+) pts past"), "reentry": "Re-entry" in text, "mgmt": []}


def mgmt_kind(text: str) -> tuple[str, float | None] | None:
    for key, kind in (("STOPPED OUT", "stopped"), ("BREAK-EVEN", "be"), ("FINAL TARGET", "final"),
                      ("TRADE CLOSED", "closed"), ("TARGET 1 HIT", "t1")):
        if key in text:
            return kind, _num(text, r"\*\*([+-]?\d+(?:\.\d+)?) pts\*\*")
    m = re.search(rf"[Mm]ove NQ stop to {_N}", text)
    if m:
        return "move", float(m.group(1).replace(",", ""))
    return None


def build_snapshot(feed_pages: list[dict], trades: dict | None = None, metrics: dict | None = None,
                   workspace: dict | None = None) -> dict:
    """Raw Alerio API responses -> snapshot (the same shape ``abg alerio import`` reads)."""
    received, flows = [], {}
    for page in feed_pages:
        for e in page.get("entries") or []:
            if e.get("event") == "received":
                md = (e.get("details") or {}).get("metadata") or {}
                received.append({"ts": e["timestamp"], "id": str(md.get("message_id") or e.get("log_id")),
                                 "ref": str(md["reply_to_message_id"]) if md.get("reply_to_message_id") else None,
                                 "flow": e.get("flow_id"), "text": (e.get("details") or {}).get("content_preview") or ""})
        for k, v in (page.get("related_entries") or {}).items():
            for x in (v if isinstance(v, list) else [v]):
                if not x:
                    continue
                f = x.get("flow_id") or k.split(":")[-1]
                o = flows.setdefault(f, {})
                det = x.get("details") or {}
                if x.get("event") == "parse_completed":
                    pr = det.get("parse_result") or {}
                    o["half"] = (pr.get("result") or {}).get("half_size")
                    o["raw_half"] = (pr.get("raw_response") or {}).get("half_size")
                elif x.get("event") == "execution_result":
                    o["exec"] = [[a.get("account_id"), a.get("outcome"), ((a.get("data") or {}).get("result") or {}).get("reject_reason"),
                                  ((a.get("data") or {}).get("result") or {}).get("fill_price")] for a in det.get("accounts") or []]
                elif x.get("event") == "position_entry_simulated":
                    o["sim"] = True
    received.sort(key=lambda m: m["ts"])
    sig, rows = {}, []
    for m in received:
        s = parse_signal_text(m["text"])
        if s:
            s.update(ts=m["ts"], id=m["id"], flow=m["flow"])
            sig[m["id"]] = s
            rows.append(s)
            continue
        if m["ref"] in sig:
            mk = mgmt_kind(m["text"])
            if mk:
                sig[m["ref"]]["mgmt"].append([m["ts"][11:19], mk[0], mk[1]])
    by_flow = {}
    for src in (trades or {}).get("sources") or []:
        for t in src.get("trades") or []:
            by_flow[str(t.get("source_trade_id", "")).replace("log:", "")] = t
    for r in rows:
        f = flows.get(r["flow"])
        if f:
            r["alerio"] = {"half": f.get("half"), "raw_half": f.get("raw_half"), "exec": f.get("exec"), "sim": bool(f.get("sim"))}
        t = by_flow.get(r["flow"])
        if t:
            r["trade"] = {"pnl": t.get("total_pnl"), "by": t.get("closed_by"), "dry": t.get("dry_run"),
                          "acc": [[a.get("account_id"), a.get("contracts"), a.get("entry_price"), a.get("exit_price"), a.get("pnl"),
                                   [[f.get("execution_kind"), f.get("qty"), f.get("exit_price"), f.get("pnl")] for f in a.get("fills") or []]]
                                  for a in t.get("accounts") or []]}
    return {"kind": "abg-alerio-snapshot", "version": 1, "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "Alerio", "route": route_from_workspace(workspace), "accounts": accounts_from(workspace, metrics),
            "signals": rows}


def route_from_workspace(ws: dict | None) -> dict:
    for routes in ((ws or {}).get("routes") or {}).values():
        for cfg in routes.values():
            pc = (cfg.get("product_configs") or {}).get("futures") or cfg
            b = (pc.get("brackets_ui") or {})
            lv = (((b.get("contracts") or {}).get("NQ") or {}).get("levels")) or []
            tps = [{"rr": float(x.get("offset_value") or 0), "pct": float(x.get("amount") or 0),
                    "stop_to_ticks": float(x.get("stop_to_value") or 0) if x.get("stop_to_value") not in (None, "") else None}
                   for x in lv if x.get("side") == "tp"]
            sl = next((float(x["offset_value"]) for x in lv if x.get("side") == "sl" and x.get("offset_value")), None)
            keep = ("allow_exits", "allow_half_size", "allow_sl_adjustments", "allow_trims", "close_at_time", "entry_order_policy",
                    "entry_window_start_time", "entry_window_end_time", "half_size_percent", "limit_order_gtd_offset_seconds",
                    "market_event_exclusions", "parser_mode", "dry_run", "disable_dry_run_fallback", "target_account_id")
            out = {k: pc.get(k) for k in keep}
            out["brackets"] = {"alert_override": b.get("alert_override"), "tp": tps, "sl_ticks": sl}
            return out
    return {}


def accounts_from(ws: dict | None, metrics: dict | None) -> list[dict]:
    m = {}
    for a in (metrics or {}).get("accounts") or []:
        for s in a.get("subaccounts") or []:
            m[s.get("account_id")] = s
    out = []
    for a in (ws or {}).get("accounts") or []:
        for s in a.get("subaccounts") or []:
            inst = (s.get("instrument_settings") or [{}])[0]
            mm = m.get(s.get("account_id"), {})
            out.append({"id": s.get("account_id"), "nickname": s.get("account_nickname"), "risk_per_trade": s.get("risk_per_trade"),
                        "contracts": inst.get("contracts"), "contract_type": inst.get("contract_type"),
                        "cash": mm.get("cashUSD"), "status": "ok", "status_reason": "", "daily_loss_limit": mm.get("daily_loss_limit")})
    return out


def mark_rejections(snap: dict) -> dict:
    """An account whose most recent entry was rejected as liquidation-only is treated as locked."""
    last = {}
    for s in snap.get("signals") or []:
        for acc, outcome, rej, _ in ((s.get("alerio") or {}).get("exec") or []):
            last[acc] = rej
    for a in snap.get("accounts") or []:
        if last.get(a["id"]) == "LiquidationOnly" and a.get("status") == "ok":
            a["status"], a["status_reason"] = "liquidation_only", a.get("status_reason") or "broker rejected the last entry: LiquidationOnly"
    return snap


class AlerioClient:
    """Read-only client for Alerio's dashboard API, authenticated with your browser session cookie
    (``ABG_ALERIO_COOKIE``: the full Cookie header value from app.alerio.dev)."""

    def __init__(self, cookie: str, base: str = BASE, timeout: float = 20.0):
        import httpx
        if not cookie:
            raise ValueError("ABG_ALERIO_COOKIE is not set - see docs/15 §15.6")
        self.http = httpx.Client(base_url=base, timeout=timeout, headers={"Cookie": cookie, "Accept": "application/json",
                                                                          "User-Agent": "abg-terminal (read-only sync)"})

    def get(self, path: str, **params) -> dict:
        r = self.http.get(path, params=params)
        if r.status_code in (401, 403) or "text/html" in r.headers.get("content-type", ""):
            raise PermissionError("Alerio session expired or invalid - copy a fresh cookie into ABG_ALERIO_COOKIE")
        r.raise_for_status()
        return r.json()

    def feed(self, max_pages: int = 20, limit: int = 200) -> list[dict]:
        pages, cursor = [], None
        for _ in range(max_pages):
            q = {"limit": limit, "order": "desc"}
            if cursor:
                q["cursor"] = cursor
            p = self.get("/api/logs/feed", **q)
            pages.append(p)
            if not p.get("has_more") or not p.get("next_cursor"):
                break
            cursor = p["next_cursor"]
        return pages

    def snapshot(self) -> dict:
        snap = build_snapshot(self.feed(), self.get("/api/user/trades", status="all", limit=100),
                              self.get("/api/user/accounts/metrics"), self.get("/api/user/workspace"))
        return mark_rejections(snap)

    def close(self) -> None:
        self.http.close()


def sync(settings) -> tuple[dict, Path]:
    c = AlerioClient(getattr(settings, "alerio_cookie", "") or "")
    try:
        snap = c.snapshot()
    finally:
        c.close()
    return snap, save_snapshot(settings.data_dir, snap)


# --------------------------------------------------------------------------- snapshot -> alerts
def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(NY)


def signal_alerts(snap: dict, symbol: str = "NQ") -> tuple[list[dict], list[Alert]]:
    """Each signal and its follow-ups as (messages, parsed alerts) for ``replay.replay``."""
    msgs, alerts = [], []
    for s in snap.get("signals") or []:
        t0 = _ts(s["ts"])
        side = 1 if s["side"] == "LONG" else -1
        a = Alert("entry", side=side, symbol=symbol, entry=s["opt"], entry_lo=s.get("zlo"), entry_hi=s.get("zhi"), stop=s["stop"],
                  targets=[x for x in (s.get("t1"), s.get("fin")) if x is not None], price=s.get("px"), ts=t0, id=s["id"],
                  num=s.get("num"), source="tradingmind", confidence=1.0)
        msgs.append({"ts": t0, "id": s["id"], "ref": None, "text": f"SIGNAL #{s.get('num')} {s['side']}"})
        alerts.append(a)
        for hhmmss, kind, val in s.get("mgmt") or []:
            h, m, sec = (int(x) for x in hhmmss.split(":"))
            tu = t0.astimezone(timezone.utc).replace(hour=h, minute=m, second=sec)
            if tu < t0.astimezone(timezone.utc):
                tu += timedelta(days=1)
            ts = tu.astimezone(NY)
            act = {"t1": "breakeven", "be": "close", "stopped": "close", "closed": "close", "final": "close", "move": "move_stop"}[kind]
            px = {"t1": s.get("t1"), "final": s.get("fin"), "stopped": s.get("stop"), "be": s.get("opt")}.get(kind)
            b = Alert(act, symbol=symbol, ts=ts, ref=s["id"], source="tradingmind", confidence=1.0, price=px,
                      trim_frac=0.5 if kind == "t1" else None, new_stop=val if kind == "move" else None)
            msgs.append({"ts": ts, "id": f"{s['id']}:{kind}", "ref": s["id"], "text": f"{kind} {val}"})
            alerts.append(b)
    order = sorted(range(len(msgs)), key=lambda i: msgs[i]["ts"])
    return [msgs[i] for i in order], [alerts[i] for i in order]


# --------------------------------------------------------------------------- routes
def mirror_route(snap: dict, contracts: int | None = None, name: str = "alerio (as configured)") -> Route:
    """Your Alerio route as it is set up: market entry, fixed size, the alert's stop/targets win, TP rows with
    "stop to N ticks" after each fill, management replies ignored, flat at close_at_time."""
    rc = snap.get("route") or {}
    b = rc.get("brackets") or {}
    tps = b.get("tp") or [{"rr": 1.0, "pct": 50, "stop_to_ticks": 10}, {"rr": 1.8, "pct": 50, "stop_to_ticks": 10}]
    n = contracts or max([a.get("contracts") or 0 for a in snap.get("accounts") or []] + [1])
    trims = [TrimRow(at_r=x["rr"], pct=x["pct"] / 100, sl_after=(x["stop_to_ticks"] or 0) * 0.25 if x.get("stop_to_ticks") is not None else None)
             for x in tps[:-1]]
    follow = bool(rc.get("allow_exits") or rc.get("allow_trims") or rc.get("allow_sl_adjustments"))
    win = [[rc.get("entry_window_start_time") or "00:00", rc.get("entry_window_end_time") or "23:59"]]
    r = RouteRules(sizing="fixed", contracts=n, max_contracts=n, risk_per_trade_usd=1e12, require_stop=False,
                   default_stop_pts=(b.get("sl_ticks") or 160) * 0.25, max_stop_pts=1e9, stop_cap_mode="skip",
                   alert_override="override" if b.get("alert_override") in ("alert_wins", "per_level") else "ignore",
                   trims=trims, runner_target_r=tps[-1]["rr"], entry_type=rc.get("entry_order_policy") or "market",
                   stale_sec=10 ** 9, max_chase_pts=1e9, max_chase_r=1e9, skip_reached_targets=False, follow_management=follow,
                   entry_windows=win, blackout_windows=[], exclude_events=list(rc.get("market_event_exclusions") or []),
                   auto_close=rc.get("close_at_time") or "", max_trades_per_day=99, stop_after_first_loss=False,
                   daily_loss_limit_usd=0.0, allowed_symbols=["NQ", "MNQ"])
    return Route(name, source="alerio", mode="dry_run", accounts=["copy"], rules=r)


def guarded_route(budget: float = 400.0, max_contracts: int = 8, name: str = "terminal (guarded)") -> Route:
    """The terminal's rule set for this source: risk-sized, limit at the optimal price, the service's own
    management followed, first-loss stop and the 10:30-11:30 blackout."""
    r = RouteRules(sizing="risk", risk_per_trade_usd=budget, max_contracts=max_contracts, max_stop_pts=80, stop_cap_mode="skip",
                   alert_override="override", trims=[TrimRow(pct=0.5, sl_after=0.0)], entry_type="market",
                   stale_sec=90, max_chase_pts=30, max_chase_r=0.6, follow_management=True, skip_reached_targets=True,
                   entry_windows=[["09:30", "15:00"]], blackout_windows=[["10:30", "11:30"]], exclude_events=[],
                   auto_close="15:00", max_trades_per_day=3, stop_after_first_loss=True, daily_loss_limit_usd=2.5 * budget)
    return Route(name, source="alerio", mode="dry_run", accounts=["copy"], rules=r)


# --------------------------------------------------------------------------- audit
def audit(snap: dict) -> dict:
    """What Alerio did with each signal, and what went wrong."""
    acc = {a["id"]: a for a in snap.get("accounts") or []}
    rc = snap.get("route") or {}
    rows, flags_total = [], {}
    for s in snap.get("signals") or []:
        side = 1 if s["side"] == "LONG" else -1
        tm = next((m[2] for m in reversed(s.get("mgmt") or []) if m[1] in ("stopped", "be", "final", "closed")), None)
        row = {"date": s["ts"][:10], "time_et": _ts(s["ts"]).strftime("%H:%M"), "num": s.get("num"), "side": s["side"],
               "optimal": s["opt"], "stop": s["stop"], "risk_pts": round(abs(s["opt"] - s["stop"]), 2), "service_pts": tm,
               "flags": [], "accounts": []}
        al = s.get("alerio") or {}
        t = s.get("trade") or {}
        if al.get("raw_half") and not al.get("half"):
            row["flags"].append("signal said size down - Alerio traded full size")
        ignored = [m for m in s.get("mgmt") or [] if m[1] in ("t1", "move", "closed", "be")]
        if ignored and not (rc.get("allow_sl_adjustments") or rc.get("allow_exits")):
            row["flags"].append(f"{len(ignored)} management message(s) ignored (route allow_* off)")
        for a_id, outcome, rej, _ in al.get("exec") or []:
            if rej:
                row["flags"].append(f"{(acc.get(a_id) or {}).get('nickname', a_id)} rejected: {rej}")
        if al.get("sim"):
            row["flags"].append("no live fill - Alerio opened a simulated position")
        if t and not t.get("dry"):
            for a_id, n, fill, exit_px, pnl, fills in t.get("acc") or []:
                if fill is None or not n:
                    continue
                worse = round((fill - s["opt"]) * side, 2)                  # + = paid more than the optimal
                risk_usd = round(n * abs(fill - s["stop"]) * MNQ_PV, 0)
                budget = (acc.get(a_id) or {}).get("risk_per_trade")
                info = {"account": (acc.get(a_id) or {}).get("nickname") or str(a_id), "contracts": n, "fill": fill,
                        "chase_pts": worse, "risk_usd": risk_usd, "pnl": pnl}
                row["accounts"].append(info)
                if worse >= 10:
                    row["flags"].append(f"filled {worse:.1f} pts worse than the optimal (market order)")
                if budget and risk_usd > 1.5 * budget:
                    row["flags"].append(f"risked ${risk_usd:,.0f} vs ${budget:,.0f} risk-per-trade ({risk_usd / budget:.1f}x)")
                if s.get("t1") is not None and (s["t1"] - fill) * side <= 0:
                    row["flags"].append("filled at/through Target 1 - the bracket's first take-profit was already reached")
                if exit_px is None and not pnl:
                    row["flags"].append("Alerio has no exit price / P&L for this trade")
        for f in row["flags"]:
            key = _flag_category(f)
            flags_total[key] = flags_total.get(key, 0) + 1
        rows.append(row)
    live = [a for r in rows for a in r["accounts"]]
    return {"signals": len(rows), "live_fills": len(live),
            "avg_chase_pts": round(sum(a["chase_pts"] for a in live) / len(live), 2) if live else None,
            "alerio_pnl": round(sum(a["pnl"] or 0 for a in live), 2),
            "service_pts": round(sum(r["service_pts"] or 0 for r in rows), 2),
            "flag_counts": dict(sorted(flags_total.items(), key=lambda x: -x[1])), "rows": rows,
            "accounts": snap.get("accounts") or []}


_CATEGORIES = (("management message", "follow-up messages ignored"), ("worse than the optimal", "filled worse than the optimal"),
               ("size down", "full size when the signal said size down"), ("risk-per-trade", "risked more than your risk-per-trade"),
               ("rejected", "account rejected by the broker"), ("no exit price", "no exit / P&L recorded"),
               ("simulated", "no live fill (simulated)"), ("Target 1", "entered at/through Target 1"))


def _flag_category(f: str) -> str:
    return next((label for needle, label in _CATEGORIES if needle in f), f)


def compare(snap: dict, bars, budgets=(300.0, 400.0, 600.0), contracts: int | None = None, dd_limit: float = 2000.0,
            samples: int = 2000) -> dict:
    """Replay every signal on the bars under (a) your Alerio route as configured and (b) the terminal's guarded
    route at a few risk budgets.  Accounts start unlocked, so this answers 'what would each rule set have done'."""
    from .replay import replay
    msgs, alerts = signal_alerts(snap)
    lo, hi = bars.index[0], bars.index[-1]
    keep = [i for i, m in enumerate(msgs) if lo <= m["ts"] <= hi]
    msgs, alerts = [msgs[i] for i in keep], [alerts[i] for i in keep]
    out = {"signals": sum(1 for a in alerts if a.action == "entry"), "from": str(lo.date()), "to": str(hi.date()), "runs": {}}
    m = mirror_route(snap, contracts)
    out["runs"][m.name] = replay(msgs, bars, m, parsed=alerts)
    for b in budgets:
        g = guarded_route(b, contracts or 8, name=f"terminal ${b:,.0f}/trade")
        out["runs"][g.name] = replay(msgs, bars, g, parsed=alerts)
    days = sorted({m["ts"].date().isoformat() for m, a in zip(msgs, alerts) if a.action == "entry"})
    for run in out["runs"].values():
        run["breach"] = breach_odds(run["trades"], days, dd_limit, samples)
    out["dd_limit"] = dd_limit
    return out


def breach_odds(trades: list[dict], days: list[str], dd_limit: float = 2000.0, samples: int = 2000, length: int = 21,
                seed: int = 7) -> dict:
    """Resample the signal days (with replacement) into ``samples`` synthetic months: how often does the
    drawdown inside a month reach ``dd_limit`` (i.e. the prop account would be closed)?"""
    import numpy as np
    if not days:
        return {}
    pnl = {d: 0.0 for d in days}
    for t in trades:
        pnl[t["opened"][:10]] = pnl.get(t["opened"][:10], 0.0) + t["usd"]
    x = np.array([pnl[d] for d in days])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), (samples, length))
    cum = np.cumsum(x[idx], axis=1)
    peak = np.maximum.accumulate(np.concatenate([np.zeros((samples, 1)), cum], axis=1), axis=1)[:, 1:]
    dd = (peak - cum).max(axis=1)
    net = cum[:, -1]
    return {"samples": samples, "length": length, "p_breach": round(float((dd >= dd_limit).mean()), 4),
            "median_month": round(float(np.median(net)), 2), "p05_month": round(float(np.percentile(net, 5)), 2),
            "p_losing_month": round(float((net < 0).mean()), 4), "median_dd": round(float(np.median(dd)), 2)}


def account_states(snap: dict) -> dict[str, AccountState]:
    out = {}
    for a in snap.get("accounts") or []:
        st = AccountState(a.get("nickname") or str(a["id"]), equity=a.get("cash") or 50_000.0,
                          status=a.get("status") or "ok", status_reason=a.get("status_reason") or "")
        out[st.name] = st
    return out
