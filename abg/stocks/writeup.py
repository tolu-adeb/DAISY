"""The analyst write-up (docs/21.6, behind ABG_PITCH_AI_MEMO): Claude explains, the pack decides.

The model receives a slimmed JSON pack and returns prose for the template's fields. It may only quote
numbers that are already in the pack. ``validate`` enforces that mechanically: every number, ticker
and date in the reply is checked against the pack, hype words are refused, and any field that fails
falls back to the template's own text - with the reason logged, so a rejection is visible, not silent.

Nothing the model writes can change the rating, the entry, the stop, the targets or the size: those
fields are never read back from the reply.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

SYSTEM = (
    "You are an equity analyst at the University of Michigan AI Business Group's AI Investment Fund, writing "
    "a stock pitch for the investment committee. You receive a JSON data pack. The rating, entry, stop, targets "
    "and size are already decided by tested rules. Explain them; never change or second-guess them. Use only "
    "facts and numbers that appear in the pack. Don't calculate new numbers. Use no outside facts, prices, "
    "estimates, deals or news beyond the company description and the headlines given. If something isn't in "
    "the data, write \"not in data\". Write for a smart analyst who is not a quant: plain words, with jargon "
    "defined in passing (ATR, R, RSI, momentum). No hype words (\"skyrocket\", \"guaranteed\", \"can't lose\"). "
    "Don't add disclaimers; the template adds them.\n"
    "Return only JSON:\n"
    '{"what_they_do": "2 sentences from the description field",\n'
    ' "why_now": "the signal and catalysts in the pack, <=3 bullets as a JSON list of strings",\n'
    ' "moat": "only what the pack\'s data supports (margins, market share, backlog if present), else \'not in data\'",\n'
    ' "core_thesis": "<=2 sentences",\n'
    ' "bull_case": "tie it to the bull target", "bear_case": "tie it to the bear target and the stop",\n'
    ' "valuation_view": "whether valuation agrees with the trade and what that means for size",\n'
    ' "risks": ["<=3, each with its mitigant"],\n'
    ' "why_it_fits": "from the fund-fit numbers",\n'
    ' "questions": ["2-3, each naming the number behind it"]}'
)
FIELDS = ("what_they_do", "why_now", "moat", "core_thesis", "bull_case", "bear_case", "valuation_view",
          "risks", "why_it_fits", "questions")
LIST_FIELDS = {"why_now": 3, "risks": 3, "questions": 3}
HYPE = re.compile(r"\b(skyrocket\w*|guarantee\w*|can'?t lose|sure thing|no[- ]brainer|moon\w*|explod\w*|"
                  r"risk[- ]free|to the moon|massive upside|once[- ]in[- ]a[- ]lifetime)\b", re.I)
# words in capitals that are vocabulary, not tickers
ACRONYMS = {"ATR", "RSI", "EMA", "SMA", "MACD", "R", "EPS", "DCF", "AI", "ETF", "ETFS", "US", "USA", "CEO", "CFO",
            "YOY", "TTM", "NTM", "LTM", "PE", "P", "E", "BUY", "ADD", "HOLD", "TRIM", "SELL", "WATCH", "AVOID",
            "ADR", "ADRS", "GAAP", "FCF", "EBITDA", "NYSE", "NASDAQ", "S", "Q", "Q1", "Q2", "Q3", "Q4", "FY",
            "T1", "T2", "ML", "ET", "OK", "A", "I", "II", "III", "IV", "NOT", "IN", "DATA", "AIBG", "UMICH", "IPO",
            "FDA", "SEC", "GDP", "CPI", "FOMC", "VIX", "HIGH", "LOW", "MEDIUM", "NA", "N"}
_NUM = re.compile(r"(?<![A-Za-z])([-+−]?\$?\d[\d,]*(?:\.\d+)?)\s*(%|x|×|[BMT]\b|bn\b|R\b|billion|million|trillion)?",
                  re.I)
_ISO = re.compile(r"\b(19|20)\d\d-\d\d-\d\d\b")
_YEAR = re.compile(r"\b(19[5-9]\d|20\d\d)\b")
_CAPS = re.compile(r"\b[A-Z][A-Z0-9.\-]{0,5}\b")


# --------------------------------------------------------------------------- the pack the model sees
def slim(pack: dict) -> dict:
    """Only what the write-up needs - no ids, no provider internals, no secrets."""
    pl, rec, ov = pack.get("plan") or {}, pack.get("rec") or {}, pack.get("overview") or {}
    inv, tgt, risk = pl.get("investment") or {}, pl.get("targets") or {}, pl.get("risk") or {}
    return {
        "symbol": pack.get("symbol"), "name": pack.get("name"), "sector": pack.get("sector"),
        "as_of": pack.get("as_of"), "price": pack.get("price"),
        "rating": rec.get("rating"), "action": rec.get("action_line"), "conviction": rec.get("conviction"),
        "method": rec.get("method"), "core_thesis_template": rec.get("core_thesis"),
        "signal": pack.get("what_happened"), "gates": pack.get("gates"),
        "entry": pl.get("entry"), "stop": pack.get("stop"),
        "trade_targets": {k: tgt.get(k) for k in ("t1", "t2", "t1_basis", "t2_basis", "review_date", "notes")},
        "twelve_month": {k: inv.get(k) for k in ("cases", "prob_weighted", "prob_upside", "total_return",
                                                 "dividend_yield", "street_target", "implied_growth", "growth",
                                                 "horizon", "lines")},
        "risk": {k: risk.get(k) for k in ("rr", "stop_pct", "pct_of_fund", "risk_dollars", "shares", "bound")},
        "views_disagree": rec.get("conflict"), "wrong_if": pl.get("wrong_if"),
        "fund_fit": pl.get("fit"), "base_rates": pl.get("base_rates"),
        "overview": {k: ov.get(k) for k in ("market_cap", "pe_ntm", "pe_ltm", "description", "metrics", "industry")},
        "headlines": [n.get("title") for n in pack.get("news") or []],
        "regime": (pack.get("context") or {}).get("regime"),
        "flags_on": (pack.get("context") or {}).get("flags_on"),
        "template_risks": pack.get("risks"), "template_questions": pl.get("questions"),
    }


# --------------------------------------------------------------------------- what the pack allows
def _walk(x, nums: list, strs: list) -> None:
    if isinstance(x, dict):
        for k, v in x.items():
            strs.append(str(k))
            _walk(v, nums, strs)
    elif isinstance(x, (list, tuple)):
        for v in x:
            _walk(v, nums, strs)
    elif isinstance(x, bool) or x is None:
        return
    elif isinstance(x, (int, float)):
        nums.append(float(x))
    else:
        s = str(x)
        strs.append(s)
        for m in _NUM.finditer(s):
            v = _to_float(m.group(1))
            if v is not None:
                nums.append(v)


def allowed(pack: dict) -> dict:
    nums, strs = [], []
    _walk(pack, nums, strs)          # the whole pack: it never holds a secret, and the pitch desk's has another shape
    text = " ".join(strs)
    return {"numbers": sorted({round(abs(v), 6) for v in nums}), "text": text,
            "caps": set(_CAPS.findall(text)) | ACRONYMS | {pack.get("symbol") or ""},
            "years": set(_YEAR.findall(text))}


def _to_float(s: str):
    try:
        return float(s.replace("$", "").replace(",", "").replace("−", "-").replace("+", ""))
    except ValueError:
        return None


SCALE = {"": (1.0, 100.0), "x": (1.0,), "×": (1.0,), "r": (1.0,), "%": (100.0, 1.0),
         "b": (1e-9,), "bn": (1e-9,), "billion": (1e-9,), "m": (1e-6,), "million": (1e-6,),
         "t": (1e-12,), "trillion": (1e-12,)}


def _close(a: float, p: float) -> bool:
    if abs(a - p) <= max(0.011, 0.006 * p):
        return True
    if a.is_integer() and p >= 10 and abs(a - p) <= 0.5:          # "$427" for 427.21
        return True
    return p >= 1 and abs(a - round(p, 1)) <= 0.051              # one decimal place


def _num_ok(v: float, unit: str, allowed_vals: list[float]) -> bool:
    """Is ``v`` (in ``unit``) a number the pack contains? A unit is compared only in its own scale:
    "$48.2B" must match something near 48.2e9, not any 48.2 elsewhere in the pack."""
    a = abs(v)
    if not unit and a in (0.0, 1.0, 2.0, 3.0):
        return True          # "two flags", "1 share", "3 bullets": counting words, harmless
    for k in SCALE.get(unit, (1.0,)):
        for p in allowed_vals:
            if _close(a, p * k):
                return True
    return False


def check_text(text: str, al: dict) -> list[str]:
    """Problems with one piece of model text, or [] if it only uses what the pack contains."""
    problems = []
    if HYPE.search(text):
        problems.append(f"hype word: {HYPE.search(text).group(0)!r}")
    for d in _ISO.finditer(text):
        if d.group(0) not in al["text"]:
            problems.append(f"date not in pack: {d.group(0)}")
    for y in _YEAR.findall(text):
        if y not in al["years"] and y not in al["text"]:
            problems.append(f"year not in pack: {y}")
    stripped = _ISO.sub(" ", text)
    for m in _NUM.finditer(stripped):
        raw = m.group(1)
        if _YEAR.fullmatch(raw.strip("$+-−")):
            continue
        v = _to_float(raw)
        if v is None:
            continue
        unit = (m.group(2) or "").lower()
        if not _num_ok(v, unit, al["numbers"]):
            problems.append(f"number not in pack: {m.group(0).strip()}")
    for w in _CAPS.findall(text):
        w2 = w.strip(".-")
        if len(w2) >= 2 and w2 not in al["caps"] and w2.upper() == w2 and not w2.isdigit():
            if not re.search(rf"\b{re.escape(w2)}\b", al["text"]):
                problems.append(f"ticker or name not in pack: {w2}")
    return problems


def validate(reply: dict, pack: dict) -> tuple[dict, list[dict]]:
    """(accepted fields, rejections). A field is kept only if every part of it passes."""
    al = allowed(pack)
    ok, rejected = {}, []
    for f in FIELDS:
        v = reply.get(f)
        if v in (None, "", []):
            continue
        if f in LIST_FIELDS:
            if isinstance(v, str):
                v = [x.strip("-• ").strip() for x in v.split("\n") if x.strip()]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                rejected.append({"field": f, "why": "not a list of strings"})
                continue
            v = [x.strip()[:400] for x in v if x.strip()][:LIST_FIELDS[f]]
            probs = [p for x in v for p in check_text(x, al)]
        else:
            if not isinstance(v, str):
                rejected.append({"field": f, "why": "not a string"})
                continue
            v = v.strip()[:900]
            probs = check_text(v, al)
        if probs:
            rejected.append({"field": f, "why": "; ".join(sorted(set(probs)))[:300]})
        else:
            ok[f] = v
    return ok, rejected


def parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in the reply")
    d = json.loads(m.group(0))
    if not isinstance(d, dict):
        raise ValueError("reply JSON is not an object")
    return d


# --------------------------------------------------------------------------- calling the model
async def generate(settings, http, pack: dict) -> dict:
    """Ask Claude for the write-up and keep only what validates. Never raises: on any failure the
    pack keeps the template text and the reason is recorded."""
    out = {"fields": {}, "rejected": [], "model": None, "error": None}
    key = getattr(settings, "anthropic_api_key", None)
    if not key:
        out["error"] = "no ABG_ANTHROPIC_API_KEY"
        return out
    model = getattr(settings, "anthropic_model", None) or "claude-sonnet-4-5"
    out["model"] = model
    try:
        resp = await http.request(
            "POST", (getattr(settings, "anthropic_base_url", None) or "https://api.anthropic.com").rstrip("/") + "/v1/messages",
            provider="anthropic", timeout=120,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 2000, "system": SYSTEM,
                  "messages": [{"role": "user", "content": json.dumps(slim(pack), default=str)[:30000]}]})
        body = resp.json()
        text = "".join(b.get("text", "") for b in (body.get("content") or []) if b.get("type") == "text")
        reply = parse(text)
    except Exception as e:                          # never echo the request: it carries the key
        out["error"] = f"{type(e).__name__}"
        log.warning("write-up for %s failed (%s); template text kept", pack.get("symbol"), type(e).__name__)
        return out
    out["fields"], out["rejected"] = validate(reply, pack)
    for r in out["rejected"]:
        log.info("write-up %s.%s rejected: %s", pack.get("symbol"), r["field"], r["why"])
    return out


def apply(pack: dict, result: dict) -> dict:
    """Put the accepted fields where render_md looks for them; record what was refused."""
    pack["writeup"] = dict(result.get("fields") or {}) or None
    pack["writeup_log"] = {"model": result.get("model"), "error": result.get("error"),
                           "rejected": result.get("rejected") or []}
    return pack
