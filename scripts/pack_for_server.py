"""Pack DAISY's live data and settings for the move to a Linux server (docs/20.4).

    python scripts\\pack_for_server.py            -> daisy-server.tgz next to the DAISY folder

Stop the monitor on this PC first (one poster per channel): the script refuses while it is running.  The archive holds
    abg-data/              every database (SQLite backup API) and file from data/ - lock / health / uptime files left out
    .env.server.local      this PC's .env without the Windows-only and path lines; setup-server.sh adds the server's paths
It contains your API keys and webhooks: copy it straight to the server, then delete it on both machines.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import carry_over as co  # noqa: E402

DROP = ("ABG_DATA_DIR", "ABG_CACHE_DIR", "ABG_KEEP_AWAKE")   # the server sets its own paths; keep-awake is Windows-only


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--daisy", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    daisy = a.daisy.resolve()
    out = (a.out or daisy.parent / "daisy-server.tgz").resolve()
    if (age := co.lock_age(daisy / "data")) is not None and age < 300:
        print(f"DAISY's monitor is still running on this PC (heartbeat {age:.0f}s ago) - close it first, then re-run.")
        return 2
    env = daisy / ".env"
    if not env.exists():
        print("no .env in the DAISY folder")
        return 2
    with tempfile.TemporaryDirectory(prefix="daisy-pack-") as tmp:
        stage = Path(tmp)
        counts = {"files": 0, "databases": 0, "bytes": 0}
        co.copy_tree(daisy / "data", stage / "abg-data", counts)
        for junk in ("carry_over.json",):
            (stage / "abg-data" / junk).unlink(missing_ok=True)
        lines = [ln for ln in env.read_text(encoding="utf-8").splitlines() if co.key_of(ln) not in DROP]
        held = [co.key_of(ln) for ln in lines if ln.startswith(co.HOLD)]
        (stage / ".env.server.local").write_text("\n".join(lines) + "\n", encoding="utf-8")
        with tarfile.open(out, "w:gz") as tf:
            tf.add(stage / "abg-data", arcname="abg-data")
            tf.add(stage / ".env.server.local", arcname=".env.server.local")
    print(f"packed {counts['databases']} databases + {counts['files']} files ({counts['bytes'] / 1e6:.1f} MB) -> {out}")
    if held:
        print("note: still held (commented out) in .env: " + ", ".join(held) + " - run carry_over.py --switch first")
    print("It holds your keys: copy it to the server, unpack, then delete it here and there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
