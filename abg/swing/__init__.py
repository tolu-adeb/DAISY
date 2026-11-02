"""Swing desk inside the MNQ Bot (docs/19).

The desk itself is ``swingdesk/`` at the repo root - the House of Traders project, copied unchanged (its own code,
config, research, tests).  This package is the bridge:

* ``use_home(home)``  points the desk's *live* files (book, price cache, signal list, pitches, proposals) at a folder
  in this project's git-ignored ``data/`` instead of the repo copy, by setting the module paths the desk already uses.
  The repo copy stays the seed: the first run copies ``swingdesk/data/signals.csv`` and its price cache there.
* ``intake``  turns pasted text into the desk's signal row (channel format, then the terminal's parser, then the
  optional intake agent).
* ``desk.SwingDesk``  runs inside the monitor: intraday passes, the after-close pass, scanner, digests, Discord.
* ``routes``  the dashboard API (``/api/swing/...``).

Money decisions (take / skip, size, stop, take-profit) stay in ``swingdesk.strategy.grade`` - plain code, reading
``swingdesk/config/strategy.yaml``.  Nothing here changes a rule.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DESK_ROOT = REPO / "swingdesk"               # the copied project (code + config + research + seed data)

if str(DESK_ROOT) not in sys.path:           # `import swingdesk` -> swingdesk/swingdesk/
    sys.path.insert(0, str(DESK_ROOT))

_HOME: Path | None = None


def seed_home(home: Path) -> None:
    """First run: copy the seed signal list and price cache into the live folder (never overwrites)."""
    (home / "data" / "prices").mkdir(parents=True, exist_ok=True)
    for sub in ("state", "inbox", "research/proposals"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    src = DESK_ROOT / "data"
    if not (home / "data" / "signals.csv").exists() and (src / "signals.csv").exists():
        shutil.copy2(src / "signals.csv", home / "data" / "signals.csv")
    for p in (src / "prices").glob("*.csv"):
        dst = home / "data" / "prices" / p.name
        if not dst.exists():
            shutil.copy2(p, dst)


def _save_book_atomic(book: dict) -> None:
    import json
    import os
    from swingdesk import monitor
    path = Path(monitor.BOOK)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(book, fh, indent=1, default=str)
    os.replace(tmp, path)


def use_home(home: Path | str, seed: bool = True) -> Path:
    """Point the desk's live files at ``home`` (module globals the desk reads at call time)."""
    global _HOME
    home = Path(home).expanduser().resolve()
    if seed:
        seed_home(home)
    from swingdesk import cli, data_sources, monitor
    monitor.BOOK = home / "state" / "book.json"
    monitor.save_book = _save_book_atomic                  # spin-off PATCH 5: tmp file + replace
    monitor.TERMINAL.add("CANCELLED")                      # an order you cancel on the dashboard stays cancelled
    data_sources.PRICE_DIR = home / "data" / "prices"
    cli.SIGNALS = home / "data" / "signals.csv"
    cli.SCANNER = home / "data" / "scanner_signals.csv"
    try:                                                   # the agents' tools need strands (the [agents] extra)
        from swingdesk.agents import tools
    except Exception:
        tools = None
    if tools is not None:
        tools.ROOT = home                                  # tools._prices reads ROOT / data/prices
        tools.SIGNALS, tools.SCANNER_SIGNALS = cli.SIGNALS, cli.SCANNER
        tools.PITCH_LOG = home / "state" / "pitches.jsonl"
        tools.PROPOSALS = home / "research" / "proposals"
    _HOME = home
    return home


def home() -> Path:
    if _HOME is None:
        raise RuntimeError("swing desk home not set - call abg.swing.use_home() first")
    return _HOME
