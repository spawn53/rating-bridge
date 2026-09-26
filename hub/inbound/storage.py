from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from hub.inbound.classification import classify
from hub.inbound.models import InboundError, MovieRating, Snapshot

PROVIDER = "trakt"
MEDIA = "movie"

EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK(media_type='movie'),
    content_key TEXT NOT NULL,
    generation INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('added','changed','removed')),
    old_rating INTEGER,
    new_rating INTEGER,
    provider_rated_at TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('observed','ignored','applied')),
    reason TEXT NOT NULL,
    classification TEXT NOT NULL,
    future_action TEXT,
    applied_at TEXT,
    canonical_revision INTEGER,
    CHECK((status='applied' AND applied_at IS NOT NULL
        AND canonical_revision IS NOT NULL AND canonical_revision > 0)
        OR (status!='applied' AND applied_at IS NULL AND canonical_revision IS NULL))
);
"""


class InboundStore:
    """Owns only inbound tables. Canonical/outbox access is SELECT-only."""

    def __init__(self, path: str):
        self.path = path
        if path == ":memory:":
            raise InboundError("Inbound observation requires a persistent SQLite path")
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS inbound_state (
                    provider TEXT NOT NULL,
                    media_type TEXT NOT NULL CHECK(media_type='movie'),
                    baseline_created_at TEXT NOT NULL,
                    last_successful_poll_at TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    observed_count INTEGER NOT NULL,
                    skipped_count INTEGER NOT NULL,
                    PRIMARY KEY(provider,media_type)
                );
                CREATE TABLE IF NOT EXISTS inbound_snapshots (
                    provider TEXT NOT NULL,
                    media_type TEXT NOT NULL CHECK(media_type='movie'),
                    content_key TEXT NOT NULL,
                    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 10),
                    rated_at TEXT NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    trakt_id INTEGER,
                    imdb_id TEXT,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(provider,media_type,content_key)
                );
                CREATE TABLE IF NOT EXISTS inbound_unmapped (
                    provider TEXT NOT NULL,
                    media_type TEXT NOT NULL CHECK(media_type='movie'),
                    ordinal INTEGER NOT NULL,
                    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 10),
                    rated_at TEXT NOT NULL,
                    trakt_id INTEGER,
                    imdb_id TEXT,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(provider,media_type,ordinal)
                );
            """)
            self._migrate_events(conn)

    @staticmethod
    def _migrate_events(conn: sqlite3.Connection) -> None:
        # Serialize schema checks too: concurrent constructors must not rebuild twice.
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='inbound_events'"
        ).fetchone()
        if existing is None:
            conn.execute(EVENT_SCHEMA)
            return
        columns = [r[1] for r in conn.execute("PRAGMA table_info(inbound_events)")]
        if "'applied'" in existing[0] and {"applied_at", "canonical_revision"} <= set(columns):
            return
        expected = {
            "id", "fingerprint", "provider", "media_type", "content_key", "generation",
            "event_type", "old_rating", "new_rating", "provider_rated_at", "detected_at",
            "status", "reason", "classification", "future_action", "applied_at",
            "canonical_revision",
        }
        if set(columns) - expected:
            raise InboundError("Inbound event schema was not recognized; migration refused")
        sequence = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='inbound_events'"
        ).fetchone()
        conn.execute(EVENT_SCHEMA.replace("inbound_events", "inbound_events_migration", 1))
        names = ",".join(columns)  # Restricted to the fixed column whitelist above.
        conn.execute(f"INSERT INTO inbound_events_migration ({names}) SELECT {names} FROM inbound_events")
        conn.execute("DROP TABLE inbound_events")
        conn.execute("ALTER TABLE inbound_events_migration RENAME TO inbound_events")
        if sequence is not None:
            conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='inbound_events'",
                         (sequence[0],))

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def state(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM inbound_state WHERE provider=? AND media_type=?",
                               (PROVIDER, MEDIA)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _context(conn: sqlite3.Connection, key: str) -> tuple[dict | None, list[dict]]:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        canonical = None
        jobs = []
        if "ratings" in tables:
            row = conn.execute("SELECT * FROM ratings WHERE content_key=?", (key,)).fetchone()
            canonical = dict(row) if row else None
        if "outbox" in tables:
            jobs = [dict(r) for r in conn.execute(
                "SELECT * FROM outbox WHERE content_key=? AND target='trakt'", (key,)
            )]
        return canonical, jobs

    def publish(self, snapshot: Snapshot, *, expected_generation: int | None,
                baseline: bool = False, reset: bool = False) -> dict:
        """Generation CAS and a single transaction prevent partial/crash publication."""
        counts = {"added": 0, "changed": 0, "removed": 0, "deferred": 0}
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute(
                "SELECT * FROM inbound_state WHERE provider=? AND media_type=?", (PROVIDER, MEDIA)
            ).fetchone()
            generation = state["generation"] if state else None
            if generation != expected_generation:
                raise InboundError("Inbound snapshot advanced concurrently; rerun observation")
            if baseline:
                if state is not None and not reset:
                    raise InboundError("Trakt baseline already exists; use --baseline --reset explicitly")
            elif state is None:
                raise InboundError("Trakt baseline is missing; run --baseline first")
            version = (generation or 0) + 1
            previous = {r["content_key"]: dict(r) for r in conn.execute(
                "SELECT * FROM inbound_snapshots WHERE provider=? AND media_type=?", (PROVIDER, MEDIA)
            )}
            current = {r.content_key: r for r in snapshot.eligible}
            if not baseline:
                for key in sorted(previous.keys() | current.keys()):
                    old, new = previous.get(key), current.get(key)
                    old_rating = old["rating"] if old else None
                    new_rating = new.rating if new else None
                    if old_rating == new_rating:
                        continue
                    event_type = "added" if old is None else "removed" if new is None else "changed"
                    counts[event_type] += 1
                    canonical, jobs = self._context(conn, key)
                    decision = classify(key, new_rating, canonical, jobs)
                    counts["deferred"] += decision.kind == "defer"
                    # Include occurrence generation, so A->B->A->B remains auditable.
                    identity = [PROVIDER, MEDIA, version, key, event_type, old_rating,
                                new_rating, snapshot.snapshot_hash]
                    fingerprint = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
                    conn.execute("""
                        INSERT INTO inbound_events (
                            fingerprint,provider,media_type,content_key,generation,event_type,
                            old_rating,new_rating,provider_rated_at,detected_at,status,reason,
                            classification,future_action
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (fingerprint, PROVIDER, MEDIA, key, version, event_type, old_rating,
                          new_rating, new.rated_at if new else old["rated_at"], now,
                          "ignored" if decision.kind in {"echo", "noop"} else "observed",
                          decision.reason, decision.kind, decision.future_action))
            conn.execute("DELETE FROM inbound_snapshots WHERE provider=? AND media_type=?", (PROVIDER, MEDIA))
            conn.executemany("""
                INSERT INTO inbound_snapshots VALUES (?,?,?,?,?,?,?,?,?)
            """, [(PROVIDER, MEDIA, r.content_key, r.rating, r.rated_at,
                   r.tmdb_id, r.trakt_id, r.imdb_id, now) for r in snapshot.eligible])
            conn.execute("DELETE FROM inbound_unmapped WHERE provider=? AND media_type=?", (PROVIDER, MEDIA))
            conn.executemany("INSERT INTO inbound_unmapped VALUES (?,?,?,?,?,?,?,?)",
                             [(PROVIDER, MEDIA, i, r.rating, r.rated_at,
                               r.trakt_id, r.imdb_id, now) for i, r in enumerate(snapshot.unmapped)])
            conn.execute("""
                INSERT INTO inbound_state VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(provider,media_type) DO UPDATE SET
                    baseline_created_at=excluded.baseline_created_at,
                    last_successful_poll_at=excluded.last_successful_poll_at,
                    snapshot_hash=excluded.snapshot_hash,generation=excluded.generation,
                    observed_count=excluded.observed_count,skipped_count=excluded.skipped_count
            """, (PROVIDER, MEDIA, now if baseline else state["baseline_created_at"],
                  now, snapshot.snapshot_hash, version, snapshot.movies, len(snapshot.unmapped)))
        return {**counts, "events": sum(counts[k] for k in ("added", "changed", "removed")),
                "movies": snapshot.movies, "eligible": len(snapshot.eligible),
                "skipped": len(snapshot.unmapped), "snapshot_hash": snapshot.snapshot_hash,
                "generation": version, "canonical_mutations": 0, "provider_writes": 0}
