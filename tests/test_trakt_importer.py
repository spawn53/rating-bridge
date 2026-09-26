import json
import sqlite3
from datetime import datetime, timezone

import pytest

from hub.inbound import importer
from hub.inbound.importer import apply_event, GLOBAL_TARGETS, TARGETS
from hub.inbound.models import InboundError, MovieRating, Snapshot, timestamp
from hub.inbound.storage import InboundStore
from hub.inbound.trakt import main, observe
from hub.models import RatingWrite
from hub.store import RatingStore

KEY = "movie:tmdb:265189"
DATE = "2026-09-26T11:49:04.000000+00:00"


def rows(store, table):
    with store.connect() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY 1")]


def dump(store):
    return {name: rows(store, name) for name in (
        "ratings", "outbox", "inbound_events", "inbound_state", "inbound_snapshots", "inbound_unmapped"
    )}


@pytest.fixture
def store(tmp_path):
    path = str(tmp_path / "hub.sqlite3")
    canonical = RatingStore(path)
    for score in range(1, 5):
        canonical.upsert_rating(RatingWrite(media_type="movie", tmdb_id=265189, rating=score,
                                          source="nuvio"), GLOBAL_TARGETS)
    canonical.delete_rating(KEY, GLOBAL_TARGETS)
    inbound = InboundStore(path)
    with inbound.connect() as c:
        c.execute("UPDATE outbox SET status='done'")
    observe(inbound, lambda: Snapshot(()), baseline=True)
    observe(inbound, lambda: Snapshot(()))
    observe(inbound, lambda: Snapshot(()))
    observe(inbound, lambda: Snapshot((MovieRating(8, DATE, 265189, 163864, "tt2121382"),)))
    assert rows(inbound, "ratings")[0]["revision"] == 5
    assert rows(inbound, "inbound_events")[0]["generation"] == 4
    return inbound


def apply(store, **changes):
    values = dict(event_id=1, expected_key=KEY, expected_rating=8, expected_generation=4,
                  expected_revision=5, confirmed=True)
    values.update(changes)
    return apply_event(store, GLOBAL_TARGETS, **values)


def mutate(store, sql, args=()):
    with store.connect() as c:
        c.execute(sql, args)


def test_valid_import_preserves_source_timestamp_and_audit(store):
    before = dump(store)
    result = apply(store)
    assert result["revision"] == 6 and result["queued_targets"] == list(TARGETS)
    assert result["skipped_targets"] == ["trakt"] and not result["already_applied"]
    canonical = rows(store, "ratings")[0]
    assert canonical["rating"] == 8 and canonical["deleted"] == 0
    assert canonical["source"] == "trakt-inbound:1" and timestamp(canonical["rated_at"]) == DATE
    assert canonical["tmdb_id"] == 265189 and canonical["trakt_id"] == 163864
    event = rows(store, "inbound_events")[0]
    assert event["status"] == "applied" and event["canonical_revision"] == 6
    assert datetime.fromisoformat(event["applied_at"]).utcoffset().total_seconds() == 0
    assert {k: event[k] for k in before["inbound_events"][0]} == {
        **before["inbound_events"][0], "status": "applied", "applied_at": event["applied_at"],
        "canonical_revision": 6,
    }
    jobs = rows(store, "outbox")
    assert jobs[:20] == before["outbox"]
    assert len(jobs) == 23 and {j["target"] for j in jobs[20:]} == set(TARGETS)
    assert all(j["action"] == "upsert" and j["revision"] == 6 for j in jobs[20:])
    assert all(json.loads(j["payload_json"]) == canonical for j in jobs[20:])
    for table in ("inbound_state", "inbound_snapshots", "inbound_unmapped"):
        assert rows(store, table) == before[table]


def test_repeated_applied_event_returns_original_revision_without_mutation(store):
    apply(store)
    before = dump(store)
    again = apply(store)
    assert again["already_applied"] and again["revision"] == 6 and again["queued_targets"] == []
    assert dump(store) == before
    # A later legitimate canonical command does not replay the old import.
    RatingStore(store.path).upsert_rating(
        RatingWrite(media_type="movie", tmdb_id=265189, rating=9), GLOBAL_TARGETS)
    before = dump(store)
    assert apply(store)["revision"] == 6 and dump(store) == before


def test_crash_after_canonical_commit_converges_on_restart(store, monkeypatch):
    original = importer._mark_applied
    def crash(*args, **kwargs):
        raise RuntimeError("simulated process exit after canonical commit")
    monkeypatch.setattr(importer, "_mark_applied", crash)
    with pytest.raises(RuntimeError):
        apply(store)
    assert rows(store, "ratings")[0]["revision"] == 6
    assert rows(store, "inbound_events")[0]["status"] == "observed"
    committed = rows(store, "outbox")
    monkeypatch.setattr(importer, "_mark_applied", original)
    result = apply(InboundStore(store.path))
    assert result["revision"] == 6 and result["already_applied"]
    assert rows(store, "ratings")[0]["revision"] == 6
    assert rows(store, "outbox") == committed
    assert rows(store, "inbound_events")[0]["status"] == "applied"


