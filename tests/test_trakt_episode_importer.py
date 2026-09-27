"""Offline guarded episode canonical/outbox integration tests."""
from dataclasses import asdict, replace
import json
import sqlite3

import pytest

from hub.inbound import episode_importer
from hub.inbound.auto_apply import AutoApplyError, auto_apply
from hub.inbound.episode_importer import (
    EPISODE_DELIVERY_TARGETS,
    EPISODE_SOURCE_TARGETS,
    apply_episode_event,
    apply_episode_removal,
    validated_episode_targets,
)
from hub.inbound.episode_models import EpisodeRating, EpisodeSnapshot
from hub.inbound.episode_storage import EpisodeStore
from hub.inbound.episodes import main, observe_episode
from hub.inbound.importer import GLOBAL_TARGETS, TARGETS, validated_delivery_targets
from hub.inbound.models import InboundError, timestamp
from hub.models import RatingWrite
from hub.providers.tmdb import TMDbProvider
from hub.store import RatingStore


DATE = "2026-09-26T10:00:00.000000+00:00"
KEY = "episode:tmdb:195339:s1:e1"


def episode(score=8, series=195339, season=1, number=1, tmdb=42,
            trakt=194117, imdb="tt11680642", rated_at=DATE):
    return EpisodeRating(score, rated_at, series, season, number, trakt, imdb, tmdb)


def snapshot(*values):
    return EpisodeSnapshot(tuple(values))


def rows(store, table):
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is None:
            return []
        return [dict(row) for row in conn.execute("SELECT * FROM " + table + " ORDER BY 1")]


TABLES = ("ratings", "outbox", "inbound_episode_state", "inbound_episode_snapshots",
          "inbound_episode_unmapped", "inbound_episode_events", "inbound_state",
          "inbound_snapshots", "inbound_unmapped", "inbound_events")


def dump(store):
    return {table: rows(store, table) for table in TABLES}


def mutate(store, sql, args=()):
    with sqlite3.connect(store.path) as conn:
        conn.execute(sql, args)


@pytest.fixture
def added_store(tmp_path):
    path = str(tmp_path / "hub.sqlite3")
    RatingStore(path)
    store = EpisodeStore(path)
    observe_episode(store, lambda: snapshot(), baseline=True)
    observe_episode(store, lambda: snapshot(episode()))
    assert rows(store, "inbound_episode_events")[0]["generation"] == 2
    return store


def apply_added(store, **changes):
    values = dict(event_id=1, expected_key=KEY, expected_rating=8,
                  expected_generation=2, expected_revision=0, confirmed=True)
    values.update(changes)
    return apply_episode_event(store, EPISODE_SOURCE_TARGETS, **values)


def prepare_changed(store):
    apply_added(store)
    observe_episode(store, lambda: snapshot(episode(score=7)))
    return 2


def apply_changed(store, **changes):
    values = dict(event_id=2, expected_key=KEY, expected_rating=7,
                  expected_generation=3, expected_revision=1, confirmed=True)
    values.update(changes)
    return apply_episode_event(store, EPISODE_SOURCE_TARGETS, **values)


def prepare_removed(store):
    prepare_changed(store)
    apply_changed(store)
    observe_episode(store, lambda: snapshot())
    return 3


def apply_removed(store, **changes):
    values = dict(event_id=3, expected_key=KEY, expected_old_rating=7,
                  expected_generation=4, expected_revision=2,
                  expected_source="trakt-episode-inbound:2", confirmed=True)
    values.update(changes)
    return apply_episode_removal(store, EPISODE_SOURCE_TARGETS, **values)


def test_exact_episode_target_contract():
    assert validated_episode_targets(EPISODE_SOURCE_TARGETS) == EPISODE_DELIVERY_TARGETS == ("tmdb",)


@pytest.mark.parametrize("plan", [(), ("tmdb",), ("trakt",), ("tmdb", "trakt"),
    ("trakt", "tmdb", "tmdb"), ("trakt", "tmdb", "imdb"),
    ("trakt", "tmdb", "simkl"), ("trakt", "tmdb", "mdblist"),
    ("trakt", "tmdb", "letterboxd"), GLOBAL_TARGETS, tuple(reversed(GLOBAL_TARGETS))])
