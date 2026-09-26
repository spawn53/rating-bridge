import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from hub.inbound.models import InboundError, MovieRating, Snapshot
from hub.inbound.scheduled import scheduled_lock, scheduled_observe
from hub.inbound.storage import InboundStore
from hub.inbound.trakt import main, observe
from hub.models import RatingWrite
from hub.store import RatingStore

DATE='2026-09-26T10:00:00Z'
KEY='movie:tmdb:550'


def dump(store):
    with store.connect() as conn:
        return {name:[tuple(row) for row in conn.execute('SELECT * FROM '+name+' ORDER BY 1')]
                for name in ('ratings','outbox','inbound_state','inbound_snapshots','inbound_unmapped','inbound_events')}


@pytest.fixture
def store(tmp_path):
    path=str(tmp_path/'hub.sqlite3')
    RatingStore(path)
    store=InboundStore(path)
    observe(store,lambda:Snapshot(()),baseline=True)
    return store


def scheduled(store,read):
    return scheduled_observe(store.path,read,enabled=True)


def test_disabled_scheduled_observer_never_reads_or_touches_database(store):
    before=dump(store)
    with pytest.raises(InboundError):scheduled_observe(store.path,lambda:pytest.fail('no read'),enabled=False)
    assert dump(store)==before and not Path(store.path).with_name('trakt-inbound.lock').exists()


def test_baseline_is_required_and_never_created(store):
    with store.connect() as conn:conn.execute('DELETE FROM inbound_state')
    before=dump(store)
    with pytest.raises(InboundError):scheduled(store,lambda:pytest.fail('no provider read without baseline'))
    assert dump(store)==before


def test_missing_database_is_not_created(tmp_path):
    path=tmp_path/'missing.sqlite3'
    with pytest.raises(sqlite3.OperationalError):scheduled_observe(str(path),lambda:pytest.fail('no read'),enabled=True)
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
    def read():calls.append(True);return Snapshot((MovieRating(8,DATE,550),))
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
    RatingStore(store.path).upsert_rating(RatingWrite(media_type='movie',tmdb_id=550,rating=8),('tmdb','trakt','simkl','mdblist'))
    with store.connect() as conn:conn.execute("UPDATE outbox SET status='done'")
    before=dump(store)
    scheduled(store,lambda:Snapshot((MovieRating(8,DATE,550),)))
    after=dump(store)
    assert after['ratings']==before['ratings'] and after['outbox']==before['outbox']
    with store.connect() as conn:
        e=dict(conn.execute('SELECT * FROM inbound_events').fetchone())
        assert e['status']=='ignored' and e['reason']=='same_as_canonical'


def test_unchanged_snapshot_advances_once_without_new_events(store):
    result=scheduled(store,lambda:Snapshot(()))
    assert result['generation']==2 and result['events']==0
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
    with pytest.raises(sqlite3.IntegrityError):scheduled(store,lambda:Snapshot((MovieRating(8,DATE,550),)))
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


@pytest.fixture
def cli(store,monkeypatch):
    monkeypatch.setenv('RATING_HUB_DB',store.path)
    monkeypatch.setenv('RATING_HUB_TARGETS','tmdb,trakt,simkl,mdblist')
    monkeypatch.setenv('TRAKT_INBOUND_ENABLED','true')
    return store


def mock_reads(monkeypatch,handler):
    import hub.providers.registry
    original=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    monkeypatch.setattr(hub.providers.registry,'get_provider',lambda name:SimpleNamespace(headers={'Authorization':'Bearer private-secret-marker'}))


def hdr(count=0):
    return {'X-Pagination-Page':'1','X-Pagination-Page-Count':'1','X-Pagination-Limit':'250','X-Pagination-Item-Count':str(count)}


def test_enabled_cli_fetches_once_and_logs_only_sanitized_counts(cli,monkeypatch,capsys):
    seen=[]
    def handler(request):
        assert request.method=='GET';seen.append(request);return httpx.Response(200,json=[],headers=hdr())
    mock_reads(monkeypatch,handler)
    import hub.inbound.importer,hub.inbound.removal,hub.inbound.reclassification
    for module,name in ((hub.inbound.importer,'apply_event'),(hub.inbound.removal,'apply_removal_event'),(hub.inbound.reclassification,'reclassify_event')):
        monkeypatch.setattr(module,name,lambda *a,**kw:pytest.fail('scheduled mode cannot apply'))
    before=dump(cli)
    assert main(['--scheduled-observe'])==0 and len(seen)==1
    output=capsys.readouterr().out
    assert output=='Trakt scheduled observation complete\ngeneration=2\nadded=0\nchanged=0\nremoved=0\ndeferred=0\ncanonical_mutations=0\nprovider_writes=0\n'
    after=dump(cli)
    assert after['ratings']==before['ratings'] and after['outbox']==before['outbox']
    assert after['inbound_events']==before['inbound_events']


