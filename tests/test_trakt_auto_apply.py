from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from hub.inbound import auto_apply as engine, importer, removal
from hub.inbound.auto_apply import auto_apply, AutoApplyError
from hub.inbound.models import InboundError, MovieRating, Snapshot
from hub.inbound.scheduled import scheduled_observe, scheduled_lock
from hub.inbound.storage import InboundStore
from hub.inbound.trakt import InboundSettings, observe, main
from hub.inbound.importer import GLOBAL_TARGETS, apply_event
from hub.models import RatingWrite
from hub.store import RatingStore

DATE='2026-09-26T10:00:00.000000+00:00'
NOW=datetime(2026,9,26,12,0,tzinfo=timezone.utc)
KEY='movie:tmdb:550'


def snap(*values,date=DATE):
    return Snapshot(tuple(MovieRating(score,date,identity) for identity,score in values))


def rows(store,table):
    with store.connect() as conn:return [dict(r) for r in conn.execute('SELECT * FROM '+table+' ORDER BY 1')]


def dump(store):
    return {name:rows(store,name) for name in ('ratings','outbox','inbound_state','inbound_snapshots','inbound_unmapped','inbound_events')}


@pytest.fixture
def store(tmp_path):
    path=str(tmp_path/'hub.sqlite3');RatingStore(path);store=InboundStore(path)
    observe(store,lambda:snap(),baseline=True)
    return store


def publish(store,snapshot):return observe(store,lambda:snapshot)


def auto(store,**changes):
    args=dict(generation=store.state()['generation'],max_events=10,echo_grace_seconds=600,now=NOW)
    args.update(changes);return auto_apply(store,GLOBAL_TARGETS,**args)


def seed(store,score=8,identity=550):
    result=RatingStore(store.path).upsert_rating(RatingWrite(media_type='movie',tmdb_id=identity,rating=score,
        rated_at=datetime.fromisoformat(DATE),source='nuvio'),GLOBAL_TARGETS)
    with store.connect() as conn:
        conn.execute("UPDATE outbox SET status='done',created_at=?,updated_at=?",((NOW-timedelta(hours=2)).isoformat(),(NOW-timedelta(hours=1)).isoformat()))
    return result


def test_identical_poll_updates_only_poll_metadata_and_preserves_all_snapshot_rows(store,monkeypatch):
    publish(store,snap((550,8)));before=dump(store);version=store.state()['generation']
    class Later(datetime):
        @classmethod
        def now(cls,tz=None):return NOW
    monkeypatch.setattr('hub.inbound.storage.datetime',Later)
    with store.connect() as conn:
        conn.execute("CREATE TRIGGER no_rewrite BEFORE DELETE ON inbound_snapshots BEGIN SELECT RAISE(ABORT,'no rewrite'); END")
    for _ in range(3):
        result=publish(store,snap((550,8)))
        assert not result['snapshot_changed'] and result['generation']==version and result['events']==0
    after=dump(store)
    for table in before:
        if table!='inbound_state':assert after[table]==before[table]
    assert after['inbound_state'][0]=={**before['inbound_state'][0],'last_successful_poll_at':NOW.isoformat()}


def test_candidate_remains_manually_importable_after_identical_polls_and_restart(store):
    publish(store,snap((550,8)));event=rows(store,'inbound_events')[0];version=event['generation']
    for _ in range(4):publish(InboundStore(store.path,initialize=False),snap((550,8)))
    result=apply_event(store,GLOBAL_TARGETS,event_id=event['id'],expected_key=KEY,expected_rating=8,
                       expected_generation=version,expected_revision=0,confirmed=True)
    assert result['revision']==1 and len(rows(store,'inbound_events'))==1


@pytest.mark.parametrize('old,new,kind',[((),((550,8),),'added'),(((550,8),),((550,9),),'changed'),(((550,8),),(),'removed')])
def test_content_delta_advances_generation_once(store,old,new,kind):
    observe(store,lambda:snap(*old),baseline=True,reset=True);version=store.state()['generation']
    result=publish(store,snap(*new))
    assert result['generation']==version+1 and result['snapshot_changed']
    assert rows(store,'inbound_events')[-1]['event_type']==kind


@pytest.mark.parametrize('change',['timestamp','identity','unmapped'])
def test_non_score_content_change_versions_snapshot_without_false_score_event(store,change):
    publish(store,snap((550,8)));before=rows(store,'inbound_events');version=store.state()['generation']
    snapshot=snap((550,8))
    if change=='timestamp':snapshot=snap((550,8),date='2026-09-26T11:00:00Z')
    if change=='identity':snapshot=Snapshot((MovieRating(8,DATE,550,trakt_id=123),))
    if change=='unmapped':snapshot=Snapshot(snapshot.eligible,(MovieRating(7,DATE,None,trakt_id=456),))
    result=publish(store,snapshot)
    assert result['snapshot_changed'] and result['generation']==version+1 and result['events']==0
    assert rows(store,'inbound_events')==before
    assert auto(store)['auto_candidates']==0


