"""Optional IMDb target-plan contracts for Trakt movie/show inbound."""
import json

import pytest

from hub.inbound import importer
from hub.inbound.auto_apply import auto_apply
from hub.inbound.importer import (
    GLOBAL_TARGETS,
    IMDB_GLOBAL_TARGETS,
    IMDB_TARGETS,
    TARGETS,
    apply_event,
    validated_delivery_targets,
)
from hub.inbound.models import InboundError, MovieRating, ShowRating, Snapshot
from hub.inbound.removal import apply_removal_event
from hub.inbound.storage import InboundStore
from hub.inbound.trakt import observe
from hub.store import RatingStore

DATE = "2026-09-27T12:00:00.000000+00:00"


@pytest.mark.parametrize(
    "plan,expected",
    [(GLOBAL_TARGETS, TARGETS), (IMDB_GLOBAL_TARGETS, IMDB_TARGETS)],
)
def test_only_explicitly_audited_target_plans_are_accepted(plan, expected):
    assert validated_delivery_targets(plan) == expected


@pytest.mark.parametrize(
    "plan",
    [
        ("tmdb", "trakt", "simkl"),
        ("trakt", "tmdb", "simkl", "mdblist"),
        ("tmdb", "trakt", "simkl", "mdblist", "letterboxd"),
        ("tmdb", "trakt", "simkl", "mdblist", "imdb", "letterboxd"),
        ("tmdb", "trakt", "simkl", "mdblist", "imdb", "imdb"),
    ],
)
def test_unknown_incomplete_reordered_or_duplicate_target_plans_fail_closed(plan):
    with pytest.raises(InboundError, match="explicitly audited"):
        validated_delivery_targets(plan)


@pytest.mark.parametrize(
    "media_type,model,tmdb_id,trakt_id,imdb_id",
    [
        ("movie", MovieRating, 265189, 163864, "tt2121382"),
        ("show", ShowRating, 195339, 194117, "tt11680642"),
    ],
)
def test_imdb_plan_upsert_queues_exact_four_non_trakt_jobs(
    tmp_path, media_type, model, tmdb_id, trakt_id, imdb_id
):
    path = str(tmp_path / f"{media_type}.sqlite3")
    RatingStore(path)
    store = InboundStore(path, media_type=media_type)
    observe(store, lambda: Snapshot((), media_type=media_type), baseline=True)
    observe(
        store,
        lambda: Snapshot(
            (model(8, DATE, tmdb_id, trakt_id, imdb_id),), media_type=media_type
        ),
    )

    result = apply_event(
        store,
        IMDB_GLOBAL_TARGETS,
        event_id=1,
        expected_key=f"{media_type}:tmdb:{tmdb_id}",
        expected_rating=8,
        expected_generation=2,
        expected_revision=0,
        confirmed=True,
    )

    assert tuple(result["queued_targets"]) == IMDB_TARGETS
    with store.connect() as conn:
        canonical = dict(conn.execute("SELECT * FROM ratings").fetchone())
        jobs = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM outbox WHERE revision=1 ORDER BY id"
            )
        ]
    assert canonical["imdb_id"] == imdb_id
    assert len(jobs) == 4
    assert tuple(job["target"] for job in jobs) == IMDB_TARGETS
    assert all(job["action"] == "upsert" for job in jobs)
    assert all(json.loads(job["payload_json"]) == canonical for job in jobs)
    assert "trakt" not in {job["target"] for job in jobs}