def test_malformed_reordered_duplicate_or_expanded_target_plan_refused(added_store, plan):
    before = dump(added_store)
    with pytest.raises(InboundError, match="exactly trakt,tmdb"):
        apply_episode_event(added_store, plan, event_id=1, expected_key=KEY,
                            expected_rating=8, expected_generation=2,
                            expected_revision=0, confirmed=True)
    assert dump(added_store) == before


def test_added_episode_creates_canonical_and_exactly_one_tmdb_job(added_store):
    before = dump(added_store)
    result = apply_added(added_store)
    assert result == {"event_id": 1, "content_key": KEY, "rating": 8, "revision": 1,
                      "queued_targets": ["tmdb"], "skipped_targets": ["trakt"],
                      "already_applied": False, "direct_provider_writes": 0}
    canonical = rows(added_store, "ratings")[0]
    assert canonical["media_type"] == "episode" and canonical["content_key"] == KEY
    assert (canonical["tmdb_series_id"], canonical["season_number"],
            canonical["episode_number"], canonical["tmdb_id"]) == (195339, 1, 1, 42)
    assert (canonical["trakt_id"], canonical["imdb_id"]) == (194117, "tt11680642")
    assert canonical["rating"] == 8 and canonical["deleted"] == 0
    assert canonical["source"] == "trakt-episode-inbound:1"
    assert timestamp(canonical["rated_at"]) == DATE
    jobs = rows(added_store, "outbox")
    assert len(jobs) == 1 and jobs[0]["target"] == "tmdb"
    assert jobs[0]["action"] == "upsert" and jobs[0]["revision"] == 1
    assert json.loads(jobs[0]["payload_json"]) == canonical
    event = rows(added_store, "inbound_episode_events")[0]
    assert event["status"] == "applied" and event["canonical_revision"] == 1
    assert event["applied_at"] is not None
    for table in ("inbound_episode_state", "inbound_episode_snapshots",
                  "inbound_episode_unmapped", "inbound_state", "inbound_snapshots",
                  "inbound_unmapped", "inbound_events"):
        assert rows(added_store, table) == before[table]


def test_changed_episode_increments_revision_and_adds_one_tmdb_job(added_store):
    prepare_changed(added_store)
    before = dump(added_store)
    result = apply_changed(added_store)
    assert result["revision"] == 2 and result["queued_targets"] == ["tmdb"]
    canonical = rows(added_store, "ratings")[0]
    assert canonical["rating"] == 7 and canonical["revision"] == 2
    jobs = [job for job in rows(added_store, "outbox") if job["revision"] == 2]
    assert len(jobs) == 1 and (jobs[0]["target"], jobs[0]["action"]) == ("tmdb", "upsert")
    assert json.loads(jobs[0]["payload_json"]) == canonical
    assert rows(added_store, "inbound_episode_events")[1]["canonical_revision"] == 2
    assert rows(added_store, "inbound_episode_state") == before["inbound_episode_state"]


def test_removed_episode_creates_tombstone_and_one_tmdb_remove_job(added_store):
    prepare_removed(added_store)
    rated_at = rows(added_store, "ratings")[0]["rated_at"]
    result = apply_removed(added_store)
    assert result["revision"] == 3 and result["removed"] is True
    assert result["queued_targets"] == ["tmdb"] and result["direct_provider_writes"] == 0
    canonical = rows(added_store, "ratings")[0]
    assert canonical["rating"] is None and canonical["deleted"] == 1
    assert canonical["revision"] == 3 and canonical["rated_at"] == rated_at
    assert canonical["source"] == "trakt-episode-inbound:3"
    assert (canonical["tmdb_series_id"], canonical["season_number"],
            canonical["episode_number"], canonical["tmdb_id"], canonical["trakt_id"],
            canonical["imdb_id"]) == (195339, 1, 1, 42, 194117, "tt11680642")
    jobs = [job for job in rows(added_store, "outbox") if job["revision"] == 3]
    assert len(jobs) == 1 and (jobs[0]["target"], jobs[0]["action"]) == ("tmdb", "remove")
    assert json.loads(jobs[0]["payload_json"]) == canonical