def test_disabled_cli_is_sanitized_and_makes_no_provider_call(cli,monkeypatch,capsys):
    monkeypatch.setenv('TRAKT_INBOUND_ENABLED','false')
    monkeypatch.setattr(httpx,'Client',lambda **kw:pytest.fail('disabled must not read'))
    before=dump(cli)
    assert main(['--scheduled-observe'])==1
    assert capsys.readouterr().out=='Trakt scheduled observation failed; trusted state retained\n'
    assert dump(cli)==before


@pytest.mark.parametrize('extra',[['--baseline'],['--once'],['--apply-event','1'],['--apply-removal-event','3'],
                                 ['--reclassify-event','3'],['--reset'],['--observe-only'],['--confirm-live-import'],
                                 ['--confirm-reclassification'],['--expect-content-key',KEY],['--expect-rating','8'],
                                 ['--expect-old-rating','9'],['--expect-generation','6'],['--expect-canonical-revision','7'],
                                 ['--expect-event-type','removed'],['--expect-canonical-source','trakt-inbound:2']])
def test_scheduled_cli_cannot_mix_baseline_import_or_expectations(cli,extra):
    before=dump(cli)
    try:result=main(['--scheduled-observe']+extra)
    except SystemExit as exc:result=exc.code
    assert result!=0 and dump(cli)==before


@pytest.mark.parametrize('failure',['malformed','timeout','provider_exception','missing_pagination','partial_count'])
def test_cli_failed_reads_are_sanitized_and_preserve_generation(cli,monkeypatch,capsys,failure):
    def handler(request):
        if failure=='timeout':raise httpx.ReadTimeout('private-secret-marker')
        if failure=='provider_exception':raise RuntimeError('private-secret-marker')
        if failure=='missing_pagination':return httpx.Response(200,json=[],headers={})
        if failure=='partial_count':return httpx.Response(200,json=[],headers=hdr(2))
        return httpx.Response(200,json={'private-secret-marker':'malformed'},headers=hdr())
    mock_reads(monkeypatch,handler);before=dump(cli)
    assert main(['--scheduled-observe'])==1
    assert capsys.readouterr().out=='Trakt scheduled observation failed; trusted state retained\n'
    assert dump(cli)==before


def test_cli_overlap_skips_without_fetch_or_mutation(cli,monkeypatch,capsys):
    monkeypatch.setattr(httpx,'Client',lambda **kw:pytest.fail('overlap must not read'))
    before=dump(cli)
    with scheduled_lock(cli.path) as acquired:
        assert acquired and main(['--scheduled-observe'])==0
    output=capsys.readouterr().out
    assert 'skipped: another instance is active' in output and 'provider_writes=0' in output
    assert dump(cli)==before


def test_manual_commands_still_work_when_disabled(cli,monkeypatch):
    monkeypatch.setenv('TRAKT_INBOUND_ENABLED','false')
    with cli.connect() as conn:conn.execute('DELETE FROM inbound_state')
    mock_reads(monkeypatch,lambda request:httpx.Response(200,json=[],headers=hdr()))
    assert main(['--baseline'])==0
    assert main(['--once','--observe-only'])==0
    assert cli.state()['generation']==2


def test_systemd_units_are_observe_only_and_use_absolute_paths():
    root=Path(__file__).resolve().parents[1]/'deploy/systemd'
    service=(root/'rating-hub-trakt-inbound.service').read_text()
    timer=(root/'rating-hub-trakt-inbound.timer').read_text()
    for setting in ('Type=oneshot','User=ubuntu','Restart=no','/usr/bin/docker compose','--rm --no-deps -T','--scheduled-observe'):
        assert setting in service
    for forbidden in ('--apply-event','--apply-removal-event','--reclassify-event','.env.v2','Authorization','AUTO_APPLY'):
        assert forbidden not in service
    for setting in ('OnBootSec=2min','OnUnitActiveSec=5min','Persistent=true'):assert setting in timer
