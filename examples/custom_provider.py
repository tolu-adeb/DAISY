"""Example: adding a new data vendor.

The adapter only translates the vendor's payload into terminal models; caching,
retries, rate-limits, circuit breaking and fail-over come for free from the router.

Use it by passing an instance to the engine (see bottom), or ship it in a package and
expose it through the ``abg.providers`` entry-point group, then add its name to
ABG_PROVIDER_ORDER.
"""
from __future__ import annotations

import asyncio

import pandas as pd

from abg.errors import NoDataError
from abg.models import Capability, PriceHistory, Quote
from abg.providers.base import Provider


class MyVendorProvider(Provider):
    name = "myvendor"
    label = "My Vendor"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTE})
    requires_key = True
    key_setting = "myvendor_api_key"       # read from Settings / env ABG_MYVENDOR_API_KEY
    rate_limit = (5, 1.0)                  # 5 calls per second
    URL = "https://api.myvendor.example/v1"

    @property
    def api_key(self):
        import os
        return os.getenv("ABG_MYVENDOR_API_KEY")

    async def get_history(self, symbol, start, end, interval="1d"):
        rows = await self.http.get_json(f"{self.URL}/bars/{symbol}", provider=self.name,
                                        params={"from": start.isoformat(), "to": end.isoformat(), "key": self.api_key})
        if not rows:
            raise NoDataError("no bars", provider=self.name)
        df = pd.DataFrame(rows).set_index("date")          # expects open/high/low/close/volume columns
        return PriceHistory.from_frame(symbol, df, self.name, interval)

    async def get_quote(self, symbol):
        d = await self.http.get_json(f"{self.URL}/quote/{symbol}", provider=self.name, params={"key": self.api_key})
        return Quote(symbol=symbol, price=d["last"], prev_close=d.get("prev_close"), source=self.name)


async def main():
    from abg import AnalysisEngine, Settings
    from abg.http import HttpClient
    from abg.providers import build_providers

    s = Settings(provider_order="myvendor,yahoo,stooq")
    http = HttpClient()
    providers = build_providers(s, http) + [MyVendorProvider(s, http)]
    async with AnalysisEngine(s, providers=providers, http=http) as eng:
        print(eng.status()["providers"][0])


if __name__ == "__main__":
    asyncio.run(main())
