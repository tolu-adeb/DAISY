"""News and sentiment for the stock desk (docs/22.3). Scoring is code, not an LLM: every score is a number
computed here from stored articles, and every article keeps its source link and time.

    score_text / relevance     one article: sentiment in [-1, 1] from a finance word list, and how much it
                               is about the name
    name_sentiment             one name: level, change against its 20-day baseline, news volume vs normal
    market_sentiment           the market: breadth, 52-week highs vs lows, VIX and its term structure,
                               sector ETFs, the day's market headlines -> risk-on / neutral / risk-off
    theme_shocks               an event that moves a whole theme (the NVIDIA and Anthropic-IPO lesson):
                               which listed names it touches and how each moved
    risk_flags                 what a pitch must say out loud: a bad headline, a sentiment collapse, a filing

News never changes which trades fire. It finds names to research, raises flags, and feeds two documented
conviction terms. A sentiment *gate* stays labelled untested until the R&D study shows it helps.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

POS = set("""beat beats beating surge surges surged soar soars soared record upgrade upgrades upgraded raise raises
raised strong stronger growth profit profits win wins won approval approved approves partnership buyback
outperform rally rallies rallied jump jumps jumped gain gains gained tops topped boost boosts boosted expands
expansion bullish breakthrough accelerate accelerates accelerating exceeds exceeded hikes hiked rebound rebounds
upbeat robust climbs climbed rises rose milestone""".split())
NEG = set("""miss misses missed plunge plunges plunged fall falls fell drop drops dropped cut cuts downgrade
downgrades downgraded lawsuit probe investigation fraud recall bankruptcy loss losses weak weaker warning warns
warned layoffs delay delays delayed decline declines declined slump slumps tumbles tumbled sinks sank halt halted
concern concerns bearish selloff dilution resigns fined breach default slashes slashed lowers lowered disappoints
disappointing subpoena shortfall slides slid crash crashes crashed underperform recession downturn""".split())
NEGATORS = {"not", "no", "never", "without", "fails", "failed"}
FILING_FORMS = {"8-K": "material event (8-K)", "S-1": "IPO registration (S-1)", "F-1": "IPO registration (F-1)",
                "S-1/A": "IPO registration, amended (S-1/A)", "F-1/A": "IPO registration, amended (F-1/A)",
                "424B4": "IPO priced (424B4)", "SC 13D": "activist stake (13D)", "SC 13D/A": "activist stake, amended (13D/A)",
                "SC 13G": "large passive stake (13G)", "4": "insider trade (Form 4)"}
DEFAULTS = {
    "min_articles": 3,            # fewer articles than this = no sentiment grade and no conviction term
    "positive": 0.25, "negative": -0.25,     # name sentiment at/over these counts for conviction
    "material": 0.5,              # |score| at/over this on a relevant article = a material headline
    "collapse": -0.40,            # level minus the 20-day baseline at/under this = a sentiment collapse
    "volume_spike": 3.0,          # today's article count over the 20-day daily average
    "shock": {"move_pct": 0.03, "min_names": 3, "median_pct": 0.015, "lead_pct": 0.07, "follow_pct": 0.015,
              "min_articles": 2, "corr_min": 0.75, "corr_max_add": 8},
}


def news_cfg(cfg: dict | None) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in ((cfg or {}).get("news") or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- one article
def score_text(text: str) -> float:
    """Sentiment of a headline (plus summary) in [-1, 1]: (positive - negative) / (hits + 1). A negator
    just before a word flips it ("no growth", "not weak")."""
    toks = re.findall(r"[a-z][a-z'-]*", (text or "").lower())
    pos = neg = 0
    for i, t in enumerate(toks):
        s = 1 if t in POS else -1 if t in NEG else 0
        if not s:
            continue
        if any(x in NEGATORS for x in toks[max(0, i - 2):i]):
            s = -s
        pos, neg = pos + (s > 0), neg + (s < 0)
    return round((pos - neg) / (pos + neg + 1), 4) if pos + neg else 0.0


def relevance(headline: str, summary: str, symbol: str, name: str | None = None) -> float:
    """1.0 the headline names the ticker or the company; 0.6 only the summary does; 0.3 tagged by the feed."""
    keys = [rf"\b{re.escape(symbol)}\b"]
    first = re.sub(r"[^A-Za-z0-9 ]", "", name or "").split()
    if first and len(first[0]) >= 4:
        keys.append(rf"\b{re.escape(first[0])}\b")
    pat = re.compile("|".join(keys), re.I)
    return 1.0 if pat.search(headline or "") else 0.6 if pat.search(summary or "") else 0.3


def article(symbol: str | None, headline: str, summary: str = "", url: str | None = None, at: datetime | str | None = None,
            source: str | None = None, kind: str = "news", name: str | None = None, extra: dict | None = None) -> dict:
    """One stored article: deduplicated by its link (or its title and day), scored, timestamped."""
    if isinstance(at, datetime):
        at = at.astimezone(NY).isoformat(timespec="seconds")
    at = at or datetime.now(NY).isoformat(timespec="seconds")
    headline = (headline or "").strip()[:240]
    key = f"{symbol or '*'}|{url or headline + at[:10]}"
    return {"id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:20], "symbol": symbol, "at": at,
            "source": source, "kind": kind, "title": headline, "url": url,
            "sent": score_text(f"{headline}. {summary or ''}"),
            "rel": relevance(headline, summary, symbol, name) if symbol else 1.0,
            "data": {"summary": (summary or "")[:400], **(extra or {})}}


# --------------------------------------------------------------------------- storage
def store(db, arts: list[dict]) -> int:
    """Insert new articles; an id already stored is skipped. Returns how many were new."""
    have = {r["id"] for r in db.q(f"SELECT id FROM news WHERE id IN ({','.join('?' * len(arts))})",
                                  tuple(a["id"] for a in arts))} if arts else set()
    new = [a for a in arts if a["id"] not in have]
    seen, rows = set(), []
    for a in new:
        if a["id"] in seen:
            continue
        seen.add(a["id"])
        rows.append((a["id"], a["symbol"], a["at"], a["source"], a["kind"], a["title"], a["url"], a["sent"],
                     a["rel"], json.dumps(a.get("data") or {}, default=str)))
    db.many("INSERT OR IGNORE INTO news(id, symbol, at, source, kind, title, url, sent, rel, data) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()[:120]


def dedupe(arts: list[dict], have_titles: set | None = None) -> list[dict]:
    """Drop a story already seen under another link: the same headline for the same name is one story,
    whichever source or syndication carried it. Filings are kept as they are."""
    seen, out = set(have_titles or ()), []
    for a in arts:
        if a.get("kind") != "news":
            out.append(a)
            continue
        k = (a.get("symbol"), title_key(a["title"]))
        if k in seen:
            continue
        seen.add(k)
        out.append(a)
    return out


def recent(db, symbol: str | None = None, since: str | None = None, limit: int = 50, kind: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM news WHERE 1=1", []
    if symbol is not None:
        sql, args = sql + " AND symbol=?", args + [symbol]
    if since:
        sql, args = sql + " AND at>=?", args + [since]
    if kind:
        sql, args = sql + " AND kind=?", args + [kind]
    out = []
    for r in db.q(sql + " ORDER BY at DESC LIMIT ?", tuple(args + [limit])):
        d = dict(r)
        try:
            d["data"] = json.loads(d["data"] or "{}")
        except ValueError:
            d["data"] = {}
        out.append(d)
    return out


def roll_daily(db, symbols: list[str] | None = None, days: int = 30) -> int:
    """Recompute the daily score per name (relevance-weighted mean of that day's articles) from the stored
    articles, and the market's own row ('*') from the general market headlines."""
    since = (datetime.now(NY) - timedelta(days=days)).date().isoformat()
    sql, args = "SELECT symbol, substr(at, 1, 10) day, sent, rel FROM news WHERE at>=? AND kind='news'", [since]
    if symbols:
        sql += f" AND symbol IN ({','.join('?' * len(symbols))})"
        args += list(symbols)
    acc: dict = {}
    for r in db.q(sql, tuple(args)):
        k = (r["symbol"] or "*", r["day"])
        w = float(r["rel"] or 0.3)
        a = acc.setdefault(k, [0.0, 0.0, 0])
        a[0] += w * float(r["sent"] or 0)
        a[1] += w
        a[2] += 1
    rows = [(s, d, round(v[0] / v[1], 4) if v[1] else 0.0, v[2]) for (s, d), v in acc.items()]
    db.many("INSERT INTO sentiment(symbol, day, score, n) VALUES(?,?,?,?) "
            "ON CONFLICT(symbol, day) DO UPDATE SET score=excluded.score, n=excluded.n", rows)
    return len(rows)


def series(db, symbol: str, days: int = 30) -> list[dict]:
    since = (datetime.now(NY) - timedelta(days=days)).date().isoformat()
    return [dict(r) for r in db.q("SELECT day, score, n FROM sentiment WHERE symbol=? AND day>=? ORDER BY day",
                                  (symbol, since))]


# --------------------------------------------------------------------------- one name, the market
def name_sentiment(rows: list[dict], today: str | date) -> dict:
    """``rows``: the name's daily scores (day, score, n). Level = the last 3 days, weighted by article
    count; baseline = the 20 days before that; volume = today's count over the baseline's daily average."""
    today = str(today)
    d0 = date.fromisoformat(today)
    rs = [r for r in rows if r["day"] <= today]
    near = [r for r in rs if r["day"] > (d0 - timedelta(days=3)).isoformat()]
    base = [r for r in rs if (d0 - timedelta(days=23)).isoformat() < r["day"] <= (d0 - timedelta(days=3)).isoformat()]

    def wmean(xs):
        n = sum(x["n"] for x in xs)
        return round(sum(x["score"] * x["n"] for x in xs) / n, 4) if n else None
    level, baseline = wmean(near), wmean(base)
    n_today = sum(r["n"] for r in rs if r["day"] == today)
    avg = sum(r["n"] for r in base) / 20.0 if base else None
    return {"level": level, "baseline": baseline, "n": sum(r["n"] for r in near), "n_today": n_today,
            "change": round(level - baseline, 4) if level is not None and baseline is not None else None,
            "n_avg": round(avg, 2) if avg is not None else None,
            "volume_ratio": round(n_today / avg, 2) if avg else None, "as_of": today}


def market_sentiment(breadth: dict | None, vix: float | None = None, vix3m: float | None = None,
                     sectors_up: float | None = None, news_score: float | None = None,
                     news_n: int | None = None) -> dict:
    """Risk-on, neutral or risk-off, as a count of plain signals. Each one is a number with a threshold;
    the result lists every signal that fired, so the sentence on the dashboard can be checked."""
    b = breadth or {}
    pts, why, used = 0, [], 0

    def sig(val, hi, lo, name, fmt, hi_good=True):
        nonlocal pts, used
        if val is None:
            return
        used += 1
        if val >= hi:
            pts += 1 if hi_good else -1
            why.append(f"{name} {fmt(val)} ({'supportive' if hi_good else 'a warning'})")
        elif val <= lo:
            pts += -1 if hi_good else 1
            why.append(f"{name} {fmt(val)} ({'a warning' if hi_good else 'supportive'})")
    pct = lambda v: f"{v:.0%}"                                  # noqa: E731
    sig(b.get("above_200"), 0.60, 0.40, "stocks above their 200-day average:", pct)
    sig(b.get("above_50"), 0.60, 0.40, "stocks above their 50-day average:", pct)
    hi, lo = b.get("new_highs"), b.get("new_lows")
    if hi is not None and lo is not None and hi + lo >= 10:
        used += 1
        if hi > 2 * lo:
            pts, _ = pts + 1, why.append(f"{hi} new 52-week highs against {lo} lows (supportive)")
        elif lo > 2 * hi:
            pts, _ = pts - 1, why.append(f"{lo} new 52-week lows against {hi} highs (a warning)")
    sig(vix, 24.0, 16.0, "VIX", lambda v: f"{v:.1f}", hi_good=False)
    if vix and vix3m:
        sig(vix / vix3m, 1.0, 0.90, "VIX over 3-month VIX", lambda v: f"{v:.2f}", hi_good=False)
    sig(sectors_up, 0.70, 0.30, "sector ETFs above their 50-day average:", pct)
    if news_score is not None and (news_n or 0) >= 5:
        sig(news_score, 0.20, -0.20, "market headlines score", lambda v: f"{v:+.2f}")
    score = round(pts / used, 3) if used else None
    label = "not in data" if score is None else "risk-on" if score >= 0.34 else "risk-off" if score <= -0.34 else "neutral"
    return {"label": label, "score": score, "signals": used, "why": why,
            "inputs": {"breadth": b, "vix": vix, "vix3m": vix3m, "sectors_up": sectors_up,
                       "news_score": news_score, "news_n": news_n}}


def mood_sentence(m: dict | None) -> str:
    if not m or m.get("score") is None:
        return "Market mood: not in data."
    lead = {"risk-on": "Market mood is risk-on", "risk-off": "Market mood is risk-off",
            "neutral": "Market mood is neutral"}[m["label"]]
    why = "; ".join(m["why"][:3])
    return f"{lead}: {why}." if why else f"{lead}: no signal is at an extreme."


# --------------------------------------------------------------------------- themes and shocks
def load_themes(path: str | Path | None = None, ai_seed: list[str] | None = None) -> dict:
    """``configs/themes.yaml`` -> ``{key: {label, symbols, keywords}}``. Without the file, one theme is
    built from the fund's AI-infrastructure list so the desk still has something to watch."""
    data = None
    try:
        import yaml
        p = Path(path) if path else Path(__file__).resolve().parents[2] / "configs" / "themes.yaml"
        data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None
    except Exception:
        data = None
    out = {}
    for k, v in ((data or {}).get("themes") or {}).items():
        syms = [str(s).upper().strip() for s in (v or {}).get("symbols") or [] if s]
        if syms:
            out[str(k)] = {"label": v.get("label") or str(k).replace("_", " "), "symbols": syms,
                           "keywords": [str(w).lower() for w in v.get("keywords") or []]}
    if not out and ai_seed:
        out["ai_infrastructure"] = {"label": "AI infrastructure", "symbols": list(ai_seed), "keywords": []}
    return out


def themes_of(symbol: str, themes: dict) -> list[str]:
    return [k for k, t in themes.items() if symbol in t["symbols"] or symbol in (t.get("correlated") or [])]


def extend_by_correlation(themes: dict, closes, eligible: set, min_corr: float = 0.75, max_add: int = 8,
                          window: int = 60) -> dict:
    """Add the names that move with a theme: 60-day return correlation to the theme's equal-weight basket
    at/over ``min_corr``. They are kept apart (``correlated``) from the hand-written list."""
    rets = closes.pct_change().iloc[-window:]
    rets = rets.loc[:, rets.notna().sum() >= int(window * 0.8)]
    for t in themes.values():
        members = [s for s in t["symbols"] if s in rets.columns]
        t["correlated"] = []
        if len(members) < 3:
            continue
        basket = rets[members].mean(axis=1)
        cand = [c for c in rets.columns if c in eligible and c not in t["symbols"]]
        if not cand:
            continue
        corr = rets[cand].corrwith(basket).dropna().sort_values(ascending=False)
        t["correlated"] = [str(s) for s in corr[corr >= min_corr].index[:max_add]]
    return themes


def theme_shocks(themes: dict, moves: dict, arts: list[dict], cfg: dict | None, day: str) -> list[dict]:
    """Events that hit a whole theme today.

    ``moves``: ``{symbol: {"chg": day's move, "sent_change": optional}}``. ``arts``: today's articles.
    A *move* shock: at least ``min_names`` members moved ``move_pct`` the same way and the theme's median
    move agrees; or one member moved ``lead_pct`` and two others followed. A *news* shock: at least
    ``min_articles`` of today's headlines match one of the theme's keywords (a private company's name,
    "IPO", a product) - which is how a company with no ticker still reaches the desk.
    """
    sc = news_cfg(cfg)["shock"]
    out = []
    for key, t in themes.items():
        members = list(dict.fromkeys(t["symbols"] + (t.get("correlated") or [])))
        mv = {s: moves[s] for s in members if s in moves and moves[s].get("chg") is not None}
        hits = [a for a in arts if t.get("keywords") and
                any(re.search(rf"\b{re.escape(w)}\b", (a.get("title") or "").lower()) for w in t["keywords"])]
        hits = list({a["title"]: a for a in hits}.values())
        kind = direction = lead = None
        if mv:
            chgs = [m["chg"] for m in mv.values()]
            med = median(chgs)
            lead_sym = max(mv, key=lambda s: abs(mv[s]["chg"]))
            lead = {"symbol": lead_sym, "chg": round(mv[lead_sym]["chg"], 4)}
            sign = 1 if med >= 0 else -1
            same = [s for s, m in mv.items() if m["chg"] * sign >= sc["move_pct"]]
            if len(same) >= sc["min_names"] and abs(med) >= sc["median_pct"]:
                kind, direction = "move", "up" if sign > 0 else "down"
            else:
                ls = 1 if lead["chg"] >= 0 else -1
                follow = [s for s, m in mv.items() if s != lead_sym and m["chg"] * ls >= sc["follow_pct"]]
                if abs(lead["chg"]) >= sc["lead_pct"] and len(follow) >= 2:
                    kind, direction = "move", "up" if ls > 0 else "down"
        if kind is None and len(hits) >= sc["min_articles"]:
            kind = "news"
            avg = sum(a.get("sent") or 0 for a in hits) / len(hits)
            direction = "up" if avg > 0.1 else "down" if avg < -0.1 else "mixed"
        if kind is None:
            continue
        src = sorted(hits, key=lambda a: a["at"], reverse=True)[0] if hits else None
        if src is None and lead:                                 # the lead name's own strongest headline
            own = [a for a in arts if a.get("symbol") == lead["symbol"]]
            src = max(own, key=lambda a: (a.get("rel") or 0, abs(a.get("sent") or 0)), default=None)
        names = sorted(({"symbol": s, "chg": round(m["chg"], 4), "sent_change": m.get("sent_change"),
                         "by": "list" if s in t["symbols"] else "correlation"} for s, m in mv.items()),
                       key=lambda x: -abs(x["chg"]))[:12]
        out.append({"id": f"{day}-{key}-{kind}", "day": day, "theme": key, "label": t["label"], "kind": kind,
                    "direction": direction, "lead": lead, "n_articles": len(hits),
                    "source": {"title": src["title"], "url": src.get("url"), "at": src["at"],
                               "from": src.get("source")} if src else None,
                    "names": names,
                    "median": round(median([m["chg"] for m in mv.values()]), 4) if mv else None})
    return sorted(out, key=lambda s: -abs(s["median"] or 0))


def shock_sentence(s: dict) -> str:
    arrow = {"up": "rallied", "down": "sold off", "mixed": "moved"}.get(s["direction"], "moved")
    head = f"{s['label']} {arrow}"
    if s.get("median") is not None:
        head += f" (median {s['median']:+.1%} across {len(s['names'])} names"
        head += f", led by {s['lead']['symbol']} {s['lead']['chg']:+.1%})" if s.get("lead") else ")"
    if s["kind"] == "news":
        head = f"{s['label']}: {s['n_articles']} headlines today" + (f"; listed names {arrow}" if s.get("names") else "")
    return head + "."


# --------------------------------------------------------------------------- what a pitch must say
def risk_flags(symbol: str, arts: list[dict], sent: dict | None, cfg: dict | None = None,
               shocks: list[dict] | None = None, gap_atr: float | None = None) -> list[dict]:
    """Flags for a pitch, each with its source link where there is one."""
    from abg.swing.wide import RED_FLAG_WORDS
    nc = news_cfg(cfg)
    out = []
    for a in arts:
        if a.get("kind") == "filing":
            out.append({"kind": "filing", "text": f"{a['title']}", "url": a.get("url"), "at": a["at"]})
            continue
        if (a.get("rel") or 0) < 0.6:
            continue
        low = (a.get("title") or "").lower()
        red = [n for n, pat in RED_FLAG_WORDS.items() if re.search(pat, low)]
        if red or (a.get("sent") or 0) <= -nc["material"]:
            out.append({"kind": "headline", "text": (f"{', '.join(red)}: " if red else "negative headline: ") + a["title"],
                        "url": a.get("url"), "at": a["at"]})
    s = sent or {}
    if s.get("change") is not None and s["change"] <= nc["collapse"] and (s.get("n") or 0) >= nc["min_articles"]:
        out.append({"kind": "sentiment", "url": None, "at": s.get("as_of"),
                    "text": f"news sentiment fell {s['change']:+.2f} against its 20-day baseline ({s['n']} articles)"})
    if gap_atr is not None and abs(gap_atr) >= 1.0 and arts:
        out.append({"kind": "gap", "url": arts[0].get("url"), "at": arts[0]["at"],
                    "text": f"gapped {gap_atr:+.1f} ATR at the open with news out"})
    for sh in shocks or []:
        if any(n["symbol"] == symbol for n in sh.get("names") or []) and sh.get("direction") == "down":
            out.append({"kind": "theme", "text": f"theme shock: {shock_sentence(sh)}",
                        "url": (sh.get("source") or {}).get("url"), "at": sh["day"]})
    seen, uniq = set(), []
    for f in out:
        if f["text"] not in seen:
            seen.add(f["text"])
            uniq.append(f)
    return uniq[:6]


def material(a: dict, cfg: dict | None = None) -> bool:
    """A headline worth an alert on a watched name: clearly about it and clearly good or bad, or a filing."""
    nc = news_cfg(cfg)
    return a.get("kind") == "filing" or ((a.get("rel") or 0) >= 0.6 and abs(a.get("sent") or 0) >= nc["material"])


# --------------------------------------------------------------------------- the News tab (docs/23.2)
CATEGORIES = [("holdings", "Our holdings"), ("pitches", "Our pitches and watchlist"), ("markets", "Markets and macro"),
              ("earnings", "Earnings"), ("deals", "Mergers and deals"), ("ipos", "IPOs"), ("ai_tech", "AI and tech"),
              ("filings", "Filings (SEC)")]
_RX = {
    "earnings": r"\b(earnings|quarterly results|q[1-4] results|eps|revenue (beat|miss)|guidance|profit (rises|falls|jumps|drops)|reports (first|second|third|fourth)[- ]quarter)\b",
    "deals": r"\b(acquir\w+|merger|merge[sd]?|buyout|takeover|to buy|agrees to (buy|sell)|deal to|stake in|spin[- ]?off)\b",
    "ipos": r"\b(ipo|initial public offering|goes public|going public|public debut|files to go public|direct listing)\b",
    "ai_tech": r"\b(a\.?i\.?|artificial intelligence|chip\w*|semiconductor\w*|gpu\w*|data cent(er|re)s?|openai|anthropic|nvidia|cloud|llm|chatgpt|claude|gemini)\b",
    "markets": r"\b(fed|federal reserve|inflation|cpi|rate cut|rate hike|interest rates?|treasur\w+|yields?|jobs report|payrolls|gdp|recession|tariffs?|stocks (rise|fall|rally|slide|close)|wall street|s&p 500|nasdaq|dow)\b",
}


def categorize(a: dict, holdings: set | None = None, watched: set | None = None, ai: set | None = None) -> list[str]:
    """Which News-tab categories an article belongs to. Rules only, so it can be checked: the feed's own
    category, whose ticker it is, and keywords in the headline."""
    low = (a.get("title") or "").lower()
    sym = a.get("symbol")
    out = []
    if a.get("kind") == "filing":
        out.append("filings")
    if sym and sym in (holdings or ()):
        out.append("holdings")
    if sym and sym in (watched or ()):
        out.append("pitches")
    if re.search(_RX["earnings"], low):
        out.append("earnings")
    if ((a.get("data") or {}).get("category") == "merger") or re.search(_RX["deals"], low):
        out.append("deals")
    if re.search(_RX["ipos"], low):
        out.append("ipos")
    if (sym and sym in (ai or ())) or re.search(_RX["ai_tech"], low):
        out.append("ai_tech")
    if re.search(_RX["markets"], low) or (not sym and not out):
        out.append("markets")
    return out or ["markets"]


def brief(mood: dict | None, heat: dict | None, shocks: list[dict] | None, counts: dict | None = None) -> list[str]:
    """The market brief: three to five sentences, every one built from a stored number."""
    out = []
    m, h = mood or {}, heat or {}
    if m.get("score") is not None:
        out.append(mood_sentence(m))
    secs = [x for x in h.get("sectors") or [] if x.get("chg_1d") is not None]
    if secs:
        best, worst = max(secs, key=lambda x: x["chg_1d"]), min(secs, key=lambda x: x["chg_1d"])
        up = sum(1 for x in secs if x["chg_1d"] > 0)
        out.append(f"{up} of {len(secs)} sectors rose at the last close: {best['label']} led ({best['chg_1d']:+.1%}) and "
                   f"{worst['label']} lagged ({worst['chg_1d']:+.1%}).")
    b = h.get("breadth") or {}
    if b.get("new_highs") is not None and b.get("n"):
        out.append(f"Across {b['n']:,} eligible stocks there were {b['new_highs']} new 52-week highs and {b['new_lows']} new lows.")
    for s in (shocks or [])[:2]:
        out.append(shock_sentence(s))
    c = counts or {}
    if c.get("articles"):
        out.append(f"{c['articles']} headlines and filings were stored in the last day"
                   + (f", {c['holdings']} of them about names the fund holds." if c.get("holdings") else "."))
    return out[:5] or ["No market data yet: the brief is written after the first daily pass."]
