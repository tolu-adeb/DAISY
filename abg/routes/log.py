"""Activity log: every parsed alert and every per-account decision, one JSON line each
(``ABG_DATA_DIR/routes_log.jsonl``) - what was read, how long parsing took, what each check said."""
from __future__ import annotations

import json
from pathlib import Path


def log_path(data_dir) -> Path:
    return Path(data_dir).expanduser() / "routes_log.jsonl"


def append(data_dir, rows: list[dict]) -> None:
    p = log_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")


def tail(data_dir, n: int = 50) -> list[dict]:
    p = log_path(data_dir)
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()[-n:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out
