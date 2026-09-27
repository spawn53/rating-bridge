"""Internal show one-shot safety and shared cross-media process lock contracts."""
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from hub.inbound.models import InboundError, ShowRating, Snapshot
from hub.inbound.scheduled import scheduled_lock, scheduled_observe
from hub.inbound.storage import InboundStore
from hub.inbound.trakt import main, observe
from hub.models import RatingWrite
from hub.store import RatingStore

DATE='2026-09-26T10:00:00Z'
KEY='show:tmdb:550'


def dump(store):
    with store.connect() as conn:
        return {name:[tuple(row) for row in conn.execute('SELECT * FROM '+name+' ORDER BY 1')]
                for name in ('ratings','outbox','inbound_state','inbound_snapshots','inbound_unmapped','inbound_events')}


@pytest.fixture
def store(tmp_path):
    path=str(tmp_path/'hub.sqlite3')
    RatingStore(path)
    store=InboundStore(path, media_type='show')
    observe(store,lambda:Snapshot((), media_type='show'),baseline=True)
    return store


def scheduled(store,read):
    return scheduled_observe(store.path,read,enabled=True, media_type='show')


def test_disabled_scheduled_observer_never_reads_or_touches_database(store):
    before=dump(store)
    with pytest.raises(InboundError):scheduled_observe(store.path,lambda:pytest.fail('no read'),enabled=False, media_type='show')
    assert dump(store)==before and not Path(store.path).with_name('trakt-inbound.lock').exists()


def test_baseline_is_required_and_never_created(store):
    with store.connect() as conn:conn.execute('DELETE FROM inbound_state')
    before=dump(store)
    with pytest.raises(InboundError):scheduled(store,lambda:pytest.fail('no provider read without baseline'))
    assert dump(store)==before


def test_missing_database_is_not_created(tmp_path):
    path=tmp_path/'missing.sqlite3'
    with pytest.raises(sqlite3.OperationalError):scheduled_observe(str(path),lambda:pytest.fail('no read'),enabled=True, media_type='show')
    assert not path.exists()


@pytest.mark.parametrize('table',['inbound_state','inbound_snapshots','inbound_unmapped','inbound_events','ratings','outbox'])
def test_missing_tables_are_not_rebuilt(store,table):
    with store.connect() as conn:conn.execute('DROP TABLE '+table)
    with pytest.raises(sqlite3.OperationalError):scheduled(store,lambda:pytest.fail('no read'))
    with store.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()


def test_legacy_event_schema_is_not_migrated(store):
    with store.connect() as conn:
        conn.execute('DROP TABLE inbound_events')
        conn.execute('CREATE TABLE inbound_events(id INTEGER PRIMARY KEY, fingerprint TEXT)')
    # A table without the applied-audit columns must fail, rather than migrating.
    with pytest.raises(sqlite3.OperationalError):scheduled(store,lambda:pytest.fail('no read'))
    with store.connect() as conn:
        assert 'canonical_revision' not in [r[1] for r in conn.execute('PRAGMA table_info(inbound_events)')]


def test_one_fetch_one_publication_candidate_waits_for_operator(store,monkeypatch):
    before=dump(store);calls=[];publications=[]
    original=InboundStore.publish
    def publish(self,snapshot,**kwargs):
        publications.append(kwargs)
        assert kwargs=={'expected_generation':1,'baseline':False,'reset':False}
        return original(self,snapshot,**kwargs)
    monkeypatch.setattr(InboundStore,'publish',publish)
    def read():calls.append(True);return Snapshot((ShowRating(8,DATE,550),), media_type='show')
    result=scheduled(store,read)
    assert len(calls)==len(publications)==1 and result['generation']==2
    assert result['added']==1 and result['canonical_mutations']==result['provider_writes']==0
    after=dump(store)
    assert after['ratings']==before['ratings'] and after['outbox']==before['outbox']
    with store.connect() as conn:
        event=dict(conn.execute('SELECT * FROM inbound_events').fetchone())
        assert event['status']=='observed' and event['classification']=='candidate'
        assert event['applied_at'] is event['canonical_revision'] is None