@pytest.mark.parametrize("forbidden", ["trakt", "imdb", "simkl", "mdblist", "letterboxd"])
def test_no_forbidden_episode_target_is_ever_queued(added_store, forbidden):
    prepare_removed(added_store)
    apply_removed(added_store)
    assert forbidden not in {job["target"] for job in rows(added_store, "outbox")}


@pytest.mark.parametrize("field,value", [
    ("confirmed", False), ("event_id", 0), ("event_id", True), ("event_id", 999),
    ("expected_key", "movie:tmdb:195339"),
    ("expected_key", "episode:tmdb:195339:s01:e1"),
    ("expected_key", "episode:tmdb:195339:s1:e01"),
    ("expected_key", "episode:tmdb:195339:s1:e2"),
    ("expected_rating", 7), ("expected_rating", True), ("expected_rating", 0),
    ("expected_generation", 1), ("expected_generation", True),
    ("expected_revision", 1), ("expected_revision", True),
])
def test_upsert_expectation_guards_are_read_only(added_store, field, value):
    before = dump(added_store)
    with pytest.raises(InboundError):
        apply_added(added_store, **{field: value})
    assert dump(added_store) == before


@pytest.mark.parametrize("field,value", [
    ("confirmed", False), ("event_id", 0), ("event_id", 99),
    ("expected_key", "episode:tmdb:195339:s2:e1"),
    ("expected_old_rating", 8), ("expected_old_rating", True),
    ("expected_generation", 3), ("expected_revision", 1),
    ("expected_source", ""), ("expected_source", "wrong"),
    ("expected_source", "trakt-episode-inbound:3"),
])
def test_removal_expectation_guards_are_read_only(added_store, field, value):
    prepare_removed(added_store)
    before = dump(added_store)
    with pytest.raises(InboundError):
        apply_removed(added_store, **{field: value})
    assert dump(added_store) == before


@pytest.mark.parametrize("sql", [
    "UPDATE inbound_episode_events SET provider='simkl'",
    "UPDATE inbound_episode_events SET media_type='show'",
    "UPDATE inbound_episode_events SET classification='echo'",
    "UPDATE inbound_episode_events SET reason='other'",
    "UPDATE inbound_episode_events SET future_action='delete'",
    "UPDATE inbound_episode_events SET event_type='removed',old_rating=8,new_rating=NULL",
    "UPDATE inbound_episode_events SET fingerprint='bad'",
    "UPDATE inbound_episode_events SET rating_json='{}'",
    "UPDATE inbound_episode_events SET provider_rated_at='bad'",
    "UPDATE inbound_episode_snapshots SET rating=7",
    "UPDATE inbound_episode_snapshots SET tmdb_series_id=195340",
    "UPDATE inbound_episode_snapshots SET season_number=2",
    "UPDATE inbound_episode_snapshots SET episode_number=2",
    "UPDATE inbound_episode_snapshots SET trakt_id=99",
    "UPDATE inbound_episode_state SET generation=3",
])
def test_malformed_event_identity_snapshot_or_action_refused(added_store, sql):
    before = dump(added_store)
    try:
        mutate(added_store, sql)
    except sqlite3.IntegrityError:
        assert dump(added_store) == before
        return
    before = dump(added_store)
    with pytest.raises(InboundError):
        apply_added(added_store)
    assert dump(added_store) == before


def test_newer_event_supersedes_old_intent(added_store):
    observe_episode(added_store, lambda: snapshot(episode(score=7)))
    before = dump(added_store)
    with pytest.raises(InboundError, match="newer"):
        apply_added(added_store)
    assert dump(added_store) == before


