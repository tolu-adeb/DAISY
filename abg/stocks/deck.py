"""AIBG pitch deck (docs/21.6, Part 9.4): one stock pitch as the club's 5-slide PowerPoint.

    deck_content(pack)          every slide's text, built only from the pack (testable without PowerPoint)
    build_deck(pack, out, ...)  fill ``abg/assets/aibg_pitch_template.pptx`` and drop the charts on slide 03

The template is the Round 1 deck's design with all of its content removed. Each fillable shape is named
``tok:<key>`` (``tok:<key>[]`` for a bulleted list) and holds the text ``{{key}}``; ``leftovers()`` lists
any ``{{...}}`` still in a finished deck, and ``build_deck`` refuses to save one that has any.
python-pptx is an optional extra (``pip install .[deck]``).
"""
from __future__ import annotations

import copy
import math
import re
from datetime import date
from pathlib import Path

from . import pitch_plan as P

TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "aibg_pitch_template.pptx"
ANALYST = "DAISY (quant team)"
NA = P.NOT_IN_DATA
RATING_COLOR = {"BUY": "1A7F4B", "ADD": "1A7F4B", "SELL": "B42318", "TRIM": "B42318"}
NEUTRAL = "5A6A7A"
RATING_SUB = {"BUY": "Initiate position", "ADD": "Add to position", "SELL": "Exit position",
              "TRIM": "Trim position", "HOLD": "Hold", "WATCH": "Watch list", "AVOID": "Avoid"}
TOKEN = re.compile(r"\{\{[^}]*\}\}")


def available() -> bool:
    try:
        import pptx  # noqa: F401
        return True
    except Exception:
        return False


# ------------------------------------------------------------------------------------------- words
def _px(x) -> str:
    return "—" if x is None else f"${x:,.2f}"


def _pct(x, d=1) -> str:
    return "—" if x is None else f"{x:+.{d}%}"


def _usd_s(x) -> str:
    if x is None:
        return NA
    for n, s in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(x) >= n:
            return f"${x / n:,.2f} {s}"
    return f"${x:,.0f}"


def _cut(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def _sentences(text: str, n: int) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", " ".join((text or "").split()))
    return [_cut(x, 170) for x in parts if len(x) > 20][:n]


def _long_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%B} {d.day}, {d.year}"


