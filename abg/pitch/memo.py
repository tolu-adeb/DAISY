"""Pitch memo: the data pack (every number with its source), a verdict, the markdown memo and the Discord embed.

The verdict is rules, not a model:
    LONG   fair value >= 15% above the price AND the screen ranks it in the top third
    WATCH  one of the two holds
    PASS   neither
The optional Claude write-up (ABG_PITCH_AI_MEMO=true + an API key) only sees the data pack and must use its numbers.
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def _pct(x, d=1):
    return "n/a" if x is None else f"{x * 100:+.{d}f}%"


def _px(x):
    return "n/a" if x is None else f"${x:,.2f}"


def _num(x, d=1):
    return "n/a" if x is None else f"{x:,.{d}f}"


def verdict(upside: float | None, rank: int | None, n: int) -> str:
    cheap = upside is not None and upside >= 0.15
    strong = rank is not None and n and rank <= max(1, n // 3)
    return "LONG" if cheap and strong else ("WATCH" if cheap or strong else "PASS")


def risks(pack: dict) -> list[str]:
    f, v, raw = pack["fundamentals"], pack["valuation"], pack["screen"]["raw"]
    out = []
    if (f.get("beta") or 1) > 1.4:
        out.append(f"High beta ({f['beta']:.2f}): it tends to fall harder than the market.")
    if (raw.get("momentum") or 0) < 0:
        out.append(f"Negative 12-1 month momentum ({_pct(raw.get('momentum'))}): the trend is against it.")
    ig, g = v.get("implied_growth"), v["inputs"].get("growth")
    if ig is not None and g is not None and ig > g + 0.05:
        out.append(f"The price already assumes {_pct(ig)} growth vs the {_pct(g)} estimate.")
    if (f.get("pe") or 0) > 40:
        out.append(f"Rich trailing P/E ({f['pe']:.1f}): little room for a miss.")
    if (f.get("profit_margin") or 0.1) < 0.05:
        out.append("Thin profit margin: small cost changes move earnings a lot.")
    if not out:
        out.append("No red flags in the screened data; the usual risks (earnings miss, sector rotation, macro) apply.")
    out.append("Valuation uses EPS, not free cash flow, and analyst-style forward estimates can be revised.")
    return out


def build_pack(sym: str, price: float, fund: dict, screen_row: dict, n_screened: int, val: dict, news: list[dict],
               sources: dict, spy: float | None) -> dict:
    pack = {
        "symbol": sym, "name": fund.get("name") or sym, "sector": fund.get("sector") or fund.get("industry"),
        "made_at": datetime.now(NY).isoformat(timespec="seconds"), "price": price, "spy": spy,
        "fundamentals": {k: fund.get(k) for k in ("market_cap", "pe", "forward_pe", "eps", "beta", "dividend_yield",
                                                   "profit_margin", "week52_high", "week52_low")},
        "screen": {"rank": screen_row.get("rank"), "of": n_screened, "composite": screen_row.get("composite"),
                   "z": screen_row.get("z"), "raw": screen_row.get("raw") or {}},
        "valuation": val, "news": news[:6], "sources": sources,
    }
    pack["fundamentals"]["target_mean_price"] = (fund.get("extra") or {}).get("target_mean_price")
    pack["verdict"] = verdict(val.get("upside"), screen_row.get("rank"), n_screened)
    pack["risks"] = risks(pack)
    # one rating vocabulary across both desks (docs/21.6): LONG -> BUY, WATCH -> WATCH, PASS -> AVOID
    from abg.stocks.pitch_plan import ACTION_LINE, VERDICT_TO_RATING
    r = VERDICT_TO_RATING.get(pack["verdict"], "WATCH")
    pack["rec"] = {"rating": r, "action_line": ACTION_LINE.get(r, ""),
                   "basis": "pitch desk: factor screen + valuation (no tested trading rule behind this name)"}
    return pack


def thesis(pack: dict) -> list[str]:
    s, v, z = pack["screen"], pack["valuation"], pack["screen"]["z"] or {}
    out = [f"Ranks #{s['rank']} of {s['of']} on the factor screen (composite {_num(s['composite'], 2)})."]
    from .screen import LABELS
    good = sorted(((k, x) for k, x in z.items() if x > 0.5), key=lambda kv: -kv[1])
    if good:
        out.append("Strongest factors: " + ", ".join(f"{LABELS[k]} ({x:+.1f} sd)" for k, x in good[:3]) + ".")
    if v.get("fair_value"):
        out.append(f"Blended fair value {_px(v['fair_value'])} vs price {_px(pack['price'])} ({_pct(v.get('upside'))}).")
    if v.get("implied_growth") is not None:
        out.append(f"Reverse DCF: today's price implies {_pct(v['implied_growth'])} first-year EPS growth fading to "
                   f"{_pct(v['inputs']['terminal_growth'])}; the estimate is {_pct(v['inputs']['growth'])} "
                   f"({v['inputs']['growth_source']}).")
    return out


def render_md(pack: dict, note: str | None = None) -> str:
    v, f, inp = pack["valuation"], pack["fundamentals"], pack["valuation"]["inputs"]
    sc, cm = v.get("scenarios") or {}, v.get("comps") or {}
    L = [f"# {pack['symbol']} - {pack['name']}: {pack['verdict']}",
         f"*{pack['made_at'][:16].replace('T', ' ')} ET - price {_px(pack['price'])} - sector {pack.get('sector') or 'n/a'}*",
         "", "Research only, not advice. Paper scorecard vs SPY tracks every pitch.", ""]
    rec = pack.get("rec") or {}
    if rec.get("rating"):
        L += [f"**Rating: {rec['rating']} — {rec.get('action_line', '')}** ({rec.get('basis', '')}).", ""]
    L += ["## Thesis"]
    L += [f"- {t}" for t in thesis(pack)]
    if note:
        L += ["", "## Analyst write-up (Claude, from the data pack only)", note]
    L += ["", "## Valuation", f"Method: {v['method']}.", "",
          "| case | growth (yr 1) | discount rate | value / share | vs price |", "|---|---|---|---|---|"]
    for k in ("bear", "base", "bull"):
        c = sc.get(k) or {}
        L.append(f"| {k} | {_pct(c.get('growth'))} | {_pct(c.get('rate'))} | {_px(c.get('value'))} | "
                 f"{_pct((c['value'] / pack['price'] - 1) if c.get('value') else None)} |")
    L += ["", f"Inputs: EPS {_num(inp.get('eps'), 2)}, forward EPS {_num(inp.get('forward_eps'), 2)}, beta "
          f"{_num(inp.get('beta'), 2)}, discount rate {_pct(inp['discount_rate'])} = {_pct(inp['risk_free'])} + beta x "
          f"{_pct(inp['equity_premium'])}, terminal growth {_pct(inp['terminal_growth'])}, 10 years.",
          f"Peer comps ({', '.join(cm.get('peers') or []) or 'none'}): median P/E {_num(cm.get('median_pe'))} -> "
          f"{_px(cm.get('value_pe'))}; median forward P/E {_num(cm.get('median_forward_pe'))} -> "
          f"{_px(cm.get('value_forward_pe'))}.",
          f"Blended fair value (base DCF + comps): **{_px(v.get('fair_value'))}** ({_pct(v.get('upside'))}).",
          f"Street target (provider): {_px(f.get('target_mean_price'))}.", "", "## Screen", ]
    raw = pack["screen"]["raw"]
    L.append(f"12-1m momentum {_pct(raw.get('momentum'))}, 3m return {_pct(raw.get('ret_3m'))}, 3m volatility "
             f"{_pct(raw.get('vol_3m'))}, earnings yield {_pct(raw.get('value'))}, profit margin {_pct(raw.get('quality'))}, "
             f"expected EPS growth {_pct(raw.get('growth'))}.")
    L += ["", "## Risks"] + [f"- {r}" for r in pack["risks"]]
    L += ["", "## Sources"]
    for k, src in pack["sources"].items():
        L.append(f"- {k}: {src}")
    for n in pack["news"]:
        when = (n.get("published_at") or "")[:10]
        L.append(f"- [{n.get('title')}]({n.get('url')}) - {n.get('publisher') or n.get('source')} {when}".rstrip()
                 if n.get("url") else f"- {n.get('title')} ({n.get('publisher') or n.get('source')})")
    L.append(f"- SEC filings: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={pack['symbol']}&type=10-K")
    return "\n".join(L) + "\n"


def embed(pack: dict, dashboard_note: str = "full memo on the dashboard (Pitch Desk tab)") -> dict:
    v = pack["valuation"]
    color = {"LONG": 0x2ECC71, "WATCH": 0xF1C40F, "PASS": 0x95A5A6}[pack["verdict"]]
    links = [f"[{n['title'][:70]}]({n['url']})" for n in pack["news"][:3] if n.get("url")]
    fields = [
        {"name": "Price", "value": _px(pack["price"]), "inline": True},
        {"name": "Fair value", "value": f"{_px(v.get('fair_value'))} ({_pct(v.get('upside'))})", "inline": True},
        {"name": "Screen", "value": f"#{pack['screen']['rank']} of {pack['screen']['of']}", "inline": True},
        {"name": "Thesis", "value": "\n".join(f"- {t}" for t in thesis(pack))[:1000]},
        {"name": "Risks", "value": "\n".join(f"- {r}" for r in pack["risks"][:3])[:1000]},
    ]
    if links:
        fields.append({"name": "Sources", "value": "\n".join(links)[:1000]})
    return {"title": f"Pitch: {pack['symbol']} - {pack['verdict']}", "color": color,
            "description": f"{pack['name']} - research only, not advice. {dashboard_note}.",
            "fields": fields, "footer": {"text": "DAISY pitch desk - paper scorecard vs SPY"}}


def ai_note(settings, pack: dict) -> str | None:
    """Optional analyst write-up from Claude (ABG_PITCH_AI_MEMO), using the fund's pitch prompt and the
    same validator as the stock desk: any field that quotes a number, ticker or date not in the pack is
    dropped. Returns the accepted fields as a short note, or None."""
    if not getattr(settings, "anthropic_api_key", None):
        return None
    try:
        import asyncio
        import httpx

        from abg.stocks import writeup as W

        class _Http:                                    # a minimal client for this one synchronous call
            async def request(self, method, url, *, provider, timeout=None, headers=None, json=None):
                async with httpx.AsyncClient(timeout=timeout or 120) as c:
                    r = await c.request(method, url, headers=headers, json=json)
                    r.raise_for_status()
                    return r
        res = asyncio.run(W.generate(settings, _Http(), pack))
        f = res.get("fields") or {}
        if not f:
            return None
        parts = [f.get("core_thesis"), f.get("valuation_view"),
                 f"Bull: {f['bull_case']}" if f.get("bull_case") else None,
                 f"Bear: {f['bear_case']}" if f.get("bear_case") else None]
        return " ".join(x for x in parts if x)[:1500] or None
    except Exception:
        return None
