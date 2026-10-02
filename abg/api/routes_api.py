"""REST endpoints for alert routing (dry run): list routes, test an alert, read the activity log."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..config import Settings

router = APIRouter(prefix="/api/routes", tags=["routes"])


def _dd(request: Request) -> Path:
    s = getattr(request.app.state, "settings", None) or Settings()
    return Path(s.data_dir).expanduser()


@router.get("")
async def list_routes(request: Request):
    from ..routes import load_routes
    return {"routes": [r.to_dict() for r in load_routes(_dd(request)).values()]}


@router.get("/log")
async def route_log(request: Request, n: int = 50):
    from ..routes.log import tail
    return {"log": tail(_dd(request), min(max(n, 1), 500))}


class _Test(BaseModel):
    route: str
    text: str
    price: float | None = None


@router.post("/test")
async def route_test(request: Request, body: _Test):
    from ..routes import RouteEngine, load_routes, parse_alert
    from ..routes.engine import NY
    routes = load_routes(_dd(request))
    if body.route not in routes:
        raise HTTPException(404, f"no route {body.route}")
    now = datetime.now(NY)
    a = parse_alert(body.text, ts=now)
    ds = RouteEngine(routes[body.route]).on_alert(a, now=now, price=body.price)
    return {"alert": a.to_dict(), "decisions": [d.to_dict() for d in ds]}


# --------------------------------------------------------------------------- Alerio (docs/15)
alerio_router = APIRouter(prefix="/api/alerio", tags=["alerio"])


@alerio_router.get("")
async def alerio_overview(request: Request):
    """Accounts, the latest audit and the cached comparison, plus the live shadow when the watcher runs."""
    import json as _json
    from ..routes.alerio import audit, load_snapshot
    dd = _dd(request)
    w = getattr(getattr(request.app.state, "monitor", None), "alerio", None)
    out = {"watch": w.status() if w is not None else {"enabled": False}}
    try:
        snap = load_snapshot(dd)
    except FileNotFoundError:
        out["snapshot"] = None
        return out
    a = audit(snap)
    out["snapshot"] = {"exported_at": snap.get("exported_at"), "route": snap.get("route"), "accounts": snap.get("accounts")}
    out["audit"] = {k: v for k, v in a.items() if k != "rows"} | {"rows": a["rows"][-12:]}
    p = dd / "alerio" / "compare.json"
    out["compare"] = _json.loads(p.read_text()) if p.exists() else None
    return out
