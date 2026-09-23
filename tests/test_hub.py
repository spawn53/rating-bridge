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
    assert len(pending) == 2
    assert [row["action"] for row in pending] == ["remove", "remove"]
    assert len(store.list_outbox("superseded")) == 2

def test_episode_capabilities_skip_unverified_targets() -> None:
    from hub.capabilities import split_supported

    supported, skipped = split_supported(
        "episode", ["mdblist", "trakt", "simkl", "tmdb"]
    )
    assert supported == ("trakt", "tmdb")
    assert skipped == ("mdblist", "simkl")


def test_worker_retry_state(tmp_path: Path) -> None:
    store = RatingStore(str(tmp_path / "retry.sqlite3"))
    item = RatingWrite(media_type="movie", tmdb_id=550, rating=9)
    store.upsert_rating(item, ["tmdb"])

    job = store.claim_next_job()
    assert job is not None
    assert job["target"] == "tmdb"
    assert job["attempts"] == 1

    state = store.fail_job(job["id"], "temporary", permanent=False)
    assert state == "pending"



def test_letterboxd_rating_conversion_is_lossless() -> None:
    from hub.providers.letterboxd import rating_to_stars

    assert [rating_to_stars(value) for value in range(1, 11)] == [
        0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0
    ]


def test_imdb_builds_episode_rating_mutations() -> None:
    from hub.providers.imdb_v2 import IMDbProvider

    upsert = IMDbProvider.build_request("upsert", "tt1480055", 9)
    assert upsert["operationName"] == "UpdateTitleRating"
    assert upsert["variables"] == {"rating": 9, "titleId": "tt1480055"}
    assert "rateTitle" in str(upsert["query"])

    remove = IMDbProvider.build_request("remove", "tt1480055", None)
    assert remove["operationName"] == "DeleteTitleRating"
    assert remove["variables"] == {"titleId": "tt1480055"}
    assert "deleteTitleRating" in str(remove["query"])


def test_experimental_registry_is_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.providers.base import ProviderNotConfigured
    from hub.providers.registry import get_provider

    monkeypatch.delenv("IMDB_V2_ENABLED", raising=False)
    monkeypatch.delenv("LETTERBOXD_ENABLED", raising=False)

    with pytest.raises(ProviderNotConfigured):
        get_provider("imdb")
    with pytest.raises(ProviderNotConfigured):
        get_provider("letterboxd")


def test_experimental_registry_allows_safe_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.providers.imdb_v2 import IMDbProvider
    from hub.providers.letterboxd import LetterboxdProvider
    from hub.providers.registry import get_provider

    monkeypatch.setenv("IMDB_V2_ENABLED", "true")
    monkeypatch.setenv("IMDB_V2_DRY_RUN", "true")
    monkeypatch.delenv("IMDB_COOKIE", raising=False)

    monkeypatch.setenv("LETTERBOXD_ENABLED", "true")
    monkeypatch.setenv("LETTERBOXD_DRY_RUN", "true")
    monkeypatch.delenv("LETTERBOXD_CLIENT_ID", raising=False)
    monkeypatch.delenv("LETTERBOXD_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("LETTERBOXD_REFRESH_TOKEN", raising=False)

    assert isinstance(get_provider("imdb"), IMDbProvider)
    assert isinstance(get_provider("letterboxd"), LetterboxdProvider)