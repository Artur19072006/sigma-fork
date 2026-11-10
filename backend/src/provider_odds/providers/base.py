from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from provider_odds.models import ProviderOddsEvent


class ProviderConfigurationError(RuntimeError):
    """Raised when a provider is missing required local configuration."""


class ProviderRequestError(RuntimeError):
    """Raised when a read-only provider request fails."""


class OddsProvider(ABC):
    name: str

    @abstractmethod
    def fetch_events(self, **kwargs: Any) -> Sequence[ProviderOddsEvent]:
        """Fetch and normalize bookmaker odds events from the provider."""

    @abstractmethod
    def normalize_response(
        self, payload: object, **kwargs: Any
    ) -> list[ProviderOddsEvent]:
        """Normalize a provider response payload without making network calls."""
