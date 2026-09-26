"""Repair one known historical-echo misclassification. No canonical/provider writes."""
from __future__ import annotations

import re

from hub.inbound.classification import classify
from hub.inbound.models import InboundError, timestamp
from hub.inbound.storage import InboundStore

OLD = ("ignored", "echo", "matches_latest_completed_trakt_job", None)
NEW = ("observed", "candidate", "different_provider_state", "delete")
AUDIT_FIELDS = ("status", "classification", "reason", "future_action")


def reclassify_event(store: InboundStore, *, event_id: int, expected_key: str,
                     expected_generation: int, expected_event_type: str,
                     expected_old_rating: int, expected_revision: int,
                     confirmed: bool = False) -> dict:
    """Validate and update exactly four event audit fields in one write transaction.

    Only ignored historical-outbound echoes of movie removals are supported.
    A previously repaired event safely returns without any UPDATE after the same
    snapshot/canonical/classification checks; other event shapes fail closed.
    """
    if confirmed is not True:
        raise InboundError("Event reclassification requires --confirm-reclassification")
    if (type(event_id) is not int or event_id < 1
            or not isinstance(expected_key, str)
            or not re.fullmatch(r"movie:tmdb:[1-9][0-9]*", expected_key)
            or type(expected_generation) is not int or expected_generation < 1
            or expected_event_type != "removed"
            or type(expected_old_rating) is not int or not 1 <= expected_old_rating <= 10
            or type(expected_revision) is not int or expected_revision < 1):
        raise InboundError("Event reclassification expectations were invalid")
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM inbound_events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise InboundError("Event reclassification target was not found")
        event = dict(row)
        if (event["provider"] != "trakt" or event["media_type"] != "movie"
                or event["content_key"] != expected_key or event["generation"] != expected_generation
                or event["event_type"] != expected_event_type or event["old_rating"] != expected_old_rating
                or event["new_rating"] is not None or event["applied_at"] is not None
                or event["canonical_revision"] is not None
                or not isinstance(event["fingerprint"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", event["fingerprint"])):
            raise InboundError("Event reclassification target did not match the guarded transition")
        timestamp(event["provider_rated_at"])
        timestamp(event["detected_at"])
        before = tuple(event[field] for field in AUDIT_FIELDS)
        if before not in (OLD, NEW):
            raise InboundError("Event is not the known false historical echo or its repaired state")
        state = conn.execute(
            "SELECT generation FROM inbound_state WHERE provider='trakt' AND media_type='movie'"
        ).fetchone()
        present = conn.execute(
            "SELECT 1 FROM inbound_snapshots WHERE provider='trakt' AND media_type='movie' AND content_key=?",
            (expected_key,),
        ).fetchone()
        if state is None or state["generation"] != expected_generation or present is not None:
            raise InboundError("Removal reclassification no longer matches the trusted snapshot")
        newer = conn.execute("""
            SELECT 1 FROM inbound_events
            WHERE provider='trakt' AND media_type='movie' AND content_key=?
                AND (generation>? OR (generation=? AND id>?)) LIMIT 1
        """, (expected_key, expected_generation, expected_generation, event_id)).fetchone()
        if newer is not None:
            raise InboundError("A newer inbound event supersedes the reclassification target")
        canonical, jobs = store._context(conn, expected_key)
        if (canonical is None or canonical["media_type"] != "movie"
                or canonical["tmdb_id"] != int(expected_key.rsplit(":", 1)[1])
                or canonical["deleted"] != 0 or canonical["rating"] != expected_old_rating
                or canonical["revision"] != expected_revision):
            raise InboundError("Canonical state changed; event reclassification refused")
        decision = classify(expected_key, None, canonical, jobs)
        if (decision.kind, decision.reason, decision.future_action) != NEW[1:]:
            raise InboundError("Recomputed classification does not permit candidate removal repair")
        # The exact old signature must have a demonstrably historical completed
        # Trakt removal as its false-match evidence, not merely a similar reason.
        if not any(j["revision"] < expected_revision and j["status"] == "done"
                   and j["action"] == "remove" for j in jobs):
            raise InboundError("Historical completed Trakt removal evidence was not found")
        already = before == NEW
        if not already:
            changed = conn.execute("""
                UPDATE inbound_events SET status=?,classification=?,reason=?,future_action=?
                WHERE id=? AND status=? AND classification=? AND reason=? AND future_action IS NULL
            """, (*NEW, event_id, *OLD[:3]))
            if changed.rowcount != 1:
                raise InboundError("Event changed concurrently; reclassification refused")
    return {"event_id": event_id, "content_key": expected_key, "generation": expected_generation,
            "old": dict(zip(AUDIT_FIELDS, before)), "new": dict(zip(AUDIT_FIELDS, NEW)),
            "already_reclassified": already, "canonical_mutations": 0, "outbox_mutations": 0,
            "provider_writes": 0}
