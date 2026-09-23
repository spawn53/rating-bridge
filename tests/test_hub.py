from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from hub.models import RatingWrite
from hub.store import RatingStore


def test_movie_key() -> None:
    item = RatingWrite(media_type="movie", tmdb_id=550, rating=9)
    assert item.content_key == "movie:tmdb:550"


def test_episode_key_requires_parent_coordinates() -> None:
    with pytest.raises(ValidationError):
        RatingWrite(media_type="episode", tmdb_id=123, rating=8)

    item = RatingWrite(
        media_type="episode",
        tmdb_id=123,
        tmdb_series_id=1399,
        season_number=1,
        episode_number=1,
        rating=8,
    )
    assert item.content_key == "episode:tmdb:1399:s1:e1"


def test_store_upsert_and_delete_are_outboxed(tmp_path: Path) -> None:
    store = RatingStore(str(tmp_path / "hub.sqlite3"))
    item = RatingWrite(
        media_type="episode",
        tmdb_id=63056,
        tmdb_series_id=1399,
        season_number=1,
        episode_number=1,
        imdb_id="tt1480055",
        rating=10,
    )

    created = store.upsert_rating(item, ["trakt", "tmdb"])
    assert created["revision"] == 1
    assert created["queued_targets"] == ["trakt", "tmdb"]
    assert store.get_rating(item.content_key)["rating"] == 10

    pending = store.list_outbox()
    assert [(row["target"], row["action"]) for row in pending] == [
        ("trakt", "upsert"),
        ("tmdb", "upsert"),
    ]

    removed = store.delete_rating(item.content_key, ["trakt", "tmdb"])
    assert removed["removed"] is True
    assert removed["revision"] == 2
    assert store.get_rating(item.content_key) is None

    pending = store.list_outbox()
    assert len(pending) == 4
    assert [row["action"] for row in pending[-2:]] == ["remove", "remove"]
