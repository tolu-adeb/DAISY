"""Data-provider layer: adapters for each vendor plus the fail-over router."""
from .base import PROVIDER_CLASSES, Provider, register_provider
from .router import ProviderRouter, build_providers

__all__ = ["Provider", "ProviderRouter", "PROVIDER_CLASSES", "build_providers", "register_provider"]