def deck_content(pack: dict) -> dict:
    """Every fillable field of the template, as text (lists for the bulleted boxes)."""
    rec, pl, ov = pack.get("rec") or {}, pack.get("plan") or {}, pack.get("overview") or {}
    w = pack.get("writeup") or {}
    inv, tgt, risk, ent = (pl.get("investment") or {}), (pl.get("targets") or {}), (pl.get("risk") or {}), \
        (pl.get("entry") or {})
    fit, conv = pl.get("fit") or {}, rec.get("conviction") or {}
    cases = inv.get("cases") or {}
    sym, name = pack["symbol"], pack.get("name") or pack["symbol"]
    rating = rec.get("rating") or pack.get("action") or "WATCH"
    price = pack.get("price")

    def wl(k, n=3):                                 # a validated write-up field, as a list
        v = w.get(k)
        if isinstance(v, list):
            return [_cut(x, 190) for x in v if x][:n]
        return [_cut(v, 260)] if v else []

    metrics = []
    labels = {"EPS growth (TTM, YoY)": lambda v: f"{v:+.1f}% EPS growth (TTM, year on year)",
              "Gross margin": lambda v: f"{v:.1%} gross margin", "Operating margin": lambda v: f"{v:.1%} operating margin",
              "Net margin": lambda v: f"{v:.1%} net margin", "Dividend yield": lambda v: f"{v:.2%} dividend yield",
              "Beta": lambda v: f"Beta {v:.2f}",
              "52-week range": lambda v: f"52-week range {_px(v[0])} – {_px(v[1])}"}
    for k, v in (ov.get("metrics") or {}).items():
        try:
            metrics.append(labels[k](v) if k in labels else f"{k}: {v}")
        except (TypeError, ValueError, IndexError):
            continue
    if ov.get("pe_ltm"):
        metrics.append(f"P/E (trailing) {ov['pe_ltm']:.1f}x")
    metrics.append(f"Revenue, free cash flow, segments: {NA}")

    what = wl("what_they_do") or _sentences(ov.get("description") or "", 3) or [f"Business description: {NA}."]
    why_now = wl("why_now") or [_cut(pack.get("what_happened") or "", 220)]
    earn = (pl.get("entry") or {}).get("earnings")
    why_now.append(f"Next earnings: {earn}" + (" — inside the expected hold" if pl.get("earnings_in_hold") else "")
                   if earn else f"Next earnings date: {NA}")
    for n_ in (pack.get("news") or [])[:1]:
        if n_.get("title"):
            why_now.append(_cut(f"Headline: {n_['title']}", 170))
    moat = wl("moat") or [f"{NA[0].upper() + NA[1:]}: no source for market share, patents or switching costs.",
                          f"The rule does not need one: this is a {rec.get('method', {}).get('label', pack['book']).lower()} "
                          f"setup — {_cut(pack.get('edge') or '', 120)}."]

    # thesis
    core = w.get("core_thesis") or rec.get("core_thesis") or ""
    lines = inv.get("lines") or []
    bull, bear = wl("bull_case", 1), wl("bear_case", 1)
    if cases.get("bull"):
        bull.append(f"12-month bull {_px(cases['bull']['value'])} ({_pct(cases['bull'].get('upside'))}): EPS growing "
                    f"{cases['bull']['growth']:.1%} a year, discounted at {cases['bull']['rate']:.1%}")
    if tgt.get("t2"):
        bull.append(f"Trade: T2 {_px(tgt['t2'])} ({_pct(tgt['t2'] / price - 1)}) — {tgt.get('t2_kind')}")
    elif tgt.get("t1"):
        bull.append(f"Trade: T1 {_px(tgt['t1'])} ({_pct(tgt['t1'] / price - 1)}) — {tgt.get('t1_kind')}")
    if cases.get("bear"):
        bear.append(f"12-month bear {_px(cases['bear']['value'])} ({_pct(cases['bear'].get('upside'))}): EPS growing "
                    f"{cases['bear']['growth']:.1%} a year, discounted at {cases['bear']['rate']:.1%}")
    if pack.get("stop") and price:
        bear.append(f"Trade: stop {_px(pack['stop'])} ({_pct(pack['stop'] / price - 1)}), the rule's exit")
    for x in (pl.get("wrong_if") or [])[:1]:
        bear.append(_cut(f"Wrong if: {x}", 160))
    if not bull:
        bull = [f"Upside case: {NA}"]
    if not bear:
        bear = [f"Downside case: {NA}"]

    if inv.get("available") and inv.get("prob_weighted"):
        val = [f"{_px(inv['prob_weighted'])} probability-weighted ({_pct(inv.get('prob_upside'))}) by {inv['horizon']}"
               + (f"; total return {_pct(inv['total_return'])} with the {inv['dividend_yield']:.1%} dividend"
                  if inv.get("dividend_yield") and inv.get("total_return") is not None else "")]
        wts = (pl.get("settings") or {}).get("scenario_weights") or {}
        if wts and cases.get("base"):
            val.append("Method: " + " / ".join(f"{int(round(float(v) * 100))}% {k}" for k, v in wts.items())
                       + f" earnings DCF; base {_px(cases['base']['value'])}")
        if (inv.get("comps") or {}).get("value"):
            val.append(f"Peer comps {_px(inv['comps']['value'])} ({_pct(inv['comps'].get('upside'))}, "
                       f"{inv['comps'].get('n')} peers)")
        if inv.get("street_target"):
            val.append(f"Street mean target {_px(inv['street_target'])} ({_pct(inv.get('street_upside'))}) — reference only")
        if rec.get("conflict"):
            val.append(_cut("Views disagree: " + rec["conflict"], 200))
    else:
        val = [f"12-month targets: {inv.get('reason') or NA}"]
    risks = wl("risks", 3) or [_cut(x, 170) for x in (pack.get("risks") or [])[:3]] or [f"Risks: {NA}"]

    # data slide caption
    base = pl.get("base_rates") or {}
    if base.get("n"):
        note = (f"How the {rec.get('method', {}).get('label', pack['book']).lower()} book has done "
                f"({base['period'][0]} to {base['period'][1]}, {base['n']} trades"
                f"{', small sample' if base.get('small_sample') else ''}): win rate {base.get('win_rate', 0):.0%}, "
                f"expectancy {base.get('expectancy_R') or 0:+.2f}R per trade. In-sample replay, not a walk-forward record.")
    else:
        note = f"Book base rates: {base.get('note') or NA}."
    note += f" Prices: daily closes to {pack['as_of']}. Research, not advice."

    # recommendation
    size = []
    if rating in ("BUY", "ADD"):
        z = ent.get("zone")
        size.append(f"Entry: {ent.get('tested', 'at the next open')} near {_px(ent.get('reference') or price)}"
                    + (f" (zone {_px(z[0])}–{_px(z[1])})" if z else ""))
        lv = [f"Stop {_px(pack.get('stop'))} ({_pct(risk.get('stop_pct'))})" if pack.get("stop") else None,
              f"T1 {_px(tgt.get('t1'))}" if tgt.get("t1") else None,
              f"T2 {_px(tgt.get('t2'))}" if tgt.get("t2") else None,
              f"R:R {risk['rr']:.2f}" if risk.get("rr") else None,
              f"review {tgt['review_date']}" if tgt.get("review_date") else None]
        size.append(" · ".join(x for x in lv if x))
        if risk.get("pct_of_fund"):
            size.append(f"Size: {risk['pct_of_fund']:.1%} of the fund"
                        + (f" = {risk['shares']:,} shares" if risk.get("shares") else "") + f" ({risk.get('bound')})"
                        + (f"; {risk['risk_pct_of_fund']:.2%} of the fund at risk" if risk.get("risk_pct_of_fund") is not None else ""))
        for x in (ent.get("refinements") or [])[:1]:
            size.append(_cut(f"Untested refinement: {x['text']}", 150))
    elif rating in ("SELL", "TRIM"):
        size.append(f"{rating.title()} {ent.get('tested', 'at the next open')} near {_px(price)}: "
                    f"{(pack.get('reason') or '').replace('_', ' ')}")
        ex = pl.get("exit_review") or {}
        if ex.get("return") is not None:
            size.append(f"The position: bought {_px(ex.get('entry'))}"
                        + (f" on {ex['entry_date']}" if ex.get("entry_date") else "")
                        + f", {_pct(ex['return'])}" + (f" ({ex['r']:+.2f}R)" if ex.get("r") is not None else ""))
        if ex.get("lesson"):
            size.append(_cut(ex["lesson"], 170))
    else:
        size.append(f"No position. {rec.get('action_line', '')}".strip())

    fits = wl("why_it_fits", 1)
    if fit:
        if fit.get("adds"):
            fits.append(f"Adds {fit['adds']}")
        if fit.get("sector_before") is not None:
            fits.append(f"{fit.get('sector')}: {fit['sector_before']:.1%} of the fund before, {fit['sector_after']:.1%} after")
        if fit.get("beta") is not None:
            fits.append(f"Beta {fit['beta']:.2f}" + (f" ({fit['beta_source']})" if fit.get("beta_source") else "")
                        + ("" if fit.get("corr_benchmark") is None
                           else f"; correlation to the benchmark {fit['corr_benchmark']:.2f}"))
    if not fits:
        fits = [f"Portfolio fit: {NA}"]

    qs = (w.get("questions") if isinstance(w.get("questions"), list) else None) or pl.get("questions") or []
    out = {
        "company": name, "ticker": sym, "date": _long_date(pack["as_of"]), "analyst": ANALYST,
        "footer": f"{name} ({sym})" if name != sym else sym,
        "market_cap": _usd_s(ov.get("market_cap")), "sector": ov.get("sector") or NA,
        "revenue_ttm": _usd_s(ov.get("revenue_ttm")) if ov.get("revenue_ttm") else NA,
        "pe_ntm": f"{ov['pe_ntm']:.1f}x" if ov.get("pe_ntm") else NA,
        "what_they_do": what, "why_now": [x for x in why_now if x][:4], "moat": moat, "metrics": metrics[:7],
        "core_thesis": _cut(core, 420), "bull": bull[:3], "bear": bear[:3], "valuation": val[:5], "risks": risks,
        "data_note": note,
        "rating": rating,
        "rating_sub": f"{RATING_SUB.get(rating, rating.title())} · {conv.get('bucket', '—')} conviction"
                      + (f" ({conv['score']})" if conv.get("score") is not None else ""),
        "size_entry": [x for x in size if x][:4], "fit": fits[:4],
    }
    for i in range(3):
        out[f"q{i + 1}"] = _cut(qs[i], 180) if i < len(qs) else None
    return out


