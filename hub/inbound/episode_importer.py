"""Explicit guarded episode imports with one audited TMDb outbox target."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import re
import sqlite3
from typing import Callable, Iterable

from hub.inbound.classification import classify, targets_excluding_source
from hub.inbound.episode_models import EpisodeRating
from hub.inbound.episode_storage import EpisodeStore
from hub.inbound.models import InboundError, timestamp
from hub.models import RatingWrite
from hub.store import RatingStore


EPISODE_SOURCE_TARGETS = ("trakt", "tmdb")
EPISODE_DELIVERY_TARGETS = ("tmdb",)
_KEY = re.compile(r"episode:tmdb:([1-9][0-9]*):s(0|[1-9][0-9]*):e([1-9][0-9]*)")


def validated_episode_targets(targets: Iterable[str]) -> tuple[str, ...]:
    """Accept only the explicitly audited Trakt-source-to-TMDb plan."""
    plan = tuple(targets)
    if plan != EPISODE_SOURCE_TARGETS:
        raise InboundError("Episode inbound target plan must be exactly trakt,tmdb")
    derived = targets_excluding_source(plan, "trakt")
    if derived != EPISODE_DELIVERY_TARGETS:
        raise InboundError("Episode source exclusion did not match the TMDb-only plan")
    return derived


def _expectations(event_id: int, key: str, generation: int, revision: int) -> None:
    if (type(event_id) is not int or event_id < 1
            or not isinstance(key, str) or _KEY.fullmatch(key) is None
            or type(generation) is not int or generation < 1
            or type(revision) is not int or revision < 0):
        raise InboundError("Episode import expectations were invalid")


def _event(conn: sqlite3.Connection, *, event_id: int, key: str, generation: int,
           action: str, rating: int | None = None, old_rating: int | None = None) -> dict:
    row = conn.execute("SELECT * FROM inbound_episode_events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise InboundError("Episode event was not found")
    event = dict(row)
    transition = event.get("event_type")
    valid_transition = (
        action == "upsert" and transition in {"added", "changed"}
        and event.get("new_rating") == rating
        and (transition == "added" and event.get("old_rating") is None
             or transition == "changed" and type(event.get("old_rating")) is int
             and 1 <= event["old_rating"] <= 10 and event["old_rating"] != rating)
        or action == "delete" and transition == "removed"
        and event.get("old_rating") == old_rating and event.get("new_rating") is None
    )
    if (event.get("provider") != "trakt" or event.get("media_type") != "episode"
            or event.get("content_key") != key or event.get("generation") != generation
            or event.get("classification") != "candidate"
            or event.get("reason") != "manual_episode_import_required"
            or event.get("future_action") != action
            or event.get("status") not in {"observed", "applied"}
            or not valid_transition
            or not isinstance(event.get("fingerprint"), str)
            or re.fullmatch(r"[0-9a-f]{64}", event["fingerprint"]) is None):
        raise InboundError("Episode event did not match guarded import preconditions")
    timestamp(event.get("provider_rated_at"))
    timestamp(event.get("detected_at"))
    if event["status"] == "observed":
        if event.get("applied_at") is not None or event.get("canonical_revision") is not None:
            raise InboundError("Observed episode event had inconsistent audit fields")
    else:
        timestamp(event.get("applied_at"))
        if type(event.get("canonical_revision")) is not int or event["canonical_revision"] < 1:
            raise InboundError("Applied episode event had an invalid revision audit")
    return event


def _rating(event: dict) -> EpisodeRating:
    try:
        data = json.loads(event["rating_json"])
        if not isinstance(data, dict):
            raise TypeError()
        value = EpisodeRating(**data)
    except (ValueError, TypeError, KeyError):
        raise InboundError("Episode event identity metadata was invalid") from None
    match = _KEY.fullmatch(event["content_key"])
    if (match is None or value.content_key != event["content_key"]
            or (value.tmdb_series_id, value.season_number, value.episode_number)
            != tuple(map(int, match.groups()))
            or value.rating != (event["new_rating"] if event["future_action"] == "upsert"
                                else event["old_rating"])
            or value.rated_at != timestamp(event["provider_rated_at"])
            or json.loads(event["rating_json"]) != asdict(value)):
        raise InboundError("Episode event identity metadata did not match its transition")
    return value


def _newer(conn: sqlite3.Connection, event: dict) -> None:
    row = conn.execute("""SELECT 1 FROM inbound_episode_events
        WHERE content_key=? AND (generation>? OR (generation=? AND id>?)) LIMIT 1""",
        (event["content_key"], event["generation"], event["generation"], event["id"])).fetchone()
    if row is not None:
        raise InboundError("A newer episode event supersedes this intent")


def _snapshot(conn: sqlite3.Connection, event: dict) -> EpisodeRating:
    _newer(conn, event)
    state = conn.execute("SELECT * FROM inbound_episode_state WHERE provider='trakt'").fetchone()
    row = conn.execute("SELECT * FROM inbound_episode_snapshots WHERE content_key=?",
                       (event["content_key"],)).fetchone()
    if state is None or state["generation"] != event["generation"] or row is None:
        raise InboundError("Episode event no longer matched the trusted snapshot generation")
    EpisodeStore._trusted_snapshot(conn, state)
    value = EpisodeRating(**{name: row[name] for name in (
        "rating", "rated_at", "tmdb_series_id", "season_number", "episode_number",
        "trakt_id", "imdb_id", "tmdb_id")})
    if value != _rating(event) or value.content_key != row["content_key"]:
        raise InboundError("Episode event no longer matched the trusted snapshot")
    return value


def _absence(conn: sqlite3.Connection, event: dict) -> EpisodeRating:
    _newer(conn, event)
    state = conn.execute("SELECT * FROM inbound_episode_state WHERE provider='trakt'").fetchone()
    present = conn.execute("SELECT 1 FROM inbound_episode_snapshots WHERE content_key=?",
                           (event["content_key"],)).fetchone()
    if state is None or state["generation"] != event["generation"] or present is not None:
        raise InboundError("Episode removal no longer matched trusted snapshot absence")
    EpisodeStore._trusted_snapshot(conn, state)
    value = _rating(event)
    return value


def _context(conn: sqlite3.Connection, key: str) -> tuple[dict | None, list[dict]]:
    row = conn.execute("SELECT * FROM ratings WHERE content_key=?", (key,)).fetchone()
    jobs = conn.execute("SELECT * FROM outbox WHERE content_key=? ORDER BY id", (key,)).fetchall()
    return (dict(row) if row else None), [dict(job) for job in jobs]


def _compatible(canonical: dict, value: EpisodeRating) -> bool:
    if (canonical.get("content_key") != value.content_key
            or canonical.get("media_type") != "episode"
            or canonical.get("tmdb_series_id") != value.tmdb_series_id
            or canonical.get("season_number") != value.season_number
            or canonical.get("episode_number") != value.episode_number):
        return False
    for field in ("tmdb_id", "trakt_id", "imdb_id"):
        supplied = getattr(value, field)
        if supplied is not None and canonical.get(field) != supplied:
            return False
    return True


def _verify_committed(conn: sqlite3.Connection, event: dict, value: EpisodeRating,
                      expected_revision: int, action: str) -> dict:
    canonical, jobs = _context(conn, event["content_key"])
    expected_rating = value.rating if action == "upsert" else None
    expected_deleted = 0 if action == "upsert" else 1
    if (canonical is None or canonical.get("source") != f"trakt-episode-inbound:{event['id']}"
            or canonical.get("revision") != expected_revision + 1
            or canonical.get("rating") != expected_rating
            or canonical.get("deleted") != expected_deleted
            or not _compatible(canonical, value)
            or timestamp(canonical.get("rated_at")) != value.rated_at):
        raise InboundError("Canonical episode did not match the committed inbound event")
    timestamp(canonical.get("updated_at"))
    revision_jobs = [job for job in jobs if job.get("revision") == canonical["revision"]]
    outbox_action = "upsert" if action == "upsert" else "remove"
    try:
        valid = (len(revision_jobs) == 1
                 and revision_jobs[0].get("target") == "tmdb"
                 and revision_jobs[0].get("action") == outbox_action
                 and json.loads(revision_jobs[0]["payload_json"]) == canonical)
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        raise InboundError("Committed episode audit did not match the TMDb-only target plan")
    return canonical


def _mark_applied(store: EpisodeStore, *, event_id: int, key: str, generation: int,
                  expected_revision: int, fingerprint: str, action: str,
                  rating: int | None = None, old_rating: int | None = None) -> int:
    with store.connect_for_import() as conn:
        conn.execute("BEGIN IMMEDIATE")
        event = _event(conn, event_id=event_id, key=key, generation=generation,
                       action=action, rating=rating, old_rating=old_rating)
        if event["fingerprint"] != fingerprint:
            raise InboundError("Episode event fingerprint changed before audit completion")
        value = _snapshot(conn, event) if action == "upsert" else _absence(conn, event)
        canonical = _verify_committed(conn, event, value, expected_revision, action)
        if event["status"] == "applied":
            if event["canonical_revision"] != canonical["revision"]:
                raise InboundError("Applied episode revision audit was inconsistent")
            return canonical["revision"]
        changed = conn.execute("""UPDATE inbound_episode_events
            SET status='applied',applied_at=?,canonical_revision=?
            WHERE id=? AND status='observed' AND fingerprint=?""",
            (datetime.now(timezone.utc).isoformat(), canonical["revision"], event_id, fingerprint))
        if changed.rowcount != 1:
            raise InboundError("Episode event changed before audit completion")
        return canonical["revision"]


def apply_episode_event(store: EpisodeStore, targets: Iterable[str], *, event_id: int,
                        expected_key: str, expected_rating: int, expected_generation: int,
                        expected_revision: int, confirmed: bool = False,
                        transaction_guard: Callable[[sqlite3.Connection], None] | None = None) -> dict:
    if confirmed is not True:
        raise InboundError("Episode import requires explicit confirmation")
    _expectations(event_id, expected_key, expected_generation, expected_revision)
    if type(expected_rating) is not int or not 1 <= expected_rating <= 10:
        raise InboundError("Episode import rating expectation was invalid")
    derived = validated_episode_targets(targets)
    canonical_store = RatingStore(store.path, initialize=False)
    replayed = False
    with store.connect_for_import() as conn:
        conn.execute("BEGIN IMMEDIATE")
        event = _event(conn, event_id=event_id, key=expected_key,
                       generation=expected_generation, action="upsert", rating=expected_rating)
        if transaction_guard is not None:
            transaction_guard(conn)
        if event["status"] == "applied":
            if event["canonical_revision"] != expected_revision + 1:
                raise InboundError("Applied episode revision did not match the expectation")
            return {"event_id": event_id, "content_key": expected_key,
                    "rating": expected_rating, "revision": event["canonical_revision"],
                    "queued_targets": [], "skipped_targets": ["trakt"],
                    "already_applied": True, "direct_provider_writes": 0}
        value = _snapshot(conn, event)
        canonical, jobs = _context(conn, expected_key)
        if canonical is not None and canonical.get("source") == f"trakt-episode-inbound:{event_id}":
            canonical = _verify_committed(conn, event, value, expected_revision, "upsert")
            queued: list[str] = []
            revision = canonical["revision"]
            replayed = True
        else:
            decision = classify(expected_key, expected_rating, canonical, jobs)
            if (decision.kind, decision.reason, decision.future_action) != (
                    "candidate", "different_provider_state", "upsert"):
                raise InboundError("Current episode classification refused this import")
            revision = canonical["revision"] if canonical else 0
            if canonical is not None and not _compatible(canonical, value):
                raise InboundError("Canonical episode identity was incompatible")
            if revision != expected_revision:
                raise InboundError("Canonical episode revision changed")
            if (event["event_type"] == "added" and canonical is not None
                    and (canonical["deleted"] != 1 or canonical["rating"] is not None)):
                raise InboundError("Added episode requires absent or tombstoned canonical state")
            if (event["event_type"] == "changed" and (canonical is None
                    or canonical["deleted"] != 0 or canonical["rating"] != event["old_rating"])):
                raise InboundError("Changed episode did not match canonical old rating")
            item = RatingWrite(
                media_type="episode", rating=value.rating, tmdb_id=value.tmdb_id,
                tmdb_series_id=value.tmdb_series_id, season_number=value.season_number,
                episode_number=value.episode_number, trakt_id=value.trakt_id,
                imdb_id=value.imdb_id, rated_at=datetime.fromisoformat(value.rated_at),
                source=f"trakt-episode-inbound:{event_id}")
            result = canonical_store.upsert_rating(item, derived, connection=conn)
            revision, queued = result["revision"], result["queued_targets"]
            if revision != expected_revision + 1 or tuple(queued) != derived:
                raise InboundError("Episode import did not create one revision and one TMDb job")
    marked = _mark_applied(store, event_id=event_id, key=expected_key,
                           generation=expected_generation, expected_revision=expected_revision,
                           fingerprint=event["fingerprint"], action="upsert",
                           rating=expected_rating)
    return {"event_id": event_id, "content_key": expected_key, "rating": expected_rating,
            "revision": marked, "queued_targets": queued, "skipped_targets": ["trakt"],
            "already_applied": replayed, "direct_provider_writes": 0}


def apply_episode_removal(store: EpisodeStore, targets: Iterable[str], *, event_id: int,
                          expected_key: str, expected_old_rating: int,
                          expected_generation: int, expected_revision: int,
                          expected_source: str, confirmed: bool = False,
                          transaction_guard: Callable[[sqlite3.Connection], None] | None = None) -> dict:
    if confirmed is not True:
        raise InboundError("Episode removal requires explicit confirmation")
    _expectations(event_id, expected_key, expected_generation, expected_revision)
    if (type(expected_old_rating) is not int or not 1 <= expected_old_rating <= 10
            or not isinstance(expected_source, str) or not expected_source.strip()
            or expected_source == f"trakt-episode-inbound:{event_id}"):
        raise InboundError("Episode removal expectations were invalid")
    derived = validated_episode_targets(targets)
    canonical_store = RatingStore(store.path, initialize=False)
    replayed = False
    with store.connect_for_import() as conn:
        conn.execute("BEGIN IMMEDIATE")
        event = _event(conn, event_id=event_id, key=expected_key,
                       generation=expected_generation, action="delete",
                       old_rating=expected_old_rating)
        if transaction_guard is not None:
            transaction_guard(conn)
        if event["status"] == "applied":
            if event["canonical_revision"] != expected_revision + 1:
                raise InboundError("Applied episode removal revision did not match expectation")
            return {"event_id": event_id, "content_key": expected_key,
                    "revision": event["canonical_revision"], "removed": True,
                    "queued_targets": [], "skipped_targets": ["trakt"],
                    "already_applied": True, "direct_provider_writes": 0}
        value = _absence(conn, event)
        canonical, jobs = _context(conn, expected_key)
        if canonical is not None and canonical.get("source") == f"trakt-episode-inbound:{event_id}":
            canonical = _verify_committed(conn, event, value, expected_revision, "delete")
            queued: list[str] = []
            revision = canonical["revision"]
            replayed = True
        else:
            decision = classify(expected_key, None, canonical, jobs)
            if (decision.kind, decision.reason, decision.future_action) != (
                    "candidate", "different_provider_state", "delete"):
                raise InboundError("Current episode classification refused this removal")
            if (canonical is None or canonical["deleted"] != 0
                    or canonical["rating"] != expected_old_rating
                    or canonical["source"] != expected_source
                    or not _compatible(canonical, value)):
                raise InboundError("Episode removal did not match active canonical identity")
            if canonical["revision"] != expected_revision:
                raise InboundError("Canonical episode revision changed")
            result = canonical_store.delete_rating(
                expected_key, derived, source=f"trakt-episode-inbound:{event_id}", connection=conn)
            revision, queued = result["revision"], result["queued_targets"]
            if not result["removed"] or revision != expected_revision + 1 or tuple(queued) != derived:
                raise InboundError("Episode removal did not create one tombstone and one TMDb job")
    marked = _mark_applied(store, event_id=event_id, key=expected_key,
                           generation=expected_generation, expected_revision=expected_revision,
                           fingerprint=event["fingerprint"], action="delete",
                           old_rating=expected_old_rating)
    return {"event_id": event_id, "content_key": expected_key, "revision": marked,
            "removed": True, "queued_targets": queued, "skipped_targets": ["trakt"],
            "already_applied": replayed, "direct_provider_writes": 0}