@pytest.mark.parametrize("field,value", [
    ("media_type", "show"), ("tmdb_series_id", 195340), ("season_number", 2),
    ("episode_number", 2), ("tmdb_id", 43), ("trakt_id", 99),
    ("imdb_id", "tt11680643"),
])
def test_canonical_identity_mismatch_refused(added_store, field, value):
    canonical = RatingStore(added_store.path)
    canonical.upsert_rating(RatingWrite(media_type="episode", rating=6,
        tmdb_series_id=195339, season_number=1, episode_number=1,
        tmdb_id=42, trakt_id=194117, imdb_id="tt11680642"), [])
    mutate(added_store, "UPDATE ratings SET " + field + "=?", (value,))
    before = dump(added_store)
    with pytest.raises(InboundError): apply_added(added_store, expected_revision=1)
    assert dump(added_store) == before


def test_movie_show_and_episode_same_numeric_ids_do_not_collide(added_store):
    hub = RatingStore(added_store.path)
    hub.upsert_rating(RatingWrite(media_type="movie", tmdb_id=195339, rating=5), [])
    hub.upsert_rating(RatingWrite(media_type="show", tmdb_id=195339, rating=6), [])
    apply_added(added_store)
    canonical = {row["content_key"]: row for row in rows(added_store, "ratings")}
    assert set(canonical) == {"movie:tmdb:195339", "show:tmdb:195339", KEY}
    assert [canonical[key]["rating"] for key in sorted(canonical)] == [8, 5, 6]


def test_same_episode_number_across_seasons_and_series_isolated(tmp_path):
    path = str(tmp_path / "identity.sqlite3"); RatingStore(path); store = EpisodeStore(path)
    values = (episode(), episode(series=195339, season=2, tmdb=43, trakt=194118,
                                 imdb="tt11680643"),
              episode(series=195340, season=1, tmdb=44, trakt=194119,
                      imdb="tt11680644"))
    observe_episode(store, lambda: snapshot(), baseline=True)
    observe_episode(store, lambda: snapshot(*values))
    for event in rows(store, "inbound_episode_events"):
        apply_episode_event(store, EPISODE_SOURCE_TARGETS, event_id=event["id"],
            expected_key=event["content_key"], expected_rating=8,
            expected_generation=2, expected_revision=0, confirmed=True)
    assert {row["content_key"] for row in rows(store, "ratings")} == {
        "episode:tmdb:195339:s1:e1", "episode:tmdb:195339:s2:e1",
        "episode:tmdb:195340:s1:e1"}
    assert len(rows(store, "outbox")) == 3


def test_outbox_insert_failure_rolls_back_canonical_and_event(added_store):
    with sqlite3.connect(added_store.path) as conn:
        conn.execute("""CREATE TRIGGER abort_episode_job BEFORE INSERT ON outbox
            WHEN NEW.content_key LIKE 'episode:%' BEGIN SELECT RAISE(ABORT,'offline'); END""")
    before = dump(added_store)
    with pytest.raises(sqlite3.DatabaseError): apply_added(added_store)
    assert dump(added_store) == before


def test_validation_and_canonical_write_share_one_lock(added_store, monkeypatch):
    original = RatingStore.upsert_rating
    def guarded(self, item, targets, **kwargs):
        assert kwargs["connection"].in_transaction
        with sqlite3.connect(added_store.path, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE ratings SET rating=1")
        return original(self, item, targets, **kwargs)
    monkeypatch.setattr(RatingStore, "upsert_rating", guarded)
    assert apply_added(added_store)["revision"] == 1


def test_external_transaction_owner_can_roll_back_episode_primitive(added_store):
    hub = RatingStore(added_store.path, initialize=False)
    with added_store.connect_for_import() as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = hub.upsert_rating(RatingWrite(media_type="episode", rating=8,
            tmdb_series_id=195339, season_number=1, episode_number=1),
            EPISODE_DELIVERY_TARGETS, connection=conn)
        assert result["queued_targets"] == ["tmdb"] and conn.in_transaction
        conn.rollback()
    assert rows(added_store, "ratings") == [] and rows(added_store, "outbox") == []


def crash_upsert(store, monkeypatch):
    original = episode_importer._mark_applied
    monkeypatch.setattr(episode_importer, "_mark_applied",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("process exited")))
    with pytest.raises(RuntimeError): apply_added(store)
    monkeypatch.setattr(episode_importer, "_mark_applied", original)
    assert rows(store, "ratings")[0]["revision"] == 1
    assert rows(store, "inbound_episode_events")[0]["status"] == "observed"


