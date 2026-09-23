from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from hub.models import RatingWrite


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return utc_now().isoformat()


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
                        CHECK(status IN ('pending', 'processing', 'done', 'failed', 'superseded')),
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
            # Existing V2 databases have a status CHECK without 'superseded'.
            # Rebuild only that table, preserving every outbox row and its id.
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'outbox'"
            ).fetchone()[0]
            if "'superseded'" not in schema:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ALTER TABLE outbox RENAME TO outbox_legacy")
                conn.execute("""
                    CREATE TABLE outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        content_key TEXT NOT NULL,
                        target TEXT NOT NULL,
                        action TEXT NOT NULL CHECK(action IN ('upsert', 'remove')),
                        payload_json TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending', 'processing', 'done', 'failed', 'superseded')),
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(content_key, target, action, revision)
                    )
                """)
                conn.execute("""
                    INSERT INTO outbox SELECT id, content_key, target, action,
                        payload_json, revision, status, attempts, next_attempt_at,
                        last_error, created_at, updated_at FROM outbox_legacy
                """)
                conn.execute("DROP TABLE outbox_legacy")
                conn.execute("""
                    CREATE INDEX idx_outbox_ready
                    ON outbox(status, next_attempt_at, id)
                """)

    @staticmethod
    def _supersede_pending(
        conn: sqlite3.Connection, content_key: str, revision: int, now: str
    ) -> None:
        conn.execute(
            """
            UPDATE outbox SET status = 'superseded', next_attempt_at = NULL,
                last_error = ?, updated_at = ?
            WHERE content_key = ? AND revision < ? AND status = 'pending'
            """,
            (f"superseded by canonical revision {revision}", now, content_key, revision),
        )

    @staticmethod
    def _unchanged(row: sqlite3.Row, item: RatingWrite) -> bool:
        if int(row['deleted']) or row['rating'] != item.rating or row['source'] != item.source:
            return False
        if row['media_type'] != item.media_type:
            return False
        for field in (
            'tmdb_id', 'tmdb_series_id', 'season_number', 'episode_number',
            'imdb_id', 'trakt_id', 'mdblist_id', 'title'
        ):
            value = getattr(item, field)
            if value is not None and row[field] != value:
                return False
        if item.rated_at is not None and row['rated_at'] != item.rated_at.isoformat():
            return False
        return True

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
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM ratings WHERE content_key = ?", (item.content_key,)
            ).fetchone()
            unchanged = row is not None and self._unchanged(row, item)
            if unchanged:
                revision = int(row['revision'])
                rated_at = str(row['rated_at'])
                updated_at = str(row['updated_at'])
            else:
                revision = (int(row['revision']) if row else 0) + 1
                rated_at = (
                    item.rated_at.isoformat() if item.rated_at
                    else str(row['rated_at']) if row and not int(row['deleted']) else now
                )
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
                        item.content_key, item.media_type, item.rating, item.tmdb_id,
                        item.tmdb_series_id, item.season_number, item.episode_number,
                        item.imdb_id, item.trakt_id, item.mdblist_id, item.title,
                        item.source, rated_at, now, revision,
                    ),
                )
                self._supersede_pending(conn, item.content_key, revision, now)
                updated_at = now

            current = conn.execute(
                "SELECT * FROM ratings WHERE content_key = ?", (item.content_key,)
            ).fetchone()
            queued = self._queue(
                conn, content_key=item.content_key, targets=targets, action="upsert",
                payload=dict(current), revision=revision, now=now,
            )

        return {
            "content_key": item.content_key,
            "media_type": item.media_type,
            "rating": item.rating,
            "revision": revision,
            "queued_targets": queued,
            "rated_at": rated_at,
            "updated_at": updated_at,
        }

    def delete_rating(self, content_key: str, targets: Iterable[str]) -> dict[str, Any]:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
            payload["revision"] = revision
            payload["deleted"] = 1
            payload["content_key"] = content_key

            conn.execute(
                """
                UPDATE ratings
                SET rating = NULL, deleted = 1, updated_at = ?, revision = ?
                WHERE content_key = ?
                """,
                (now, revision, content_key),
            )
            self._supersede_pending(conn, content_key, revision, now)
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
            conn.execute("BEGIN IMMEDIATE")
            while True:
                row = conn.execute(
                    """
                    SELECT * FROM outbox AS candidate
                    WHERE candidate.status = 'pending'
                      AND (candidate.next_attempt_at IS NULL OR candidate.next_attempt_at <= ?)
                      AND NOT EXISTS (
                          SELECT 1 FROM outbox AS active
                          WHERE active.content_key = candidate.content_key
                            AND active.target = candidate.target
                            AND active.status = 'processing'
                            AND active.id != candidate.id
                      )
                    ORDER BY candidate.id LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if row is None:
                    return None
                canonical = conn.execute(
                    "SELECT revision, deleted FROM ratings WHERE content_key = ?",
                    (row['content_key'],),
                ).fetchone()
                if canonical is None or (
                    int(canonical['revision']) != int(row['revision'])
                    or bool(canonical['deleted']) != (row['action'] == 'remove')
                ):
                    conn.execute(
                        """
                        UPDATE outbox SET status = 'superseded', next_attempt_at = NULL,
                            last_error = 'superseded by canonical state', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, row['id']),
                    )
                    continue
                conn.execute(
                    """
                    UPDATE outbox SET status = 'processing', attempts = attempts + 1,
                        next_attempt_at = NULL, updated_at = ? WHERE id = ?
                    """,
                    (now, row['id']),
                )
                claimed = conn.execute(
                    "SELECT * FROM outbox WHERE id = ?", (row['id'],)
                ).fetchone()
                job = dict(claimed)
                job['payload'] = json.loads(job.pop('payload_json'))
                return job

    def is_current_job(self, job_id: int, attempts: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT o.revision, o.action, o.status, o.attempts,
                       r.revision AS current_revision, r.deleted
                FROM outbox o JOIN ratings r ON r.content_key = o.content_key
                WHERE o.id = ?
                """, (job_id,),
            ).fetchone()
        return bool(row and row['status'] == 'processing'
                    and row['attempts'] == attempts
                    and row['revision'] == row['current_revision']
                    and bool(row['deleted']) == (row['action'] == 'remove'))

    def supersede_job(self, job_id: int, attempts: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE outbox SET status = 'superseded', next_attempt_at = NULL,
                    last_error = 'superseded by canonical state', updated_at = ?
                WHERE id = ? AND status = 'processing' AND attempts = ?
                """, (_now(), job_id, attempts),
            )

    def complete_job(self, job_id: int, attempts: int | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE outbox SET status = 'done', next_attempt_at = NULL,
                    last_error = NULL, updated_at = ?
                WHERE id = ? AND status = 'processing'
                  AND (? IS NULL OR attempts = ?)
                """, (_now(), job_id, attempts, attempts),
            )

    def fail_job(
        self, job_id: int, error: str, *, permanent: bool,
        attempts: int | None = None,
    ) -> str:
        now_dt = utc_now()
        now = now_dt.isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempts, status FROM outbox WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return 'missing'
            if row['status'] != 'processing' or (
                attempts is not None and int(row['attempts']) != attempts
            ):
                return str(row['status'])
            count = int(row['attempts'])
            if permanent or count >= 8:
                status, next_attempt = 'failed', None
            else:
                status = 'pending'
                delay = min(3600, 15 * (2 ** max(0, count - 1)))
                next_attempt = (now_dt + timedelta(seconds=delay)).isoformat()
            conn.execute(
                """
                UPDATE outbox SET status = ?, next_attempt_at = ?,
                    last_error = ?, updated_at = ? WHERE id = ?
                """, (status, next_attempt, error[:1000], now, job_id),
            )
            return status

    def requeue_stale_processing(self, stale_minutes: int = 10) -> int:
        now_dt = utc_now()
        cutoff = (now_dt - timedelta(minutes=stale_minutes)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE outbox SET status = CASE WHEN attempts >= 8 THEN 'failed'
                    ELSE 'pending' END,
                    last_error = CASE WHEN attempts >= 8
                        THEN 'processing lease expired after max attempts'
                        ELSE last_error END,
                    next_attempt_at = NULL, updated_at = ?
                WHERE status = 'processing' AND updated_at <= ?
                """, (now_dt.isoformat(), cutoff),
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