def test_echo_remains_ignored_and_canonical_outbox_stay_unchanged(store):
    RatingStore(store.path).upsert_rating(RatingWrite(media_type='show',tmdb_id=550,rating=8),('tmdb','trakt','simkl','mdblist'))
    with store.connect() as conn:conn.execute("UPDATE outbox SET status='done'")
    before=dump(store)
    scheduled(store,lambda:Snapshot((ShowRating(8,DATE,550),), media_type='show'))
    after=dump(store)
    assert after['ratings']==before['ratings'] and after['outbox']==before['outbox']
    with store.connect() as conn:
        e=dict(conn.execute('SELECT * FROM inbound_events').fetchone())
        assert e['status']=='ignored' and e['reason']=='same_as_canonical'


def test_unchanged_snapshot_keeps_content_generation_without_new_events(store):
    result=scheduled(store,lambda:Snapshot((), media_type='show'))
    assert result['generation']==1 and result['events']==0 and not result['snapshot_changed']
    assert dump(store)['inbound_events']==[]


@pytest.mark.parametrize('error',[InboundError('private-secret-marker'),httpx.ReadTimeout('private-secret-marker'),RuntimeError('private-secret-marker')])
def test_failed_fetch_retains_all_trusted_state(store,error):
    before=dump(store)
    def read():raise error
    with pytest.raises(type(error)):scheduled(store,read)
    assert dump(store)==before
    with scheduled_lock(store.path) as acquired:assert acquired


def test_publish_failure_rolls_back_snapshot_and_events(store):
    with store.connect() as conn:
        conn.execute("CREATE TRIGGER fail_publish BEFORE INSERT ON inbound_snapshots BEGIN SELECT RAISE(ABORT,'failure'); END")
    before=dump(store)
    with pytest.raises(sqlite3.IntegrityError):scheduled(store,lambda:Snapshot((ShowRating(8,DATE,550),), media_type='show'))
    assert dump(store)==before


def test_lock_overlap_is_nonblocking_and_has_no_publication(store):
    before=dump(store)
    with scheduled_lock(store.path) as acquired:
        assert acquired
        result=scheduled(store,lambda:pytest.fail('overlap must not fetch'))
        assert result=={'skipped_overlap':True,'canonical_mutations':0,'provider_writes':0}
        with scheduled_lock(store.path) as second:assert not second
    assert dump(store)==before
    with scheduled_lock(store.path) as acquired:assert acquired
    assert Path(store.path).with_name('trakt-inbound.lock').stat().st_mode&0o777==0o600


def test_lock_released_on_exception_without_unlinking_inode(store):
    lock=Path(store.path).with_name('trakt-inbound.lock')
    with pytest.raises(RuntimeError):
        with scheduled_lock(store.path) as acquired:
            assert acquired;identity=lock.stat().st_ino;raise RuntimeError('crash simulation')
    with scheduled_lock(store.path) as acquired:
        assert acquired and lock.stat().st_ino==identity


def test_kernel_releases_lock_after_process_is_killed(store):
    code='from hub.inbound.scheduled import scheduled_lock; import sys,time;\nwith scheduled_lock(sys.argv[1]) as acquired:\n print(acquired,flush=True)\n time.sleep(60)'
    process=subprocess.Popen([sys.executable,'-c',code,store.path],stdout=subprocess.PIPE,text=True)
    try:
        assert select.select([process.stdout],[],[],10)[0]
        assert process.stdout.readline().strip()=='True'
        assert scheduled(store,lambda:pytest.fail('no overlapping read'))['skipped_overlap']
        process.kill();process.wait(timeout=10)
        with scheduled_lock(store.path) as acquired:assert acquired
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=10)


