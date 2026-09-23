from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

import hub.store as store_module
from hub.capabilities import split_supported
from hub.models import RatingWrite
from hub.store import RatingStore
from hub.worker import process_one
from tests.fakes import FakeProviders


@pytest.fixture
def store(tmp_path: Path) -> RatingStore:
    return RatingStore(str(tmp_path / 'phase3.sqlite3'))


def movie(rating: int = 8, **kwargs: object) -> RatingWrite:
    return RatingWrite(media_type='movie', tmdb_id=550, rating=rating, **kwargs)


def rows(store: RatingStore, status: str) -> list[dict]:
    return store.list_outbox(status=status)


def all_rows(store: RatingStore) -> list[dict]:
    return sorted(sum((rows(store, s) for s in
                       ('pending', 'processing', 'done', 'failed', 'superseded')), []),
                  key=lambda r: r['id'])


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def advance_to(self, text: str) -> None:
        self.now = datetime.fromisoformat(text) + timedelta(microseconds=1)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    value = Clock()
    monkeypatch.setattr(store_module, 'utc_now', lambda: value.now)
    return value


def test_four_target_fanout_single_revision(store: RatingStore) -> None:
    targets = ('mdblist', 'trakt', 'simkl', 'tmdb')
    result = store.upsert_rating(movie(), targets)
    assert result['queued_targets'] == list(targets)
    assert store.get_rating(movie().content_key)['revision'] == 1
    assert len(store.list_ratings()) == 1
    pending = rows(store, 'pending')
    assert {r['target'] for r in pending} == set(targets)
    assert {r['revision'] for r in pending} == {1}


def test_episode_and_explicit_target_routing(store: RatingStore) -> None:
    supported, skipped = split_supported('episode', ('mdblist', 'trakt', 'simkl', 'tmdb'))
    assert supported == ('trakt', 'tmdb')
    assert skipped == ('mdblist', 'simkl')
    episode = RatingWrite(media_type='episode', tmdb_series_id=1399,
                          season_number=1, episode_number=1, rating=8)
    store.upsert_rating(episode, supported)
    assert {r['target'] for r in rows(store, 'pending')} == {'trakt', 'tmdb'}
    explicit, skipped = split_supported('movie', ('tmdb', 'tmdb', 'letterboxd'))
    assert explicit == ('tmdb', 'letterboxd') and skipped == ()


def test_provider_failures_are_isolated(store: RatingStore) -> None:
    targets = ('mdblist', 'trakt', 'simkl', 'tmdb')
    store.upsert_rating(movie(), targets)
    fake = FakeProviders({'trakt': ['transient'], 'simkl': ['permanent']})
    assert [process_one(store, fake) for _ in range(4)] == [True] * 4
    assert {r['target'] for r in rows(store, 'done')} == {'mdblist', 'tmdb'}
    assert {r['target'] for r in rows(store, 'pending')} == {'trakt'}
    assert {r['target'] for r in rows(store, 'failed')} == {'simkl'}
    assert store.get_rating(movie().content_key)['rating'] == 8
    assert [r['provider'] for r in fake.calls] == list(targets)


def test_retry_backoff_and_eventual_success(store: RatingStore, clock: Clock) -> None:
    store.upsert_rating(movie(), ('trakt',))
    fake = FakeProviders({'trakt': ['transient', 'transient', 'transient', 'success']})
    for attempt, delay in enumerate((15, 30, 60), start=1):
        assert process_one(store, fake)
        row = rows(store, 'pending')[0]
        assert row['attempts'] == attempt
        assert (datetime.fromisoformat(row['next_attempt_at']) - clock.now).total_seconds() == delay
        assert not process_one(store, fake)
        clock.advance_to(row['next_attempt_at'])
    assert process_one(store, fake)
    assert rows(store, 'done')[0]['attempts'] == 4
    assert [call['delivery_count'] for call in fake.calls] == [1, 2, 3, 4]


def test_retry_exhaustion_at_eight(store: RatingStore, clock: Clock) -> None:
    store.upsert_rating(movie(), ('trakt',))
    fake = FakeProviders({'trakt': ['transient']})
    for attempt in range(1, 9):
        assert process_one(store, fake)
        current = all_rows(store)[0]
        assert current['attempts'] == attempt
        if attempt < 8:
            assert current['status'] == 'pending'
            clock.advance_to(current['next_attempt_at'])
    assert current['status'] == 'failed'
    assert current['next_attempt_at'] is None
    assert not process_one(store, fake)


def test_stale_recovery_keeps_recent_job(store: RatingStore, clock: Clock) -> None:
    store.upsert_rating(movie(), ('trakt', 'tmdb'))
    first = store.claim_next_job()
    second = store.claim_next_job()
    assert first and second
    with sqlite3.connect(store.path) as conn:
        conn.execute('UPDATE outbox SET updated_at = ? WHERE id = ?',
                     ((clock.now - timedelta(minutes=11)).isoformat(), first['id']))
    assert store.requeue_stale_processing() == 1
    assert rows(store, 'processing')[0]['id'] == second['id']
    assert store.claim_next_job()['id'] == first['id']


