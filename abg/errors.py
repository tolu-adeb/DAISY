"""Exception hierarchy.

Every failure inside the terminal is expressed as a subclass of :class:`ABGError`
so that callers (CLI, API, engine) can catch one base type and never crash on an
unexpected third-party exception.  Provider errors carry a ``retryable`` flag and a
``counts_against_health`` flag that drive the retry loop and circuit breakers.
"""
from __future__ import annotations


class ABGError(Exception):
    """Base class for all terminal errors."""

    code = "abg_error"

    def __init__(self, message: str = "", **context):
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **{k: str(v) for k, v in self.context.items()}}


class ConfigError(ABGError):
    code = "config_error"


class InvalidSymbolError(ABGError):
    code = "invalid_symbol"


class DataValidationError(ABGError):
    """Data came back but failed sanity checks (empty, NaN-filled, negative prices...)."""

    code = "data_validation"


# --------------------------------------------------------------------------- providers
class ProviderError(ABGError):
    """A data provider failed.

    retryable              -> the same provider may succeed if called again (timeouts, 5xx)
    counts_against_health  -> failure should feed the circuit breaker (False for e.g. 404s,
                              which say nothing about the provider's health)
    """

    code = "provider_error"
    retryable = True
    counts_against_health = True

    def __init__(self, message: str = "", provider: str = "?", **context):
        super().__init__(message, provider=provider, **context)
        self.provider = provider

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"[{self.provider}] {self.message}"


class ProviderTimeout(ProviderError):
    code = "provider_timeout"


class RateLimited(ProviderError):
    code = "rate_limited"

    def __init__(self, message: str = "", provider: str = "?", retry_after: float | None = None, **context):
        super().__init__(message, provider=provider, **context)
        self.retry_after = retry_after


class AuthError(ProviderError):
    """Bad / missing / insufficient-tier API key.  Retrying will not help."""

    code = "auth_error"
    retryable = False


class NotSupported(ProviderError):
    """Provider does not implement this capability (or not on the configured plan)."""

    code = "not_supported"
    retryable = False
    counts_against_health = False


class NoDataError(ProviderError):
    """Provider is healthy but has no data for this symbol / range."""

    code = "no_data"
    retryable = False
    counts_against_health = False


class CircuitOpenError(ProviderError):
    code = "circuit_open"
    retryable = False
    counts_against_health = False


class AllProvidersFailed(ABGError):
    """Raised by the router when every candidate provider failed and no stale cache exists."""

    code = "all_providers_failed"

    def __init__(self, capability: str, symbol: str, errors: list[dict]):
        detail = "; ".join(f"{e['provider']}: {e['error']}" for e in errors) or \
            "no enabled provider supports this - check API keys, ABG_PROVIDER_ORDER, or use --demo"
        super().__init__(f"No provider could serve {capability} for {symbol} ({detail})")
        self.capability = capability
        self.symbol = symbol
        self.errors = errors

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "capability": self.capability,
                "symbol": self.symbol, "attempts": self.errors}
