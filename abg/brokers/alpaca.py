"""Alpaca paper-trading bridge: mirrors tracked-idea decisions as real paper orders.

Off unless ``ABG_BROKER=alpaca_paper`` and keys are set.  It always talks to
https://paper-api.alpaca.markets unless ``ABG_ALPACA_LIVE=true`` is set explicitly (don't, until the
paper results are proven).  Stocks, ETFs and crypto only; futures ideas are skipped with a note.

    entry / scale_in       market order for the filled units + a GTC protective stop for the position
    stop_moved             protective stop replaced at the new level
    target_hit / trim      market order for that slice; stop re-sized for what's left
    stop_hit / exit / …    cancel the stop, close whatever is left

Every order uses a client_order_id "abg-<idea>-<event>" so a restart never double-sends.
"""
from __future__ import annotations

import logging
import math

from ..errors import ABGError

log = logging.getLogger(__name__)
PAPER = "https://paper-api.alpaca.markets"
LIVE = "https://api.alpaca.markets"
EXITS = {"stop_hit", "breakeven_stop", "trailing_stop", "time_exit", "exit", "cancelled"}


def _q(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.6f}".rstrip("0")


class AlpacaBroker:
    name = "alpaca"

    def __init__(self, settings, http):
        self.s, self.http = settings, http
        self.base = LIVE if settings.alpaca_live else PAPER
        self.enabled = settings.broker == "alpaca_paper" and bool(settings.alpaca_key_id and settings.alpaca_secret_key)

    @property
    def _h(self) -> dict:
        return {"APCA-API-KEY-ID": self.s.alpaca_key_id, "APCA-API-SECRET-KEY": self.s.alpaca_secret_key}

    async def account(self) -> dict:
        return await self.http.get_json(f"{self.base}/v2/account", provider="alpaca", headers=self._h)

    def _symbol(self, idea) -> str | None:
        cls = (idea.meta.get("instrument") or {}).get("asset_class", "stock")
        if cls in ("stock", "etf", "bond_etf"):
            return idea.symbol.replace("-", ".")
        if cls == "crypto" and idea.symbol.endswith("-USD"):
            return idea.symbol[:-4] + "/USD"
        return None

    async def _order(self, body: dict) -> dict:
        return await self.http.post_json(f"{self.base}/v2/orders", provider="alpaca", json=body, headers=self._h)

    async def _cancel_stop(self, idea) -> None:
        oid = idea.flags.get("broker_stop_id")
        if oid:
            try:
                await self.http.request("DELETE", f"{self.base}/v2/orders/{oid}", provider="alpaca", headers=self._h)
            except ABGError:
                pass
            idea.flags.pop("broker_stop_id", None)

    async def _place_stop(self, idea, qty: float, sym: str) -> None:
        if qty <= 0 or idea.stop is None:
            return
        side = "sell" if idea.long else "buy"
        o = await self._order({"symbol": sym, "qty": _q(qty), "side": side, "type": "stop",
                               "stop_price": f"{idea.stop:.2f}", "time_in_force": "gtc",
                               "client_order_id": f"abg-{idea.id}-stop-{idea.stop:.2f}-{qty}"[:48]})
        idea.flags["broker_stop_id"] = o.get("id")

    def _qty(self, idea, frac: float, sym: str) -> float:
        q = idea.shares * frac
        return round(q, 6) if "/" in sym else float(math.floor(q + 1e-9))

    async def on_event(self, idea, ev: dict) -> str:
        """Mirror one tracker event.  Returns a short status for the event's relay log."""
        if not self.enabled:
            return "off"
        sym = self._symbol(idea)
        if sym is None:
            return "skipped: futures/other instruments aren't supported by Alpaca"
        k = ev["type"]
        try:
            held = self._qty(idea, idea.remaining, sym)
            if k in ("entry", "scale_in"):
                frac = sum(x["frac"] for x in ev.get("fills") or []) or ev.get("fraction") or 1.0
                qty = self._qty(idea, frac, sym)
                if qty <= 0:
                    return "skipped: size rounds to 0"
                await self._order({"symbol": sym, "qty": _q(qty), "side": "buy" if idea.long else "sell",
                                   "type": "market", "time_in_force": "gtc" if "/" in sym else "day",
                                   "client_order_id": f"abg-{idea.id}-{k}-{ev.get('ts', 0):.0f}"[:48]})
                await self._cancel_stop(idea)
                await self._place_stop(idea, held, sym)
                return f"ok: {qty:g} {sym}"
            if k == "stop_moved" and idea.status == "active":
                await self._cancel_stop(idea)
                await self._place_stop(idea, held, sym)
                return "ok: stop replaced"
            if k in ("target_hit", "trim"):
                qty = self._qty(idea, ev.get("fraction") or 0, sym)
                await self._cancel_stop(idea)
                if qty > 0:
                    await self._order({"symbol": sym, "qty": _q(qty), "side": "sell" if idea.long else "buy",
                                       "type": "market", "time_in_force": "gtc" if "/" in sym else "day",
                                       "client_order_id": f"abg-{idea.id}-{k}-{ev.get('target_index', '')}-{ev.get('ts', 0):.0f}"[:48]})
                if idea.status == "active":
                    await self._place_stop(idea, held, sym)
                return f"ok: sold {qty:g}"
            if k in EXITS:
                await self._cancel_stop(idea)
                if k != "cancelled" or idea.entry_price is not None:
                    try:
                        await self.http.request("DELETE", f"{self.base}/v2/positions/{sym.replace('/', '')}",
                                                provider="alpaca", headers=self._h)
                    except ABGError as e:
                        if "404" not in e.message:
                            raise
                return "ok: position closed"
        except ABGError as e:
            log.warning("alpaca %s for #%s failed: %s", k, idea.id, e.message)
            return f"error: {e.message[:120]}"
        return "n/a"
