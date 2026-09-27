"""Explicit manual episode baseline/observation; no import or scheduling route."""
from __future__ import annotations

import argparse
import math
import re
import time
from typing import Callable

import httpx

from hub.inbound.episode_models import EpisodeSnapshot, normalize_episode
from hub.inbound.episode_storage import EpisodeStore
from hub.inbound.models import InboundError


def fetch_episode_snapshot(provider: object, client: httpx.Client, timeout: float = 60,
                           *, clock: Callable[[], float] | None = None) -> EpisodeSnapshot:
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise InboundError("Trakt episode snapshot deadline was invalid")
    clock = clock or time.monotonic
    deadline = clock() + timeout
    eligible, unmapped = [], []
    page_number, observed, totals = 1, 0, None
    while True:
        try:
            headers = provider.headers
            remaining = deadline - clock()
            if remaining <= 0:
                raise InboundError("Trakt episode snapshot deadline exhausted")
            response = client.get("https://api.trakt.tv/users/me/ratings/episodes",
                                  headers=headers,
                                  params={"page": str(page_number), "limit": "250"},
                                  timeout=min(10.0, remaining), follow_redirects=False)
            if clock() > deadline:
                raise InboundError("Trakt episode snapshot deadline exhausted")
            if not response.is_success or response.is_redirect:
                raise InboundError("Trakt episode snapshot read failed")
            items = response.json()
            values = []
            for name in ("Page", "Page-Count", "Limit", "Item-Count"):
                raw = response.headers.get("X-Pagination-" + name)
                if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
                    raise InboundError("Trakt episode pagination headers were invalid")
                values.append(int(raw))
            page, pages, limit, count = values
            if (not isinstance(items, list) or page != page_number
                    or not 0 <= pages <= 1000 or not 1 <= limit <= 250 or len(items) > limit
                    or pages == 0 and (page != 1 or items or count != 0)
                    or pages > 0 and not 1 <= page <= pages):
                raise InboundError("Trakt episode pagination was inconsistent")
            if totals is not None and totals != (pages, limit, count):
                raise InboundError("Trakt episode pagination changed during read")
            totals = (pages, limit, count)
            for item in items:
                rating = normalize_episode(item)
                (eligible if rating.content_key is not None else unmapped).append(rating)
            observed += len(items)
            if observed > count:
                raise InboundError("Trakt episode item count was inconsistent")
            if pages == 0 or page >= pages:
                if observed != count:
                    raise InboundError("Trakt episode item count was inconsistent")
                snapshot = EpisodeSnapshot(tuple(eligible), tuple(unmapped))
                if clock() > deadline:
                    raise InboundError("Trakt episode snapshot deadline exhausted")
                return snapshot
            page_number += 1
        except InboundError:
            raise
        except Exception:
            raise InboundError("Trakt episode snapshot could not be verified") from None


def observe_episode(store: EpisodeStore, read: Callable[[], EpisodeSnapshot], *,
                    baseline: bool = False, reset: bool = False) -> dict:
    if not isinstance(store, EpisodeStore):
        raise InboundError("Episode observation requires an episode audit store")
    if reset and not baseline:
        raise InboundError("Reset requires explicit episode baseline mode")
    previous = store.state()
    if baseline and previous is not None and not reset:
        raise InboundError("Episode baseline already exists; use explicit reset")
    if not baseline and previous is None:
        raise InboundError("Episode baseline is missing")
    snapshot = read()
    return store.publish(snapshot, expected_generation=previous["generation"] if previous else None,
                         baseline=baseline, reset=reset)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual Trakt episode observation only")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--baseline", action="store_true")
    mode.add_argument("--once", action="store_true")
    # No default production DB or automatic configuration. The operator must
    # select an audit DB explicitly. Unsupported write/scheduler flags fail here.
    parser.add_argument("--db", required=True)
    parser.add_argument("--observe-only", action="store_true")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args(argv)
    if (args.once and (not args.observe_only or args.reset)
            or args.baseline and args.observe_only or args.reset and not args.baseline):
        print("Episode observer refused: use --baseline [--reset] or --once --observe-only")
        return 2
    try:
        from hub.providers.registry import get_provider
        store = EpisodeStore(args.db)
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            result = observe_episode(store, lambda: fetch_episode_snapshot(get_provider("trakt"), client),
                                     baseline=args.baseline, reset=args.reset)
        print("Trakt episode baseline created" if args.baseline else "Trakt episode observation complete")
        for key in ("episodes", "eligible", "skipped", "added", "changed", "removed", "events",
                    "generation", "snapshot_changed", "auto_apply_enabled", "canonical_mutations",
                    "outbox_mutations", "provider_writes"):
            value = result[key]
            print(f"{key}={str(value).lower() if type(value) is bool else value}")
        return 0
    except Exception:
        print("Trakt episode observation failed; no canonical/outbox/provider writes authorized")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
