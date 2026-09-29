"""Labeled / report-style signals ("Buying zone: $168-$175", "Stop Loss: $99.79 – $99", "Ticker: DIS" …).

Signal services often post a research write-up (prose full of revenue, EPS, %, dates, Fib levels)
followed by labeled trade levels.  The free-text tokenizer would read every number in the prose,
so for these posts the levels are taken **only** from labeled lines, and the prose is kept as the
source's thesis (summary + risk sentences) for the Discord analysis.

Also here: ``split_signals`` (one message may contain several ideas separated by ––– / --- lines
or repeated "Ticker:" blocks) and date parsing for "Next Earnings: November 12".
"""
from __future__ import annotations

import re
from datetime import date, datetime

MONEY = r"\$?\s*(\d{1,6}(?:,\d{3})*(?:\.\d+)?)\s*(k|K)?"
SEP_RE = re.compile(r"^\s*(?:[-–—_=*•~]\s*){3,}\s*$", re.M)
LABEL_RE = re.compile(r"^\s*[*_>•\-]*\s*([A-Za-z][A-Za-z0-9 &/()'’\-.,]{0,70}?)\s*[*_]*\s*[:：]\s*(.+?)\s*$")
HEADER_RE = re.compile(r"^\s*\$?([A-Z]{1,5}(?:[.\-][A-Z]{1,2})?)\s*[|:–—-]\s*([A-Z][\w .,&'’()-]{1,80}?)[,.]?\s*$")

LABELS: list[tuple[str, str]] = [   # (canonical, regex on the lower-cased label) - first match wins
    ("stop", r"^(?:hard\s+)?stop(?:[\s-]*loss(?:es)?)?$|^sl$|^invalidation(?: level)?$|^stop(?: level| price| zone| area)$|^risk level$"),
    ("target", r"^(?:take[\s-]*profits?|tp\s*\d?|pt\s*\d?|price[\s-]*targets?|targets?(?:\s*\d)?|objectives?|exit targets?|profit targets?|goal)$"),
    ("entry", r"^(?:entry|entries|entry (?:zone|price|range|area)|buy(?:ing)? (?:zone|area|range|price)|buy|long entry|"
              r"accumulation zone|accumulate|add zone|demand zone|short entry|sell(?:ing)? (?:zone|area|range)|short zone)$"),
    ("ticker", r"^(?:ticker|symbol|stock|asset|pair|instrument)$"),
    ("setup", r"^(?:setup|set-up|pattern|chart pattern|strategy|trade type|play|structure)$"),
    ("direction", r"^(?:direction|side|bias|position)$"),
    ("rr", r"^(?:risk[\s-]*(?:to|/|:)[\s-]*reward|r\s*[:/]\s*r|rr|reward[\s-]*(?:to|/)[\s-]*risk)$"),
    ("fund_score", r"fundamental"),
    ("fin_target", r"financials?.*target|target.*financials?"),
    ("confidence", r"^(?:confidence|conviction)(?: score| level| rating)?$"),
    ("reason", r"^(?:reason(?:ing)?|thesis|rationale|why|summary)$"),
    ("catalyst", r"catalysts?$"),
    ("risk_note", r"issue|risks?$|concerns?$|watch ?out|bear case|headwinds?"),
    ("earnings", r"earnings"),
    ("timeframe", r"^(?:time ?frame|horizon|holding period|hold time|duration)$"),
]


def split_signals(text: str) -> list[str]:
    """Split a message holding several ideas into one block per idea."""
    parts = [p.strip() for p in SEP_RE.split(text or "") if p and p.strip()]
    out: list[str] = []
    for p in parts:                                  # "Ticker:" repeated without separators
        idx = [m.start() for m in re.finditer(r"(?im)^\s*[*_]*\s*(?:ticker|symbol)\s*[*_]*\s*:", p)]
        if len(idx) <= 1:
            out.append(p)
            continue
        # keep any company description that precedes each Ticker: line with that block
        cuts = [0]
        for i in idx[1:]:
            prev_blank = p.rfind("\n\n", 0, i)
            cuts.append(prev_blank if prev_blank > cuts[-1] else i)
        cuts.append(len(p))
        out += [p[a:b].strip() for a, b in zip(cuts, cuts[1:]) if p[a:b].strip()]
    return out or [text or ""]


def _canon(label: str) -> str | None:
    lab = re.sub(r"\s+", " ", label.lower().replace("’", "'")).strip(" .")
    for name, rx in LABELS:
        if re.search(rx, lab):
            return name
    return None


def money_values(s: str) -> list[float]:
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"\b\d+(?:\.\d+)?\s*%", " ", s)                      # percentages are never levels
    s = re.sub(r"\b\d+(?:\.\d+)?\s*[rR]\b", " ", s)                  # "1.6R"
    s = re.sub(r"\b\d+(?:\.\d+)?\s*/\s*10\b", " ", s)                # scores
    vals = []
    for m in re.finditer(MONEY, s):
        v = float(m.group(1).replace(",", ""))
        if m.group(2):
            v *= 1000
        vals.append(v)
    return vals