def test_explicit_baseline_reset_still_advances_identical_content(store):
    version=store.state()['generation']
    result=observe(store,lambda:snap(),baseline=True,reset=True)
    assert result['generation']==version+1 and result['snapshot_changed'] and result['events']==0


def test_feature_defaults_are_safe(monkeypatch):
    for name in ('TRAKT_INBOUND_AUTO_APPLY','TRAKT_INBOUND_AUTO_APPLY_MAX_EVENTS','TRAKT_INBOUND_ECHO_GRACE_SECONDS'):monkeypatch.delenv(name,raising=False)
    config=InboundSettings.from_env()
    assert not config.auto_apply and config.auto_apply_max_events==10 and config.echo_grace_seconds==600


@pytest.mark.parametrize('name,value',[
    ('TRAKT_INBOUND_AUTO_APPLY','yes'),('TRAKT_INBOUND_AUTO_APPLY','1'),('TRAKT_INBOUND_AUTO_APPLY',''),
    ('TRAKT_INBOUND_AUTO_APPLY_MAX_EVENTS','0'),('TRAKT_INBOUND_AUTO_APPLY_MAX_EVENTS','101'),
    ('TRAKT_INBOUND_AUTO_APPLY_MAX_EVENTS','1.5'),('TRAKT_INBOUND_AUTO_APPLY_MAX_EVENTS','+10'),
    ('TRAKT_INBOUND_AUTO_APPLY_MAX_EVENTS','-1'),('TRAKT_INBOUND_AUTO_APPLY_MAX_EVENTS','true'),
    ('TRAKT_INBOUND_ECHO_GRACE_SECONDS','-1'),('TRAKT_INBOUND_ECHO_GRACE_SECONDS','86401'),
    ('TRAKT_INBOUND_ECHO_GRACE_SECONDS','nan'),('TRAKT_INBOUND_ECHO_GRACE_SECONDS','1.5'),
])
def test_feature_settings_are_strict_and_bounded(monkeypatch,name,value):
    monkeypatch.setenv(name,value)
    with pytest.raises(InboundError):InboundSettings.from_env()


def test_feature_boundary_values(monkeypatch):
    monkeypatch.setenv('TRAKT_INBOUND_AUTO_APPLY','true')
    monkeypatch.setenv('TRAKT_INBOUND_AUTO_APPLY_MAX_EVENTS','100')
    monkeypatch.setenv('TRAKT_INBOUND_ECHO_GRACE_SECONDS','0')
    config=InboundSettings.from_env();assert config.auto_apply and config.auto_apply_max_events==100 and config.echo_grace_seconds==0


def test_default_disabled_scheduler_keeps_candidate_observed(store,monkeypatch):
    monkeypatch.setattr(engine,'auto_apply',lambda *a,**k:pytest.fail('disabled never applies'))
    before=dump(store)
    result=scheduled_observe(store.path,lambda:snap((550,8)),enabled=True)
    assert not result['auto_apply_enabled'] and result['auto_applied']==result['canonical_mutations']==0
    after=dump(store);assert after['ratings']==before['ratings'] and after['outbox']==before['outbox']
    assert after['inbound_events'][0]['status']=='observed'


def test_enabled_scheduler_fetches_once_and_holds_same_lock_through_import(store,monkeypatch):
    calls=[];original=engine.apply_event
    def apply(*args,**kwargs):
        with scheduled_lock(store.path) as acquired:assert not acquired
        assert kwargs['confirmed'] is True
        return original(*args,**kwargs)
    monkeypatch.setattr(engine,'apply_event',apply)
    def read():calls.append(True);return snap((550,8))
    result=scheduled_observe(store.path,read,enabled=True,auto_apply_enabled=True)
    assert len(calls)==1 and result['auto_candidates']==result['auto_applied']==result['canonical_mutations']==1
    assert result['provider_writes']==0
    jobs=rows(store,'outbox');assert len(jobs)==3 and {j['target'] for j in jobs}=={'tmdb','simkl','mdblist'}
    assert rows(store,'ratings')[0]['revision']==1
    with scheduled_lock(store.path) as acquired:assert acquired


