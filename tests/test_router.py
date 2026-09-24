import asyncio
import time
from datetime import date

import pytest

from abg.errors import AllProvidersFailed, AuthError, ConfigError, NoDataError, ProviderError
from abg.models import Capability
from conftest import FakeProvider


def q(router, sym="AAPL", **kw):
    return router.fetch(Capability.QUOTE, sym, lambda p: p.get_quote(sym), key="q", ttl=kw.pop("ttl", 60), **kw)


async def test_failover_to_second(settings, make_router):
    a = FakeProvider(settings, "a", fail=ProviderError("down", provider="a"))
    b = FakeProvider(settings, "b", price=42)
    r, _ = make_router([a, b])
    f = await q(r)
    assert f.value.price == 42 and f.provenance.provider == "b"
    assert f.provenance.attempts[0]["provider"] == "a"


async def test_cache_hit_and_singleflight(settings, make_router):
    a = FakeProvider(settings, "a", delay=0.05)
    r, _ = make_router([a])
    res = await asyncio.gather(*(q(r) for _ in range(10)))
    assert a.calls == 1                                    # coalesced
    assert {x.provenance.cache for x in res} <= {"miss", "coalesced"}
    f = await q(r)
    assert f.provenance.cache == "fresh" and a.calls == 1


async def test_hedging_cuts_tail_latency(settings, make_router):
    slow = FakeProvider(settings, "slow", delay=2.0, price=1)
    fast = FakeProvider(settings, "fast", delay=0.01, price=2)
    r, _ = make_router([slow, fast], hedge_delay=0.1)
    t = time.perf_counter()
    f = await q(r)
    assert f.provenance.provider == "fast"
    assert time.perf_counter() - t < 0.6


async def test_synthetic_never_used_as_hedge(settings, make_router):
    slow = FakeProvider(settings, "real", delay=0.4, price=1)
    synth = FakeProvider(settings, "synthetic", price=2)
    r, _ = make_router([slow, synth], hedge_delay=0.05)
    f = await q(r)
    assert f.provenance.provider == "real" and synth.calls == 0


async def test_stale_if_error(settings, make_router):
    a = FakeProvider(settings, "a", price=7)
    r, s = make_router([a])
    await q(r, ttl=60)
    a.fail = ProviderError("down", provider="a")
    f = await q(r, ttl=0)                                  # force refetch -> fails -> stale served
    assert f.provenance.cache == "stale" and f.value.price == 7


async def test_all_failed_raises_with_details(settings, make_router):
    a = FakeProvider(settings, "a", fail=AuthError("bad key", provider="a"))
    b = FakeProvider(settings, "b", fail=NoDataError("unknown", provider="b"))
    r, _ = make_router([a, b])
    with pytest.raises(AllProvidersFailed) as ei:
        await q(r)
    assert {e["provider"] for e in ei.value.errors} == {"a", "b"}
    assert r.slots["a"].breaker.state == "open"           # auth error trips immediately
    assert r.slots["b"].breaker.state == "closed"         # 404-style says nothing about health


async def test_breaker_skips_dead_provider(settings, make_router):
    a = FakeProvider(settings, "a", fail=ProviderError("down", provider="a"))
    b = FakeProvider(settings, "b")
    r, _ = make_router([a, b], breaker_failures=2)
    for i in range(3):
        await r.fetch(Capability.QUOTE, "X", lambda p: p.get_quote("X"), key=str(i), ttl=0)
    assert a.calls == 2                                    # third call skipped by open circuit


async def test_validation_failure_counts_as_provider_failure(settings, make_router):
    a = FakeProvider(settings, "a", bars=1)
    b = FakeProvider(settings, "b", bars=50)
    r, _ = make_router([a, b])

    def validate(ph):
        from abg.errors import DataValidationError
        if len(ph) < 2:
            raise DataValidationError("too short", provider=ph.source)
    f = await r.fetch(Capability.HISTORY, "X", lambda p: p.get_history("X", date(2024, 1, 1), date(2024, 6, 1)),
                      key="h", ttl=60, validate=validate)
    assert f.provenance.provider == "b"


async def test_adapter_bug_is_contained(settings, make_router):
    a = FakeProvider(settings, "a", fail=KeyError("unexpected payload"))
    b = FakeProvider(settings, "b")
    r, _ = make_router([a, b])
    assert (await q(r)).provenance.provider == "b"


async def test_forced_source_errors(settings, make_router):
    a = FakeProvider(settings, "a")
    r, _ = make_router([a])
    with pytest.raises(ConfigError):
        r.candidates(Capability.QUOTE, only="nope")
    assert [s.name for s in r.candidates(Capability.QUOTE, only="a")] == ["a"]


async def test_provider_order_is_allow_list(settings, make_router, tmp_path):
    from abg.cache import TieredCache
    from abg.config import Settings
    from abg.providers.router import ProviderRouter
    a, b = FakeProvider(settings, "a"), FakeProvider(settings, "b")
    s = Settings(provider_order="b", cache_dir=tmp_path, _env_file=None)
    r = ProviderRouter([a, b], s, TieredCache(tmp_path))
    assert [x.name for x in r.candidates(Capability.QUOTE)] == ["b"]


async def test_cached_data_from_disallowed_provider_is_not_served(settings, tmp_path):
    """Synthetic data cached in a --demo run must never leak into a normal run."""
    from abg.cache import TieredCache
    from abg.config import Settings
    from abg.providers.local import SyntheticProvider
    from abg.providers.router import ProviderRouter
    cache = TieredCache(tmp_path)
    demo = Settings(allow_synthetic=True, provider_order="synthetic", cache_dir=tmp_path, _env_file=None)
    r1 = ProviderRouter([SyntheticProvider(demo, None)], demo, cache)
    await r1.fetch(Capability.QUOTE, "AAPL", lambda p: p.get_quote("AAPL"), key="q", ttl=600)
    normal = Settings(allow_synthetic=False, provider_order="synthetic", cache_dir=tmp_path, _env_file=None)
    r2 = ProviderRouter([SyntheticProvider(normal, None)], normal, cache)
    with pytest.raises(AllProvidersFailed):
        await r2.fetch(Capability.QUOTE, "AAPL", lambda p: p.get_quote("AAPL"), key="q", ttl=600)