def parse_date(s: str, today: date | None = None) -> str | None:
    """'November 12', 'Oct 22, 2026', '11/12', '2026-11-12' -> ISO date (next occurrence if no year)."""
    today = today or date.today()
    s = s.strip().replace(",", " ")
    s = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", s)
    s = re.sub(r"\s+", " ", s)
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    fmts = [("%B %d %Y", True), ("%b %d %Y", True), ("%B %d", False), ("%b %d", False), ("%m/%d/%Y", True),
            ("%m/%d/%y", True), ("%m/%d", False)]
    tokens = s.split(" ")
    for n in (3, 2, 1):
        for i in range(0, max(1, len(tokens) - n + 1)):
            chunk = " ".join(tokens[i:i + n]).strip(".")
            for fmt, has_year in fmts:
                try:
                    d = datetime.strptime(chunk, fmt).date()
                except ValueError:
                    continue
                if not has_year:
                    d = d.replace(year=today.year)
                    if d < today:
                        d = d.replace(year=today.year + 1)
                return d.isoformat()
    return None


def _score(s: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*10\b", s) or re.search(r"(\d+(?:\.\d+)?)\s*%", s)
    if not m:
        return None
    v = float(m.group(1))
    return v if "/" in m.group(0) else v / 10


def sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+(?=[A-Z$0-9])", re.sub(r"\s+", " ", text)) if x.strip()]


RISK_WORDS = re.compile(r"\b(risks?|worry|worries|concern|weak|expensive|cyclical|slowdown|downside|however|"
                        r"headwind|rejected|stalled|issue|threat|pressure|cutting)\b|"
                        r"\b(?:revenue|sales|earnings|margins?|growth|guidance)\b[^.]{0,40}\b(?:fell|dropped|declin\w*|miss\w*)", re.I)


def parse_structured(block: str) -> dict | None:
    """Return the labeled fields of one block, or None if it isn't a labeled signal."""
    lines = [ln for ln in (block or "").splitlines()]
    fields: dict[str, str] = {}
    prose: list[str] = []
    header = None
    for ln in lines:
        if not ln.strip():
            continue
        if header is None and not fields and HEADER_RE.match(ln.strip()):
            m = HEADER_RE.match(ln.strip())
            header = (m.group(1), m.group(2).strip(" ,."))
            continue
        m = LABEL_RE.match(ln)
        name = _canon(m.group(1)) if m else None
        if name and name not in fields:
            fields[name] = m.group(2).strip()
        elif name:                                   # repeated label (e.g. "Target:" twice) -> append
            fields[name] += " / " + m.group(2).strip()
        else:
            prose.append(ln.strip())
    if "entry" not in fields or not ({"stop", "target"} & set(fields)):
        return None
    out: dict = {"fields": fields, "header": header}
    ev = money_values(fields["entry"])
    out["entry"] = ev
    out["stop"] = money_values(fields.get("stop", ""))
    tv = money_values(fields.get("target", ""))
    out["targets"] = tv
    tk = fields.get("ticker")
    sym = None
    if tk:
        m = re.search(r"\$?([A-Za-z]{1,5}(?:[.\-][A-Za-z]{1,2})?)\b", tk)
        sym = m.group(1).upper() if m else None
    if not sym and header:
        sym = header[0]
    out["symbol"] = sym
    out["company"] = header[1] if header else None
    lab_entry = fields["entry"].lower()
    d = (fields.get("direction") or "").lower()
    if re.search(r"short|sell|bear", d) or re.search(r"short|sell", lab_entry):
        out["direction"] = "short"
    elif re.search(r"long|buy|bull", d):
        out["direction"] = "long"
    else:
        out["direction"] = None
    meta: dict = {}
    if "setup" in fields:
        meta["setup"] = fields["setup"]
    if "rr" in fields:
        m = re.search(r"(\d+(?:\.\d+)?)", fields["rr"])
        meta["rr_stated"] = float(m.group(1)) if m else None
    for k in ("fund_score", "fin_target", "confidence"):
        if k in fields:
            meta[k] = _score(fields[k])
    for k in ("reason", "catalyst", "risk_note"):
        if k in fields:
            meta[k] = fields[k]
    if "earnings" in fields:
        meta["next_earnings"] = parse_date(fields["earnings"])
        meta["next_earnings_text"] = fields["earnings"]
    if "timeframe" in fields:
        meta["timeframe_text"] = fields["timeframe"]
    text = " ".join(prose)
    sents = sentences(text)
    if sents:
        meta["summary"] = " ".join(sents[:2])[:400]
        meta["source_risks"] = [s[:220] for s in sents if RISK_WORDS.search(s)][:4]
        meta["thesis"] = text[:3000]
    if out["company"]:
        meta["company"] = out["company"]
    out["meta"] = meta
    return out