@pytest.mark.parametrize('kind',['changed','removed'])
def test_auto_change_and_removal_use_existing_guarded_importers(store,kind):
    seed(store);publish(store,snap((550,8)))  # Existing provider/canonical state, echo ignored.
    publish(store,snap((550,9)) if kind=='changed' else snap())
    result=auto(store)
    assert result['auto_applied']==result['canonical_mutations']==1
    canonical=rows(store,'ratings')[0];event=rows(store,'inbound_events')[-1]
    assert canonical['revision']==2 and canonical['source']==f"trakt-inbound:{event['id']}"
    assert canonical['rating']==(9 if kind=='changed' else None)
    assert canonical['deleted']==(kind=='removed') and event['status']=='applied'
    jobs=[j for j in rows(store,'outbox') if j['revision']==2]
    assert len(jobs)==3 and {j['target'] for j in jobs}=={'tmdb','simkl','mdblist'}
    assert all(j['action']==('upsert' if kind=='changed' else 'remove') and json.loads(j['payload_json'])==canonical for j in jobs)


@pytest.mark.parametrize('kind',['changed','removed'])
def test_old_rating_guard_refuses_auto_change_and_removal(store,kind):
    seed(store);publish(store,snap((550,8)));publish(store,snap((550,9)) if kind=='changed' else snap())
    with store.connect() as conn:conn.execute('UPDATE ratings SET rating=7')
    before=dump(store)
    with pytest.raises(AutoApplyError):auto(store)
    assert dump(store)==before


@pytest.mark.parametrize('sql',[
    "UPDATE inbound_events SET status='ignored'", "UPDATE inbound_events SET classification='echo'",
    "UPDATE inbound_events SET classification='noop'", "UPDATE inbound_events SET classification='defer'",
    "UPDATE inbound_events SET reason='other'", "UPDATE inbound_events SET provider='simkl'",
    "UPDATE inbound_events SET generation=1", "UPDATE inbound_events SET future_action=NULL",
    "UPDATE inbound_events SET status='applied',applied_at='2026-09-26T10:00:00Z',canonical_revision=1",
])
def test_non_eligible_events_never_processed(store,sql):
    publish(store,snap((550,8)))
    with store.connect() as conn:conn.execute(sql)
    before=dump(store);result=auto(store)
    assert result['auto_candidates']==result['auto_applied']==0 and dump(store)==before


def test_exact_maximum_allows_deterministic_id_order(store,monkeypatch):
    publish(store,snap((550,8),(551,9)));order=[];original=engine.apply_event
    def apply(*a,**kw):order.append(kw['event_id']);return original(*a,**kw)
    monkeypatch.setattr(engine,'apply_event',apply)
    result=auto(store,max_events=2)
    assert order==[1,2] and result['auto_applied']==2 and len(rows(store,'outbox'))==6


def test_maximum_plus_one_applies_zero_and_leaves_all_candidates(store):
    publish(store,snap((550,8),(551,9),(552,7)));before=dump(store)
    with pytest.raises(AutoApplyError) as caught:auto(store,max_events=2)
    assert caught.value.result['auto_candidates']==3 and caught.value.result['auto_applied']==0
    assert caught.value.result['canonical_mutations']==0 and dump(store)==before


def prepare_grace(store,age=599):
    seed(store);publish(store,snap((550,8)));publish(store,snap((550,9)))
    with store.connect() as conn:
        conn.execute("UPDATE outbox SET updated_at=? WHERE target='trakt' AND revision=1",((NOW-timedelta(seconds=age)).isoformat(),))


def test_recent_completed_job_holds_candidate_then_same_candidate_imports_after_grace(store):
    prepare_grace(store);before=dump(store)
    result=auto(store)
    assert result['auto_grace_deferred']==1 and result['auto_applied']==0 and dump(store)==before
    generation=store.state()['generation']
    assert not publish(store,snap((550,9)))['snapshot_changed']
    assert store.state()['generation']==generation
    result=auto(store,now=NOW+timedelta(seconds=2))
    assert result['auto_applied']==1 and rows(store,'ratings')[0]['rating']==9


@pytest.mark.parametrize('age',[600,601])
def test_grace_boundary_allows_old_completed_job(store,age):
    prepare_grace(store,age=age);assert auto(store)['auto_applied']==1


@pytest.mark.parametrize('value',['bad','',None,'2026-09-26T11:59:00','2026-09-26T12:59:00+01:00','2026-09-26T12:00:01Z'])
def test_missing_malformed_non_utc_or_future_completion_timing_fails_closed(store,value):
    prepare_grace(store)
    with store.connect() as conn:
        if value is None:
            # The real schema rejects NULL; simulate a legacy/corrupt missing audit using an empty value.
            value=''
        conn.execute("UPDATE outbox SET updated_at=? WHERE target='trakt' AND revision=1",(value,))
    before=dump(store)
    with pytest.raises(AutoApplyError):auto(store)
    assert dump(store)==before


