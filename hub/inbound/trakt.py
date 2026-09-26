"""Manual movie-only baseline/observer. No import mode or scheduling entrypoint."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
import os
import re
import time
from typing import Callable

import httpx

from hub.inbound.models import InboundError, Snapshot, normalize
from hub.inbound.storage import InboundStore


@dataclass(frozen=True)
class InboundSettings:
    enabled: bool = False
    media_types: tuple[str, ...] = ("movie",)
    poll_seconds: int = 300

    @classmethod
    def from_env(cls) -> "InboundSettings":
        enabled = os.getenv("TRAKT_INBOUND_ENABLED", "false").strip().lower()
        media = tuple(x.strip() for x in os.getenv("TRAKT_INBOUND_MEDIA_TYPES", "movie").split(","))
        try:
            interval = int(os.getenv("TRAKT_INBOUND_POLL_SECONDS", "300"))
        except ValueError:
            raise InboundError("Trakt inbound polling interval was invalid") from None
        if enabled not in {"false", "true"} or media != ("movie",) or interval < 1:
            raise InboundError("Trakt inbound settings must remain movie-only with a valid interval")
        return cls(enabled == "true", media, interval)


def fetch_snapshot(provider: object, client: httpx.Client, timeout: float = 60,
                   *, clock: Callable[[], float] | None = None) -> Snapshot:
    clock = clock or time.monotonic
    if not math.isfinite(timeout) or timeout <= 0:
        raise InboundError("Trakt snapshot deadline was invalid")
    deadline = clock() + timeout
    eligible, unmapped = [], []
    page_number = 1
    totals = None
    observed = 0
    while True:
        try:
            headers = provider.headers  # Existing OAuth supplier/refresh lifecycle.
            remaining = deadline - clock()
            if remaining <= 0:
                raise InboundError("Trakt snapshot deadline exhausted")
            response = client.get(
                "https://api.trakt.tv/users/me/ratings/movies",
                headers=headers, params={"page": str(page_number), "limit": "250"},
                timeout=min(10.0, remaining),
            )
            if clock() > deadline:
                raise InboundError("Trakt snapshot deadline exhausted")
            if not response.is_success:
                raise InboundError("Trakt snapshot read failed")
            items = response.json()
            values = []
            for name in ("Page", "Page-Count", "Limit", "Item-Count"):
                raw = response.headers.get("X-Pagination-" + name)
                if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
                    raise InboundError("Trakt snapshot pagination headers were invalid")
                values.append(int(raw))
            page, pages, limit, count = values
            if (not isinstance(items, list) or page != page_number
                    or not 0 <= pages <= 1000 or not 1 <= limit <= 250
                    or len(items) > limit
                    or (pages == 0 and (page != 1 or items or count != 0))
                    or (pages > 0 and not 1 <= page <= pages)):
                raise InboundError("Trakt snapshot pagination was inconsistent")
            current_totals = (pages, limit, count)
            if totals is not None and current_totals != totals:
                raise InboundError("Trakt snapshot pagination changed during read")
            totals = current_totals
            for item in items:
                rating = normalize(item)
                (eligible if rating.tmdb_id is not None else unmapped).append(rating)
            observed += len(items)
            if observed > count:
                raise InboundError("Trakt snapshot item count was inconsistent")
            if pages == 0 or page >= pages:
                if observed != count:
                    raise InboundError("Trakt snapshot item count was inconsistent")
                snapshot = Snapshot(tuple(eligible), tuple(unmapped))
                if clock() > deadline:
                    raise InboundError("Trakt snapshot deadline exhausted")
                return snapshot
            page_number += 1
        except InboundError:
            raise
        except Exception:
            # HTTP errors can contain credential-bearing URLs and raw responses.
            raise InboundError("Trakt snapshot could not be verified") from None


def observe(store: InboundStore, read: Callable[[], Snapshot], *,
            baseline: bool = False, reset: bool = False) -> dict:
    if reset and not baseline:
        raise InboundError("Reset requires explicit baseline mode")
    previous = store.state()
    if baseline and previous is not None and not reset:
        raise InboundError("Trakt baseline already exists; use --baseline --reset explicitly")
    if not baseline and previous is None:
        raise InboundError("Trakt baseline is missing; run --baseline first")
    snapshot = read()
    return store.publish(snapshot, expected_generation=previous["generation"] if previous else None,
                         baseline=baseline, reset=reset)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trakt inbound observer: no canonical imports")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--baseline", action="store_true")
    mode.add_argument("--once", action="store_true")
    parser.add_argument("--observe-only", action="store_true")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args(argv)
    if (args.once and (not args.observe_only or args.reset)
            or args.baseline and args.observe_only or args.reset and not args.baseline):
        print("Trakt observer refused: use --baseline [--reset] or --once --observe-only")
        return 2
    try:
        InboundSettings.from_env()
        # The explicit manual modes remain available while automatic inbound is disabled.
        from hub.providers.registry import get_provider
        from hub.settings import HubSettings
        store = InboundStore(HubSettings.from_env().db_path)
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            result = observe(store, lambda: fetch_snapshot(get_provider("trakt"), client),
                             baseline=args.baseline, reset=args.reset)
        print("Trakt inbound baseline created" if args.baseline else "Trakt inbound observation complete")
        keys = ("movies", "eligible", "skipped", "snapshot_hash", "events") if args.baseline else (
            "added", "changed", "removed", "deferred"
        )
        for key in (*keys, "canonical_mutations", "provider_writes"):
            print(f"{key}={result[key]}")
        return 0
    except InboundError as exc:
        # Only this module's fixed local messages may reach operator output.
        print(str(exc))
        return 1
    except Exception:
        print("Trakt inbound observation failed; trusted snapshot retained")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
