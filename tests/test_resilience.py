import asyncio
import time

import pytest

from abg.cache import TieredCache
from abg.errors import AuthError, ProviderError, RateLimited
from abg.resilience import CircuitBreaker, HealthStats, RateLimiter, retry_async


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_breaker_lifecycle():
    c = Clock()
    b = CircuitBreaker(failure_threshold=2, cooldown=10, clock=c)
    assert b.allow()
    b.record_failure()
    assert b.state == b.CLOSED
    b.record_failure()
    assert b.state == b.OPEN and not b.allow()
    c.t = 10
    assert b.allow() and b.state == b.HALF_OPEN
    assert not b.allow()                       # only one probe
    b.record_failure()                          # failed probe -> reopen, cooldown doubles
    assert b.state == b.OPEN and b.cooldown == 20
    c.t = 30
    assert b.allow()
    b.record_success()
    assert b.state == b.CLOSED and b.cooldown == 10


async def test_retry_only_retryable():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderError("boom", provider="x")
        return "ok"
    assert await retry_async(flaky, attempts=3, base_delay=0) == "ok"

    calls["n"] = 0

    async def auth():
        calls["n"] += 1
        raise AuthError("bad key", provider="x")
    with pytest.raises(AuthError):
        await retry_async(auth, attempts=3, base_delay=0)
    assert calls["n"] == 1


async def test_retry_gives_up_on_long_retry_after():
    async def limited():
        raise RateLimited("slow down", provider="x", retry_after=60)
    t = time.perf_counter()
    with pytest.raises(RateLimited):
        await retry_async(limited, attempts=3, max_delay=1)
    assert time.perf_counter() - t < 0.5


async def test_rate_limiter_fails_fast_when_exhausted():
    rl = RateLimiter(2, per=60, provider="x")
    await rl.acquire()
    await rl.acquire()
    with pytest.raises(RateLimited) as ei:
        await rl.acquire(max_wait=0.1)
    assert ei.value.counts_against_health is False


def test_health_degrades():
    h = HealthStats()
    for _ in range(5):
        h.record(False, 10, "x")
    assert h.degraded
    for _ in range(10):
        h.record(True, 10)
    assert not h.degraded


def test_tiered_cache_fresh_stale_and_persistence(tmp_path):
    c = TieredCache(tmp_path, 8)
    c.set("k", {"v": 1})
    assert c.get("k", ttl=10).fresh
    c.memory._data["k"] = (time.time() - 100, {"v": 1})
    c.disk.set("k", {"v": 1}, stored_at=time.time() - 100)
    assert c.get("k", ttl=10) is None
    hit = c.get("k", ttl=10, max_stale=1000)
    assert hit and not hit.fresh
    c.close()
    c2 = TieredCache(tmp_path, 8)                  # survives restart via SQLite
    assert c2.get("k", ttl=1000).value == {"v": 1}
    assert c2.clear("k") >= 1 and c2.get("k", 1000) is None


def test_cache_survives_corrupt_file(tmp_path):
    (tmp_path / "cache.sqlite3").write_bytes(b"not a database at all" * 100)
    c = TieredCache(tmp_path, 8)
    c.set("a", 1)                                  # must not raise
    assert c.get("a", 10).value == 1
