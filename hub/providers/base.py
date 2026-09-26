from __future__ import annotations

from typing import Any, Protocol


class ProviderError(RuntimeError):
    pass


class ProviderNotConfigured(ProviderError):
    pass


class UnsupportedDelivery(ProviderError):
    pass


class RatingProvider(Protocol):
    name: str

    def deliver(self, action: str, payload: dict[str, Any]) -> None:
        ...
