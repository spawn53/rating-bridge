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
    """Conservative revision-scoped hints, never proof of user intent."""
    relevant = [j for j in jobs if j.get("target") == "trakt" and j.get("content_key") == content_key]
    if observed_rating is not None and (
        type(observed_rating) is not int or not 1 <= observed_rating <= 10
    ):
        return Classification("defer", "invalid_observed_rating")
    if canonical is not None:
        if type(canonical.get("deleted")) is not int or canonical["deleted"] not in (0, 1):
            return Classification("defer", "invalid_canonical_state")
        revision = canonical.get("revision")
        if type(revision) is not int or revision < 1:
            return Classification("defer", "invalid_canonical_revision")
    else:
        revision = None
    active = canonical is not None and canonical["deleted"] == 0
    score = canonical.get("rating") if active else None
    if active and (type(score) is not int or not 1 <= score <= 10):
        return Classification("defer", "invalid_canonical_rating")
    if any(type(j.get("revision")) is not int or j["revision"] < 1 for j in relevant):
        return Classification("defer", "invalid_outbound_audit")
    # Without canonical state there is no trustworthy causal revision anchor.
    if relevant and revision is None:
        if observed_rating is None:
            return Classification("noop", "removal_no_canonical_state")
        return Classification("defer", "invalid_outbound_audit")
    if any(j["revision"] > revision for j in relevant):
        return Classification("defer", "trakt_outbound_audit_ahead")
    current = [j for j in relevant if j["revision"] == revision]
    # Stale rows, including stale unresolved jobs, are history only. Multiple
    # current rows are ambiguous (the outbox normally permits one per action).
    if len(current) > 1:
        return Classification("defer", "invalid_outbound_audit")
    completed_state = None
    has_completed = False
    if current:
        job = current[0]
        if (type(job.get("id")) is not int or job["id"] < 1
                or job.get("status") not in {"pending", "processing", "failed", "done", "superseded"}):
            return Classification("defer", "invalid_outbound_audit")
        if job["status"] in {"pending", "processing", "failed"}:
            return Classification("defer", "trakt_outbound_unsettled")
        if job["status"] == "done":
            try:
                if job.get("action") == "remove":
                    completed_state = None
                elif job.get("action") == "upsert":
                    payload = json.loads(job["payload_json"])
                    completed_state = payload["rating"]
                    if type(completed_state) is not int or not 1 <= completed_state <= 10:
                        raise ValueError()
                else:
                    raise ValueError()
                has_completed = True
            except (ValueError, TypeError, KeyError):
                return Classification("defer", "invalid_outbound_audit")
    if observed_rating is None and not active:
        reason = "removal_no_canonical_state" if canonical is None else "both_unrated"
        return Classification("noop", reason)
    if active and observed_rating == score:
        return Classification("echo", "same_as_canonical")
    if has_completed and observed_rating == completed_state:
        return Classification("echo", "matches_latest_completed_trakt_job")
    return Classification("candidate", "different_provider_state",
                          "delete" if observed_rating is None else "upsert")
