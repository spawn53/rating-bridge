from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable, Mapping


def targets_excluding_source(targets: Iterable[str], source: str) -> tuple[str, ...]:
    return tuple(target for target in targets if target != source)


@dataclass(frozen=True)
class Classification:
    kind: str
    reason: str
    future_action: str | None = None


def classify(
    content_key: str,
    observed_rating: int | None,
    canonical: Mapping[str, object] | None,
    jobs: Iterable[Mapping[str, object]] = (),
) -> Classification:
    """Conservative hints for a future importer, never proof of user intent."""
    relevant = [j for j in jobs if j.get("target") == "trakt" and j.get("content_key") == content_key]
    if any(j.get("status") in {"pending", "processing", "failed"} for j in relevant):
        return Classification("defer", "trakt_outbound_unsettled")
    if observed_rating is not None and (
        type(observed_rating) is not int or not 1 <= observed_rating <= 10
    ):
        return Classification("defer", "invalid_observed_rating")
    active = canonical is not None and canonical.get("deleted") == 0
    if canonical is not None and canonical.get("deleted") not in (0, 1):
        return Classification("defer", "invalid_canonical_state")
    score = canonical.get("rating") if active else None
    if active and (type(score) is not int or not 1 <= score <= 10):
        return Classification("defer", "invalid_canonical_rating")
    if observed_rating is None and not active:
        reason = "removal_no_canonical_state" if canonical is None else "both_unrated"
        return Classification("noop", reason)
    if active and observed_rating == score:
        return Classification("echo", "same_as_canonical")
    completed = [j for j in relevant if j.get("status") == "done"]
    if completed:
        try:
            if any(type(j.get("revision")) is not int or type(j.get("id")) is not int for j in completed):
                raise ValueError()
            latest = max(completed, key=lambda j: (j["revision"], j["id"]))
            if latest.get("action") == "remove":
                outbound = None
            elif latest.get("action") == "upsert":
                payload = json.loads(latest["payload_json"])
                outbound = payload["rating"]
                if type(outbound) is not int or not 1 <= outbound <= 10:
                    raise ValueError()
            else:
                raise ValueError()
            if observed_rating == outbound:
                return Classification("echo", "matches_latest_completed_trakt_job")
        except (ValueError, TypeError, KeyError):
            return Classification("defer", "invalid_outbound_audit")
    return Classification("candidate", "different_provider_state",
                          "delete" if observed_rating is None else "upsert")
