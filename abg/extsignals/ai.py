"""Claude as a fallback reader and a narrator (optional; needs ANTHROPIC_API_KEY).

* ``parse``: when the rule-based parser can't find a plan in a post, Claude extracts the signals as
  JSON.  Every level it returns must appear literally in the post (no invented numbers), otherwise
  that signal is dropped.  Ideas read this way carry a warning so they can be double-checked.
* ``narrative``: two or three plain-English sentences on top of key Discord messages, written from
  the same structured analysis (never from its own market knowledge).

Both calls time out quickly and fail silently: the terminal behaves exactly as before without a key.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from ..errors import ABGError
from .parser import ParsedSignal, _map_symbol

log = logging.getLogger(__name__)

PARSE_SYSTEM = """You extract trade signals from chat messages for a paper-tracking tool.
Return ONLY JSON: {"signals": [{"symbol": str, "direction": "long"|"short", "entry_type": "zone"|"breakout_above"|
"breakdown_below"|"market", "entry_low": number|null, "entry_high": number|null, "stop": number|null,
"targets": [number], "setup": str|null, "timeframe": "day"|"swing"|"position", "next_earnings": "YYYY-MM-DD"|null}]}
Rules: only include a signal when the message states a ticker and at least an entry and a stop or target.
Copy numbers exactly as written; never compute, round or invent levels. Numbers inside the prose that
are financial figures (revenue, EPS, percentages, dates) are NOT levels. If there is no signal return {"signals": []}."""

NARRATE_SYSTEM = """You write the 2-3 sentence lead for a trading-desk message about a paper-tracked trade idea.
Use ONLY the facts in the JSON you are given (decision, reasons, market context, the source's thesis, plan,
risks). Plain English, specific numbers, no hype, no advice language ("you should"), no emojis. Explain what
happened and the main reason, then the main thing to watch."""

NARRATE_KINDS = {"ingested", "entry", "entry_blocked", "entry_adjust", "target_hit", "stop_hit", "exit", "advisory",
                 "trailing_stop", "breakeven_stop", "time_exit", "missed", "invalidated"}


def _nums(text: str) -> set[float]:
    return {float(x.replace(",", "")) for x in re.findall(r"\d{1,6}(?:,\d{3})*(?:\.\d+)?", text or "")}


class SignalAI:
    def __init__(self, settings, http):
        self.s, self.http = settings, http
        self._cache: dict[str, str] = {}
        self.calls = 0
        self.failures = 0

    async def _ask(self, system: str, user: str, max_tokens: int = 700) -> str | None:
        body = {"model": self.s.anthropic_model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        self.calls += 1
        try:
            data = await asyncio.wait_for(self.http.post_json(
                f"{self.s.anthropic_base_url.rstrip('/')}/v1/messages", provider="anthropic", json=body,
                headers={"x-api-key": self.s.anthropic_api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"}), timeout=self.s.ai_timeout)
            return "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text").strip()
        except (ABGError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 - optional feature, never fatal
            self.failures += 1
            log.info("AI call failed: %s", getattr(e, "message", e))
            return None

    async def parse(self, text: str) -> list[ParsedSignal]:
        if not self.s.ext_ai_parse:
            return []
        out = await self._ask(PARSE_SYSTEM, text[:6000])
        if not out:
            return []
        m = re.search(r"\{.*\}", out, re.S)
        try:
            items = json.loads(m.group(0)).get("signals") or [] if m else []
        except ValueError:
            return []
        present = _nums(text)
        res = []
        for it in items[:10]:
            try:
                lv = [it.get("entry_low"), it.get("entry_high"), it.get("stop"), *(it.get("targets") or [])]
                lv = [float(x) for x in lv if x is not None]
                if not lv or any(x not in present for x in lv) or not it.get("symbol"):
                    continue                                      # a number not in the post = hallucinated
                sym, inst = _map_symbol(str(it["symbol"]).upper().lstrip("$"))
                lo, hi = it.get("entry_low"), it.get("entry_high")
                lo = float(lo) if lo is not None else (float(hi) if hi is not None else None)
                hi = float(hi) if hi is not None else lo
                p = ParsedSignal(kind="idea", symbol=sym, instrument=inst, direction=it.get("direction"),
                                 entry_type=it.get("entry_type") or "zone", entry_low=min(lo, hi) if lo else None,
                                 entry_high=max(lo, hi) if lo else None,
                                 stop=float(it["stop"]) if it.get("stop") is not None else None,
                                 targets=[float(t) for t in it.get("targets") or []],
                                 timeframe=it.get("timeframe") or "swing", raw=text, format="ai",
                                 meta={"setup": it.get("setup"), "parsed_by": "ai",
                                       "next_earnings": it.get("next_earnings")},
                                 warnings=["read by AI (the rule parser couldn't): double-check the levels"],
                                 confidence=0.6)
                if p.direction in ("long", "short") and p.entry_type:
                    res.append(p)
            except (TypeError, ValueError, KeyError):
                continue
        return res

    async def narrative(self, kind: str, idea, x: dict) -> str | None:
        if not self.s.ext_ai_narrative or kind not in NARRATE_KINDS:
            return None
        key = f"{idea.id}:{kind}:{x.get('title')}"
        if key in self._cache:
            return self._cache[key]
        payload = {k: x.get(k) for k in ("title", "summary", "why", "context", "thesis", "plan", "risks", "watch")}
        out = await self._ask(NARRATE_SYSTEM, json.dumps(payload, ensure_ascii=False)[:6000], max_tokens=220)
        if out:
            out = re.sub(r"\s+", " ", out).strip()[:600]
            self._cache[key] = out
        return out
