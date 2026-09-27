"""Episode audit storage with an isolated manual-import write connection."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from hub.inbound.episode_models import EpisodeRating, EpisodeSnapshot
from hub.inbound.models import InboundError
from hub.inbound.schema import _signature


# Separate tables keep episode observations unreachable by the existing
# movie/show importers, candidate selectors and scheduler. No shared migration.
SCHEMAS = {
    "inbound_episode_state": """CREATE TABLE inbound_episode_state (
        provider TEXT PRIMARY KEY CHECK(provider='trakt'),
        media_type TEXT NOT NULL CHECK(media_type='episode'),
        baseline_created_at TEXT NOT NULL, last_successful_poll_at TEXT NOT NULL,
        snapshot_hash TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>0),
        observed_count INTEGER NOT NULL, skipped_count INTEGER NOT NULL)""",
    "inbound_episode_snapshots": """CREATE TABLE inbound_episode_snapshots (
        content_key TEXT PRIMARY KEY, rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 10),
        rated_at TEXT NOT NULL, tmdb_series_id INTEGER NOT NULL CHECK(tmdb_series_id>0),
        season_number INTEGER NOT NULL CHECK(season_number>=0),
        episode_number INTEGER NOT NULL CHECK(episode_number>0),
        trakt_id INTEGER, imdb_id TEXT, tmdb_id INTEGER, observed_at TEXT NOT NULL)""",
    "inbound_episode_unmapped": """CREATE TABLE inbound_episode_unmapped (
        ordinal INTEGER PRIMARY KEY, rating_json TEXT NOT NULL, observed_at TEXT NOT NULL)""",
    "inbound_episode_events": """CREATE TABLE inbound_episode_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE,
        provider TEXT NOT NULL CHECK(provider='trakt'),
        media_type TEXT NOT NULL CHECK(media_type='episode'), content_key TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK(generation>0),
        event_type TEXT NOT NULL CHECK(event_type IN ('added','changed','removed')),
        old_rating INTEGER, new_rating INTEGER,
        provider_rated_at TEXT NOT NULL, detected_at TEXT NOT NULL,
        rating_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('observed','applied')),
        reason TEXT NOT NULL CHECK(reason='manual_episode_import_required'),
        classification TEXT NOT NULL CHECK(classification='candidate'),
        future_action TEXT NOT NULL CHECK(future_action IN ('upsert','delete')),
        applied_at TEXT, canonical_revision INTEGER,
        CHECK((status='applied' AND applied_at IS NOT NULL
            AND canonical_revision IS NOT NULL AND canonical_revision > 0)
            OR (status='observed' AND applied_at IS NULL AND canonical_revision IS NULL)))""",
}


class EpisodeStore:
    media_type = "episode"

    def __init__(self, path: str, *, initialize: bool = True):
        if path == ":memory:":
            raise InboundError("Episode observation requires a persistent SQLite path")
        self.path = str(Path(path).expanduser().resolve())
        if initialize:
            with self.connect(create=True) as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._schema(conn, create=True)
        else:
            with self.connect() as conn:
                self._schema(conn, create=False)

    @staticmethod
    def _schema(conn: sqlite3.Connection, *, create: bool) -> None:
        existing = {name: conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
                    for name in SCHEMAS}
        if any(existing.values()) and not all(existing.values()):
            raise InboundError("Episode audit schema was incomplete; initialization refused")
        for name, sql in SCHEMAS.items():
            row = existing[name]
            if row is None:
                if not create:
                    raise InboundError("Episode audit schema is missing")
                conn.execute(sql)
            elif _signature(row[0]) != _signature(sql):
                raise InboundError("Episode audit schema was not recognized")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE tbl_name=? AND "
                            "type IN ('index','trigger') AND sql IS NOT NULL", (name,)).fetchone():
                raise InboundError("Episode audit schema objects were not recognized")

    def connect(self, *, create: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(Path(self.path).as_uri() + ("?mode=rwc" if create else "?mode=rw"),
                               uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        # Even a trigger cannot use this observer connection to write canonical,
        # outbox or movie/show tables. Explicit sequence edits are denied too.
        def authorize(action, table, column, database, source):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                if table not in SCHEMAS and table not in ("sqlite_master", "sqlite_schema"):
                    return sqlite3.SQLITE_DENY
                if (table == "inbound_episode_events"
                        and action in (sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE)):
                    return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_CREATE_TABLE and table not in (*SCHEMAS, "sqlite_sequence"):
                return sqlite3.SQLITE_DENY
            if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
                          sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_DROP_TABLE,
                          sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_DROP_TRIGGER):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return conn

    def connect_for_import(self) -> sqlite3.Connection:
        """Open the narrow write surface used only by the manual E3 importer."""
        conn = sqlite3.connect(Path(self.path).as_uri() + "?mode=rw", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        writable = {"ratings", "outbox", "sqlite_sequence"}
        event_audit = {"status", "applied_at", "canonical_revision"}

        def authorize(action, table, column, database, source):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                if table == "inbound_episode_events":
                    if action != sqlite3.SQLITE_UPDATE or column not in event_audit:
                        return sqlite3.SQLITE_DENY
                elif table not in writable:
                    return sqlite3.SQLITE_DENY
            if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
                          sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_DROP_TABLE,
                          sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_TRIGGER,
                          sqlite3.SQLITE_DROP_TRIGGER):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    def state(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM inbound_episode_state WHERE provider='trakt'").fetchone()
            return dict(row) if row else None

    @staticmethod
    def _trusted_snapshot(conn: sqlite3.Connection, state: sqlite3.Row) -> EpisodeSnapshot:
        try:
            rows = conn.execute("SELECT * FROM inbound_episode_snapshots").fetchall()
            eligible = []
            for row in rows:
                fields = {k: row[k] for k in (
                    "rating", "rated_at", "tmdb_series_id", "season_number", "episode_number",
                    "trakt_id", "imdb_id", "tmdb_id")}
                rating = EpisodeRating(**fields)
                if rating.content_key != row["content_key"]:
                    raise InboundError("Episode trusted snapshot identity was inconsistent")
                eligible.append(rating)
            unmapped = tuple(EpisodeRating(**json.loads(row[0])) for row in conn.execute(
                "SELECT rating_json FROM inbound_episode_unmapped ORDER BY ordinal"))
            snapshot = EpisodeSnapshot(tuple(eligible), unmapped)
            if (snapshot.snapshot_hash != state["snapshot_hash"]
                    or snapshot.observed_count != state["observed_count"]
                    or len(snapshot.unmapped) != state["skipped_count"]):
                raise InboundError("Episode trusted snapshot audit was inconsistent")
            return snapshot
        except InboundError:
            raise
        except (ValueError, TypeError, KeyError):
            raise InboundError("Episode trusted snapshot could not be verified") from None

    def publish(self, snapshot: EpisodeSnapshot, *, expected_generation: int | None,
                baseline: bool = False, reset: bool = False) -> dict:
        if not isinstance(snapshot, EpisodeSnapshot):
            raise InboundError("Episode snapshot media does not match the store scope")
        if expected_generation is not None and (type(expected_generation) is not int
                                                or expected_generation < 1):
            raise InboundError("Episode expected generation was invalid")
        if reset and not baseline:
            raise InboundError("Reset requires explicit episode baseline mode")
        counts = {"added": 0, "changed": 0, "removed": 0}
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._schema(conn, create=False)
            state = conn.execute("SELECT * FROM inbound_episode_state WHERE provider='trakt'").fetchone()
            generation = state["generation"] if state else None
            if generation != expected_generation:
                raise InboundError("Episode snapshot advanced concurrently; observation refused")
            if baseline and state is not None and not reset:
                raise InboundError("Episode baseline already exists; use explicit reset")
            if not baseline and state is None:
                raise InboundError("Episode baseline is missing")
            if state is not None and not (baseline and reset):
                self._trusted_snapshot(conn, state)
            changed = baseline or snapshot.snapshot_hash != state["snapshot_hash"]
            if changed:
                version = (generation or 0) + 1
                previous = {r["content_key"]: dict(r) for r in conn.execute(
                    "SELECT * FROM inbound_episode_snapshots")}
                current = {r.content_key: r for r in snapshot.eligible}
                if not baseline:
                    for key in sorted(previous.keys() | current.keys()):
                        old, new = previous.get(key), current.get(key)
                        a, b = old["rating"] if old else None, new.rating if new else None
                        if a == b:
                            continue
                        kind = "added" if old is None else "removed" if new is None else "changed"
                        counts[kind] += 1
                        fingerprint = hashlib.sha256(json.dumps(
                            ["trakt", "episode", version, key, kind, a, b, snapshot.snapshot_hash],
                            separators=(",", ":")).encode()).hexdigest()
                        action = "delete" if kind == "removed" else "upsert"
                        fields = asdict(new) if new else {k: old[k] for k in (
                            "rating", "rated_at", "tmdb_series_id", "season_number", "episode_number",
                            "trakt_id", "imdb_id", "tmdb_id")}
                        conn.execute("""INSERT INTO inbound_episode_events
                            (fingerprint,provider,media_type,content_key,generation,event_type,
                             old_rating,new_rating,provider_rated_at,detected_at,rating_json,
                             status,reason,classification,future_action,applied_at,canonical_revision)
                            VALUES (?,'trakt','episode',?,?,?,?,?,?,?,?,'observed',
                                    'manual_episode_import_required','candidate',?,NULL,NULL)""",
                            (fingerprint, key, version, kind, a, b, fields["rated_at"], now,
                             json.dumps(fields, sort_keys=True, separators=(",", ":")), action))
                conn.execute("DELETE FROM inbound_episode_snapshots")
                conn.executemany("INSERT INTO inbound_episode_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 [(r.content_key, r.rating, r.rated_at, r.tmdb_series_id,
                                   r.season_number, r.episode_number, r.trakt_id, r.imdb_id, r.tmdb_id, now)
                                  for r in snapshot.eligible])
                conn.execute("DELETE FROM inbound_episode_unmapped")
                conn.executemany("INSERT INTO inbound_episode_unmapped VALUES (?,?,?)",
                                 [(i, json.dumps(asdict(r), sort_keys=True, separators=(",", ":")), now)
                                  for i, r in enumerate(snapshot.unmapped)])
                conn.execute("""INSERT INTO inbound_episode_state VALUES
                    ('trakt','episode',?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET
                    last_successful_poll_at=excluded.last_successful_poll_at,
                    snapshot_hash=excluded.snapshot_hash,generation=excluded.generation,
                    observed_count=excluded.observed_count,skipped_count=excluded.skipped_count""",
                    (state["baseline_created_at"] if state else now, now, snapshot.snapshot_hash,
                     version, snapshot.observed_count, len(snapshot.unmapped)))
            else:
                version = generation
                conn.execute("UPDATE inbound_episode_state SET last_successful_poll_at=? WHERE provider='trakt'", (now,))
        return {**counts, "events": sum(counts.values()), "generation": version,
                "snapshot_changed": changed, "snapshot_hash": snapshot.snapshot_hash,
                "episodes": snapshot.observed_count, "eligible": len(snapshot.eligible),
                "skipped": len(snapshot.unmapped), "canonical_mutations": 0,
                "outbox_mutations": 0, "provider_writes": 0, "auto_apply_enabled": False}