@pytest.mark.parametrize("corruption", ["source", "rating", "rated_at", "revision", "target", "payload", "trakt_job"])
def test_partial_commit_replay_requires_exact_provenance_and_delivery_audit(store, monkeypatch, corruption):
    original = importer._mark_applied
    monkeypatch.setattr(importer, "_mark_applied", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError): apply(store)
    monkeypatch.setattr(importer, "_mark_applied", original)
    if corruption == "source": mutate(store, "UPDATE ratings SET source='other'")
    if corruption == "rating": mutate(store, "UPDATE ratings SET rating=9")
    if corruption == "rated_at": mutate(store, "UPDATE ratings SET rated_at='2026-09-26T12:00:00Z'")
    if corruption == "revision": mutate(store, "UPDATE ratings SET revision=7")
    if corruption == "target": mutate(store, "UPDATE outbox SET target='trakt' WHERE revision=6 AND target='tmdb'")
    if corruption == "payload": mutate(store, "UPDATE outbox SET payload_json='{}' WHERE revision=6 AND target='tmdb'")
    if corruption == "trakt_job": mutate(store, "UPDATE outbox SET status='pending' WHERE target='trakt' AND revision=5")
    before = dump(store)
    with pytest.raises(InboundError): apply(store)
    assert dump(store) == before


@pytest.mark.parametrize("field,value", [
    ("confirmed", False), ("event_id", 0), ("event_id", True), ("event_id", 999),
    ("expected_key", "show:tmdb:265189"), ("expected_key", "movie:tmdb:265190"),
    ("expected_key", "movie:tmdb:0265189"), ("expected_rating", 9), ("expected_rating", True),
    ("expected_rating", 0), ("expected_generation", 3), ("expected_revision", 4),
])
def test_expectation_guards_make_no_changes(store, field, value):
    before = dump(store)
    with pytest.raises(InboundError): apply(store, **{field: value})
    assert dump(store) == before


@pytest.mark.parametrize("sql", [
    "UPDATE inbound_events SET status='ignored'",
    "UPDATE inbound_events SET classification='echo'",
    "UPDATE inbound_events SET reason='other'",
    "UPDATE inbound_events SET future_action='delete'",
    "UPDATE inbound_events SET event_type='removed',old_rating=8,new_rating=NULL",
    "UPDATE inbound_events SET provider='simkl'",
    "UPDATE inbound_events SET media_type='show'",
    "UPDATE inbound_events SET media_type='episode'",
    "UPDATE inbound_events SET fingerprint='bad'",
    "UPDATE inbound_events SET old_rating=6",
    "UPDATE inbound_snapshots SET rating=9",
    "UPDATE inbound_snapshots SET rated_at='2026-09-26T12:00:00Z'",
    "UPDATE inbound_snapshots SET tmdb_id=265190",
    "DELETE FROM inbound_snapshots",
    "UPDATE inbound_state SET generation=5",
    "UPDATE ratings SET revision=7",
    "UPDATE ratings SET deleted=0,rating=7",
])
def test_stale_unsupported_and_incompatible_states_refused(store, sql):
    with store.connect() as c:
        # Simulate an unsupported legacy/corrupt row despite the movie-only schema CHECK.
        c.execute("PRAGMA ignore_check_constraints=ON")
        c.execute(sql)
    before = dump(store)
    with pytest.raises(InboundError): apply(store)
    assert dump(store) == before


@pytest.mark.parametrize("status", ["pending", "processing", "failed"])
def test_unresolved_trakt_job_refuses_import(store, status):
    mutate(store, "UPDATE outbox SET status=? WHERE target='trakt' AND revision=5", (status,))
    before = dump(store)
    with pytest.raises(InboundError): apply(store)
    assert dump(store) == before


def test_latest_completed_trakt_rating_echo_refuses_import(store):
    mutate(store, "UPDATE outbox SET action='upsert',payload_json=? WHERE target='trakt' AND revision=5",
           (json.dumps({"rating": 8}),))
    before = dump(store)
    with pytest.raises(InboundError): apply(store)
    assert dump(store) == before