def test_upsert_crash_gap_recovery_does_not_duplicate_revision_or_job(added_store, monkeypatch):
    crash_upsert(added_store, monkeypatch); committed = dump(added_store)
    monkeypatch.setattr(RatingStore, "upsert_rating", lambda *a, **k: pytest.fail("must not replay"))
    result = apply_added(EpisodeStore(added_store.path, initialize=False))
    assert result["already_applied"] and result["revision"] == 1
    after = dump(added_store)
    assert after["ratings"] == committed["ratings"] and after["outbox"] == committed["outbox"]
    assert after["inbound_episode_events"][0]["status"] == "applied"


@pytest.mark.parametrize("sql", [
    "UPDATE ratings SET source='other'", "UPDATE ratings SET revision=2",
    "UPDATE ratings SET rating=9", "UPDATE ratings SET deleted=1",
    "UPDATE ratings SET tmdb_series_id=195340", "UPDATE ratings SET season_number=2",
    "UPDATE ratings SET episode_number=2", "UPDATE ratings SET tmdb_id=43",
    "UPDATE ratings SET trakt_id=99", "UPDATE ratings SET imdb_id='tt11680643'",
    "UPDATE ratings SET rated_at='2026-09-26T12:00:00+00:00'",
    "UPDATE outbox SET target='trakt'", "UPDATE outbox SET action='remove'",
    "UPDATE outbox SET payload_json='{}'", "UPDATE outbox SET payload_json='bad'",
    "DELETE FROM outbox",
])
def test_upsert_crash_recovery_refuses_corrupted_or_missing_audit(added_store, monkeypatch, sql):
    crash_upsert(added_store, monkeypatch); mutate(added_store, sql); before = dump(added_store)
    with pytest.raises(InboundError): apply_added(added_store)
    assert dump(added_store) == before


def test_upsert_crash_recovery_refuses_extra_job(added_store, monkeypatch):
    crash_upsert(added_store, monkeypatch)
    mutate(added_store, """INSERT INTO outbox
        (content_key,target,action,payload_json,revision,status,attempts,created_at,updated_at)
        SELECT content_key,'imdb',action,payload_json,revision,'pending',0,created_at,updated_at
        FROM outbox""")
    before = dump(added_store)
    with pytest.raises(InboundError): apply_added(added_store)
    assert dump(added_store) == before


def test_already_applied_upsert_is_idempotent_and_read_only(added_store):
    apply_added(added_store); before = dump(added_store)
    again = apply_added(added_store)
    assert again["already_applied"] and again["queued_targets"] == []
    assert dump(added_store) == before
    RatingStore(added_store.path).upsert_rating(RatingWrite(media_type="episode", rating=9,
        tmdb_series_id=195339, season_number=1, episode_number=1), [])
    before = dump(added_store)
    assert apply_added(added_store)["revision"] == 1 and dump(added_store) == before


def crash_removal(store, monkeypatch):
    original = episode_importer._mark_applied
    monkeypatch.setattr(episode_importer, "_mark_applied",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("process exited")))
    with pytest.raises(RuntimeError): apply_removed(store)
    monkeypatch.setattr(episode_importer, "_mark_applied", original)
    assert rows(store, "ratings")[0]["revision"] == 3
    assert rows(store, "inbound_episode_events")[2]["status"] == "observed"


def test_removal_crash_gap_recovery_does_not_duplicate_tombstone_or_job(added_store, monkeypatch):
    prepare_removed(added_store); crash_removal(added_store, monkeypatch); committed = dump(added_store)
    monkeypatch.setattr(RatingStore, "delete_rating", lambda *a, **k: pytest.fail("must not replay"))
    result = apply_removed(EpisodeStore(added_store.path, initialize=False))
    assert result["already_applied"] and result["revision"] == 3
    after = dump(added_store)
    assert after["ratings"] == committed["ratings"] and after["outbox"] == committed["outbox"]
    assert after["inbound_episode_events"][2]["status"] == "applied"


