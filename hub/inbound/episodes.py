"""Explicit manual episode observation and guarded import; no scheduling route."""
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
    parser = argparse.ArgumentParser(
        description="Manual Trakt episode observation and guarded import"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--baseline", action="store_true")
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--apply-event", type=int)
    mode.add_argument("--apply-removal-event", type=int)
    # No default production DB or automatic configuration. The operator must
    # select an audit DB explicitly. Unsupported write/scheduler flags fail here.
    parser.add_argument("--db", required=True)
    parser.add_argument("--observe-only", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--expect-content-key")
    parser.add_argument("--expect-rating", type=int)
    parser.add_argument("--expect-old-rating", type=int)
    parser.add_argument("--expect-generation", type=int)
    parser.add_argument("--expect-canonical-revision", type=int)
    parser.add_argument("--expect-canonical-source")
    parser.add_argument("--confirm-live-import", action="store_true")
    args = parser.parse_args(argv)
    applying = args.apply_event is not None
    removing = args.apply_removal_event is not None
    guards = (args.expect_content_key, args.expect_rating, args.expect_old_rating,
              args.expect_generation, args.expect_canonical_revision,
              args.expect_canonical_source)
    if applying:
        required = (args.expect_content_key, args.expect_rating,
                    args.expect_generation, args.expect_canonical_revision)
        invalid = (not args.confirm_live_import or any(value is None for value in required)
                   or args.expect_old_rating is not None
                   or args.expect_canonical_source is not None
                   or args.observe_only or args.reset)
    elif removing:
        required = (args.expect_content_key, args.expect_old_rating, args.expect_generation,
                    args.expect_canonical_revision, args.expect_canonical_source)
        invalid = (not args.confirm_live_import or any(value is None for value in required)
                   or args.expect_rating is not None or args.observe_only or args.reset)
    else:
        invalid = (any(value is not None for value in guards) or args.confirm_live_import
                   or args.once and (not args.observe_only or args.reset)
                   or args.baseline and args.observe_only or args.reset and not args.baseline)
    if invalid:
        print("Episode observer refused: use --baseline [--reset] or --once --observe-only")
        return 2
    try:
        store = EpisodeStore(args.db, initialize=not (applying or removing))
        if applying:
            from hub.inbound.episode_importer import (
                EPISODE_SOURCE_TARGETS, apply_episode_event,
            )
            result = apply_episode_event(
                store, EPISODE_SOURCE_TARGETS, event_id=args.apply_event,
                expected_key=args.expect_content_key, expected_rating=args.expect_rating,
                expected_generation=args.expect_generation,
                expected_revision=args.expect_canonical_revision,
                confirmed=args.confirm_live_import,
            )
            print("Trakt episode single-event import complete")
            for key in ("event_id", "content_key", "rating", "revision", "queued_targets",
                        "skipped_targets", "already_applied", "direct_provider_writes"):
                print(f"{key}={result[key]}")
            return 0
        if removing:
            from hub.inbound.episode_importer import (
                EPISODE_SOURCE_TARGETS, apply_episode_removal,
            )
            result = apply_episode_removal(
                store, EPISODE_SOURCE_TARGETS, event_id=args.apply_removal_event,
                expected_key=args.expect_content_key,
                expected_old_rating=args.expect_old_rating,
                expected_generation=args.expect_generation,
                expected_revision=args.expect_canonical_revision,
                expected_source=args.expect_canonical_source,
                confirmed=args.confirm_live_import,
            )
            print("Trakt episode single-event removal import complete")
            for key in ("event_id", "content_key", "revision", "removed",
                        "queued_targets", "skipped_targets", "already_applied",
                        "direct_provider_writes"):
                print(f"{key}={result[key]}")
            return 0
        from hub.providers.registry import get_provider
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
        if applying or removing:
            print("Trakt episode manual import failed; audit database state before retry; "
                  "no provider writes attempted")
        else:
            print("Trakt episode observation failed; no canonical/outbox/provider writes authorized")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