def test_lock_symlink_refused(store,tmp_path):
    target=tmp_path/'other';target.write_text('do not touch')
    Path(store.path).with_name('trakt-inbound.lock').symlink_to(target)
    with pytest.raises(OSError):scheduled(store,lambda:pytest.fail('no read'))
    assert target.read_text()=='do not touch'



@pytest.mark.parametrize('holder', ['movie', 'show'])
def test_cross_media_scheduled_process_holds_same_lock_through_fetch(store, holder):
    movie = InboundStore(store.path, media_type='movie')
    observe(movie, lambda:Snapshot((), media_type='movie'), baseline=True)
    before = dump(store)
    code = """import sys,time
from hub.inbound.scheduled import scheduled_observe
from hub.inbound.models import Snapshot
def read():
 print('fetch_under_lock',flush=True)
 time.sleep(60)
 return Snapshot((),media_type=sys.argv[2])
scheduled_observe(sys.argv[1],read,enabled=True,auto_apply_enabled=True,media_type=sys.argv[2])
"""
    process = subprocess.Popen([sys.executable,'-c',code,store.path,holder],stdout=subprocess.PIPE,text=True)
    try:
        assert select.select([process.stdout],[],[],10)[0]
        assert process.stdout.readline().strip()=='fetch_under_lock'
        other = 'show' if holder=='movie' else 'movie'
        result = scheduled_observe(store.path,lambda:pytest.fail('overlap must never fetch'),
            enabled=True,auto_apply_enabled=True,media_type=other)
        assert result=={'skipped_overlap':True,'canonical_mutations':0,'provider_writes':0}
        assert dump(store)==before
        assert [p.name for p in Path(store.path).parent.glob('*.lock')]==['trakt-inbound.lock']
        process.kill();process.wait(timeout=10)
        with scheduled_lock(store.path) as acquired:assert acquired
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=10)


@pytest.mark.parametrize('media_type', ['episode', 'season', 'unknown', 'movie,show'])
def test_internal_scheduler_rejects_unsupported_media_before_io(tmp_path, media_type):
    path=tmp_path/'must-not-exist.sqlite3'
    with pytest.raises(InboundError):
        scheduled_observe(str(path),lambda:pytest.fail('unsupported media must not fetch'),
            enabled=True,auto_apply_enabled=True,media_type=media_type)
    assert not path.exists() and not path.with_name('trakt-inbound.lock').exists()


def test_internal_show_scheduler_refuses_movie_snapshot_without_publication(store):
    before=dump(store)
    with pytest.raises(InboundError,match='media'):
        scheduled_observe(store.path,lambda:Snapshot((),media_type='movie'),
            enabled=True,auto_apply_enabled=True,media_type='show')
    assert dump(store)==before


def test_public_show_scheduling_refuses_before_settings_http_or_database(tmp_path,monkeypatch,capsys):
    import hub.inbound.trakt as cli
    db=tmp_path/'must-not-exist.sqlite3'
    monkeypatch.setenv('RATING_HUB_DB',str(db))
    monkeypatch.setenv('TRAKT_INBOUND_ENABLED','true')
    monkeypatch.setenv('TRAKT_INBOUND_AUTO_APPLY','true')
    monkeypatch.setattr(cli.InboundSettings,'from_env',lambda:pytest.fail('CLI refusal before settings'))
    monkeypatch.setattr(httpx,'Client',lambda **kw:pytest.fail('CLI refusal before HTTP'))
    assert main(['--scheduled-observe','--media-type','show'])==2
    assert 'scheduling refused' in capsys.readouterr().out
    assert not db.exists() and not db.with_name('trakt-inbound.lock').exists()


@pytest.mark.parametrize('media_types',['show','movie,show'])
def test_production_settings_still_reject_show_scope(monkeypatch,media_types):
    from hub.inbound.trakt import InboundSettings
    monkeypatch.setenv('TRAKT_INBOUND_MEDIA_TYPES',media_types)
    with pytest.raises(InboundError,match='movie-only'):InboundSettings.from_env()
