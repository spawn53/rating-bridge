from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from hub.models import RatingWrite


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RatingStore:
    """SQLite source-of-truth plus transactional outbox.

    The rating row and its delivery jobs are committed in the same transaction,
    so a provider outage can never lose the user's rating.
    """

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ratings (
                    content_key TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL,
                    rating INTEGER,
                    tmdb_id INTEGER,
                    tmdb_series_id INTEGER,
                    season_number INTEGER,
                    episode_number INTEGER,
                    imdb_id TEXT,
                    trakt_id INTEGER,
                    mdblist_id TEXT,
                    title TEXT,
                    source TEXT NOT NULL,
                    rated_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_key TEXT NOT NULL,
                    target TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('upsert', 'remove')),
                    payload_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'processing', 'done', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(content_key, target, action, revision)
                );

                CREATE INDEX IF NOT EXISTS idx_outbox_ready
                    ON outbox(status, next_attempt_at, id);
                """
            )

    @staticmethod
    def _payload(item: RatingWrite) -> dict[str, Any]:
        payload = item.model_dump(mode="json", exclude={"targets"})
        payload["content_key"] = item.content_key
        return payload

    @staticmethod
    def _queue(
        conn: sqlite3.Connection,
        *,
        content_key: str,
        targets: Iterable[str],
        action: str,
        payload: dict[str, Any],
        revision: int,
        now: str,
    ) -> list[str]:
        queued: list[str] = []
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        for target in dict.fromkeys(targets):
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO outbox(
                    content_key, target, action, payload_json, revision,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (content_key, target, action, encoded, revision, now, now),
            )
            if cursor.rowcount:
                queued.append(target)
        return queued

    def upsert_rating(self, item: RatingWrite, targets: Iterable[str]) -> dict[str, Any]:
        now = _now()
        rated_at = item.rated_at.isoformat() if item.rated_at else now
        payload = self._payload(item)

        with self._connect() as conn:
            row = conn.execute(
                "SELECT revision FROM ratings WHERE content_key = ?",
                (item.content_key,),
            ).fetchone()
            revision = (int(row["revision"]) if row else 0) + 1

            conn.execute(
                """
                INSERT INTO ratings(
                    content_key, media_type, rating, tmdb_id, tmdb_series_id,
                    season_number, episode_number, imdb_id, trakt_id, mdblist_id,
                    title, source, rated_at, updated_at, revision, deleted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(content_key) DO UPDATE SET
                    media_type = excluded.media_type,
                    rating = excluded.rating,
                    tmdb_id = COALESCE(excluded.tmdb_id, ratings.tmdb_id),
                    tmdb_series_id = COALESCE(excluded.tmdb_series_id, ratings.tmdb_series_id),
                    season_number = COALESCE(excluded.season_number, ratings.season_number),
                    episode_number = COALESCE(excluded.episode_number, ratings.episode_number),
                    imdb_id = COALESCE(excluded.imdb_id, ratings.imdb_id),
                    trakt_id = COALESCE(excluded.trakt_id, ratings.trakt_id),
                    mdblist_id = COALESCE(excluded.mdblist_id, ratings.mdblist_id),
                    title = COALESCE(excluded.title, ratings.title),
                    source = excluded.source,
                    rated_at = excluded.rated_at,
                    updated_at = excluded.updated_at,
                    revision = excluded.revision,
                    deleted = 0
                """,
                (
                    item.content_key,
                    item.media_type,
                    item.rating,
                    item.tmdb_id,
                    item.tmdb_series_id,
                    item.season_number,
                    item.episode_number,
                    item.imdb_id,
                    item.trakt_id,
                    item.mdblist_id,
                    item.title,
                    item.source,
                    rated_at,
                    now,
                    revision,
                ),
            )
            queued = self._queue(
                conn,
                content_key=item.content_key,
                targets=targets,
                action="upsert",
                payload=payload,
                revision=revision,
                now=now,
            )

        return {
            "content_key": item.content_key,
            "media_type": item.media_type,
            "rating": item.rating,
            "revision": revision,
            "queued_targets": queued,
            "rated_at": rated_at,
            "updated_at": now,
        }

    def delete_rating(self, content_key: str, targets: Iterable[str]) -> dict[str, Any]:
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ratings WHERE content_key = ?",
                (content_key,),
            ).fetchone()
            if row is None or int(row["deleted"]):
                return {
                    "content_key": content_key,
                    "removed": False,
                    "revision": None,
                    "queued_targets": [],
                }

            revision = int(row["revision"]) + 1
            payload = dict(row)
            payload["rating"] = None
            payload["content_key"] = content_key

            conn.execute(
                """
                UPDATE ratings
                SET rating = NULL, deleted = 1, updated_at = ?, revision = ?
                WHERE content_key = ?
                """,
                (now, revision, content_key),
            )
            queued = self._queue(
                conn,
                content_key=content_key,
                targets=targets,
                action="remove",
                payload=payload,
                revision=revision,
                now=now,
            )

        return {
            "content_key": content_key,
            "removed": True,
            "revision": revision,
            "queued_targets": queued,
        }

    def get_rating(self, content_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ratings WHERE content_key = ? AND deleted = 0",
                (content_key,),
            ).fetchone()
        return dict(row) if row else None

    def list_ratings(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ratings
                WHERE deleted = 0
                ORDER BY rated_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_next_job(self) -> dict[str, Any] | None:
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM outbox
                WHERE status = 'pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None

            cursor = conn.execute(
                """
                UPDATE outbox
                SET status = 'processing', attempts = attempts + 1, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = conn.execute(
                "SELECT * FROM outbox WHERE id = ?", (row["id"],)
            ).fetchone()

        if claimed is None:
            return None
        job = dict(claimed)
        job["payload"] = json.loads(job.pop("payload_json"))
        return job

    def complete_job(self, job_id: int) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET status = 'done', next_attempt_at = NULL,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )

    def fail_job(self, job_id: int, error: str, *, permanent: bool) -> str:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM outbox WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return "missing"

            attempts = int(row["attempts"])
            exhausted = attempts >= 8
            if permanent or exhausted:
                status = "failed"
                next_attempt = None
            else:
                status = "pending"
                delay = min(3600, 15 * (2 ** max(0, attempts - 1)))
                next_attempt = (now_dt + timedelta(seconds=delay)).isoformat()

            conn.execute(
                """
                UPDATE outbox
                SET status = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, next_attempt, error[:1000], now, job_id),
            )
        return status

    def requeue_stale_processing(self, stale_minutes: int = 10) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
        ).isoformat()
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE outbox
                SET status = 'pending', updated_at = ?
                WHERE status = 'processing' AND updated_at <= ?
                """,
                (now, cutoff),
            )
            return int(cursor.rowcount)

    def list_outbox(self, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, content_key, target, action, revision, status, attempts,
                       next_attempt_at, last_error, created_at, updated_at
                FROM outbox
                WHERE status = ?
                ORDER BY id
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        return [dict(row) for row in rows]
