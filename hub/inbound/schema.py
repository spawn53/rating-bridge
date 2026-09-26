"""Lossless, explicit inbound-only schema initialization and migration."""
from __future__ import annotations

import re
import sqlite3

from hub.inbound.models import InboundError

MOVIE_SCHEMAS = {
    'inbound_state': """
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
""",
    'inbound_snapshots': """
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
""",
    'inbound_unmapped': """
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
""",
    'inbound_events': """
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
""",
}

LEGACY_EVENT_SCHEMA = """
CREATE TABLE inbound_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE,
 provider TEXT NOT NULL, media_type TEXT NOT NULL CHECK(media_type='movie'),
 content_key TEXT NOT NULL, generation INTEGER NOT NULL,
 event_type TEXT NOT NULL CHECK(event_type IN ('added','changed','removed')),
 old_rating INTEGER, new_rating INTEGER, provider_rated_at TEXT NOT NULL,
 detected_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('observed','ignored')),
 reason TEXT NOT NULL, classification TEXT NOT NULL, future_action TEXT
)
"""

SCHEMAS = {name: sql.replace("CHECK(media_type='movie')", "CHECK(media_type IN ('movie','show'))")
           for name, sql in MOVIE_SCHEMAS.items()}
EVENT_SCHEMA = SCHEMAS["inbound_events"]


def _signature(sql: str) -> str:
    # SQLite removes IF NOT EXISTS and quotes a table name after RENAME.
    # Preserve constraint literal case/whitespace: altered literals are unknown DDL.
    parts = re.split(r"('(?:''|[^'])*')", sql)
    return "".join(part if i % 2 else re.sub(r"\s+", "", part.replace('"', '')).lower().replace("ifnotexists", "")
                   for i, part in enumerate(parts)).rstrip(";")


def _recognized(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    if row is None:
        return None
    signature = _signature(row[0])
    if signature == _signature(SCHEMAS[name]):
        kind = "current"
    elif signature == _signature(MOVIE_SCHEMAS[name]):
        kind = "movie"
    elif name == "inbound_events" and signature == _signature(LEGACY_EVENT_SCHEMA):
        kind = "legacy"
    else:
        raise InboundError("Inbound schema was not recognized; migration refused")
    # Unrecognized attached schema objects cannot be discarded by a rebuild.
    objects = conn.execute("SELECT type,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger')", (name,))
    if any(sql is not None for _, sql in objects):
        raise InboundError("Inbound schema objects were not recognized; migration refused")
    return kind


def _rebuild(conn: sqlite3.Connection, name: str) -> None:
    sequence = None
    if name == "inbound_events":
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (name,)).fetchone()
    columns = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
    temporary = name + "_migration"
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (temporary,)).fetchone():
        raise InboundError("Inbound schema migration name was occupied; migration refused")
    conn.execute(SCHEMAS[name].replace(name, temporary, 1))
    names = ",".join(columns)  # Only exact recognized DDL reaches this point.
    conn.execute(f"INSERT INTO {temporary} ({names}) SELECT {names} FROM {name}")
    conn.execute(f"DROP TABLE {name}")
    conn.execute(f"ALTER TABLE {temporary} RENAME TO {name}")
    if name == "inbound_events":
        conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (name,))
        if sequence is not None:
            conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", (name, sequence[0]))


def migrate_inbound(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    kinds = {name: _recognized(conn, name) for name in SCHEMAS}
    if any(kind is None for kind in kinds.values()) and any(kind is not None for kind in kinds.values()):
        raise InboundError("Inbound schema was incomplete; migration refused")
    for name, kind in kinds.items():
        if kind is None:
            conn.execute(SCHEMAS[name])
        elif kind != "current":
            _rebuild(conn, name)


def migrate_events(conn: sqlite3.Connection) -> None:
    """Retain the existing explicit legacy-event migration interface."""
    conn.execute("BEGIN IMMEDIATE")
    kind = _recognized(conn, "inbound_events")
    if kind is None:
        conn.execute(EVENT_SCHEMA)
    elif kind != "current":
        _rebuild(conn, "inbound_events")
