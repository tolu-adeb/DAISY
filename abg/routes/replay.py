"""Execution replay: run an exported channel history through a route and fill it on real bars.

Alerio's replay covers 30 days of a Discord route and shows what *would have been parsed*.  This one
takes any length of DiscordKit JSON export (or a list of (time, text) pairs), fills every bracket on
the intraday bars, and reports the $ result per day under your rules next to "as copied" (fixed size,
the service's own stops and targets, no day rules) - i.e. what the plain copier did.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from .alerts import Alert, parse_alert
from .engine import NY, AccountState, RouteEngine
from .rules import Route, RouteRules, TrimRow


def load_export(path) -> list[dict]:
    """DiscordKit export -> [{"ts", "id", "ref", "text"}] oldest first."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for m in data.get("messages") or []:
        parts = [m.get("content") or ""]
        for e in m.get("embeds") or []:
            parts += [e.get("title") or "", e.get("description") or ""]
            parts += [f"{f.get('name', '')}: {f.get('value', '')}" for f in e.get("fields") or []]
        ts = pd.Timestamp(m["timestamp"])
        ts = (ts.tz_localize("UTC") if ts.tzinfo is None else ts).tz_convert(NY).to_pydatetime()
        out.append({"ts": ts, "id": str(m.get("id")), "ref": str((m.get("reference") or {}).get("messageId") or "") or None,
                    "text": "\n".join(p for p in parts if p)})
    return sorted(out, key=lambda x: x["ts"])


def as_copied_route(contracts: int = 8, name: str = "as copied") -> Route:
    """What a plain copier does: fixed size, the service's own stop/targets, no day rules or guards."""
    r = RouteRules(sizing="fixed", contracts=contracts, max_contracts=contracts, risk_per_trade_usd=1e12,
                   require_stop=False, default_stop_pts=1e9, max_stop_pts=1e9, alert_override="override",
                   trims=[TrimRow(pct=0.5, sl_after=0.0)], stale_sec=10 ** 9, max_chase_pts=1e9, max_chase_r=1e9,
                   entry_windows=[["00:00", "23:59"]], blackout_windows=[], exclude_events=[],
                   max_trades_per_day=99, stop_after_first_loss=False, daily_loss_limit_usd=0.0,
                   allowed_symbols=["NQ", "MNQ", "ES", "MES"])
    return Route(name, mode="dry_run", accounts=["copy"], rules=r)


def replay(messages: list[dict], bars: pd.DataFrame, route: Route, start_equity: float = 50_000.0,
           default_symbol: str = "NQ", parsed: list[Alert] | None = None) -> dict:
    eng = RouteEngine(route, {a: AccountState(a, equity=start_equity) for a in route.accounts})
    idx = list(bars.index)
    i, log = 0, []
    alerts = parsed or [parse_alert(m["text"], ts=m["ts"], default_symbol=default_symbol, ref=m.get("ref"), id=m.get("id"))
                        for m in messages]
    last_close = None
    step = (idx[1] - idx[0]) if len(idx) > 1 else pd.Timedelta(minutes=5)
    for m, a in zip(messages, alerts):
        while i < len(idx) and idx[i] + step <= m["ts"]:
            row = bars.iloc[i]
            eng.on_bar((idx[i] + step).to_pydatetime(), float(row.high), float(row.low), float(row.close), idx[i].to_pydatetime())
            last_close = float(row.close)
            i += 1
        if a.action in ("info", "unknown"):
            continue
        # entries: the price quoted in the alert is the market at that moment; updates: the last closed bar
        # (a reply that names its price - Target 1, the stop, the final - fills there)
        price = a.price if a.price is not None else last_close
        for d in eng.on_alert(a, now=m["ts"], price=price):
            log.append({"ts": m["ts"].isoformat(), "text": m["text"][:160], **d.to_dict()})
    while i < len(idx):
        row = bars.iloc[i]
        eng.on_bar((idx[i] + step).to_pydatetime(), float(row.high), float(row.low), float(row.close), idx[i].to_pydatetime())
        i += 1
    trades = [h | {"account": n} for n, acct in eng.accounts.items() for h in acct.history]
    return {"route": route.name, "log": log, "trades": trades, "summary": summarize(trades)}


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    usd = pd.Series([t["usd"] for t in trades])
    days = pd.Series([t["opened"][:10] for t in trades])
    daily = usd.groupby(days).sum()
    cum = daily.cumsum()
    return {"trades": int(len(usd)), "win_rate": round(float((usd > 0).mean()), 3), "net_usd": round(float(usd.sum()), 2),
            "worst_trade_usd": round(float(usd.min()), 2), "best_trade_usd": round(float(usd.max()), 2),
            "worst_day_usd": round(float(daily.min()), 2), "max_dd_usd": round(float((cum.cummax().clip(lower=0) - cum).max()), 2),
            "profit_factor": round(float(usd[usd > 0].sum() / -usd[usd < 0].sum()), 2) if (usd < 0).any() else None,
            "days": int(len(daily))}
