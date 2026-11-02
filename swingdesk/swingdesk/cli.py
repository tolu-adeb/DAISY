"""swingdesk command line.

  python -m swingdesk.cli ingest --export path/to/DiscordKit.html   # add new posts to data/signals.csv
  python -m swingdesk.cli ingest-text "TICKER | ... Entry Area: ..."  # one pasted post
  python -m swingdesk.cli monitor [--quotes]                          # refresh book, send alerts
  python -m swingdesk.cli scan --universe config/universe.txt         # desk's own setups
  python -m swingdesk.cli research [--deep]                           # weekly R&D agent
  python -m swingdesk.cli digest --period daily|weekly|monthly        # scoreboard to Discord
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import data_sources as ds
from .monitor import load_book, save_book, scoreboard, update
from .notify import discord, sheet_append
from .parse_export import extract_signal, parse

ROOT = Path(__file__).resolve().parents[1]
SIGNALS = ROOT / "data/signals.csv"
SCANNER = ROOT / "data/scanner_signals.csv"
COLS = ["posted_et", "author", "symbol", "direction", "entry_type", "entry_lo", "entry_hi", "target_lo",
        "target_hi", "stop", "stop_basis", "levels_source", "note"]
HAS_LLM = bool(os.getenv("ANTHROPIC_API_KEY"))


def _append(rows: list[dict], path: Path = SIGNALS) -> int:
    old = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=COLS)
    have = set(zip(old.posted_et, old.symbol))
    new = [r for r in rows if (r["posted_et"], r["symbol"]) not in have]
    if new:
        pd.concat([old, pd.DataFrame(new)[COLS]], ignore_index=True).to_csv(path, index=False)
    return len(new)


def _from_parsed(p, posted_et: str, author: str) -> dict | None:
    if not p or not p.is_trade_idea:
        return None
    lo = p.entry_lo if p.entry_lo is not None else ""
    hi = p.entry_hi if p.entry_hi is not None else lo
    return {"posted_et": posted_et, "author": author, "symbol": p.symbol, "direction": p.direction,
            "entry_type": p.entry_type, "entry_lo": lo, "entry_hi": hi, "target_lo": p.target_lo,
            "target_hi": p.target_hi, "stop": p.stop, "stop_basis": p.stop_basis,
            "levels_source": p.levels_source, "note": p.notes[:200]}


def cmd_ingest(export: str) -> None:
    rows = []
    for m in parse(export):
        s = extract_signal(m)
        if not s:
            continue
        posted = m["ts"][:16].replace("T", " ")
        if s["target"] and s["stop"] and (s["entry"] or s["cmp_entry"]):
            rows.append({"posted_et": posted, "author": m["author"], "symbol": s["ticker"], "direction": s["direction"],
                         "entry_type": "cmp" if s["cmp_entry"] else ("close_above" if "close above" in s["text"].lower()
                                                                    else "zone"),
                         "entry_lo": s["entry"][0] if s["entry"] else "", "entry_hi": s["entry"][1] if s["entry"] else "",
                         "target_lo": s["target"][0], "target_hi": s["target"][1], "stop": s["stop"][0],
                         "stop_basis": "close" if "close" in s["text"].lower().split("stop")[-1] else "intraday",
                         "levels_source": "stated", "note": ""})
        elif HAS_LLM:  # prose-style posts: let the intake agent structure them
            from .agents.desk import parse_post
            r = _from_parsed(parse_post(s["text"]), posted, m["author"])
            if r:
                rows.append(r)
    n = _append(rows)
    print(f"ingested {n} new signals")


def cmd_ingest_text(text: str, author: str = "manual") -> None:
    from .agents.desk import parse_post
    r = _from_parsed(parse_post(text), datetime.now().strftime("%Y-%m-%d %H:%M"), author)
    print("not a trade idea" if not r else f"added {_append([r])}: {r}")


def _prices_for(symbols) -> dict:
    out = {}
    for s in set(symbols) | {"SPY"}:
        try:
            out[s] = ds.load_or_update(s)
        except Exception as e:  # keep going; one bad ticker should not stop the desk
            print(f"price update failed for {s}: {e}", file=sys.stderr)
    return out


def cmd_monitor(quotes: bool, pitch: bool) -> None:
    frames = [pd.read_csv(SIGNALS)] + ([pd.read_csv(SCANNER)] if SCANNER.exists() else [])
    sigs = pd.concat(frames, ignore_index=True)
    book = load_book()
    live = sigs[[f"{r.symbol}@{r.posted_et}" not in book or
                 book[f"{r.symbol}@{r.posted_et}"]["state"] in ("WATCHING", "FILLED") for r in sigs.itertuples()]]
    prices = _prices_for(live.symbol.unique())
    q = {s: ds.live_quote(s) for s in live.symbol.unique()} if quotes else {}
    book, alerts = update(live, prices, prices["SPY"], quotes=q, book=book)
    save_book(book)
    for a in alerts:
        discord(a.text, a.kind)
        sheet_append([datetime.now().isoformat(timespec="minutes"), a.kind, a.symbol, a.text.replace("\n", " | ")])
        if pitch and HAS_LLM and a.kind == "GRADED" and a.data.get("take"):
            from .agents.desk import pitch as make_pitch
            sig = live[(live.symbol + "@" + live.posted_et) == a.key].iloc[0].to_dict()
            p = make_pitch(sig)
            discord("\n".join([f"**Thesis**", *[f"• {t}" for t in p.thesis], "**Risks**", *[f"• {r}" for r in p.risks],
                               f"**Events** {', '.join(p.upcoming_events) or 'none found'}",
                               f"**News sentiment** {p.news_sentiment:+.2f} — {p.sentiment_rationale}",
                               f"**Plan** {p.when_to_enter}"]), "PITCH", title=p.headline)
    print(f"{len(alerts)} alerts | {scoreboard(book)}")


def cmd_scan(universe: str) -> None:
    from .scanner import find_setups
    syms = [s.strip() for s in open(universe) if s.strip() and not s.startswith("#")]
    rows = []
    for s in syms:
        try:
            px = ds.load_or_update(s)
        except Exception:
            continue
        setups = find_setups(s, px)
        today = [x for x in setups if x["posted_et"].startswith(str(px.index[-1].date()))]
        rows += today
    n = _append(rows, SCANNER)
    print(f"scanner: {n} new setups from {len(syms)} symbols")


def cmd_research(deep: bool) -> None:
    from .agents.desk import research
    out = research(deep)
    discord(out[:3900], "RESEARCH", title="R&D weekly")
    print(out)


def cmd_digest(period: str) -> None:
    sb = scoreboard(load_book())
    discord("\n".join(f"**{k}**: {v}" for k, v in sb.items()), "RESEARCH", title=f"Desk {period} digest")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="swingdesk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ingest"); a.add_argument("--export", required=True)
    a = sub.add_parser("ingest-text"); a.add_argument("text"); a.add_argument("--author", default="manual")
    a = sub.add_parser("monitor"); a.add_argument("--quotes", action="store_true"); a.add_argument("--no-pitch", action="store_true")
    a = sub.add_parser("scan"); a.add_argument("--universe", default=str(ROOT / "config/universe.txt"))
    a = sub.add_parser("research"); a.add_argument("--deep", action="store_true")
    a = sub.add_parser("digest"); a.add_argument("--period", default="daily")
    x = ap.parse_args(argv)
    {"ingest": lambda: cmd_ingest(x.export), "ingest-text": lambda: cmd_ingest_text(x.text, x.author),
     "monitor": lambda: cmd_monitor(x.quotes, not x.no_pitch), "scan": lambda: cmd_scan(x.universe),
     "research": lambda: cmd_research(x.deep), "digest": lambda: cmd_digest(x.period)}[x.cmd]()


if __name__ == "__main__":
    main()
