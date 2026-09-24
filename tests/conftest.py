"""Shared fixtures: an offline Settings object and scriptable fake providers."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from abg.cache import TieredCache
from abg.config import Settings
from abg.errors import AuthError, NoDataError, ProviderError
from abg.models import Capability, PriceHistory, Quote
from abg.providers.base import Provider
from abg.providers.local import synthetic_frame


@pytest.fixture
def settings(tmp_path):
    return Settings(allow_synthetic=True, cache_dir=tmp_path / "cache", data_dir=tmp_path / "data", provider_order="synthetic",
                    hedge_delay=0.0, ai_enabled=False, max_retries=0, anthropic_api_key=None, _env_file=None)


@pytest.fixture
def ohlcv() -> pd.DataFrame:
    return synthetic_frame("TEST", date(2021, 1, 1), date(2024, 6, 30))


class FakeProvider(Provider):
    """Behaviour scripted per test: delay, failure type, call counting."""

    capabilities = frozenset({Capability.HISTORY, Capability.QUOTE})

    def __init__(self, settings, name, *, delay=0.0, fail: Exception | None = None, price=100.0, bars=300):
        super().__init__(settings, http=None)
        self.name = name  # instance attribute shadows ClassVar
        self.label = name
        self.delay, self.fail, self.price, self.bars = delay, fail, price, bars
        self.calls = 0

    async def get_quote(self, symbol):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return Quote(symbol=symbol, price=self.price, source=self.name, prev_close=self.price * 0.99)

    async def get_history(self, symbol, start, end, interval="1d"):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        idx = pd.bdate_range(end=end, periods=self.bars)
        c = self.price * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, len(idx))))
        df = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6}, index=idx)
        return PriceHistory.from_frame(symbol, df, self.name, interval)


@pytest.fixture
def make_router(tmp_path):
    from abg.providers.router import ProviderRouter

    def _make(providers, **kw):
        s = Settings(provider_order=",".join(p.name for p in providers), cache_dir=tmp_path / "c",
                     max_retries=0, _env_file=None, **{"hedge_delay": 0.0, **kw})
        for p in providers:
            p.settings = s
        return ProviderRouter(providers, s, TieredCache(tmp_path / "c", 64, True)), s
    return _make


__all__ = ["FakeProvider", "AuthError", "NoDataError", "ProviderError"]