@pytest.mark.parametrize("sql", [
    "UPDATE ratings SET source='other'", "UPDATE ratings SET deleted=0,rating=7",
    "UPDATE ratings SET revision=4", "UPDATE ratings SET tmdb_series_id=195340",
    "UPDATE ratings SET rated_at='2026-09-26T12:00:00+00:00'",
    "UPDATE outbox SET payload_json='{}' WHERE revision=3",
    "UPDATE outbox SET target='trakt' WHERE revision=3",
    "UPDATE outbox SET action='upsert' WHERE revision=3",
    "DELETE FROM outbox WHERE revision=3",
])
def test_removal_crash_recovery_refuses_corrupt_or_missing_audit(added_store, monkeypatch, sql):
    prepare_removed(added_store); crash_removal(added_store, monkeypatch)
    mutate(added_store, sql); before = dump(added_store)
    with pytest.raises(InboundError): apply_removed(added_store)
    assert dump(added_store) == before


def test_already_applied_removal_is_idempotent_and_read_only(added_store):
    prepare_removed(added_store); apply_removed(added_store); before = dump(added_store)
    again = apply_removed(added_store)
    assert again["already_applied"] and again["queued_targets"] == []
    assert dump(added_store) == before


def test_mark_applied_failure_leaves_committed_recoverable_gap(added_store, monkeypatch):
    original = episode_importer._mark_applied
    def fail_after_commit(*args, **kwargs):
        raise sqlite3.OperationalError("audit connection unavailable")
    monkeypatch.setattr(episode_importer, "_mark_applied", fail_after_commit)
    with pytest.raises(sqlite3.OperationalError): apply_added(added_store)
    assert len(rows(added_store, "ratings")) == len(rows(added_store, "outbox")) == 1
    assert rows(added_store, "inbound_episode_events")[0]["status"] == "observed"
    monkeypatch.setattr(episode_importer, "_mark_applied", original)
    assert apply_added(added_store)["already_applied"] is True


def test_fingerprint_change_in_audit_gap_refuses_marking(added_store, monkeypatch):
    original = episode_importer._mark_applied
    def alter(*args, **kwargs):
        mutate(added_store, "UPDATE inbound_episode_events SET fingerprint=?", ("e" * 64,))
        return original(*args, **kwargs)
    monkeypatch.setattr(episode_importer, "_mark_applied", alter)
    with pytest.raises(InboundError, match="fingerprint"):
        apply_added(added_store)
    assert rows(added_store, "ratings")[0]["revision"] == 1
    assert rows(added_store, "inbound_episode_events")[0]["status"] == "observed"


def test_import_connection_cannot_mutate_snapshot_or_movie_show_inbound(added_store):
    before = dump(added_store)
    with added_store.connect_for_import() as conn:
        for sql in ("DELETE FROM inbound_episode_state", "DELETE FROM inbound_episode_snapshots",
                    "DELETE FROM inbound_state", "DELETE FROM inbound_snapshots",
                    "DELETE FROM inbound_events", "DELETE FROM inbound_episode_events",
                    "UPDATE inbound_episode_events SET fingerprint='bad'",
                    "UPDATE inbound_episode_events SET future_action='delete'",
                    "INSERT INTO inbound_episode_events(fingerprint) VALUES ('bad')"):
            with pytest.raises(sqlite3.DatabaseError): conn.execute(sql)
    assert dump(added_store) == before


def test_episode_auto_apply_remains_impossible(added_store):
    before = dump(added_store)
    with pytest.raises(AutoApplyError) as error:
        auto_apply(added_store, EPISODE_SOURCE_TARGETS, generation=2)
    assert error.value.result["canonical_mutations"] == 0
    assert error.value.result["provider_writes"] == 0 and dump(added_store) == before