# ------------------------------------------------------------------------------------------- pptx
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _fit_size(items: list[str], w_in: float, h_in: float, start: float = 10, floor: float = 7) -> float:
    """Largest font size (pt) at which the items should fit the box (Calibri, ~0.5 em per character)."""
    size = start
    while size > floor:
        per_line = max(8, int((w_in - 0.25) * 72 / (size * 0.5)))
        lines = sum(max(1, math.ceil(len(t) / per_line)) for t in items)
        if lines * size * 1.2 / 72 <= h_in - 0.08:
            return size
        size -= 0.5
    return floor


def _set_text(sh, text: str, size: float | None = None) -> None:
    run = sh.text_frame.paragraphs[0].runs[0]
    run.text = text
    if size:
        run.font.size = _pt(size)


def _pt(x):
    from pptx.util import Pt
    return Pt(x)


def _set_list(sh, items: list[str]) -> None:
    body = sh.text_frame._txBody
    proto = sh.text_frame.paragraphs[0]._p
    size = _fit_size(items, sh.width / 914400, sh.height / 914400, start=9.5)
    for it in items:
        p = copy.deepcopy(proto)
        r = p.find(A + "r")
        r.find(A + "t").text = it
        r.find(A + "rPr").set("sz", str(int(size * 100)))
        body.append(p)
    body.remove(proto)