def test_completion_before_creation_fails_closed(store):
    prepare_grace(store)
    with store.connect() as conn:conn.execute("UPDATE outbox SET created_at=? WHERE target='trakt'",(NOW.isoformat(),))
    before=dump(store)
    with pytest.raises(AutoApplyError):auto(store)
    assert dump(store)==before


def test_stale_historical_completed_job_does_not_trigger_grace(store):
    seed(store);RatingStore(store.path).delete_rating(KEY,GLOBAL_TARGETS)
    with store.connect() as conn:
        conn.execute("UPDATE outbox SET status='done'")
        conn.execute("UPDATE outbox SET updated_at='bad' WHERE target='trakt' AND revision=1")
        conn.execute("UPDATE outbox SET created_at=?,updated_at=? WHERE target='trakt' AND revision=2",((NOW-timedelta(hours=2)).isoformat(),(NOW-timedelta(hours=1)).isoformat()))
    publish(store,snap((550,8)));assert auto(store)['auto_applied']==1


@pytest.mark.parametrize('status',['pending','processing','failed'])
def test_current_unresolved_audit_blocks_stored_candidate(store,status):
    prepare_grace(store,601)
    with store.connect() as conn:conn.execute("UPDATE outbox SET status=? WHERE target='trakt'",(status,))
    before=dump(store)
    with pytest.raises(AutoApplyError):auto(store)
    assert dump(store)==before


def test_crash_after_snapshot_publication_recovers_same_event_on_identical_next_poll(store,monkeypatch):
    original=engine.auto_apply
    monkeypatch.setattr(engine,'auto_apply',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('process died')))
    with pytest.raises(RuntimeError):scheduled_observe(store.path,lambda:snap((550,8)),enabled=True,auto_apply_enabled=True)
    event=rows(store,'inbound_events')[0];generation=store.state()['generation']
    monkeypatch.setattr(engine,'auto_apply',original)
    result=scheduled_observe(store.path,lambda:snap((550,8)),enabled=True,auto_apply_enabled=True)
    assert not result['snapshot_changed'] and result['generation']==generation and result['auto_applied']==1
    assert len(rows(store,'inbound_events'))==1 and rows(store,'inbound_events')[0]['fingerprint']==event['fingerprint']


@pytest.mark.parametrize('kind',['added','changed','removed'])
def test_canonical_commit_audit_gap_replay_reuses_existing_importer_without_duplicate_jobs(store,monkeypatch,kind):
    if kind!='added':seed(store);publish(store,snap((550,8)))
    source=snap() if kind=='removed' else snap((550,9 if kind=='changed' else 8))
    module=removal if kind=='removed' else importer
    original=module._mark_applied
    monkeypatch.setattr(module,'_mark_applied',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('private-secret-marker')))
    with pytest.raises(AutoApplyError) as caught:
        scheduled_observe(store.path,lambda:source,enabled=True,auto_apply_enabled=True,echo_grace_seconds=0)
    assert caught.value.result['canonical_mutations']==1 and caught.value.result['auto_failed']==1
    committed=dump(store);generation=store.state()['generation']
    monkeypatch.setattr(module,'_mark_applied',original)
    result=scheduled_observe(store.path,lambda:source,enabled=True,auto_apply_enabled=True,echo_grace_seconds=0)
    assert result['generation']==generation and not result['snapshot_changed']
    assert result['auto_applied']==1 and result['canonical_mutations']==0
    after=dump(store);assert after['ratings']==committed['ratings'] and after['outbox']==committed['outbox']
    assert after['inbound_events'][-1]['status']=='applied'


def test_source_changes_after_canonical_commit_old_generation_not_replayed_and_latest_state_wins(store,monkeypatch):
    original=importer._mark_applied
    monkeypatch.setattr(importer,'_mark_applied',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('audit gap')))
    with pytest.raises(AutoApplyError):scheduled_observe(store.path,lambda:snap((550,8)),enabled=True,auto_apply_enabled=True)
    old=rows(store,'inbound_events')[0]
    monkeypatch.setattr(importer,'_mark_applied',original)
    result=scheduled_observe(store.path,lambda:snap((550,9)),enabled=True,auto_apply_enabled=True)
    assert result['generation']==old['generation']+1 and result['auto_applied']==1
    events=rows(store,'inbound_events');assert events[0]==old and events[1]['status']=='applied'
    assert rows(store,'ratings')[0]['rating']==9 and rows(store,'ratings')[0]['revision']==2


