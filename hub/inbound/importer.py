"""Explicit, guarded import of one persisted Trakt movie event. No provider I/O."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sqlite3
from typing import Iterable

from hub.inbound.classification import classify, targets_excluding_source
from hub.inbound.models import InboundError, MovieRating, timestamp
from hub.inbound.storage import InboundStore
from hub.models import RatingWrite
from hub.store import RatingStore

TARGETS = ("tmdb", "simkl", "mdblist")
GLOBAL_TARGETS = ("tmdb", "trakt", "simkl", "mdblist")


def _event(conn: sqlite3.Connection, event_id: int, key: str, rating: int,
           generation: int) -> dict:
    row = conn.execute("SELECT * FROM inbound_events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise InboundError("Inbound event was not found")
    event = dict(row)
    if (event["provider"] != "trakt" or event["media_type"] != "movie"
            or event["content_key"] != key or event["new_rating"] != rating
            or event["generation"] != generation
            or event["event_type"] not in {"added", "changed"}
            or event["classification"] != "candidate"
            or event["reason"] != "different_provider_state"
            or event["future_action"] != "upsert"
            or event["status"] not in {"observed", "applied"}
            or not re.fullmatch(r"[0-9a-f]{64}", event["fingerprint"])):
        raise InboundError("Inbound event did not match the guarded import preconditions")
    if (event["event_type"] == "added" and event["old_rating"] is not None
            or event["event_type"] == "changed" and (
                type(event["old_rating"]) is not int or not 1 <= event["old_rating"] <= 10
                or event["old_rating"] == rating)):
        raise InboundError("Inbound event transition was invalid")
    timestamp(event["provider_rated_at"])
    return event


def _snapshot(conn: sqlite3.Connection, event: dict) -> MovieRating:
    key = event["content_key"]
    state = conn.execute(
        "SELECT generation FROM inbound_state WHERE provider='trakt' AND media_type='movie'"
    ).fetchone()
    row = conn.execute(
        "SELECT * FROM inbound_snapshots WHERE provider='trakt' AND media_type='movie' AND content_key=?",
        (key,),
    ).fetchone()
    if state is None or state[0] != event["generation"] or row is None:
        raise InboundError("Inbound event no longer matches the trusted snapshot generation")
    movie = MovieRating(row["rating"], row["rated_at"], row["tmdb_id"],
                        row["trakt_id"], row["imdb_id"])
    if (movie.content_key != key or movie.rating != event["new_rating"]
            or movie.rated_at != timestamp(event["provider_rated_at"])):
        raise InboundError("Inbound event no longer matches the trusted snapshot")
    # Include ignored events: a newer echo/removal still supersedes old intent.
    newer = conn.execute("""
        SELECT 1 FROM inbound_events
        WHERE provider='trakt' AND media_type='movie' AND content_key=?
          AND (generation>? OR (generation=? AND id>?)) LIMIT 1
    """, (key, event["generation"], event["generation"], event["id"])).fetchone()
    if newer is not None:
        raise InboundError("A newer inbound event supersedes this event")
    return movie


def _verify_committed(conn: sqlite3.Connection, event: dict, movie: MovieRating,
                      expected_revision: int) -> dict:
    canonical, trakt_jobs = InboundStore._context(conn, event["content_key"])
    # Apply the same causal scope during crash recovery as before first import.
    decision = classify(event["content_key"], movie.rating, canonical, trakt_jobs)
    if decision.kind == "defer":
        raise InboundError("Trakt outbound audit is unresolved or inconsistent; import refused")
    if (canonical is None or canonical["deleted"] != 0
            or canonical["source"] != f"trakt-inbound:{event['id']}"
            or canonical["revision"] != expected_revision + 1
            or canonical["media_type"] != "movie"
            or canonical["rating"] != movie.rating or canonical["tmdb_id"] != movie.tmdb_id
            or (movie.trakt_id is not None and canonical["trakt_id"] != movie.trakt_id)
            or (movie.imdb_id is not None and canonical["imdb_id"] != movie.imdb_id)
            or timestamp(canonical["rated_at"]) != movie.rated_at):
        raise InboundError("Canonical state does not match this committed inbound event")
    jobs = [dict(r) for r in conn.execute(
        "SELECT * FROM outbox WHERE content_key=? AND revision=?",
        (event["content_key"], canonical["revision"]),
    )]
    if (len(jobs) != 3 or {j["target"] for j in jobs} != set(TARGETS)
            or any(j["action"] != "upsert" or json.loads(j["payload_json"]) != canonical for j in jobs)):
        raise InboundError("Committed inbound delivery audit does not match the three allowed targets")
    return canonical


def _mark_applied(store: InboundStore, *, event_id: int, key: str, rating: int,
                  generation: int, expected_revision: int, fingerprint: str) -> int:
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        event = _event(conn, event_id, key, rating, generation)
        if event["fingerprint"] != fingerprint:
            raise InboundError("Inbound event identity changed before audit completion")
        movie = _snapshot(conn, event)
        canonical = _verify_committed(conn, event, movie, expected_revision)
        if event["status"] == "applied":
            if event["canonical_revision"] != canonical["revision"]:
                raise InboundError("Inbound applied revision audit was inconsistent")
            return canonical["revision"]
        conn.execute("""
            UPDATE inbound_events SET status='applied', applied_at=?, canonical_revision=?
            WHERE id=? AND status='observed'
        """, (datetime.now(timezone.utc).isoformat(), canonical["revision"], event_id))
        return canonical["revision"]


def apply_event(store: InboundStore, targets: Iterable[str], *, event_id: int,
                expected_key: str, expected_rating: int, expected_generation: int,
                expected_revision: int, confirmed: bool = False) -> dict:
    """Commit canonical/outbox together; then complete the replayable event audit.

    BEGIN IMMEDIATE holds validation through the canonical commit. A crash in
    the following audit gap is recovered only by exact provenance and a complete
    three-job payload audit, never by score equality alone.
    """
    if confirmed is not True:
        raise InboundError("Single-event import requires --confirm-live-import")
    if (type(event_id) is not int or event_id < 1
            or not isinstance(expected_key, str)
            or not re.fullmatch(r"movie:tmdb:[1-9][0-9]*", expected_key)
            or type(expected_rating) is not int or not 1 <= expected_rating <= 10
            or type(expected_generation) is not int or expected_generation < 1
            or type(expected_revision) is not int or expected_revision < 0):
        raise InboundError("Single-event import expectations were invalid")
    targets = tuple(targets)
    derived = targets_excluding_source(targets, "trakt")
    if targets != GLOBAL_TARGETS or derived != TARGETS:
        raise InboundError("Single-event import requires the unchanged four targets and exact source exclusion")
    canonical_store = RatingStore(store.path)
    replayed = False
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        event = _event(conn, event_id, expected_key, expected_rating, expected_generation)
        if event["status"] == "applied":
            if event["canonical_revision"] != expected_revision + 1:
                raise InboundError("Inbound applied revision did not match the expectation")
            timestamp(event["applied_at"])
            return {"event_id": event_id, "content_key": expected_key, "rating": expected_rating,
                    "revision": event["canonical_revision"], "queued_targets": [],
                    "skipped_targets": ["trakt"], "already_applied": True}
        movie = _snapshot(conn, event)
        canonical, jobs = store._context(conn, expected_key)
        if canonical is not None and canonical["source"] == f"trakt-inbound:{event_id}":
            canonical = _verify_committed(conn, event, movie, expected_revision)
            revision = canonical["revision"]
            queued = []
            replayed = True
        else:
            decision = classify(expected_key, expected_rating, canonical, jobs)
            if (decision.kind != "candidate" or decision.reason != "different_provider_state"
                    or decision.future_action != "upsert"):
                raise InboundError("Current canonical/outbox classification refuses this import")
            revision = canonical["revision"] if canonical else 0
            if canonical is not None and (canonical["media_type"] != "movie"
                    or canonical["tmdb_id"] != movie.tmdb_id):
                raise InboundError("Canonical movie identity is incompatible with the inbound event")
            if revision != expected_revision:
                raise InboundError("Canonical revision changed; import refused")
            if (event["event_type"] == "added" and canonical is not None
                    and (canonical["deleted"] != 1 or canonical["rating"] is not None)):
                raise InboundError("Added inbound event requires absent or tombstoned canonical state")
            if (event["event_type"] == "changed" and (canonical is None
                    or canonical["deleted"] != 0 or canonical["rating"] != event["old_rating"])):
                raise InboundError("Changed inbound event does not match previous canonical state")
            item = RatingWrite(media_type="movie", tmdb_id=movie.tmdb_id,
                               trakt_id=movie.trakt_id, imdb_id=movie.imdb_id,
                               rating=movie.rating, rated_at=datetime.fromisoformat(movie.rated_at),
                               source=f"trakt-inbound:{event_id}")
            result = canonical_store.upsert_rating(item, derived, connection=conn)
            revision, queued = result["revision"], result["queued_targets"]
            if revision != expected_revision + 1 or tuple(queued) != TARGETS:
                raise InboundError("Canonical import did not produce exactly one revision and three jobs")
    # No automatic retry or provider call occurs here. Tests simulate a crash at this boundary.
    marked = _mark_applied(store, event_id=event_id, key=expected_key, rating=expected_rating,
                           generation=expected_generation, expected_revision=expected_revision,
                           fingerprint=event["fingerprint"])
    return {"event_id": event_id, "content_key": expected_key, "rating": expected_rating,
            "revision": marked, "queued_targets": queued, "skipped_targets": ["trakt"],
            "already_applied": replayed}