def test_existing_movie_show_target_plans_are_unchanged():
    assert validated_delivery_targets(GLOBAL_TARGETS) == TARGETS
    assert validated_delivery_targets(GLOBAL_TARGETS + ("imdb",)) == TARGETS + ("imdb",)


def test_tmdb_episode_payload_path_uses_series_coordinates_without_http(added_store):
    apply_added(added_store)
    payload = json.loads(rows(added_store, "outbox")[0]["payload_json"])
    assert TMDbProvider._path(payload) == "/tv/195339/season/1/episode/1/rating"
    assert payload["tmdb_id"] == 42 and payload["tmdb_series_id"] == 195339


def test_importer_has_zero_provider_io(added_store, monkeypatch):
    import httpx
    import hub.providers.registry
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("HTTP forbidden"))
    monkeypatch.setattr(hub.providers.registry, "get_provider",
                        lambda *a, **k: pytest.fail("provider lookup forbidden"))
    assert apply_added(added_store)["direct_provider_writes"] == 0


UPSERT_ARGS = ["--apply-event", "1", "--expect-content-key", KEY,
    "--expect-rating", "8", "--expect-generation", "2",
    "--expect-canonical-revision", "0", "--confirm-live-import"]


def test_explicit_cli_import_and_removal_never_open_http(added_store, monkeypatch, capsys):
    import httpx
    import hub.providers.registry
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("HTTP forbidden"))
    monkeypatch.setattr(hub.providers.registry, "get_provider",
                        lambda *a, **k: pytest.fail("provider lookup forbidden"))
    assert main(UPSERT_ARGS + ["--db", added_store.path]) == 0
    observe_episode(added_store, lambda: snapshot())
    removal = ["--apply-removal-event", "2", "--expect-content-key", KEY,
        "--expect-old-rating", "8", "--expect-generation", "3",
        "--expect-canonical-revision", "1", "--expect-canonical-source",
        "trakt-episode-inbound:1", "--confirm-live-import", "--db", added_store.path]
    assert main(removal) == 0
    output = capsys.readouterr().out
    assert "queued_targets=['tmdb']" in output and output.count("direct_provider_writes=0") == 2
    assert "tt11680642" not in output


@pytest.mark.parametrize("omit", ["--expect-content-key", "--expect-rating",
    "--expect-generation", "--expect-canonical-revision", "--confirm-live-import"])
def test_cli_import_requires_every_guard(added_store, omit):
    args = UPSERT_ARGS.copy(); index = args.index(omit)
    del args[index:index + (1 if omit == "--confirm-live-import" else 2)]
    before = dump(added_store)
    assert main(args + ["--db", added_store.path]) == 2
    assert dump(added_store) == before


@pytest.mark.parametrize("extra", [["--baseline"], ["--once"], ["--apply-removal-event", "1"],
    ["--observe-only"], ["--reset"], ["--expect-old-rating", "8"],
    ["--expect-canonical-source", "nuvio"]])
def test_cli_import_cannot_mix_operations(added_store, extra):
    before = dump(added_store)
    try:
        result = main(UPSERT_ARGS + extra + ["--db", added_store.path])
    except SystemExit as exc:
        result = exc.code
    assert result == 2
    assert dump(added_store) == before


def test_cli_failure_is_sanitized(added_store, monkeypatch, capsys):
    monkeypatch.setattr(episode_importer, "apply_episode_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("private-secret")))
    assert main(UPSERT_ARGS + ["--db", added_store.path]) == 1
    assert "private-secret" not in capsys.readouterr().out


def test_import_refuses_database_without_canonical_schema(tmp_path):
    path = str(tmp_path / "episode-only.sqlite3"); store = EpisodeStore(path)
    observe_episode(store, lambda: snapshot(), baseline=True)
    observe_episode(store, lambda: snapshot(episode()))
    before = rows(store, "inbound_episode_events")
    with pytest.raises(sqlite3.DatabaseError): apply_added(store)
    assert rows(store, "inbound_episode_events") == before


def test_rating_store_initialize_false_never_creates_missing_database(tmp_path):
    path = tmp_path / "missing.sqlite3"
    RatingStore(str(path), initialize=False)
    assert not path.exists()
