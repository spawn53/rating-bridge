"""Explicitly enabled scheduler orchestration of existing guarded importers."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import sqlite3
import re
from typing import Iterable

from hub.inbound.classification import classify
from hub.inbound.importer import apply_event, GLOBAL_TARGETS
from hub.inbound.models import InboundError, timestamp, validate_media_type
from hub.inbound.removal import apply_removal_event
from hub.inbound.storage import InboundStore


class AutoApplyError(InboundError):
    def __init__(self, result: dict):
        super().__init__("Trakt automatic application failed; remaining candidates retained")
        self.result = result


class EchoGraceDeferred(InboundError):
    pass


def counters(enabled: bool) -> dict:
    return {"auto_apply_enabled": enabled, "auto_candidates": 0, "auto_applied": 0,
            "auto_grace_deferred": 0, "auto_failed": 0, "canonical_mutations": 0,
            "provider_writes": 0}


def _utc(value: object) -> datetime:
    timestamp(value)  # Strict ISO format, full time and explicit timezone.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise InboundError("Automatic echo grace requires UTC delivery timestamps")
    return parsed


def _plan(store: InboundStore, event: dict, generation: int, grace: int,
          now: datetime, *, connection: sqlite3.Connection | None = None) -> tuple[dict, bool]:
    media_type = validate_media_type(store.media_type)
    with (store.connect() if connection is None else nullcontext(connection)) as conn:
        if connection is None:
            conn.execute("BEGIN IMMEDIATE")
        elif not conn.in_transaction:
            raise InboundError("Automatic import guard requires an active transaction")
        state = conn.execute("SELECT generation FROM inbound_state WHERE provider='trakt' AND media_type=?",
                             (media_type,)).fetchone()
        row = conn.execute("""
            SELECT * FROM inbound_events WHERE id=? AND provider='trakt' AND media_type=?
            AND generation=? AND status='observed' AND classification='candidate'
            AND reason='different_provider_state' AND future_action IN ('upsert','delete')
        """, (event["id"], media_type, generation)).fetchone()
        if state is None or state[0] != generation or row is None or dict(row) != event:
            raise InboundError("Automatic candidate context changed")
        if (not isinstance(event["content_key"], str)
                or not re.fullmatch(rf"{media_type}:tmdb:[1-9][0-9]*", event["content_key"])):
            raise InboundError("Automatic candidate media identity is invalid")
        canonical, jobs = store._context(conn, event["content_key"])
        if canonical is not None and (canonical["media_type"] != media_type
                or canonical["content_key"] != event["content_key"]
                or canonical["tmdb_id"] != int(event["content_key"].rsplit(":", 1)[1])):
            raise InboundError("Automatic candidate canonical media identity is invalid")
        revision = canonical["revision"] if canonical else 0
        if type(revision) is not int or revision < (1 if canonical else 0):
            raise InboundError("Automatic candidate canonical revision is invalid")
        recovery = canonical is not None and canonical["source"] == f"trakt-inbound:{event['id']}"
        if recovery:
            # Existing importers alone decide whether provenance and complete
            # payload audit establish a committed event. No mutation is repeated.
            revision -= 1
        else:
            decision = classify(event["content_key"], event["new_rating"], canonical, jobs)
            if (decision.kind, decision.reason, decision.future_action) != (
                    "candidate", "different_provider_state", event["future_action"]):
                raise InboundError("Automatic candidate classification is no longer safe")
            if event["event_type"] == "added":
                if canonical is not None and (canonical["deleted"] != 1 or canonical["rating"] is not None):
                    raise InboundError("Automatic added candidate requires absent or tombstoned canonical")
            elif event["event_type"] in {"changed", "removed"}:
                if canonical is None or canonical["deleted"] != 0 or canonical["rating"] != event["old_rating"]:
                    raise InboundError("Automatic candidate old rating does not match canonical")
            else:
                raise InboundError("Automatic candidate transition is unsupported")
        held = False
        for job in jobs:
            if canonical is not None and job["revision"] == canonical["revision"] and job["status"] == "done":
                completed = _utc(job["updated_at"])
                created = _utc(job["created_at"])
                if completed < created or completed > now:
                    raise InboundError("Automatic echo grace delivery timing is inconsistent")
                held = (now - completed).total_seconds() < grace
        expected = {"event_id": event["id"], "expected_key": event["content_key"],
                    "expected_generation": generation, "expected_revision": revision, "confirmed": True}
        if event["event_type"] in {"added", "changed"} and event["future_action"] == "upsert":
            expected["expected_rating"] = event["new_rating"]
        elif event["event_type"] == "removed" and event["future_action"] == "delete":
            expected["expected_old_rating"] = event["old_rating"]
            # On audit-gap recovery the old source is no longer present; this
            # sentinel can never authorize a fresh delete if canonical changes.
            expected["expected_source"] = "recovery-audit-gap" if recovery else canonical["source"]
        else:
            raise InboundError("Automatic candidate action does not match its transition")
        return expected, held


def auto_apply(store: InboundStore, targets: Iterable[str], *, generation: int,
               max_events: int = 10, echo_grace_seconds: int = 600,
               now: datetime | None = None) -> dict:
    result = counters(True)
    try:
        media_type = validate_media_type(store.media_type)
        targets = tuple(targets)
        if (targets != GLOBAL_TARGETS or type(max_events) is not int or not 1 <= max_events <= 100
                or type(echo_grace_seconds) is not int or not 0 <= echo_grace_seconds <= 86400
                or type(generation) is not int or generation < 1):
            raise InboundError("Automatic application settings are invalid")
        now_is_live = now is None
        now = now or datetime.now(timezone.utc)
        if now.utcoffset() != timedelta(0):
            raise InboundError("Automatic application clock requires UTC")
        with store.connect() as conn:
            state = conn.execute("SELECT generation FROM inbound_state WHERE provider='trakt' AND media_type=?",
                                 (media_type,)).fetchone()
            if state is None or state[0] != generation:
                raise InboundError("Automatic generation is no longer current")
            events = [dict(row) for row in conn.execute("""
                SELECT * FROM inbound_events WHERE provider='trakt' AND media_type=?
                AND generation=? AND status='observed' AND classification='candidate'
                AND reason='different_provider_state' AND future_action IN ('upsert','delete')
                ORDER BY id
            """, (media_type, generation))]
        result["auto_candidates"] = len(events)
        if len(events) > max_events:
            raise InboundError("Automatic candidate limit exceeded")
        for event in events:
            expected, held = _plan(store, event, generation, echo_grace_seconds, now)
            if held:
                result["auto_grace_deferred"] += 1
                continue
            with store.connect() as conn:
                previous, _ = store._context(conn, event["content_key"])
            def guard(conn: sqlite3.Connection) -> None:
                fresh, deferred = _plan(store, event, generation, echo_grace_seconds,
                                        datetime.now(timezone.utc) if now_is_live else now, connection=conn)
                if fresh != expected:
                    raise InboundError("Automatic import expectations changed under write lock")
                if deferred:
                    raise EchoGraceDeferred("Automatic candidate is within outbound echo grace")
            try:
                try:
                    if event["event_type"] == "removed":
                        apply_removal_event(store, targets, **expected, automation_guard=guard)
                    else:
                        apply_event(store, targets, **expected, automation_guard=guard)
                except EchoGraceDeferred:
                    result["auto_grace_deferred"] += 1
                    continue
            finally:
                # Include a canonical commit even if audit marking subsequently
                # fails. Recovery-only marking contributes no new mutation.
                with store.connect() as conn:
                    current, _ = store._context(conn, event["content_key"])
                if (current is not None and current["source"] == f"trakt-inbound:{event['id']}"
                        and (previous is None or current["revision"] != previous["revision"])):
                    result["canonical_mutations"] += 1
            result["auto_applied"] += 1
        return result
    except Exception:
        result["auto_failed"] += 1
        raise AutoApplyError(result) from None