def test_newer_event_refuses_older_event_even_if_snapshot_manually_restored(store):
    mutate(store, """INSERT INTO inbound_events
        (fingerprint,provider,media_type,content_key,generation,event_type,old_rating,new_rating,
         provider_rated_at,detected_at,status,reason,classification,future_action)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("a"*64, "trakt", "movie", KEY, 5, "changed", 8, 9, DATE, DATE,
         "observed", "different_provider_state", "candidate", "upsert"))
    before = dump(store)
    with pytest.raises(InboundError, match="newer"): apply(store)
    assert dump(store) == before


@pytest.mark.parametrize("targets", [TARGETS, GLOBAL_TARGETS + ("imdb",),
                                     tuple(reversed(GLOBAL_TARGETS)), ("tmdb", "trakt", "simkl")])
def test_unsafe_target_configuration_refused(store, targets):
    before = dump(store)
    with pytest.raises(InboundError):
        apply_event(store, targets, event_id=1, expected_key=KEY, expected_rating=8,
                    expected_generation=4, expected_revision=5, confirmed=True)
    assert dump(store) == before


def test_changed_event_import_when_previous_canonical_score_agrees(store):
    mutate(store, "UPDATE ratings SET deleted=0,rating=6")
    mutate(store, "UPDATE inbound_events SET event_type='changed',old_rating=6")
    assert apply(store)["revision"] == 6


def test_first_canonical_insert_from_added_event(store):
    mutate(store, "DELETE FROM ratings")
    mutate(store, "DELETE FROM outbox")
    assert apply(store, expected_revision=0)["revision"] == 1
    assert len(rows(store, "outbox")) == 3


def test_validation_and_upsert_share_write_lock(store, monkeypatch):
    original = RatingStore.upsert_rating
    def concurrent_write(self, item, targets, *, connection=None):
        assert connection is not None and connection.in_transaction
        with sqlite3.connect(store.path, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE ratings SET rating=9")
        return original(self, item, targets, connection=connection)
    monkeypatch.setattr(RatingStore, "upsert_rating", concurrent_write)
    assert apply(store)["revision"] == 6


def test_outbox_insert_failure_rolls_back_canonical_and_event(store):
    mutate(store, """CREATE TRIGGER stop_import BEFORE INSERT ON outbox
        WHEN NEW.revision=6 BEGIN SELECT RAISE(ABORT,'simulated insert failure'); END""")
    before = dump(store)
    with pytest.raises(sqlite3.IntegrityError): apply(store)
    assert dump(store) == before


def test_ordinary_nuvio_write_keeps_four_provider_targets(store):
    result = RatingStore(store.path).upsert_rating(
        RatingWrite(media_type="movie", tmdb_id=550, rating=7), GLOBAL_TARGETS)
    assert result["queued_targets"] == list(GLOBAL_TARGETS)
    assert rows(store, "inbound_events")[0]["status"] == "observed"


ARGS = ["--apply-event", "1", "--expect-content-key", KEY, "--expect-rating", "8",
        "--expect-generation", "4", "--expect-canonical-revision", "5", "--confirm-live-import"]


@pytest.mark.parametrize("omit", ["--apply-event", "--expect-content-key", "--expect-rating",
                                   "--expect-generation", "--expect-canonical-revision",
                                   "--confirm-live-import"])
def test_cli_requires_every_guard(store, monkeypatch, omit):
    monkeypatch.setenv("RATING_HUB_DB", store.path)
    arguments = ARGS.copy()
    index = arguments.index(omit)
    del arguments[index:index + (1 if omit == "--confirm-live-import" else 2)]
    before = dump(store)
    try: result = main(arguments)
    except SystemExit as exc: result = exc.code
    assert result != 0 and dump(store) == before


@pytest.mark.parametrize("extra", ["--observe-only", "--reset", "--baseline", "--once"])
def test_cli_import_cannot_mix_observer_modes(store, monkeypatch, extra):
    monkeypatch.setenv("RATING_HUB_DB", store.path)
    before = dump(store)
    try: result = main(ARGS + [extra])
    except SystemExit as exc: result = exc.code
    assert result != 0 and dump(store) == before


def test_cli_import_does_not_call_provider_or_http(store, monkeypatch, capsys):
    monkeypatch.setenv("RATING_HUB_DB", store.path)
    monkeypatch.setenv("RATING_HUB_TARGETS", ",".join(GLOBAL_TARGETS))
    monkeypatch.setenv("TRAKT_INBOUND_ENABLED", "false")
    import httpx
    import hub.providers.registry
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("no HTTP in importer"))
    monkeypatch.setattr(hub.providers.registry, "get_provider", lambda *args: pytest.fail("no provider in importer"))
    assert main(ARGS) == 0
    output = capsys.readouterr().out
    assert "revision=6" in output and "direct_provider_writes=0" in output


LEGACY_SCHEMA = """
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


def legacy(store):
    original = rows(store, "inbound_events")[0]
    with store.connect() as c:
        c.execute("DROP TABLE inbound_events")
        c.execute(LEGACY_SCHEMA)
        old = {k: v for k, v in original.items() if k not in {"applied_at", "canonical_revision"}}
        c.execute(f"INSERT INTO inbound_events ({','.join(old)}) VALUES ({','.join('?' for _ in old)})",
                  tuple(old.values()))
        second = {**old, "id": 9, "fingerprint": "b"*64, "status": "ignored"}
        c.execute(f"INSERT INTO inbound_events ({','.join(second)}) VALUES ({','.join('?' for _ in second)})",
                  tuple(second.values()))
        c.execute("UPDATE sqlite_sequence SET seq=17 WHERE name='inbound_events'")
    return [old, second]


def test_legacy_migration_preserves_every_field_id_fingerprint_and_sequence(store):
    previous = legacy(store)
    before = dump(store)
    migrated = InboundStore(store.path)
    events = rows(migrated, "inbound_events")
    assert [{k: r[k] for k in previous[0]} for r in events] == previous
    assert all(r["applied_at"] is None and r["canonical_revision"] is None for r in events)
    for name in before:
        if name != "inbound_events": assert rows(migrated, name) == before[name]
    assert rows(InboundStore(store.path), "inbound_events") == events
    with store.connect() as c:
        assert c.execute("SELECT seq FROM sqlite_sequence WHERE name='inbound_events'").fetchone()[0] == 17
    mutate(store, "UPDATE inbound_events SET status='applied',applied_at=?,canonical_revision=6 WHERE id=1", (DATE,))
    assert rows(store, "inbound_events")[0]["status"] == "applied"


def test_migration_failure_rolls_back_rebuild_without_losing_legacy_rows(store):
    previous = legacy(store)
    with store.connect() as c:
        def deny_drop(action, name, *args):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_TABLE else sqlite3.SQLITE_OK
        c.set_authorizer(deny_drop)
        with pytest.raises(sqlite3.DatabaseError):
            with c: InboundStore._migrate_events(c)
    assert rows(store, "inbound_events") == previous
    with store.connect() as c:
        assert not c.execute("SELECT name FROM sqlite_master WHERE name='inbound_events_migration'").fetchall()


@pytest.mark.parametrize("field,value", [("media_type", "show"), ("tmdb_id", 265190)])
def test_canonical_identity_corruption_refuses_import(store, field, value):
    mutate(store, f"UPDATE ratings SET {field}=?", (value,))
    before = dump(store)
    with pytest.raises(InboundError): apply(store)
    assert dump(store) == before


def test_optional_snapshot_ids_do_not_erase_existing_canonical_metadata(store):
    mutate(store, "UPDATE ratings SET trakt_id=163864,imdb_id='tt2121382'")
    mutate(store, "UPDATE inbound_snapshots SET trakt_id=NULL,imdb_id=NULL")
    assert apply(store)["revision"] == 6
    canonical = rows(store, "ratings")[0]
    assert canonical["trakt_id"] == 163864 and canonical["imdb_id"] == "tt2121382"


def test_external_transaction_upsert_does_not_commit_callers_transaction(store):
    canonical = RatingStore(store.path)
    before = rows(store, "ratings")
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        canonical.upsert_rating(RatingWrite(media_type="movie",tmdb_id=550,rating=8),
                                GLOBAL_TARGETS, connection=c)
        assert c.in_transaction
        c.rollback()
    assert rows(store, "ratings") == before


def test_external_transaction_upsert_requires_transaction(store):
    with store.connect() as c:
        with pytest.raises(RuntimeError, match="transaction"):
            RatingStore(store.path).upsert_rating(
                RatingWrite(media_type="movie",tmdb_id=550,rating=8), GLOBAL_TARGETS, connection=c)


def test_import_errors_hide_secret_bearing_exceptions(store, monkeypatch, capsys):
    monkeypatch.setenv("RATING_HUB_DB", store.path)
    monkeypatch.setenv("RATING_HUB_TARGETS", ",".join(GLOBAL_TARGETS))
    def fail(*args, **kwargs): raise RuntimeError("Bearer secret-token client-secret raw-response")
    monkeypatch.setattr(importer, "apply_event", fail)
    assert main(ARGS) == 1
    output = capsys.readouterr().out
    assert "secret-token" not in output and "client-secret" not in output and "raw-response" not in output
    assert "inspect canonical/event audit" in output


@pytest.mark.parametrize("revision,applied_at", [(None, DATE), (6, None), (0, DATE)])
def test_applied_status_requires_complete_valid_audit_fields(store, revision, applied_at):
    with pytest.raises(sqlite3.IntegrityError):
        mutate(store, "UPDATE inbound_events SET status='applied',canonical_revision=?,applied_at=?",
               (revision, applied_at))
    assert rows(store, "inbound_events")[0]["status"] == "observed"
