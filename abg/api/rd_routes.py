"""REST endpoints for the dashboard's R&D tab (docs/17): Strategy Lab state, research reports, digests.

GET  /api/rd                    lab overview, recent trials, proposals, reports, saved digests, desk status
GET  /api/rd/report/{name}      one research report (Markdown)
GET  /api/rd/digest?period=     build a digest now (preview; not posted)
POST /api/rd/digest?period=     build and post it to the R&D Discord channel (admin token when one is set)

Read-only except the POST, which only sends a message.  Nothing here can approve a proposal or change the bot.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from ..utils import jsonable

router = APIRouter(prefix="/api/rd", tags=["rd"])
_CACHE: dict = {}


def _lab(settings):
    """One Lab per dataset version (rebuilt when the CSV changes); loading the bars takes a second or two."""
    from ..agents.lab import Lab
    from ..agents.lab.dataset import default_paths
    bars, trend = default_paths(Path(settings.data_dir).expanduser())
    if not Path(bars).exists():
        return None
    key = (str(bars), Path(bars).stat().st_mtime)
    if _CACHE.get("key") != key:
        _CACHE["lab"] = Lab.from_paths(bars, trend, settings.data_dir, settings.lab_holdout_months, workers=1)
        _CACHE["key"] = key
    return _CACHE["lab"]


def _overview(settings) -> dict:
    from ..agents.digest import fresh_holdout_eta, recent_digests
    from ..agents.tools import lab_trials
    out: dict = {"digests": recent_digests(settings, 12)}
    lab = _lab(settings)
    if lab is None:
        out["lab"] = None
        out["hint"] = "no lab dataset yet: data/mnq_proxy_5m.csv (see docs/17)"
        return out
    d = lab.describe()
    out["lab"] = {"data": d["data"], "gate": d["gate"], "trials_so_far": lab.ledger.count_trials(),
                  "holdout_looks": d["holdout_peeks_used"], "look_budget": d["peek_budget"],
                  "fresh_holdout_from": fresh_holdout_eta(lab),
                  "current": {k: v for k, v in lab.base.to_dict().items() if k in ("t1_r", "scale_frac", "be_mode", "orx_trend_len",
                                                                                   "entry_end", "flat_time", "setups")}}
    out["trials"] = lab_trials(lab, 30)
    out["proposals"] = lab.ledger.proposals()
    reps = sorted((Path(lab.ledger.path).parent / "reports").glob("run-*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    out["reports"] = []
    for f in reps[:10]:
        txt = f.read_text(encoding="utf-8", errors="replace")
        res = next((ln.strip() for ln in txt.splitlines() if ln.strip().startswith("**Result")), "")
        out["reports"].append({"name": f.name, "at": f.stat().st_mtime, "result": res})
    return out


@router.get("")
async def rd_overview(request: Request):
    s = request.app.state.engine.settings
    desk = getattr(request.app.state.monitor, "rd", None)
    out = await asyncio.to_thread(_overview, s)
    out["desk"] = desk.status() if desk else {"enabled": False, "webhook": bool(s.rd_webhook_url), "auto_run": s.lab_auto_run,
                                              "api_key": bool(s.anthropic_api_key),
                                              "hint": "start the monitor (abg serve / abg monitor) for scheduled digests"}
    return jsonable(out)


@router.get("/report/{name}")
async def rd_report(request: Request, name: str):
    if not re.fullmatch(r"run-[0-9-]+\.md", name):
        raise HTTPException(400, "bad report name")
    lab = await asyncio.to_thread(_lab, request.app.state.engine.settings)
    f = Path(lab.ledger.path).parent / "reports" / name if lab else None
    if not f or not f.exists():
        raise HTTPException(404, "no such report")
    return {"name": name, "text": f.read_text(encoding="utf-8", errors="replace")}


def _period(p: str) -> str:
    if p not in ("daily", "weekly", "monthly"):
        raise HTTPException(400, "period must be daily, weekly or monthly")
    return p


@router.get("/digest")
async def rd_digest_preview(request: Request, period: str = Query("daily")):
    from ..agents.digest import build_digest
    s = request.app.state.engine.settings
    d = await asyncio.to_thread(build_digest, s, _period(period), None, _lab(s), False)
    return jsonable(d)


@router.post("/digest")
async def rd_digest_post(request: Request, period: str = Query("daily")):
    from ..agents.digest import build_digest, post_digest, save_digest
    s = request.app.state.engine.settings
    if not s.rd_webhook_url:
        raise HTTPException(400, "set ABG_RD_WEBHOOK_URL in .env first")
    last = _CACHE.get("posted_at", 0)
    if time.time() - last < 30:
        raise HTTPException(429, "just posted - wait 30 seconds")
    _CACHE["posted_at"] = time.time()
    d = await asyncio.to_thread(build_digest, s, _period(period), None, _lab(s), s.rd_analyst_note)
    d["delivered"] = await post_digest(s, request.app.state.engine.http, d)
    save_digest(s, d, d["delivered"])
    return jsonable(d)
