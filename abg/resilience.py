"""Resilience primitives: retry with jittered backoff, circuit breaker, token-bucket
rate limiter and a rolling health tracker.

These are what keep the terminal from crashing or hanging when a data vendor is
slow, rate-limits us, or goes down:

* ``retry_async``   – retries *retryable* errors a small number of times.
* ``CircuitBreaker``– after N consecutive failures a provider is skipped for a
                      cool-down period instead of burning latency on it every call.
* ``RateLimiter``   – spaces requests to respect free-tier quotas proactively.
* ``HealthStats``   – EWMA latency + success rate used by the router to reorder
                      providers dynamically.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

from .errors import ProviderError, RateLimited

T = TypeVar("T")


# --------------------------------------------------------------------------- retry
async def retry_async(fn: Callable[[], Awaitable[T]], *, attempts: int = 2, base_delay: float = 0.25,
                      max_delay: float = 4.0) -> T:
    """Call ``fn`` up to ``attempts`` times, retrying only ``ProviderError.retryable`` errors.

    Uses "full jitter" exponential backoff (AWS architecture blog) so parallel callers
    don't retry in lock-step.  ``RateLimited.retry_after`` is honoured when small; a long
    Retry-After is surfaced immediately so the router can fail over instead of waiting.
    """
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return await fn()
        except ProviderError as e:
            last = e
            if not e.retryable or i == attempts - 1:
                raise
            if isinstance(e, RateLimited) and e.retry_after is not None:
                if e.retry_after > max_delay:
                    raise
                delay = e.retry_after
            else:
                delay = random.uniform(0, min(max_delay, base_delay * (2 ** i)))
            await asyncio.sleep(delay)
    raise last  # pragma: no cover


# --------------------------------------------------------------------------- circuit breaker
class CircuitBreaker:
    """Classic three-state breaker.

    CLOSED    -> calls flow; consecutive failures counted.
    OPEN      -> calls rejected until ``cooldown`` elapses.
    HALF_OPEN -> exactly one probe call allowed; success closes, failure re-opens
                 (with the cooldown doubled, capped at 16x, so a dead vendor costs ~nothing).
    """

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, failure_threshold: int = 3, cooldown: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.failure_threshold = failure_threshold
        self.base_cooldown = cooldown
        self.cooldown = cooldown
        self._clock = clock
        self.state = self.CLOSED
        self.failures = 0
        self.opened_at = 0.0
        self._probe_in_flight = False

    def allow(self) -> bool:
        if self.state == self.CLOSED:
            return True
        if self.state == self.OPEN and self._clock() - self.opened_at >= self.cooldown:
            self.state = self.HALF_OPEN
            self._probe_in_flight = False
        if self.state == self.HALF_OPEN and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self.state = self.CLOSED
        self.failures = 0
        self.cooldown = self.base_cooldown
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == self.HALF_OPEN:
            self.cooldown = min(self.cooldown * 2, self.base_cooldown * 16)
            self._trip()
        elif self.failures >= self.failure_threshold:
            self._trip()

    def release_probe(self) -> None:
        """A half-open probe was cancelled (e.g. lost a hedge race) - allow another probe."""
        self._probe_in_flight = False

    def trip(self, cooldown: float | None = None) -> None:
        """Force the breaker open (e.g. after an auth error or a long Retry-After)."""
        if cooldown is not None:
            self.cooldown = max(self.cooldown, cooldown)
        self._trip()

    def _trip(self) -> None:
        self.state = self.OPEN
        self.opened_at = self._clock()
        self._probe_in_flight = False

    def seconds_until_retry(self) -> float:
        if self.state != self.OPEN:
            return 0.0
        return max(0.0, self.cooldown - (self._clock() - self.opened_at))

    def snapshot(self) -> dict:
        return {"state": self.state, "consecutive_failures": self.failures,
                "retry_in_s": round(self.seconds_until_retry(), 1)}


# --------------------------------------------------------------------------- rate limiter
class RateLimiter:
    """Async token bucket.  ``rate`` tokens per ``per`` seconds, burst up to ``rate``.

    ``acquire(max_wait)`` raises RateLimited instead of waiting longer than ``max_wait``
    so a heavily-throttled provider is failed-over rather than stalling the request.
    """

    def __init__(self, rate: float, per: float = 1.0, provider: str = "?", clock: Callable[[], float] = time.monotonic):
        self.capacity = float(rate)
        self.fill_rate = float(rate) / float(per)
        self.tokens = float(rate)
        self.provider = provider
        self._clock = clock
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.fill_rate)
        self._last = now

    async def acquire(self, max_wait: float = 2.0) -> None:
        async with self._lock:
            self._refill()
            if self.tokens >= 1:
                self.tokens -= 1
                return
            wait = (1 - self.tokens) / self.fill_rate
            if wait > max_wait:
                err = RateLimited(f"local quota exhausted, next slot in {wait:.1f}s",
                                  provider=self.provider, retry_after=wait)
                err.retryable = False               # fail over now rather than sleep
                err.counts_against_health = False   # our own budget, not the vendor's fault
                raise err
            await asyncio.sleep(wait)
            self._refill()
            self.tokens = max(0.0, self.tokens - 1)


# --------------------------------------------------------------------------- health
@dataclass
class HealthStats:
    """Rolling provider health: EWMA latency and success ratio (alpha=0.3)."""

    alpha: float = 0.3
    calls: int = 0
    successes: int = 0
    failures: int = 0
    ewma_latency_ms: float | None = None
    success_rate: float = 1.0
    last_error: str | None = None
    last_success_at: float | None = None
    history: list[str] = field(default_factory=list)

    def record(self, ok: bool, latency_ms: float, error: str | None = None) -> None:
        self.calls += 1
        if ok:
            self.successes += 1
            self.last_success_at = time.time()
            self.ewma_latency_ms = latency_ms if self.ewma_latency_ms is None else \
                self.alpha * latency_ms + (1 - self.alpha) * self.ewma_latency_ms
        else:
            self.failures += 1
            self.last_error = error
        self.success_rate = self.alpha * (1.0 if ok else 0.0) + (1 - self.alpha) * self.success_rate

    @property
    def degraded(self) -> bool:
        return self.calls >= 4 and self.success_rate < 0.5

    def snapshot(self) -> dict:
        return {"calls": self.calls, "successes": self.successes, "failures": self.failures,
                "success_rate": round(self.success_rate, 3),
                "ewma_latency_ms": None if self.ewma_latency_ms is None else round(self.ewma_latency_ms, 1),
                "degraded": self.degraded, "last_error": self.last_error}
