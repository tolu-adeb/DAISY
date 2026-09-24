"""AI insight section.

Two layers:
1. ``rule_based_insight`` - deterministic narrative built from the report.  Always
   available, zero latency, used as the fallback.
2. ``claude_insight`` - calls the Anthropic Messages API with a *compact* JSON digest of
   the report (not raw price data, which keeps tokens and latency low) and asks for a
   structured JSON answer.  Any failure (no key, timeout, bad JSON) falls back to (1).

Responses are cached by a hash of the digest, so re-running the same analysis within
``ttl_ai`` costs nothing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from ..config import Settings
from ..errors import ABGError
from ..http import HttpClient
from ..utils import jsonable

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are the analyst voice of the AI Business Group Intelligence Terminal. You receive a JSON digest of "
    "technical, statistical, sentiment, options and risk analytics for one stock. Write a concise, balanced, "
    "evidence-based read of the setup for a student investment team. Reference specific numbers from the digest. "
    "Do not invent data that is not in the digest. This is educational analysis, not investment advice. "
    "Respond with ONLY a JSON object with keys: summary (2-4 sentences), bull_case (array of 2-4 strings), "
    "bear_case (array of 2-4 strings), key_levels (array of strings), risks_to_watch (array of 2-4 strings), "
    "stance (one of: bullish, cautiously bullish, neutral, cautiously bearish, bearish), confidence (low|medium|high)."
)


def digest(report: dict) -> dict:
    """Pick the few dozen numbers that matter; keeps the prompt ~1-2k tokens."""
    ind = report.get("indicators") or {}
    stats = report.get("statistics") or {}
    risk = (report.get("risk") or [{}])[0] if report.get("risk") else {}
    opt = (report.get("options") or {}).get("summary") or {}
    fund = report.get("fundamentals") or {}
    keep = lambda d, ks: {k: d.get(k) for k in ks if d.get(k) is not None}  # noqa: E731
    return jsonable({
        "symbol": report.get("symbol"), "as_of": report.get("as_of"), "price": (report.get("quote") or {}).get("price"),
        "change_pct": (report.get("quote") or {}).get("change_pct"),
        "fundamentals": keep(fund, ["name", "sector", "industry", "market_cap", "pe", "forward_pe", "beta",
                                    "dividend_yield", "week52_high", "week52_low"]),
        "signal": {k: (report.get("signal") or {}).get(k) for k in ("score", "label")},
        "regime": report.get("regime"),
        "indicators": keep(ind, ["rsi_14", "macd_hist", "adx", "plus_di", "minus_di", "bb_pctb", "atr_pct",
                                 "williams_r", "cci_20", "mfi_14", "rel_volume", "sma_50", "sma_200", "vwap_20"]),
        "plays": [{k: p.get(k) for k in ("name", "direction", "confidence", "levels")} for p in (report.get("plays") or [])[:3]],
        "levels": report.get("levels"),
        "statistics": keep(stats, ["total_return_pct", "ann_volatility_pct", "vol_20d_pct", "sharpe", "max_drawdown_pct",
                                   "current_drawdown_pct", "beta", "relative_return_pct"]),
        "sentiment": keep(report.get("sentiment") or {}, ["score", "label", "articles", "last_24h"]),
        "headlines": [n.get("title") for n in (report.get("news") or [])[:6]],
        "options": keep(opt, ["atm_iv", "expected_move_pct", "skew_25d", "put_call_oi_ratio", "max_pain"]),
        "risk": keep(risk, ["score", "level", "metrics"]),
    })


def rule_based_insight(report: dict) -> dict:
    sig = report.get("signal") or {}
    reg = report.get("regime") or {}
    ind = report.get("indicators") or {}
    sent = report.get("sentiment") or {}
    risk = (report.get("risk") or [{}])[0] if report.get("risk") else {}
    plays = report.get("plays") or []
    score = sig.get("score") or 0
    stance = ("bullish" if score >= 50 else "cautiously bullish" if score >= 20 else "neutral" if score > -20
              else "cautiously bearish" if score > -50 else "bearish")
    bull, bear = [], []
    for name, c in (sig.get("components") or {}).items():
        if c["vote"] > 0.2:
            bull.append(f"{name.upper()}: {c['reason']}")
        elif c["vote"] < -0.2:
            bear.append(f"{name.upper()}: {c['reason']}")
    if sent.get("score") is not None:
        (bull if sent["score"] > 0.15 else bear if sent["score"] < -0.15 else []).append(
            f"News sentiment {sent['label'].lower()} ({sent['score']:+.2f} across {sent.get('articles', 0)} articles)")
    top = plays[0] if plays else {}
    summary = (f"{report.get('symbol')} composite signal is {sig.get('label', 'n/a')} ({score:+.0f}/100) in "
               f"{'an' if str(reg.get('trend', '')).startswith('u') else 'a'} {reg.get('trend', 'n/a')} with {reg.get('trend_strength') or 'unclear'} trend strength and "
               f"{reg.get('volatility_regime') or 'normal'} volatility. ")
    if top and top.get("name") != "No Clear Setup":
        summary += f"Top rule-based setup: {top['name']} ({top['confidence']:.0%} of conditions met). "
    if risk.get("level"):
        summary += f"Baseline risk is {risk['level'].lower()} ({risk.get('score', 0):.0f}/100)."
    levels = report.get("levels") or {}
    kl = [f"Support {x:.2f}" for x in (levels.get("support") or [])[:2]] + \
         [f"Resistance {x:.2f}" for x in (levels.get("resistance") or [])[:2]]
    risks = []
    if (ind.get("atr_pct") or 0) > 4:
        risks.append(f"High daily range: ATR is {ind['atr_pct']:.1f}% of price")
    if (ind.get("rsi_14") or 50) > 75:
        risks.append("RSI overbought - pullback risk")
    if (ind.get("rsi_14") or 50) < 25:
        risks.append("RSI deeply oversold - trend may still be under pressure")
    for d in (risk.get("drivers") or [])[:2]:
        risks.append(f"Risk driver: {d.get('factor')} ({d.get('detail', '')})")
    return {"engine": "rule-based", "summary": summary.strip(), "bull_case": bull[:4] or ["No strong bullish evidence"],
            "bear_case": bear[:4] or ["No strong bearish evidence"], "key_levels": kl, "risks_to_watch": risks[:4],
            "stance": stance, "confidence": "medium" if abs(score) >= 35 else "low"}


def _extract_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    return json.loads(m.group(0))


async def claude_insight(report: dict, settings: Settings, http: HttpClient, cache=None) -> dict:
    fallback = rule_based_insight(report)
    if not settings.ai_enabled or not settings.anthropic_api_key:
        fallback["note"] = "Set ANTHROPIC_API_KEY to enable Claude-generated insight." if settings.ai_enabled else "AI disabled."
        return fallback
    dg = digest(report)
    key = "ai:" + hashlib.sha256(json.dumps(dg, sort_keys=True, default=str).encode()).hexdigest()[:32]
    if cache is not None:
        hit = cache.get(key, settings.ttl_ai)
        if hit is not None and hit.fresh:
            return {**hit.value, "cached": True}
    body = {"model": settings.anthropic_model, "max_tokens": settings.ai_max_tokens, "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": "Analytics digest:\n" + json.dumps(dg, separators=(",", ":"))}]}
    try:
        data = await http.post_json(f"{settings.anthropic_base_url.rstrip('/')}/v1/messages", provider="anthropic",
                                    json=body, timeout=settings.ai_timeout,
                                    headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01",
                                             "content-type": "application/json"})
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        out = _extract_json(text)
        out = {"engine": f"claude:{settings.anthropic_model}", **{k: out.get(k) for k in
               ("summary", "bull_case", "bear_case", "key_levels", "risks_to_watch", "stance", "confidence")}}
        if cache is not None:
            cache.set(key, out)
        return out
    except (ABGError, ValueError, KeyError, TypeError) as e:
        log.warning("Claude insight failed, using rule-based fallback: %s", e)
        fallback["note"] = f"Claude unavailable ({getattr(e, 'code', type(e).__name__)}); showing rule-based insight."
        return fallback
