"""Parse a DiscordKit HTML export of a swing-ideas channel into structured signals.

Usage:  python -m swingdesk.parse_export data/export.html data/signals_raw.json
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime

from bs4 import BeautifulSoup

NUM = r"\$?\s*([0-9][0-9,]*\.?[0-9]*)"
RANGE = NUM + r"\s*(?:[–\-]|to)\s*" + NUM

ENTRY_KEYS = r"(?:entry area|buying zone|buying area|put entry zone|entry zone|shorting zone)"
TARGET_KEYS = r"(?:target|take profit(?: \(tp\))?|tp)"
STOP_KEYS = r"(?:stop loss(?: \(sl\))?|top loss|sl)"


def _num(s: str) -> float:
    return float(s.replace(",", "").replace("$", "").strip())


def _levels(text: str, keys: str):
    """Return (low, high) from the structured level lines at the end of a post.

    Only lines that START with the key are used (e.g. "Target: $205", "Stop Loss (SL): $840"),
    so prose like "the target at $23.5 is the 1.0 extension" is never picked up.
    """
    for line in text.split("\n"):
        line = line.strip()
        if not re.match(r"^" + keys + r"\b", line, re.I) or ":" not in line:
            continue
        rhs = line.split(":", 1)[1]
        m = re.search(RANGE, rhs)
        if m:
            a, b = _num(m.group(1)), _num(m.group(2))
            return min(a, b), max(a, b)
        m = re.search(NUM, rhs)
        if m:
            v = _num(m.group(1))
            return v, v
    return None


def parse(path: str) -> list[dict]:
    soup = BeautifulSoup(open(path, encoding="utf-8").read(), "html.parser")
    out = []
    for grp in soup.select(".chatlog__message-group"):
        author = None
        for cont in grp.select(".chatlog__message-container"):
            a = cont.select_one(".chatlog__author")
            if a:
                author = a.get("title") or a.text.strip()
            ts_el = cont.select_one(".chatlog__timestamp") or cont.select_one(".chatlog__short-timestamp")
            ts = datetime.strptime(ts_el["title"], "%m/%d/%y, %I:%M %p") if ts_el else None
            body = cont.select_one(".chatlog__content .chatlog__markdown-preserve")
            if not body:
                continue
            for br in body.find_all("br"):
                br.replace_with("\n")
            text = body.get_text("\n").strip()
            reply = cont.select_one(".chatlog__reply-content")
            out.append({
                "id": cont.get("data-message-id"),
                "author": author,
                "ts": ts.isoformat() if ts else None,
                "text": text,
                "reply_to": reply.get_text(" ").strip()[:120] if reply else None,
                "has_chart": bool(cont.select_one(".chatlog__attachment")),
            })
    return out


def extract_signal(msg: dict) -> dict | None:
    text = msg["text"]
    first = text.split("\n", 1)[0].strip()
    m = re.match(r"^([A-Z][A-Z.&]{0,6})\s*[|\-—]", first) or re.match(r"^.*\(([A-Z]{1,5})\)\s*-", first)
    if not m:
        return None
    ticker = m.group(1).replace("&", "")
    entry = _levels(text, ENTRY_KEYS)
    target = _levels(text, TARGET_KEYS)
    stop = _levels(text, STOP_KEYS)
    lower = text.lower()
    direction = "short" if ("put entry" in lower or "shorting zone" in lower) else "long"
    return {
        "id": msg["id"], "author": msg["author"], "ts": msg["ts"], "ticker": ticker,
        "direction": direction, "entry": entry, "target": target, "stop": stop,
        "cmp_entry": "current market price" in lower,
        "conditional": any(k in lower for k in ("contingent", "conditional", "daily close above", "confirm")),
        "text": text,
    }


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    msgs = parse(src)
    sigs = [s for s in (extract_signal(m) for m in msgs) if s]
    hits = [m for m in msgs if re.match(r"^target hit", m["text"], re.I)]
    json.dump({"messages": msgs, "signals": sigs, "target_hit_posts": hits}, open(dst, "w"), indent=1)
    print(f"{len(msgs)} messages, {len(sigs)} signal posts, {len(hits)} 'target hit' posts")
