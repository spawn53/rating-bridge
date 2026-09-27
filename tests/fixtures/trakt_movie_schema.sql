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