def test_source_changes_before_any_commit_new_candidate_waits_if_old_score_is_not_canonical(store):
    publish(store,snap((550,8)));old=rows(store,'inbound_events')[0]
    with pytest.raises(AutoApplyError):scheduled_observe(store.path,lambda:snap((550,9)),enabled=True,auto_apply_enabled=True)
    assert rows(store,'ratings')==rows(store,'outbox')==[]
    events=rows(store,'inbound_events');assert events[0]==old and events[1]['new_rating']==9 and events[1]['status']=='observed'
    assert events[1]['generation']==old['generation']+1


def test_failure_stops_later_candidates_and_retains_earlier_commit(store,monkeypatch):
    publish(store,snap((550,8),(551,9),(552,7)));original=engine.apply_event;seen=[]
    def apply(*a,**kw):
        seen.append(kw['event_id'])
        if kw['event_id']==2:raise RuntimeError('private-secret-marker')
        return original(*a,**kw)
    monkeypatch.setattr(engine,'apply_event',apply)
    with pytest.raises(AutoApplyError) as caught:auto(store)
    assert seen==[1,2] and caught.value.result['auto_applied']==caught.value.result['canonical_mutations']==1
    assert 'private-secret-marker' not in str(caught.value)
    assert [e['status'] for e in rows(store,'inbound_events')]==['applied','observed','observed']
    assert len(rows(store,'ratings'))==1 and len(rows(store,'outbox'))==3


def test_scheduler_cli_enabled_failure_reports_sanitized_counters(store,monkeypatch,capsys):
    publish(store,snap((550,8),(551,9)))
    monkeypatch.setenv('RATING_HUB_DB',store.path);monkeypatch.setenv('RATING_HUB_TARGETS',','.join(GLOBAL_TARGETS))
    monkeypatch.setenv('TRAKT_INBOUND_ENABLED','true');monkeypatch.setenv('TRAKT_INBOUND_AUTO_APPLY','true')
    monkeypatch.setenv('TRAKT_INBOUND_AUTO_APPLY_MAX_EVENTS','1')
    import hub.inbound.trakt
    monkeypatch.setattr(hub.inbound.trakt,'fetch_snapshot',lambda *a,**kw:snap((550,8),(551,9)))
    import hub.providers.registry
    monkeypatch.setattr(hub.providers.registry,'get_provider',lambda name:object())
    assert main(['--scheduled-observe'])==1
    output=capsys.readouterr().out
    assert 'auto_candidates=2' in output and 'auto_applied=0' in output and 'auto_failed=1' in output
    assert 'canonical_mutations=0' in output and 'provider_writes=0' in output
    assert rows(store,'ratings')==rows(store,'outbox')==[]


def test_already_applied_event_is_not_selected_again(store):
    publish(store,snap((550,8)));auto(store);before=dump(store)
    result=auto(store);assert result['auto_candidates']==result['auto_applied']==0 and dump(store)==before


def test_recent_delivery_arriving_after_plan_is_held_under_importer_write_lock(store,monkeypatch):
    prepare_grace(store,age=601)
    original=engine.apply_event
    def delivery_arrives(*args,**kwargs):
        with store.connect() as conn:
            conn.execute("UPDATE outbox SET updated_at=? WHERE target='trakt' AND revision=1",(NOW.isoformat(),))
        return original(*args,**kwargs)
    monkeypatch.setattr(engine,'apply_event',delivery_arrives)
    result=auto(store)
    assert result['auto_grace_deferred']==1 and result['auto_applied']==result['canonical_mutations']==0
    assert rows(store,'ratings')[0]['rating']==8
    assert rows(store,'inbound_events')[-1]['status']=='observed'


def test_canonical_change_after_plan_refuses_under_importer_write_lock(store,monkeypatch):
    publish(store,snap((550,8)));original=engine.apply_event
    def canonical_changes(*args,**kwargs):
        seed(store,score=7)
        return original(*args,**kwargs)
    monkeypatch.setattr(engine,'apply_event',canonical_changes)
    with pytest.raises(AutoApplyError):auto(store)
    assert rows(store,'ratings')[0]['rating']==7
    assert rows(store,'inbound_events')[0]['status']=='observed'


def test_malformed_creation_timestamp_fails_closed(store):
    prepare_grace(store)
    with store.connect() as conn:conn.execute("UPDATE outbox SET created_at='bad' WHERE target='trakt'")
    before=dump(store)
    with pytest.raises(AutoApplyError):auto(store)
    assert dump(store)==before