def test_stale_lease_cannot_complete_reclaimed_job(store: RatingStore, clock: Clock) -> None:
    store.upsert_rating(movie(), ('trakt',))
    old = store.claim_next_job()
    with sqlite3.connect(store.path) as conn:
        conn.execute('UPDATE outbox SET updated_at = ? WHERE id = ?',
                     ((clock.now - timedelta(minutes=11)).isoformat(), old['id']))
    assert store.requeue_stale_processing() == 1
    new = store.claim_next_job()
    assert new['attempts'] == old['attempts'] + 1
    store.complete_job(old['id'], attempts=old['attempts'])
    assert rows(store, 'processing')[0]['id'] == new['id']


def test_superseded_rating_never_delivered(store: RatingStore) -> None:
    store.upsert_rating(movie(7), ('trakt',))
    store.upsert_rating(movie(9), ('trakt',))
    assert rows(store, 'superseded')[0]['revision'] == 1
    fake = FakeProviders()
    assert process_one(store, fake)
    assert fake.calls[0]['rating'] == 9 and fake.calls[0]['revision'] == 2
    assert rows(store, 'done')[0]['revision'] == 2


def test_delete_tombstone_supersedes_pending_upsert(store: RatingStore) -> None:
    store.upsert_rating(movie(), ('trakt',))
    deleted = store.delete_rating(movie().content_key, ('trakt',))
    assert deleted['revision'] == 2
    assert store.get_rating(movie().content_key) is None
    assert rows(store, 'superseded')[0]['action'] == 'upsert'
    fake = FakeProviders()
    assert process_one(store, fake)
    assert [(c['action'], c['rating'], c['revision']) for c in fake.calls] == [('remove', None, 2)]
    assert rows(store, 'done')[0]['revision'] == 2


def test_processing_upsert_finishes_before_newer_remove(store: RatingStore) -> None:
    store.upsert_rating(movie(), ('trakt',))
    old = store.claim_next_job()
    store.delete_rating(movie().content_key, ('trakt',))
    assert store.claim_next_job() is None
    store.complete_job(old['id'], attempts=old['attempts'])
    newest = store.claim_next_job()
    assert newest and newest['action'] == 'remove' and newest['revision'] == 2


def test_factory_time_revision_change_skips_old_job(store: RatingStore) -> None:
    store.upsert_rating(movie(7), ('trakt',))
    fake = FakeProviders()
    def factory(name: str):
        store.upsert_rating(movie(9), ('trakt',))
        return fake(name)
    assert process_one(store, factory)
    assert fake.calls == []
    assert rows(store, 'superseded')[0]['revision'] == 1
    assert process_one(store, fake)
    assert fake.calls[0]['rating'] == 9


def test_identical_upsert_is_idempotent(store: RatingStore) -> None:
    first = store.upsert_rating(movie(), ('trakt',))
    again = store.upsert_rating(movie(), ('trakt',))
    assert again['revision'] == first['revision'] == 1
    assert again['queued_targets'] == []
    assert again['rated_at'] == first['rated_at']
    assert len(all_rows(store)) == 1


def test_same_rating_new_target_queues_only_missing_target(store: RatingStore) -> None:
    store.upsert_rating(movie(), ('trakt',))
    again = store.upsert_rating(movie(), ('trakt', 'tmdb'))
    assert again['revision'] == 1
    assert again['queued_targets'] == ['tmdb']
    assert len(all_rows(store)) == 2


def test_changed_rating_creates_new_revision(store: RatingStore) -> None:
    store.upsert_rating(movie(8), ('trakt',))
    changed = store.upsert_rating(movie(9), ('trakt',))
    assert changed['revision'] == 2
    assert rows(store, 'superseded')[0]['revision'] == 1


def test_identical_write_does_not_retry_failed_job(store: RatingStore) -> None:
    store.upsert_rating(movie(), ('trakt',))
    fake = FakeProviders({'trakt': ['permanent']})
    assert process_one(store, fake)
    assert store.upsert_rating(movie(), ('trakt',))['queued_targets'] == []
    assert rows(store, 'failed')[0]['attempts'] == 1


def test_duplicate_delete_and_restore(store: RatingStore) -> None:
    store.upsert_rating(movie(), ('trakt',))
    first = store.delete_rating(movie().content_key, ('trakt',))
    again = store.delete_rating(movie().content_key, ('trakt',))
    assert first['revision'] == 2 and again['removed'] is False
    restored = store.upsert_rating(movie(9), ('trakt',))
    assert restored['revision'] == 3
    assert rows(store, 'superseded')[-1]['action'] == 'remove'
    fake = FakeProviders()
    assert process_one(store, fake)
    assert fake.calls[0]['action'] == 'upsert' and fake.calls[0]['rating'] == 9


def test_two_stores_claim_once(tmp_path: Path) -> None:
    path = str(tmp_path / 'two-workers.sqlite3')
    stores = [RatingStore(path), RatingStore(path)]
    stores[0].upsert_rating(movie(), ('trakt',))
    barrier = Barrier(2)
    def claim(index: int):
        barrier.wait()
        return stores[index].claim_next_job()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, range(2)))
    assert sum(job is not None for job in claimed) == 1


