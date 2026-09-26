"""Explicit single-event Trakt removal import. Provider delivery uses the outbox."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sqlite3
from typing import Iterable

from hub.inbound.classification import classify, targets_excluding_source
from hub.inbound.importer import GLOBAL_TARGETS, TARGETS
from hub.inbound.models import InboundError, timestamp
from hub.inbound.storage import InboundStore
from hub.store import RatingStore


def _event(conn: sqlite3.Connection, event_id: int, key: str, generation: int,
           old_rating: int, revision: int) -> dict:
    row = conn.execute("SELECT * FROM inbound_events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise InboundError("Removal event was not found")
    event = dict(row)
    if (event["provider"] != "trakt" or event["media_type"] != "movie"
            or event["event_type"] != "removed" or event["content_key"] != key
            or event["generation"] != generation or event["old_rating"] != old_rating
            or event["new_rating"] is not None or event["status"] not in {"observed", "applied"}
            or event["classification"] != "candidate" or event["reason"] != "different_provider_state"
            or event["future_action"] != "delete"
            or not isinstance(event["fingerprint"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", event["fingerprint"])):
        raise InboundError("Removal event did not match the guarded transition")
    timestamp(event["provider_rated_at"])
    timestamp(event["detected_at"])
    if event["status"] == "observed":
        if event["applied_at"] is not None or event["canonical_revision"] is not None:
            raise InboundError("Observed removal event has inconsistent applied audit")
    else:
        if event["canonical_revision"] != revision + 1:
            raise InboundError("Applied removal revision did not match the expectation")
        timestamp(event["applied_at"])
    return event


def _absence(conn: sqlite3.Connection, event: dict) -> None:
    state = conn.execute(
        "SELECT generation FROM inbound_state WHERE provider='trakt' AND media_type='movie'"
    ).fetchone()
    present = conn.execute(
        "SELECT 1 FROM inbound_snapshots WHERE provider='trakt' AND media_type='movie' AND content_key=?",
        (event["content_key"],),
    ).fetchone()
    if state is None or state[0] != event["generation"] or present is not None:
        raise InboundError("Removal no longer matches trusted snapshot absence and generation")
    newer = conn.execute("""
        SELECT 1 FROM inbound_events WHERE provider='trakt' AND media_type='movie' AND content_key=?
        AND (generation>? OR (generation=? AND id>?)) LIMIT 1
    """, (event["content_key"], event["generation"], event["generation"], event["id"])).fetchone()
    if newer is not None:
        raise InboundError("A newer inbound event supersedes this removal")


def _verify_committed(conn: sqlite3.Connection, event: dict, expected_revision: int) -> dict:
    canonical, trakt_jobs = InboundStore._context(conn, event["content_key"])
    if (canonical is None or canonical["deleted"] != 1 or canonical["rating"] is not None
            or canonical["revision"] != expected_revision + 1
            or canonical["source"] != f"trakt-inbound:{event['id']}"
            or canonical["media_type"] != "movie"
            or canonical["tmdb_id"] != int(event["content_key"].rsplit(":", 1)[1])
            or timestamp(canonical["rated_at"]) != timestamp(event["provider_rated_at"])):
        raise InboundError("Canonical tombstone does not match this committed removal")
    timestamp(canonical["updated_at"])
    if classify(event["content_key"], None, canonical, trakt_jobs).kind == "defer":
        raise InboundError("Trakt outbound audit is inconsistent with committed removal")
    jobs = [dict(r) for r in conn.execute(
        "SELECT * FROM outbox WHERE content_key=? AND revision=?",
        (event["content_key"], canonical["revision"]),
    )]
    try:
        valid = (len(jobs) == 3 and {j["target"] for j in jobs} == set(TARGETS)
                 and all(j["action"] == "remove" and json.loads(j["payload_json"]) == canonical for j in jobs))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise InboundError("Committed removal audit does not match exactly three tombstone jobs")
    return canonical


def _mark_applied(store: InboundStore, *, event_id: int, key: str, generation: int,
                  old_rating: int, expected_revision: int, fingerprint: str) -> int:
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        event = _event(conn, event_id, key, generation, old_rating, expected_revision)
        if event["fingerprint"] != fingerprint:
            raise InboundError("Removal fingerprint changed before audit completion")
        _absence(conn, event)
        canonical = _verify_committed(conn, event, expected_revision)
        if event["status"] != "applied":
            changed = conn.execute("""
                UPDATE inbound_events SET status='applied',applied_at=?,canonical_revision=?
                WHERE id=? AND status='observed'
            """, (datetime.now(timezone.utc).isoformat(), canonical["revision"], event_id))
            if changed.rowcount != 1:
                raise InboundError("Removal event changed before audit completion")
        return canonical["revision"]


def apply_removal_event(store: InboundStore, targets: Iterable[str], *, event_id: int,
                        expected_key: str, expected_generation: int, expected_old_rating: int,
                        expected_revision: int, expected_source: str,
                        confirmed: bool = False) -> dict:
    """Validate and delete under one write lock; recover only exact committed audit.

    Event marking follows canonical/outbox commit. A process exit at that boundary
    can be recovered without another delete or duplicate delivery. Applied events
    return their original audit and never replay against later canonical state.
    """
    if confirmed is not True:
        raise InboundError("Removal import requires --confirm-live-import")
    if (type(event_id) is not int or event_id < 1
            or not isinstance(expected_key, str) or not re.fullmatch(r"movie:tmdb:[1-9][0-9]*", expected_key)
            or type(expected_generation) is not int or expected_generation < 1
            or type(expected_old_rating) is not int or not 1 <= expected_old_rating <= 10
            or type(expected_revision) is not int or expected_revision < 1
            or not isinstance(expected_source, str) or not expected_source.strip()
            or expected_source == f"trakt-inbound:{event_id}"):
        raise InboundError("Removal import expectations were invalid")
    targets = tuple(targets)
    derived = targets_excluding_source(targets, "trakt")
    if targets != GLOBAL_TARGETS or derived != TARGETS:
        raise InboundError("Removal requires the unchanged four targets and exact source exclusion")
    canonical_store = RatingStore(store.path)
    replayed = False
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        event = _event(conn, event_id, expected_key, expected_generation, expected_old_rating, expected_revision)
        if event["status"] == "applied":
            return {"event_id": event_id, "content_key": expected_key, "revision": event["canonical_revision"],
                    "removed": True, "queued_targets": [], "skipped_targets": ["trakt"],
                    "already_applied": True, "direct_provider_writes": 0}
        _absence(conn, event)
        canonical, jobs = store._context(conn, expected_key)
        if canonical is not None and canonical["source"] == f"trakt-inbound:{event_id}":
            canonical = _verify_committed(conn, event, expected_revision)
            revision, queued, replayed = canonical["revision"], [], True
        else:
            if (canonical is None or canonical["media_type"] != "movie"
                    or canonical["tmdb_id"] != int(expected_key.rsplit(":", 1)[1])
                    or canonical["rating"] != expected_old_rating or canonical["deleted"] != 0
                    or canonical["revision"] != expected_revision or canonical["source"] != expected_source
                    or timestamp(canonical["rated_at"]) != timestamp(event["provider_rated_at"])):
                raise InboundError("Canonical state does not match the guarded active removal source")
            decision = classify(expected_key, None, canonical, jobs)
            if (decision.kind, decision.reason, decision.future_action) != (
                    "candidate", "different_provider_state", "delete"):
                raise InboundError("Current canonical/outbox classification refuses removal import")
            result = canonical_store.delete_rating(expected_key, derived,
                                                   source=f"trakt-inbound:{event_id}", connection=conn)
            revision, queued = result["revision"], result["queued_targets"]
            if not result["removed"] or revision != expected_revision + 1 or tuple(queued) != TARGETS:
                raise InboundError("Removal did not produce exactly one revision and three jobs")
            _verify_committed(conn, event, expected_revision)
    marked = _mark_applied(store, event_id=event_id, key=expected_key, generation=expected_generation,
                           old_rating=expected_old_rating, expected_revision=expected_revision,
                           fingerprint=event["fingerprint"])
    return {"event_id": event_id, "content_key": expected_key, "revision": marked, "removed": True,
            "queued_targets": queued, "skipped_targets": ["trakt"], "already_applied": replayed,
            "direct_provider_writes": 0}