def _recolor(sh, hex_: str, line: bool = True, fill: bool = False) -> None:
    from pptx.dml.color import RGBColor
    if line:
        sh.line.color.rgb = RGBColor.from_string(hex_)
    if fill:
        sh.fill.solid()
        sh.fill.fore_color.rgb = RGBColor.from_string(hex_)


def leftovers(path_or_prs) -> list[str]:
    """Any ``{{token}}`` (or the old template's bracket placeholders) still in a deck."""
    from pptx import Presentation
    prs = Presentation(str(path_or_prs)) if isinstance(path_or_prs, (str, Path)) else path_or_prs
    found = []
    for i, s in enumerate(prs.slides, 1):
        for sh in s.shapes:
            if sh.has_text_frame:
                t = sh.text_frame.text
                found += [f"slide {i}: {m}" for m in TOKEN.findall(t)]
                found += [f"slide {i}: {m}" for m in re.findall(r"\[(?:Company Name|Ticker|Date|Analyst)[^\]]*\]", t)]
    return found


def build_deck(pack: dict, out: Path, charts_dir: Path | None = None, template: Path | None = None) -> Path:
    """Fill the template from the pack and save it. Raises if python-pptx is missing or a token is left."""
    from pptx import Presentation
    from pptx.util import Inches
    c = deck_content(pack)
    prs = Presentation(str(template or TEMPLATE))
    rating_hex = RATING_COLOR.get(c["rating"], NEUTRAL)
    for si, s in enumerate(prs.slides, 1):
        drop = []
        for sh in list(s.shapes):
            name = sh.name or ""
            if name == "tok:title":
                sh.text_frame.paragraphs[0].runs[0].text = f"{c['company']} "
                if len(c["company"]) > 14:
                    for r in sh.text_frame.paragraphs[0].runs:
                        r.font.size = _pt(40 if len(c["company"]) <= 22 else 32)
                continue
            if name in ("tok:rating_box",):
                _recolor(sh, rating_hex, line=True)
                continue
            if name == "tok:rating_bar":
                _recolor(sh, rating_hex, line=True, fill=True)
                continue
            if not name.startswith("tok:"):
                continue
            key = name[4:]
            if key.endswith("[]"):
                _set_list(sh, c[key[:-2]] or [NA])
                continue
            val = c.get(key)
            if key in ("q1", "q2", "q3") and not val:
                drop.append(sh.top)
                continue
            if key == "rating":
                from pptx.dml.color import RGBColor
                _set_text(sh, val, 40 if len(val) <= 4 else 32)
                sh.text_frame.paragraphs[0].runs[0].font.color.rgb = RGBColor.from_string(rating_hex)
                continue
            size = None
            if key == "core_thesis":
                size = _fit_size([val], sh.width / 914400, sh.height / 914400, start=10, floor=7.5)
            elif key in ("q1", "q2", "q3"):
                size = 9 if len(val) > 140 else None
            elif key in ("company", "ticker", "date", "analyst") and len(str(val)) > 22:
                size = 9
            elif key == "rating_sub":
                size = 10
            _set_text(sh, str(val), size)
            if key == "data_note":
                from pptx.dml.color import RGBColor
                run = sh.text_frame.paragraphs[0].runs[0]
                run.font.color.rgb = RGBColor.from_string(NEUTRAL)
                run.font.name = "Calibri"
        for top in drop:                             # an unused question row: number, circle, panel, text
            for sh in list(s.shapes):
                if abs(sh.top - top) < Inches(0.06):
                    sh._element.getparent().remove(sh._element)
        if si == 4:
            _place_charts(s, pack, charts_dir)
    left = leftovers(prs)
    if left:
        raise ValueError(f"deck has unfilled placeholders: {left[:5]}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.core_properties.title = f"{c['company']} ({c['ticker']}) stock pitch — {c['date']}"
    prs.core_properties.author = ANALYST
    prs.save(str(out))
    return out


def _place_charts(slide, pack: dict, charts_dir: Path | None) -> None:
    """Price chart on the left, valuation and peers stacked on the right, inside 0.95in-4.5in."""
    from pptx.util import Inches
    ch = pack.get("charts") or {}
    if not charts_dir or not ch:
        return

    def size(p: Path):
        try:
            from PIL import Image
            with Image.open(p) as im:
                return im.size
        except Exception:
            return None

    top, bottom = 0.95, 4.5
    price = Path(charts_dir) / ch["price"] if ch.get("price") else None
    right = [Path(charts_dir) / ch[k] for k in ("valuation", "peers") if ch.get(k)]
    right = [p for p in right if p.is_file()]
    left_w = 5.95 if right else 9.5
    if price and price.is_file():
        wh = size(price) or (1500, 810)
        w = left_w
        h = w * wh[1] / wh[0]
        if h > bottom - top:
            h = bottom - top
            w = h * wh[0] / wh[1]
        slide.shapes.add_picture(str(price), Inches(0.25), Inches(top), Inches(w), Inches(h))
    if right:
        x, w = 0.25 + left_w + 0.15, 9.75 - (0.25 + left_w + 0.15)
        dims = [size(p) or (1500, 700) for p in right]
        hs = [w * d[1] / d[0] for d in dims]
        avail = bottom - top - 0.1 * (len(right) - 1)
        scale = min(1.0, avail / sum(hs))
        y = top
        for p, h in zip(right, hs):
            slide.shapes.add_picture(str(p), Inches(x), Inches(y), Inches(w * scale), Inches(h * scale))
            y += h * scale + 0.1
