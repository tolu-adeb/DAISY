"""The R&D desk inside the monitor: scheduled digests and the weekly research run (docs/17, docs/21).

Since docs/21 the MNQ bot is retired, so its digests and lab jobs below only run with
ABG_ALPHA_RETIRED=false. What runs instead: Saturday 09:00 ET, the stock desk's study (ablations,
walk-forward, early-warning flags) posted to ABG_RD_WEBHOOK_URL - findings are proposals only.

    weekdays  ABG_RD_DAILY_TIME (12:20 ET)   daily digest  -> Discord (ABG_RD_WEBHOOK_URL) + the R&D tab
    Friday    16:30 ET                       weekly digest (with a 1-month regime watch)
    1st weekday of the month, 08:00 ET       monthly digest for the month just ended (3-month regime watch)
    Saturday  09:00 ET                       refresh the lab dataset, then (ABG_LAB_AUTO_RUN=weekly + an API key)
                                             one research run by the Strands agents; its report is posted too

Each job runs once per period (remembered in data/rd/state.json, so restarts never double-post), in a worker
thread so the bot's live loop is never blocked.  Backtests run single-process here (safe on Windows).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .digest import NY, build_digest, post_digest, save_digest, store_dir

log = logging.getLogger(__name__)


def _first_weekday(d: date) -> date:
    x = d.replace(day=1)
    while x.weekday() >= 5:
        x += timedelta(days=1)
    return x


def _hm(s: str) -> int:
    h, m = (s or "12:20").split(":")
    return int(h) * 60 + int(m)


class RDDesk:
    def __init__(self, monitor):
        self.m = monitor
        self.s = monitor.s
        self.http = monitor.engine.http
        self._stop = asyncio.Event()
        self.state_path = store_dir(self.s) / "state.json"
        self.last: dict[str, str] = {}
        self.errors: list[str] = []
        self.busy: str | None = None

    # ------------------------------------------------------------------ state
    def _state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _once(self, key: str) -> bool:
        st = self._state()
        if key in st:
            return False
        st[key] = time.time()
        self.state_path.write_text(json.dumps(st))
        return True

    def _mnq(self) -> bool:
        """The MNQ digests and lab only make sense while the MNQ bot is live (docs/21: retired)."""
        return not bool(getattr(self.s, "alpha_retired", True))

    def _stocks(self):
        return getattr(self.m, "stocks", None)

    def status(self) -> dict:
        st = self._stocks()
        return {"webhook": bool(self.s.rd_webhook_url), "daily": self.s.rd_daily, "weekly": self.s.rd_weekly,
                "monthly": self.s.rd_monthly, "auto_run": self.s.lab_auto_run, "api_key": bool(self.s.anthropic_api_key),
                "mnq": self._mnq(), "stocks_research": getattr(self.s, "stocks_research", "weekly"),
                "stocks_desk": st is not None,
                "busy": self.busy, "last": self.last, "errors": self.errors[-3:]}

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ loop
    def due(self, now: datetime) -> list[tuple[str, str, dict]]:
        from ..live.market_hours import is_trading_day
        s, mins, out = self.s, now.hour * 60 + now.minute, []
        wk = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
        mnq = self._mnq()
        if mnq and s.rd_daily and is_trading_day(now.date()) and mins >= _hm(s.rd_daily_time):
            out.append(("daily", f"rd:daily:{now.date()}", {"ref": now.date()}))
        if mnq and s.rd_weekly and now.weekday() == 4 and mins >= 16 * 60 + 30:
            out.append(("weekly", f"rd:weekly:{wk}", {"ref": now.date()}))
        if mnq and s.rd_monthly and now.date() == _first_weekday(now.date()) and now.hour >= 8:
            prev = now.date().replace(day=1) - timedelta(days=1)
            out.append(("monthly", f"rd:monthly:{prev:%Y-%m}", {"ref": prev}))
        if mnq and now.weekday() == 5 and now.hour >= 9:
            out.append(("research", f"rd:research:{wk}", {}))
        if (str(getattr(s, "stocks_research", "weekly") or "off").lower() == "weekly"
                and self._stocks() is not None and now.weekday() == 5 and now.hour >= 9):
            out.append(("stocks_research", f"rd:stocks_research:{wk}", {}))
        return out

    async def run(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(NY)
            for job, key, kw in self.due(now):
                if not self._once(key):
                    continue
                try:
                    self.busy = job
                    if job == "research":
                        await self.weekly_research()
                    elif job == "stocks_research":
                        await self.stocks_research(post=True)
                    else:
                        await self.digest(job, kw["ref"], post=True)
                    self.last[job] = now.isoformat(timespec="minutes")
                except asyncio.CancelledError:
                    raise
                except Exception as e:                       # a failing job must never stop the monitor
                    log.exception("R&D job %s failed", job)
                    self.errors.append(f"{job}: {type(e).__name__}: {e}"[:200])
                finally:
                    self.busy = None
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ jobs
    def _lab(self):
        from .lab import Lab
        return Lab.from_paths(None, None, self.s.data_dir, self.s.lab_holdout_months, workers=1)

    async def digest(self, period: str, ref: date | None = None, post: bool = False) -> dict:
        d = await asyncio.to_thread(build_digest, self.s, period, ref, None, self.s.rd_analyst_note)
        delivered = await post_digest(self.s, self.http, d) if post else "preview"
        save_digest(self.s, d, delivered)
        d["delivered"] = delivered
        return d

    async def stocks_research(self, post: bool = False) -> dict:
        """The stock desk's weekly study, posted to the R&D channel. Proposals only."""
        st = self._stocks()
        if st is None:
            return {"skipped": "the stock desk is not running (ABG_STOCKS_ENABLED)"}
        url = self.s.rd_webhook_url if post else None
        rep = await st.research(post_url=url)
        return {"at": rep.get("at"), "verdict": (rep.get("verdict") or {}).get("overall"),
                "delivered": rep.get("delivered"), "skipped": rep.get("skipped")}

    async def weekly_research(self) -> dict:
        out: dict = {}
        if self.s.lab_refresh_data:
            from .lab.dataset import default_paths, refresh_proxy_csv
            bars, trend = default_paths(Path(self.s.data_dir).expanduser())
            if Path(bars).exists():
                out["refresh"] = await refresh_proxy_csv(self.m.engine, bars, trend)
        if (self.s.lab_auto_run or "off").lower() == "weekly" and self.s.anthropic_api_key:
            out["research"] = await asyncio.to_thread(self._research)
        elif (self.s.lab_auto_run or "off").lower() == "weekly":
            out["research"] = {"skipped": "no ABG_ANTHROPIC_API_KEY - the agents didn't run"}
        await self._post_research(out)
        return out

    def _research(self) -> dict:
        from .graph import new_run_id, run_research_session, strands_roles
        from .tools import LabSession
        lab = self._lab()
        runs = Path(lab.ledger.path).parent / "runs"
        session = LabSession(lab, max_rounds=self.s.lab_rounds, max_per_round=self.s.lab_per_round,
                             max_model_calls=self.s.lab_max_model_calls)
        session.run_id = new_run_id()
        roles = strands_roles(session, self.s, memory_dir=runs / "agents")
        res = run_research_session(lab, roles, session, report_dir=runs.parent / "reports", state_path=runs / "current.json")
        s = res["session"]
        return {"status": res["status"], "report_path": res.get("report_path"),
                "evaluated": len(s.get("evaluated", [])), "candidates": sum(1 for r in s.get("evaluated", []) if r["verdict"] == "candidate"),
                "confirmed": [c.get("proposal_id") for c in s.get("confirmations", []) if c.get("pass")],
                "result": next((ln for ln in (res.get("report") or "").splitlines() if ln.startswith("**Result")), "")}

    async def _post_research(self, out: dict) -> None:
        if not self.s.rd_webhook_url:
            return
        lines = []
        r = out.get("refresh")
        if r:
            lines.append(f"Dataset refresh: +{r.get('added_sessions', 0)} sessions (data now ends {r.get('last_session')}).")
        rr = out.get("research") or {}
        if rr.get("skipped"):
            lines.append(f"Agent research: skipped - {rr['skipped']}.")
        elif rr:
            lines.append(f"Agent research: {rr.get('evaluated', 0)} hypotheses backtested, {rr.get('candidates', 0)} passed the "
                         f"search window, {len(rr.get('confirmed') or [])} confirmed on the holdout.")
            if rr.get("result"):
                lines.append(rr["result"].replace("**", ""))
            if rr.get("confirmed"):
                lines.append("A proposal is waiting for you: `abg lab proposals`, then approve or reject. Nothing changed yet.")
        if not lines:
            return
        body = {"username": "ABG R&D", "allowed_mentions": {"parse": []},
                "embeds": [{"title": "🔬 R&D weekly research run", "description": "\n".join(f"• {x}" for x in lines)[:3900],
                            "color": 0xF59E0B, "footer": {"text": "ABG Strategy Lab · agents propose, code decides, you approve"}}]}
        url = self.s.rd_webhook_url
        try:
            await self.http.request("POST", url + ("&" if "?" in url else "?") + "wait=true", provider="discord", json=body)
        except Exception as e:
            self.errors.append(f"research post: {type(e).__name__}")
