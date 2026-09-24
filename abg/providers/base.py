"""Provider plug-in contract.

To add a new data source:

1. Subclass :class:`Provider`, set ``name``, ``capabilities`` and (optionally)
   ``requires_key`` / ``key_setting`` / ``rate_limit``.
2. Implement the ``get_*`` coroutines for the capabilities you declared and return the
   models from :mod:`abg.models`.  Raise ``NoDataError`` / ``AuthError`` / ``RateLimited``
   (or let :class:`abg.http.HttpClient` raise them) - never return ``None``.
3. Register it with ``@register_provider`` (built-ins) or pass an instance to
   ``AnalysisEngine(providers=[...])``, or expose it through the ``abg.providers``
   entry-point group from another package.

The router handles caching, retries, rate limiting, circuit breaking and fail-over;
adapters only translate payloads.
"""
from __future__ import annotations

from abc import ABC
from datetime import date
from typing import TYPE_CHECKING, ClassVar

from ..errors import NotSupported
from ..models import Capability, Fundamentals, NewsItem, OptionChain, PriceHistory, Quote

if TYPE_CHECKING:
    from ..config import Settings
    from ..http import HttpClient

PROVIDER_CLASSES: dict[str, type["Provider"]] = {}


def register_provider(cls: type["Provider"]) -> type["Provider"]:
    PROVIDER_CLASSES[cls.name] = cls
    return cls


class Provider(ABC):
    name: ClassVar[str] = "base"
    label: ClassVar[str] = "Base"
    capabilities: ClassVar[frozenset[Capability]] = frozenset()
    requires_key: ClassVar[bool] = False
    key_setting: ClassVar[str | None] = None          # attribute on Settings holding the key
    rate_limit: ClassVar[tuple[float, float]] = (10, 1.0)   # (calls, per_seconds)
    intraday: ClassVar[bool] = False                  # supports intraday intervals
    notes: ClassVar[str] = ""

    def __init__(self, settings: "Settings", http: "HttpClient"):
        self.settings = settings
        self.http = http

    # ------------------------------------------------------------------ config
    @property
    def api_key(self) -> str | None:
        return getattr(self.settings, self.key_setting, None) if self.key_setting else None

    def is_configured(self) -> bool:
        return (not self.requires_key) or bool(self.api_key)

    def supports(self, cap: Capability, interval: str = "1d") -> bool:
        if cap not in self.capabilities:
            return False
        if cap == Capability.HISTORY and interval not in ("1d", "1wk", "1mo") and not self.intraday:
            return False
        return True

    def map_symbol(self, symbol: str) -> str:
        """Translate a canonical ticker (e.g. 'BRK.B') into this vendor's format."""
        return symbol

    # ------------------------------------------------------------------ capabilities
    async def get_history(self, symbol: str, start: date, end: date, interval: str = "1d") -> PriceHistory:
        raise NotSupported("history not supported", provider=self.name)

    async def get_quote(self, symbol: str) -> Quote:
        raise NotSupported("quote not supported", provider=self.name)

    async def get_news(self, symbol: str, limit: int = 20) -> list[NewsItem]:
        raise NotSupported("news not supported", provider=self.name)

    async def get_options(self, symbol: str, expiry: date | None = None) -> OptionChain:
        raise NotSupported("options not supported", provider=self.name)

    async def get_fundamentals(self, symbol: str) -> Fundamentals:
        raise NotSupported("fundamentals not supported", provider=self.name)

    async def aclose(self) -> None:  # override if the adapter holds resources
        return None

    def describe(self) -> dict:
        return {"name": self.name, "label": self.label, "configured": self.is_configured(),
                "requires_key": self.requires_key, "key_env": f"ABG_{self.key_setting.upper()}" if self.key_setting else None,
                "capabilities": sorted(c.value for c in self.capabilities), "intraday": self.intraday,
                "rate_limit": f"{self.rate_limit[0]:g}/{self.rate_limit[1]:g}s", "notes": self.notes}
