"""The Strategy Lab and the bot's state over MCP (``abg mcp``), for Claude / Cowork / Claude Code / any MCP client.

Read-only except ``lab_test``, which backtests a hypothesis on the search window and records it in the ledger
(it can't see the holdout, confirm, approve or change the live bot).  Runs over stdio:

    claude mcp add abg-lab -- abg mcp            # from the repo folder, with the venv active
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .tools import bot_recent, insights_digest, lab_baseline, lab_overview, lab_space, lab_trial, lab_trials


def build_server(bars_csv: str | None = None, trend_csv: str | None = None):
    from mcp.server.fastmcp import FastMCP

    from ..config import Settings
    from .lab import Hypothesis, Lab

    settings = Settings()

    @lru_cache(maxsize=1)
    def lab() -> Lab:
        return Lab.from_paths(bars_csv, trend_csv, settings.data_dir, settings.lab_holdout_months)

    mcp = FastMCP("abg-lab")

    @mcp.tool()
    def overview() -> dict:
        """Strategy Lab data windows, validation gate, trials so far and holdout looks used."""
        return lab_overview(lab())

    @mcp.tool()
    def search_space() -> list:
        """Settings a hypothesis may change, with current values and allowed ranges."""
        return lab_space(lab())

    @mcp.tool()
    def baseline() -> dict:
        """The current settings' search-window numbers and four sub-periods."""
        return lab_baseline(lab())

    @mcp.tool()
    def past_trials(limit: int = 40) -> list:
        """Recently tested hypotheses with verdicts."""
        return lab_trials(lab(), limit)

    @mcp.tool()
    def trial_detail(trial_id: int) -> dict:
        """All checks for one trial."""
        return lab_trial(lab(), trial_id)

    @mcp.tool()
    def lab_test(name: str, rationale: str, changes: dict) -> dict:
        """Backtest one hypothesis on the search window (recorded in the ledger).  changes: {setting: value}, 1-4 keys."""
        r = lab().test(Hypothesis(name=name, rationale=rationale, changes=changes), source="mcp")
        return {k: r.get(k) for k in ("trial_id", "name", "changes", "verdict", "failed", "search", "baseline")} | {
            "checks": [{k: c.get(k) for k in ("check", "pass", "detail")} for c in r.get("checks", [])]}

    @mcp.tool()
    def research_log() -> str:
        """docs/16: what helped, what didn't, known traps."""
        return insights_digest(Path.cwd())

    @mcp.tool()
    def bot_state(limit: int = 20) -> dict:
        """The live bot's recent paper trades and adaptive-book scores (read-only)."""
        return bot_recent(settings.data_dir, limit)

    return mcp


def main(bars_csv: str | None = None, trend_csv: str | None = None) -> None:
    build_server(bars_csv, trend_csv).run()
