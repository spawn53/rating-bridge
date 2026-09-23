"""Deterministic in-memory providers for Rating Hub worker tests."""
from collections import defaultdict
from typing import Any

from hub.providers.base import ProviderError, ProviderNotConfigured, UnsupportedDelivery


class FakeProviders:
    def __init__(self, outcomes: dict[str, list[str]] | None = None):
        self.outcomes = outcomes or {}
        self.calls: list[dict[str, Any]] = []
        self.counts: dict[str, int] = defaultdict(int)

    def __call__(self, name: str) -> 'FakeProvider':
        if self.outcomes.get(name, ['success'])[0] == 'not_configured':
            raise ProviderNotConfigured('fake provider unavailable')
        return FakeProvider(name, self)


class FakeProvider:
    def __init__(self, name: str, harness: FakeProviders):
        self.name = name
        self.harness = harness

    def deliver(self, action: str, payload: dict[str, Any]) -> None:
        h = self.harness
        h.counts[self.name] += 1
        h.calls.append({
            'provider': self.name, 'action': action,
            'content_key': payload['content_key'],
            'revision': payload['revision'], 'rating': payload['rating'],
            'delivery_count': h.counts[self.name],
        })
        outcomes = h.outcomes.get(self.name, ['success'])
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if outcome == 'transient':
            raise ProviderError('temporary fake failure')
        if outcome == 'permanent' or outcome == 'unsupported':
            raise UnsupportedDelivery('permanent fake failure')
        if outcome == 'unexpected':
            raise RuntimeError('sensitive-value-must-not-be-recorded')
        if outcome != 'success':
            raise AssertionError(f'unknown fake outcome: {outcome}')