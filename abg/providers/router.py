"""The provider router - the heart of the multi-source data layer.

For every request (capability + symbol + params) the router:

1. **Cache**      returns a fresh cached value immediately if one exists.
2. **Coalesce**   if the identical request is already in flight, awaits that one
                  instead of issuing a duplicate ("single-flight").
3. **Rank**       builds the candidate list: providers that are configured, support
                  the capability/interval, in the configured priority order, with
                  *degraded* providers (low rolling success rate) pushed to the back.
4. **Hedge**      starts the first candidate; if it hasn't answered within
                  ``hedge_delay`` seconds, starts the next one in parallel and takes
                  whichever succeeds first (tail-latency cut).  Failures immediately
                  launch the next candidate.
5. **Protect**    each attempt passes a per-provider token bucket, circuit breaker,
                  timeout and small retry loop; outcomes feed health stats.
6. **Validate**   results are checked (e.g. enough bars); bad data counts as a failure.
7. **Degrade**    if every provider fails, serves a *stale* cached copy (flagged) up to
                  ``max_stale`` old; only if that is missing does it raise
                  ``AllProvidersFailed`` with a per-provider error list.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Awaitable, Callable, TypeVar

from ..cache import TieredCache
from ..config import Settings
from ..errors import (AllProvidersFailed, AuthError, CircuitOpenError, ConfigError, DataValidationError,
                      ProviderError, ProviderTimeout, RateLimited)
from ..http import HttpClient
from ..models import Capability, Fetched, Provenance
from ..resilience import CircuitBreaker, HealthStats, RateLimiter, retry_async
from .base import PROVIDER_CLASSES, Provider

log = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class ProviderSlot:
    provider: Provider
    breaker: CircuitBreaker
    limiter: RateLimiter
    health: HealthStats
    rank: int

    @property
    def name(self) -> str:
        return self.provider.name


def load_entry_point_providers() -> None:
    """Import third-party providers advertised under the ``abg.providers`` entry-point group."""
    try:
        eps = entry_points(group="abg.providers")
    except TypeError:  # pragma: no cover - py<3.10 API
        eps = entry_points().get("abg.providers", [])
    for ep in eps:
        try:
            cls = ep.load()
            PROVIDER_CLASSES.setdefault(cls.name, cls)
        except Exception as e:  # never let a broken plug-in take the app down
            log.warning("failed to load provider plug-in %s: %s", ep.name, e)


def build_providers(settings: Settings, http: HttpClient) -> list[Provider]:
    from . import free, keyed, local, yahoo  # noqa: F401  (registers built-ins)
    load_entry_point_providers()
    return [cls(settings, http) for cls in PROVIDER_CLASSES.values()]


class ProviderRouter:
    def __init__(self, providers: list[Provider], settings: Settings, cache: TieredCache):
        self.settings = settings
        self.cache = cache
        # ABG_PROVIDER_ORDER is both priority *and* allow-list: providers not named there are
        # never used automatically (they can still be forced with source=...).
        order = settings.provider_list()
        disabled = {d.strip().lower() for d in settings.disabled_providers.split(",") if d.strip()}
        rank = {n: i for i, n in enumerate(order)}
        self.slots: dict[str, ProviderSlot] = {}
        for p in providers:
            if p.name in disabled:
                continue
            calls, per = p.rate_limit
            self.slots[p.name] = ProviderSlot(
                provider=p,
                breaker=CircuitBreaker(settings.breaker_failures, settings.breaker_cooldown),
                limiter=RateLimiter(calls, per, provider=p.name),
                health=HealthStats(),
                rank=rank.get(p.name, 999))
        self._inflight: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ candidates
    def candidates(self, cap: Capability, interval: str = "1d", only: str | None = None) -> list[ProviderSlot]:
        if only:
            slot = self.slots.get(only.lower())
            if slot is None:
                raise ConfigError(f"Unknown or disabled provider '{only}'. Known: {', '.join(sorted(self.slots))}")
            if not slot.provider.is_configured():
                env = slot.provider.describe().get("key_env") or "its dependency"
                raise ConfigError(f"Provider '{only}' is not configured (set {env}).")
            if not slot.provider.supports(cap, interval):
                raise ConfigError(f"Provider '{only}' does not support {cap.value} at interval {interval}.")
            return [slot]
        cap_order = self.settings.capability_order(cap.value)
        if cap_order:                       # per-capability priority/allow-list overrides the global one
            rank = {n: i for i, n in enumerate(cap_order)}
            c = [s for s in self.slots.values()
                 if s.name in rank and s.provider.is_configured() and s.provider.supports(cap, interval)]
            return sorted(c, key=lambda s: (s.name == "synthetic", s.health.degraded, rank[s.name]))
        c = [s for s in self.slots.values()
             if s.rank < 999 and s.provider.is_configured() and s.provider.supports(cap, interval)]
        # synthetic is always the absolute last resort
        return sorted(c, key=lambda s: (s.name == "synthetic", s.health.degraded, s.rank))

    def _usable(self, provider: str, only: str | None) -> bool:
        """Cached data is only served if its provider is still allowed (e.g. synthetic data
        cached during a --demo run must never leak into a normal run)."""
        slot = self.slots.get(provider)
        if slot is None:
            return provider not in ("synthetic",)       # e.g. data from a since-removed plug-in
        allowed = slot.rank < 999 or only == provider or any(
            provider in (self.settings.capability_order(c.value) or []) for c in Capability)
        return slot.provider.is_configured() and allowed

    # ------------------------------------------------------------------ public API
    async def fetch(self, cap: Capability, symbol: str, call: Callable[[Provider], Awaitable[T]], *,
                    key: str, ttl: float, interval: str = "1d", only: str | None = None,
                    validate: Callable[[T], None] | None = None, use_cache: bool = True,
                    persist: bool = True) -> Fetched[T]:
        cache_key = f"{cap.value}:{symbol}:{key}:{only or '*'}"
        if use_cache:
            hit = self.cache.get(cache_key, ttl)
            if hit is not None and hit.fresh and self._usable(hit.value[1], only):
                value, provider = hit.value
                return Fetched(value, Provenance(cap.value, symbol, provider, 0.0, "fresh", round(hit.age, 1)))

        task = self._inflight.get(cache_key)
        if task is not None:
            res = await asyncio.shield(task)
            return Fetched(res.value, Provenance(cap.value, symbol, res.provenance.provider, 0.0, "coalesced"))

        task = asyncio.ensure_future(self._fetch_uncached(cap, symbol, call, cache_key, interval, only,
                                                          validate, persist))
        self._inflight[cache_key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._inflight.pop(cache_key, None)
            else:  # caller was cancelled; let the shared task finish then clean up
                task.add_done_callback(lambda _t: self._inflight.pop(cache_key, None))

    async def _fetch_uncached(self, cap, symbol, call, cache_key, interval, only, validate, persist) -> Fetched:
        t0 = time.perf_counter()
        cands = self.candidates(cap, interval, only)
        try:
            value, slot, attempts = await self._race(cap, symbol, cands, call, validate)
        except AllProvidersFailed as e:
            hit = self.cache.get(cache_key, ttl=0, max_stale=self.settings.max_stale)
            if hit is not None and self._usable(hit.value[1], only):
                value, provider = hit.value
                log.warning("serving stale %s for %s (%.0fs old): %s", cap.value, symbol, hit.age, e.message)
                return Fetched(value, Provenance(cap.value, symbol, provider, round((time.perf_counter() - t0) * 1000, 1),
                                                 "stale", round(hit.age, 1), e.errors))
            raise
        self.cache.set(cache_key, (value, slot.name), persist=persist)
        return Fetched(value, Provenance(cap.value, symbol, slot.name,
                                         round((time.perf_counter() - t0) * 1000, 1), "miss", 0.0, attempts))

    # ------------------------------------------------------------------ hedged race
    async def _race(self, cap, symbol, cands: list[ProviderSlot], call, validate):
        attempts: list[dict] = []
        pending: dict[asyncio.Task, ProviderSlot] = {}
        queue = list(cands)
        hedge = self.settings.hedge_delay

        def launch(hedging: bool = False) -> bool:
            while queue:
                if hedging and queue[0].name == "synthetic":
                    return False                   # never hedge real data with simulated data
                slot = queue.pop(0)
                if not slot.breaker.allow():
                    attempts.append({"provider": slot.name, "error": f"circuit open ({slot.breaker.seconds_until_retry():.0f}s)",
                                     "code": CircuitOpenError.code})
                    continue
                pending[asyncio.ensure_future(self._attempt(slot, call, validate))] = slot
                return True
            return False

        launch()
        try:
            while pending:
                timeout = hedge if (hedge and hedge > 0 and queue) else None
                done, _ = await asyncio.wait(list(pending), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:                       # slow provider -> hedge with the next one
                    if not launch(hedging=True):
                        hedge = 0                  # nothing left to hedge with; just wait
                    continue
                for t in done:
                    slot = pending.pop(t)
                    exc = t.exception()
                    if exc is None:
                        return t.result(), slot, attempts
                    attempts.append({"provider": slot.name, "error": getattr(exc, "message", str(exc))[:200],
                                     "code": getattr(exc, "code", type(exc).__name__)})
                    launch()                       # replace the failed attempt immediately
        finally:
            for t in pending:
                t.cancel()
        raise AllProvidersFailed(cap.value, symbol, attempts)

    async def _attempt(self, slot: ProviderSlot, call, validate):
        p = slot.provider
        call_timeout = getattr(p, "call_timeout", None) or self.settings.http_timeout * 1.5
        t0 = time.perf_counter()

        async def once():
            await slot.limiter.acquire(max_wait=1.0)
            try:
                return await asyncio.wait_for(call(p), timeout=call_timeout)
            except asyncio.TimeoutError:
                raise ProviderTimeout(f"no answer within {call_timeout:.1f}s", provider=p.name) from None

        try:
            value = await retry_async(once, attempts=1 + self.settings.max_retries,
                                      base_delay=self.settings.retry_base_delay)
            if validate is not None:
                validate(value)
        except asyncio.CancelledError:
            slot.breaker.release_probe()
            raise
        except ProviderError as e:
            self._record_failure(slot, e, t0)
            raise
        except DataValidationError as e:
            err = ProviderError(f"invalid data: {e.message}", provider=p.name)
            self._record_failure(slot, err, t0)
            raise err from None
        except Exception as e:  # adapter bug / unexpected payload shape: contain it
            log.exception("provider %s raised unexpectedly", p.name)
            err = ProviderError(f"{type(e).__name__}: {str(e)[:160]}", provider=p.name)
            err.retryable = False
            self._record_failure(slot, err, t0)
            raise err from None
        latency = (time.perf_counter() - t0) * 1000
        slot.breaker.record_success()
        slot.health.record(True, latency)
        return value

    def _record_failure(self, slot: ProviderSlot, e: ProviderError, t0: float) -> None:
        latency = (time.perf_counter() - t0) * 1000
        if not e.counts_against_health:
            if slot.breaker.state == CircuitBreaker.HALF_OPEN:
                if isinstance(e, RateLimited):
                    slot.breaker.release_probe()    # our own quota stopped the probe; try again later
                else:
                    slot.breaker.record_success()   # provider answered sanely (e.g. 404) -> it's alive
            return
        slot.health.record(False, latency, f"{e.code}: {e.message[:120]}")
        if isinstance(e, AuthError):
            slot.breaker.trip(self.settings.breaker_cooldown * 10)
        elif isinstance(e, RateLimited) and e.retry_after:
            slot.breaker.trip(e.retry_after)
        else:
            slot.breaker.record_failure()

    # ------------------------------------------------------------------ introspection
    def status(self) -> list[dict]:
        out = []
        for s in sorted(self.slots.values(), key=lambda s: s.rank):
            d = s.provider.describe()
            d.update(rank=s.rank if s.rank < 999 else None, breaker=s.breaker.snapshot(), health=s.health.snapshot())
            out.append(d)
        return out

    async def probe(self, symbol: str, cap: Capability = Capability.QUOTE) -> list[dict]:
        """Call every configured provider directly (no cache, no failover) and report."""
        from datetime import date, timedelta

        async def one(slot: ProviderSlot) -> dict:
            t0 = time.perf_counter()
            p = slot.provider
            try:
                if cap == Capability.HISTORY:
                    r: Any = await asyncio.wait_for(p.get_history(symbol, date.today() - timedelta(days=30), date.today()), 15)
                    detail = f"{len(r)} bars, last {r.last_close:.2f}"
                elif cap == Capability.NEWS:
                    r = await asyncio.wait_for(p.get_news(symbol, 5), 15)
                    detail = f"{len(r)} articles"
                elif cap == Capability.OPTIONS:
                    r = await asyncio.wait_for(p.get_options(symbol), 20)
                    detail = f"{len(r.contracts)} contracts"
                elif cap == Capability.FUNDAMENTALS:
                    r = await asyncio.wait_for(p.get_fundamentals(symbol), 15)
                    detail = r.name or "ok"
                else:
                    r = await asyncio.wait_for(p.get_quote(symbol), 15)
                    detail = f"price {r.price:.2f}"
                ok = True
            except Exception as e:
                ok, detail = False, f"{getattr(e, 'code', type(e).__name__)}: {getattr(e, 'message', str(e))[:120]}"
            return {"provider": p.name, "ok": ok, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": detail}

        slots = [s for s in self.slots.values() if s.provider.is_configured() and s.provider.supports(cap)]
        return list(await asyncio.gather(*(one(s) for s in slots)))
