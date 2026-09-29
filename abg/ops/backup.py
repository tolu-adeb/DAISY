"""Online SQLite backups (safe while the terminal is running) with retention.

    ABG_DATA_DIR/backups/2026-09-30/portfolio.sqlite3
                                    signals.sqlite3
Restore: stop the terminal, copy the two files back into ABG_DATA_DIR, start it again.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DBS = ("portfolio.sqlite3", "signals.sqlite3")


def backup_databases(data_dir, keep_days: int = 14, today: date | None = None) -> dict:
    root = Path(data_dir).expanduser()
    day = (today or date.today()).isoformat()
    dest = root / "backups" / day
    dest.mkdir(parents=True, exist_ok=True)
    done = []
    for name in DBS:
        src = root / name
        if not src.exists():
            continue
        s = sqlite3.connect(str(src))
        d = sqlite3.connect(str(dest / name))
        try:
            s.backup(d)                      # consistent snapshot even while being written
            done.append(name)
        finally:
            d.close()
            s.close()
    removed = []
    cutoff = (today or date.today()) - timedelta(days=keep_days)
    for p in (root / "backups").iterdir():
        try:
            if p.is_dir() and date.fromisoformat(p.name) < cutoff:
                shutil.rmtree(p)
                removed.append(p.name)
        except ValueError:
            continue
    return {"dir": str(dest), "files": done, "removed": removed}