def test_two_stores_identical_write_once(tmp_path: Path) -> None:
    path = str(tmp_path / 'two-writers.sqlite3')
    stores = [RatingStore(path), RatingStore(path)]
    barrier = Barrier(2)
    def write(index: int):
        barrier.wait()
        return stores[index].upsert_rating(movie(), ('trakt',))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))
    assert [r['revision'] for r in results] == [1, 1]
    assert len(all_rows(stores[0])) == 1


def test_unexpected_exception_is_sanitized_and_loop_continues(store: RatingStore) -> None:
    store.upsert_rating(movie(), ('trakt', 'tmdb'))
    fake = FakeProviders({'trakt': ['unexpected']})
    assert process_one(store, fake)
    assert rows(store, 'pending')[0]['last_error'] == 'RuntimeError'
    assert 'sensitive-value' not in str(all_rows(store))
    assert process_one(store, fake)
    assert rows(store, 'done')[0]['target'] == 'tmdb'


def test_not_configured_and_unsupported_are_permanent(store: RatingStore) -> None:
    store.upsert_rating(movie(), ('trakt', 'tmdb'))
    fake = FakeProviders({'trakt': ['not_configured'], 'tmdb': ['unsupported']})
    assert process_one(store, fake)
    assert process_one(store, fake)
    assert {r['last_error'] for r in rows(store, 'failed')} == {
        'ProviderNotConfigured', 'UnsupportedDelivery'
    }


def test_rating_and_outbox_roll_back_together(store: RatingStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError('queue unavailable')
    monkeypatch.setattr(store, '_queue', fail)
    with pytest.raises(RuntimeError):
        store.upsert_rating(movie(), ('trakt',))
    assert store.get_rating(movie().content_key) is None
    assert all_rows(store) == []


def test_legacy_outbox_schema_migrates_without_losing_jobs(tmp_path: Path) -> None:
    path = str(tmp_path / 'legacy.sqlite3')
    store = RatingStore(path)
    store.upsert_rating(movie(), ('trakt',))
    with sqlite3.connect(path) as conn:
        row = conn.execute('SELECT * FROM outbox').fetchone()
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'outbox'"
        ).fetchone()[0]
        old_schema = schema.replace(", 'superseded'", '')
        conn.execute('DROP TABLE outbox')
        conn.execute(old_schema)
        conn.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', row)
    migrated = RatingStore(path)
    assert len(rows(migrated, 'pending')) == 1
    migrated.upsert_rating(movie(9), ('trakt',))
    assert rows(migrated, 'superseded')[0]['revision'] == 1
    assert rows(migrated, 'pending')[0]['revision'] == 2


def test_recovered_old_processing_job_is_superseded(store: RatingStore, clock: Clock) -> None:
    store.upsert_rating(movie(7), ('trakt',))
    old = store.claim_next_job()
    store.upsert_rating(movie(9), ('trakt',))
    assert store.claim_next_job() is None
    with sqlite3.connect(store.path) as conn:
        conn.execute('UPDATE outbox SET updated_at = ? WHERE id = ?',
                     ((clock.now - timedelta(minutes=11)).isoformat(), old['id']))
    assert store.requeue_stale_processing() == 1
    new = store.claim_next_job()
    assert new['revision'] == 2
    assert rows(store, 'superseded')[0]['revision'] == 1


def test_remove_delivery_failure_is_isolated(store: RatingStore) -> None:
    store.upsert_rating(movie(), ('trakt', 'tmdb'))
    store.delete_rating(movie().content_key, ('trakt', 'tmdb'))
    fake = FakeProviders({'trakt': ['transient']})
    assert process_one(store, fake)
    assert process_one(store, fake)
    assert {r['target'] for r in rows(store, 'pending')} == {'trakt'}
    assert {r['target'] for r in rows(store, 'done')} == {'tmdb'}
    assert store.get_rating(movie().content_key) is None
    assert all(call['action'] == 'remove' and call['revision'] == 2 for call in fake.calls)


def test_metadata_enrichment_changes_revision_but_omission_does_not(store: RatingStore) -> None:
    store.upsert_rating(movie(), ('trakt',))
    enriched = store.upsert_rating(movie(imdb_id='tt0137523'), ('trakt',))
    assert enriched['revision'] == 2
    omitted = store.upsert_rating(movie(), ('trakt',))
    assert omitted['revision'] == 2 and omitted['queued_targets'] == []
    assert store.get_rating(movie().content_key)['imdb_id'] == 'tt0137523'


def test_eighth_expired_processing_lease_is_terminal(store: RatingStore, clock: Clock) -> None:
    store.upsert_rating(movie(), ('trakt',))
    job = store.claim_next_job()
    with sqlite3.connect(store.path) as conn:
        conn.execute('UPDATE outbox SET attempts = 8, updated_at = ? WHERE id = ?',
                     ((clock.now - timedelta(minutes=11)).isoformat(), job['id']))
    assert store.requeue_stale_processing() == 1
    assert rows(store, 'failed')[0]['next_attempt_at'] is None
    assert store.claim_next_job() is None