@pytest.mark.parametrize(
    "media_type,model,tmdb_id,trakt_id,imdb_id",
    [
        ("movie", MovieRating, 265189, 163864, "tt2121382"),
        ("show", ShowRating, 195339, 194117, "tt11680642"),
    ],
)
def test_imdb_plan_removal_queues_exact_four_tombstone_jobs(
    tmp_path, media_type, model, tmdb_id, trakt_id, imdb_id
):
    path = str(tmp_path / f"{media_type}.sqlite3")
    RatingStore(path)
    store = InboundStore(path, media_type=media_type)
    key = f"{media_type}:tmdb:{tmdb_id}"
    observe(store, lambda: Snapshot((), media_type=media_type), baseline=True)
    observe(
        store,
        lambda: Snapshot(
            (model(8, DATE, tmdb_id, trakt_id, imdb_id),), media_type=media_type
        ),
    )
    apply_event(
        store,
        IMDB_GLOBAL_TARGETS,
        event_id=1,
        expected_key=key,
        expected_rating=8,
        expected_generation=2,
        expected_revision=0,
        confirmed=True,
    )
    with store.connect() as conn:
        conn.execute("UPDATE outbox SET status='done'")
    observe(store, lambda: Snapshot((), media_type=media_type))

    result = apply_removal_event(
        store,
        IMDB_GLOBAL_TARGETS,
        event_id=2,
        expected_key=key,
        expected_generation=3,
        expected_old_rating=8,
        expected_revision=1,
        expected_source="trakt-inbound:1",
        confirmed=True,
    )

    assert tuple(result["queued_targets"]) == IMDB_TARGETS
    with store.connect() as conn:
        canonical = dict(conn.execute("SELECT * FROM ratings").fetchone())
        jobs = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM outbox WHERE revision=2 ORDER BY id"
            )
        ]
    assert canonical["deleted"] == 1 and canonical["rating"] is None
    assert len(jobs) == 4
    assert tuple(job["target"] for job in jobs) == IMDB_TARGETS
    assert all(job["action"] == "remove" for job in jobs)
    assert all(json.loads(job["payload_json"]) == canonical for job in jobs)
    assert "trakt" not in {job["target"] for job in jobs}


def test_audit_gap_recovery_is_bound_to_the_original_imdb_target_plan(tmp_path, monkeypatch):
    path = str(tmp_path / "audit.sqlite3")
    RatingStore(path)
    store = InboundStore(path)
    observe(store, lambda: Snapshot(()), baseline=True)
    observe(
        store,
        lambda: Snapshot((MovieRating(8, DATE, 265189, 163864, "tt2121382"),)),
    )

    original = importer._mark_applied
    monkeypatch.setattr(
        importer,
        "_mark_applied",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit gap")),
    )
    with pytest.raises(RuntimeError, match="audit gap"):
        apply_event(
            store,
            IMDB_GLOBAL_TARGETS,
            event_id=1,
            expected_key="movie:tmdb:265189",
            expected_rating=8,
            expected_generation=2,
            expected_revision=0,
            confirmed=True,
        )
    monkeypatch.setattr(importer, "_mark_applied", original)

    with pytest.raises(InboundError, match="audited target plan"):
        apply_event(
            store,
            GLOBAL_TARGETS,
            event_id=1,
            expected_key="movie:tmdb:265189",
            expected_rating=8,
            expected_generation=2,
            expected_revision=0,
            confirmed=True,
        )

    result = apply_event(
        store,
        IMDB_GLOBAL_TARGETS,
        event_id=1,
        expected_key="movie:tmdb:265189",
        expected_rating=8,
        expected_generation=2,
        expected_revision=0,
        confirmed=True,
    )
    assert result["already_applied"] is True
    assert result["queued_targets"] == []


@pytest.mark.parametrize("media_type,model", [("movie", MovieRating), ("show", ShowRating)])
def test_auto_apply_accepts_imdb_plan_without_direct_provider_io(tmp_path, media_type, model):
    path = str(tmp_path / f"auto-{media_type}.sqlite3")
    RatingStore(path)
    store = InboundStore(path, media_type=media_type)
    tmdb_id = 550 if media_type == "show" else 265189
    observe(store, lambda: Snapshot((), media_type=media_type), baseline=True)
    observe(
        store,
        lambda: Snapshot(
            (model(8, DATE, tmdb_id, 456, "tt1234567"),),
            media_type=media_type,
        ),
    )
    result = auto_apply(store, IMDB_GLOBAL_TARGETS, generation=2, echo_grace_seconds=0)
    assert result["auto_candidates"] == result["auto_applied"] == 1
    assert result["canonical_mutations"] == 1
    assert result["provider_writes"] == 0
    with store.connect() as conn:
        targets = [
            row[0]
            for row in conn.execute(
                "SELECT target FROM outbox WHERE revision=1 ORDER BY id"
            )
        ]
    assert tuple(targets) == IMDB_TARGETS
