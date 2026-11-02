"""Carry the old terminal's history (and the MNQ bot spin-off's swing / forward-lab data) into DAISY (docs/20, stage 7).

    python scripts/carry_over.py --terminal ..\\school\\AIBG\\abg-terminal --mnq ..\\c3.lab\\mnq-bot
    python scripts/carry_over.py ... --switch        after the old monitor is closed: release the held Discord webhooks

What it does
    data/   every file of <terminal>/data (bot trades + events, portfolio, signals, R&D digests, lab, backups, Alerio,
            models, bar caches); SQLite databases are copied with the backup API, so a monitor that is still running
            can't leave a half-written copy.  Lock / WAL / health files are skipped.
            From the spin-off: data/swing (the swing desk's book, prices, wide scan), data/lab/rolling (forward lab),
            data/live (day logs), data/parity, data/papers, refit.json.
    .env    the terminal's .env, plus the spin-off-only keys DAISY understands, plus the new desks switched on
            (ABG_SWING_ENABLED, ABG_FWD_ENABLED, ABG_PITCH_ENABLED).  Values are copied, never printed.

One poster per channel: until --switch, every *_WEBHOOK_URL line (and the e-mail alert lines) in DAISY's .env is commented out with "#HELD ", so a
DAISY monitor started early only shows things on the dashboard.  --switch refuses while the old terminal's monitor
lock still has a fresh heartbeat.  Re-running before --switch refreshes the copy; after --switch it refuses unless
--force, because DAISY's own data is then the newest.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

SKIP = {"monitor.lock", "health.json", "uptime.json", "jobs.json", "spinoff.json", "site_commands.json"}
MNQ_ITEMS = ["swing", "lab/rolling", "live", "parity", "papers", "refit.json"]
MNQ_KEYS = ["ABG_SWING_WEBHOOK_URL", "ABG_SWING_RESEARCH", "ABG_SWING_WIDE_POST", "ABG_RD_ENABLED", "ABG_RD_ANALYST_NOTE",
            "ABG_LAB_AUTO_RUN", "ABG_LAB_REFRESH_DATA"]
TURN_ON = {"ABG_SWING_ENABLED": "true", "ABG_FWD_ENABLED": "true", "ABG_PITCH_ENABLED": "true", "ABG_DATA_DIR": "data"}
HOLD = "#HELD "
HELD_KEYS = ("ABG_SMTP_HOST", "ABG_EMAIL_TO")          # e-mail alerts would also go out twice


def _snapshot(src: Path, tmpdir: Path) -> Path:
    """Copy the database and its WAL to a local folder (some mounted / network drives can't share SQLite's memory map)."""
    tmpdir.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        shutil.copy2(src, tmpdir / src.name)
        wal = Path(str(src) + "-wal")
        if wal.exists():
            shutil.copy2(wal, tmpdir / (src.name + "-wal"))
        c = sqlite3.connect(str(tmpdir / src.name))
        ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        c.close()
        if ok:
            return tmpdir / src.name
        time.sleep(1)
    raise RuntimeError(f"{src.name}: copy kept changing under a running monitor - try again in a minute")


def _copy_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    def backup(s):
        d = sqlite3.connect(str(tmp))
        try:
            with d:
                s.backup(d)
        finally:
            s.close()
            d.close()
    try:
        backup(sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30))
    except sqlite3.OperationalError:
        import tempfile
        if tmp.exists():
            tmp.unlink()
        backup(sqlite3.connect(str(_snapshot(src, Path(tempfile.mkdtemp(prefix="daisy-carry-"))))))
    for ext in ("-wal", "-shm"):
        side = Path(str(dst) + ext)
        if side.exists():
            side.unlink()
    tmp.replace(dst)


def copy_tree(src: Path, dst: Path, counts: dict) -> None:
    if src.is_file():
        files = [src]
        base = src.parent
    else:
        files = [p for p in src.rglob("*") if p.is_file()]
        base = src
    for p in files:
        name = p.name
        if name in SKIP or name.endswith(("-wal", "-shm", ".lock", ".tmp")):
            continue
        out = dst / p.relative_to(base) if src.is_dir() else dst
        if name.endswith(".sqlite3"):
            _copy_db(p, out)
            counts["databases"] += 1
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)
            counts["files"] += 1
        counts["bytes"] += p.stat().st_size


def read_env(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def key_of(line: str) -> str | None:
    s = line[len(HOLD):] if line.startswith(HOLD) else line
    s = s.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    return s.split("=", 1)[0].strip()


def build_env(daisy: Path, terminal: Path, mnq: Path | None, hold: bool) -> list[str]:
    cur = read_env(daisy / ".env")
    lines = cur or read_env(terminal / ".env")
    have = {key_of(l) for l in lines}
    added = []
    if mnq is not None:
        mnq_lines = {key_of(l): l for l in read_env(mnq / ".env") if key_of(l)}
        for k in MNQ_KEYS:
            if k not in have and k in mnq_lines:
                lines.append(mnq_lines[k].strip())
                have.add(k)
                added.append(k)
    out = []
    for l in lines:
        k = key_of(l)
        if k in TURN_ON:
            continue
        if k and (k.endswith("_WEBHOOK_URL") or k in HELD_KEYS):
            bare = l[len(HOLD):] if l.startswith(HOLD) else l
            l = HOLD + bare if hold else bare
        out.append(l)
    out.append("# --- DAISY upgrade (docs/20): new desks on, data in DAISY/data ---")
    out += [f"{k}={v}" for k, v in TURN_ON.items()]
    # drop a repeated marker from an earlier run
    seen_marker = False
    final = []
    for l in out:
        if l.startswith("# --- DAISY upgrade"):
            if seen_marker:
                continue
            seen_marker = True
        final.append(l)
    (daisy / ".env").write_text("\n".join(final) + "\n", encoding="utf-8")
    return added


def lock_age(data: Path) -> float | None:
    p = data / "monitor.lock"
    try:
        return time.time() - float(json.loads(p.read_text()).get("heartbeat", 0))
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--terminal", required=True, type=Path, help="the old abg-terminal folder")
    ap.add_argument("--mnq", type=Path, help="the c3.lab/mnq-bot folder (swing desk + forward lab data)")
    ap.add_argument("--daisy", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--switch", action="store_true", help="release the held webhooks (old monitor must be closed)")
    ap.add_argument("--env-only", action="store_true", help="only rewrite .env (no data copy)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    daisy, term = a.daisy.resolve(), a.terminal.resolve()
    data = daisy / "data"
    rec_path = data / "carry_over.json"
    rec = json.loads(rec_path.read_text()) if rec_path.exists() else {}
    if (dl := lock_age(data)) is not None and dl < 180:
        print("DAISY's own monitor is running - stop it first.")
        return 2
    if a.switch:
        age = lock_age(term / "data")
        if age is not None and age < 300 and not a.force:
            print(f"the old terminal's monitor is still running (heartbeat {age:.0f}s ago) - close it, then re-run.")
            return 2
    if rec.get("switched") and not a.force and not a.env_only:
        print("already switched over: DAISY's data is now the newest - not copying again (use --force to overwrite).")
        return 2
    counts = {"files": 0, "databases": 0, "bytes": 0}
    if not a.env_only:
        copy_tree(term / "data", data, counts)
        if a.mnq:
            for item in MNQ_ITEMS:
                src = a.mnq.resolve() / "data" / item
                if src.exists():
                    copy_tree(src, data / item, counts)
    added = build_env(daisy, term, a.mnq.resolve() if a.mnq else None, hold=not a.switch)
    rec.update(at=time.strftime("%Y-%m-%d %H:%M:%S"), terminal=str(term), mnq=str(a.mnq) if a.mnq else None,
               switched=bool(a.switch or rec.get("switched")), **({"copied": counts} if not a.env_only else {}))
    data.mkdir(exist_ok=True)
    rec_path.write_text(json.dumps(rec, indent=1))
    print(f"copied {counts['databases']} databases + {counts['files']} files ({counts['bytes'] / 1e6:.1f} MB) into data/")
    print(".env: terminal keys" + (f" + {', '.join(added)}" if added else "") + "; swing / forward lab / pitch desks on")
    print("Discord webhooks: " + ("RELEASED - DAISY is now the poster" if a.switch else
                                  "HELD (dashboard only) until you run with --switch after closing the old monitor"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
