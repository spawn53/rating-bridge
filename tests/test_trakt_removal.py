import json
import sqlite3
from datetime import datetime, timezone

import pytest

from hub.inbound import removal
from hub.inbound.classification import Classification
from hub.inbound.importer import apply_event, GLOBAL_TARGETS, TARGETS
from hub.inbound.models import InboundError, MovieRating, Snapshot, timestamp
from hub.inbound.removal import apply_removal_event
from hub.inbound.storage import InboundStore
from hub.inbound.trakt import main, observe
from hub.models import RatingWrite
from hub.store import RatingStore

KEY = 'movie:tmdb:265189'
DATE = '2026-09-26T13:02:32.000000+00:00'


def rows(store, name):
    with store.connect() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM '+name+' ORDER BY 1')]


def dump(store):
    return {name: rows(store, name) for name in (
        'ratings', 'outbox', 'inbound_events', 'inbound_state', 'inbound_snapshots', 'inbound_unmapped'
    )}


def mutate(store, sql, args=()):
    with store.connect() as conn:
        conn.execute('PRAGMA ignore_check_constraints=ON')
        conn.execute(sql, args)


@pytest.fixture
def store(tmp_path):
    inbound = InboundStore(str(tmp_path / 'hub.sqlite3'))
    hub = RatingStore(inbound.path)
    for score in (7, 8, 7, 9):
        hub.upsert_rating(RatingWrite(media_type='movie', tmdb_id=265189, rating=score,
                                     trakt_id=163864, imdb_id='tt2121382', mdblist_id='retained',
                                     title='Force Majeure', source='phase4b-e2e'), GLOBAL_TARGETS)
    hub.delete_rating(KEY, GLOBAL_TARGETS)
    mutate(inbound, "UPDATE outbox SET status='done'")
    observe(inbound, lambda: Snapshot(()), baseline=True)
    for _ in range(2): observe(inbound, lambda: Snapshot(()))
    for event_id, score in ((1, 8), (2, 9)):
        observe(inbound, lambda: Snapshot((MovieRating(score, DATE, 265189, 163864, 'tt2121382'),)))
        result = apply_event(inbound, GLOBAL_TARGETS, event_id=event_id, expected_key=KEY,
                             expected_rating=score, expected_generation=event_id+3,
                             expected_revision=event_id+4, confirmed=True)
        assert result['queued_targets'] == list(TARGETS)
        mutate(inbound, "UPDATE outbox SET status='done'")
    observe(inbound, lambda: Snapshot(()))
    assert rows(inbound, 'inbound_events')[2]['future_action'] == 'delete'
    return inbound


def apply(store, **changes):
    guards = dict(event_id=3, expected_key=KEY, expected_generation=6, expected_old_rating=9,
                  expected_revision=7, expected_source='trakt-inbound:2', confirmed=True)
    guards.update(changes)
    return apply_removal_event(store, GLOBAL_TARGETS, **guards)


def test_valid_removal_retains_identity_timestamp_and_only_updates_event_audit(store):
    before = dump(store)
    result = apply(store)
    assert result == dict(event_id=3, content_key=KEY, revision=8, removed=True,
                          queued_targets=list(TARGETS), skipped_targets=['trakt'],
                          already_applied=False, direct_provider_writes=0)
    after = dump(store)
    canonical = after['ratings'][0]
    assert canonical == {**before['ratings'][0], 'rating':None, 'deleted':1, 'revision':8,
                         'source':'trakt-inbound:3', 'updated_at':canonical['updated_at']}
    assert timestamp(canonical['rated_at']) == DATE
    assert datetime.fromisoformat(canonical['updated_at']).utcoffset().total_seconds() == 0
    assert after['outbox'][:len(before['outbox'])] == before['outbox']
    jobs = after['outbox'][len(before['outbox']):]
    assert len(jobs) == 3 and {j['target'] for j in jobs} == set(TARGETS)
    assert all(j['revision']==8 and j['action']=='remove' and json.loads(j['payload_json'])==canonical for j in jobs)
    assert after['inbound_events'][:2] == before['inbound_events'][:2]
    event = after['inbound_events'][2]
    assert event == {**before['inbound_events'][2], 'status':'applied', 'canonical_revision':8,
                     'applied_at':event['applied_at']}
    assert datetime.fromisoformat(event['applied_at']).utcoffset().total_seconds() == 0
    for name in ('inbound_state', 'inbound_snapshots', 'inbound_unmapped'): assert after[name]==before[name]


def test_delete_primitive_external_transaction_owns_commit_and_rollback(store):
    hub = RatingStore(store.path)
    before = dump(store)
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        result = hub.delete_rating(KEY, GLOBAL_TARGETS, source='explicit-source', connection=conn)
        assert result['revision']==8 and conn.in_transaction
        final = dict(conn.execute('SELECT * FROM ratings').fetchone())
        assert final['source']=='explicit-source' and final['rating'] is None
        assert all(json.loads(row[0]) == final for row in conn.execute('SELECT payload_json FROM outbox WHERE revision=8'))
        assert dump(store)==before  # Independent readers still see the committed active row.
        conn.rollback()
    assert dump(store)==before


def test_delete_primitive_rejects_inactive_external_transaction(store):
    hub = RatingStore(store.path); before = dump(store)
    with store.connect() as conn:
        with pytest.raises(RuntimeError, match='active transaction'):
            hub.delete_rating(KEY, GLOBAL_TARGETS, connection=conn)
    assert dump(store)==before


def test_normal_delete_preserves_source_and_queues_final_tombstone(store):
    hub = RatingStore(store.path); previous = rows(store,'ratings')[0]
    result = hub.delete_rating(KEY, GLOBAL_TARGETS)
    canonical = rows(store,'ratings')[0]
    assert result['queued_targets']==list(GLOBAL_TARGETS) and canonical['source']==previous['source']
    assert canonical['rated_at']==previous['rated_at']
    assert all(json.loads(j['payload_json'])==canonical for j in rows(store,'outbox') if j['revision']==8)
    before=dump(store)
    assert hub.delete_rating(KEY, GLOBAL_TARGETS)['removed'] is False
    assert hub.delete_rating('movie:tmdb:550', GLOBAL_TARGETS)['removed'] is False
    assert dump(store)==before


@pytest.mark.parametrize('target', TARGETS)
def test_any_outbox_insertion_failure_rolls_back_delete_and_event(store, target):
    mutate(store, "CREATE TRIGGER fail_remove BEFORE INSERT ON outbox WHEN NEW.revision=8 AND NEW.target='"+target+"' BEGIN SELECT RAISE(ABORT,'insertion failure'); END")
    before=dump(store)
    with pytest.raises(sqlite3.IntegrityError): apply(store)
    assert dump(store)==before


def test_delete_primitive_failure_in_owned_transaction_rolls_back(store):
    mutate(store,"CREATE TRIGGER fail_remove BEFORE INSERT ON outbox WHEN NEW.revision=8 AND NEW.target='simkl' BEGIN SELECT RAISE(ABORT,'insertion failure'); END")
    before=dump(store)
    with pytest.raises(sqlite3.IntegrityError): RatingStore(store.path).delete_rating(KEY, GLOBAL_TARGETS)
    assert dump(store)==before


@pytest.mark.parametrize('field,value', [
    ('confirmed',False), ('confirmed',1), ('event_id',0), ('event_id',True), ('event_id',1), ('event_id',999),
    ('expected_key','movie:tmdb:550'), ('expected_key','show:tmdb:265189'), ('expected_key','movie:tmdb:0265189'),
    ('expected_generation',5), ('expected_generation',True), ('expected_generation',0),
    ('expected_old_rating',8), ('expected_old_rating',True), ('expected_old_rating',11),
    ('expected_revision',6), ('expected_revision',True), ('expected_revision',0),
    ('expected_source','other'), ('expected_source',''), ('expected_source','trakt-inbound:3'),
])
def test_expectation_guards_are_read_only(store, field, value):
    before=dump(store)
    with pytest.raises(InboundError): apply(store, **{field:value})
    assert dump(store)==before


@pytest.mark.parametrize('sql', [
    "UPDATE inbound_events SET status='ignored' WHERE id=3",
    "UPDATE inbound_events SET classification='echo' WHERE id=3",
    "UPDATE inbound_events SET reason='matches_latest_completed_trakt_job' WHERE id=3",
    "UPDATE inbound_events SET future_action='upsert' WHERE id=3",
    "UPDATE inbound_events SET new_rating=9 WHERE id=3",
    "UPDATE inbound_events SET event_type='changed' WHERE id=3",
    "UPDATE inbound_events SET old_rating=8 WHERE id=3",
    "UPDATE inbound_events SET provider='simkl' WHERE id=3",
    "UPDATE inbound_events SET media_type='show' WHERE id=3",
    "UPDATE inbound_events SET fingerprint='bad' WHERE id=3",
    "UPDATE inbound_events SET provider_rated_at='bad' WHERE id=3",
    "UPDATE inbound_events SET provider_rated_at='2026-09-26T14:00:00Z' WHERE id=3",
    "UPDATE inbound_events SET detected_at='bad' WHERE id=3",
    "UPDATE inbound_events SET applied_at='2026-09-26T14:00:00Z' WHERE id=3",
    "UPDATE inbound_events SET canonical_revision=8 WHERE id=3",
    "UPDATE inbound_state SET generation=7",
    "DELETE FROM inbound_state",
    "DELETE FROM ratings",
    "UPDATE ratings SET deleted=1,rating=NULL",
    "UPDATE ratings SET rating=8",
    "UPDATE ratings SET revision=8",
    "UPDATE ratings SET source='other'",
    "UPDATE ratings SET tmdb_id=550",
    "UPDATE ratings SET media_type='show'",
    "UPDATE ratings SET rated_at='2026-09-26T14:00:00Z'",
    "UPDATE ratings SET revision='bad'",
    "UPDATE outbox SET revision='bad' WHERE target='trakt' AND revision=5",
])
def test_corrupted_or_changed_state_refuses_without_mutation(store, sql):
    mutate(store,sql);before=dump(store)
    with pytest.raises(InboundError): apply(store)
    assert dump(store)==before


def test_snapshot_reappearance_refuses_removal(store):
    mutate(store, "INSERT INTO inbound_snapshots (provider,media_type,content_key,rating,rated_at,tmdb_id,observed_at) VALUES ('trakt','movie',?,9,?,265189,?)", (KEY,DATE,DATE))
    before=dump(store)
    with pytest.raises(InboundError): apply(store)
    assert dump(store)==before


@pytest.mark.parametrize('generation',[6,7])
def test_newer_event_including_ignored_supersedes_removal(store,generation):
    mutate(store,"""INSERT INTO inbound_events
        (fingerprint,provider,media_type,content_key,generation,event_type,old_rating,new_rating,
         provider_rated_at,detected_at,status,reason,classification,future_action)
        VALUES (?,'trakt','movie',?,?,'added',NULL,9,?,?,'ignored','same_as_canonical','echo',NULL)""",
        ('f'*64,KEY,generation,DATE,DATE))
    before=dump(store)
    with pytest.raises(InboundError): apply(store)
    assert dump(store)==before


@pytest.mark.parametrize('revision,status',[(7,s) for s in ('pending','processing','failed','done')]+[(8,s) for s in ('pending','processing','failed','done')])
def test_current_or_future_trakt_audit_refuses(store,revision,status):
    mutate(store,"UPDATE outbox SET revision=?,status=? WHERE target='trakt' AND revision=5",(revision,status))
    before=dump(store)
    with pytest.raises(InboundError): apply(store)
    assert dump(store)==before


@pytest.mark.parametrize('status',['pending','processing','failed'])
def test_stale_trakt_unresolved_does_not_block_current_removal(store,status):
    mutate(store,"UPDATE outbox SET status=? WHERE target='trakt' AND revision=5",(status,))
    assert apply(store)['revision']==8


@pytest.mark.parametrize('decision',[Classification('echo','same_as_canonical'),Classification('noop','both_unrated'),Classification('defer','trakt_outbound_unsettled'),Classification('candidate','different_provider_state','upsert')])
def test_fixed_classifier_must_return_exact_candidate_delete(store,monkeypatch,decision):
    monkeypatch.setattr(removal,'classify',lambda *a:decision)
    before=dump(store)
    with pytest.raises(InboundError): apply(store)
    assert dump(store)==before


def crash_after_commit(store,monkeypatch):
    original=removal._mark_applied
    monkeypatch.setattr(removal,'_mark_applied',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('process exited')))
    with pytest.raises(RuntimeError): apply(store)
    monkeypatch.setattr(removal,'_mark_applied',original)
    assert rows(store,'ratings')[0]['revision']==8 and rows(store,'inbound_events')[2]['status']=='observed'


def test_crash_gap_recovery_does_not_delete_twice_or_duplicate_jobs(store,monkeypatch):
    crash_after_commit(store,monkeypatch)
    committed=dump(store)
    monkeypatch.setattr(RatingStore,'delete_rating',lambda *a,**k:pytest.fail('must not delete again'))
    result=apply(InboundStore(store.path))
    assert result['revision']==8 and result['already_applied'] and result['queued_targets']==[]
    after=dump(store)
    assert after['ratings']==committed['ratings'] and after['outbox']==committed['outbox']
    assert after['inbound_events'][2]['status']=='applied'


@pytest.mark.parametrize('sql', [
    "UPDATE ratings SET source='other'", "UPDATE ratings SET revision=9",
    "UPDATE ratings SET rating=9", "UPDATE ratings SET deleted=0",
    "UPDATE ratings SET tmdb_id=550", "UPDATE ratings SET media_type='show'",
    "UPDATE ratings SET rated_at='2026-09-26T14:00:00Z'", "UPDATE ratings SET updated_at='bad'",
    "UPDATE outbox SET payload_json='{}' WHERE revision=8 AND target='tmdb'",
    "UPDATE outbox SET payload_json='bad' WHERE revision=8 AND target='tmdb'",
    "UPDATE outbox SET target='trakt' WHERE revision=8 AND target='tmdb'",
    "UPDATE outbox SET action='upsert' WHERE revision=8 AND target='tmdb'",
    "DELETE FROM outbox WHERE revision=8 AND target='tmdb'",
    "UPDATE inbound_state SET generation=7",
    "UPDATE inbound_events SET fingerprint='bad' WHERE id=3",
])
def test_crash_gap_inconsistent_audit_fails_closed(store,monkeypatch,sql):
    crash_after_commit(store,monkeypatch);mutate(store,sql);before=dump(store)
    with pytest.raises(InboundError): apply(store)
    assert dump(store)==before


def test_extra_trakt_revision8_job_refuses_crash_recovery(store,monkeypatch):
    crash_after_commit(store,monkeypatch)
    mutate(store,"""INSERT INTO outbox (content_key,target,action,payload_json,revision,status,created_at,updated_at)
        SELECT content_key,'trakt',action,payload_json,revision,'done',created_at,updated_at FROM outbox WHERE revision=8 AND target='tmdb'""")
    before=dump(store)
    with pytest.raises(InboundError): apply(store)
    assert dump(store)==before


def test_already_applied_is_read_only_even_after_later_canonical_write(store):
    apply(store);before=dump(store)
    result=apply(store)
    assert result['already_applied'] and result['revision']==8 and result['queued_targets']==[]
    assert dump(store)==before
    RatingStore(store.path).upsert_rating(RatingWrite(media_type='movie',tmdb_id=265189,rating=7),GLOBAL_TARGETS)
    before=dump(store)
    assert apply(store)['revision']==8 and dump(store)==before


def test_validation_and_delete_share_one_write_lock(store,monkeypatch):
    original=RatingStore.delete_rating
    def attempt(self,key,targets,**kwargs):
        assert kwargs['connection'].in_transaction
        with sqlite3.connect(store.path,timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError,match='locked'):
                other.execute('UPDATE ratings SET rating=8')
        return original(self,key,targets,**kwargs)
    monkeypatch.setattr(RatingStore,'delete_rating',attempt)
    assert apply(store)['revision']==8


@pytest.mark.parametrize('targets',[TARGETS,tuple(reversed(GLOBAL_TARGETS)),GLOBAL_TARGETS+('imdb',),('tmdb','trakt','simkl')])
def test_removal_refuses_target_config_changes(store,targets):
    before=dump(store)
    with pytest.raises(InboundError):
        apply_removal_event(store,targets,event_id=3,expected_key=KEY,expected_generation=6,
                            expected_old_rating=9,expected_revision=7,expected_source='trakt-inbound:2',confirmed=True)
    assert dump(store)==before


def test_existing_add_change_and_normal_api_delete_keep_target_semantics(store,monkeypatch):
    assert [(e['event_type'],e['canonical_revision']) for e in rows(store,'inbound_events')[:2]]==[('added',6),('changed',7)]
    for rev in (6,7):assert {j['target'] for j in rows(store,'outbox') if j['revision']==rev}==set(TARGETS)
    hub=RatingStore(store.path)
    result=hub.upsert_rating(RatingWrite(media_type='movie',tmdb_id=550,rating=7),GLOBAL_TARGETS)
    assert result['queued_targets']==list(GLOBAL_TARGETS)
    # Directly exercise the API handler with isolated settings/store and no provider I/O.
    monkeypatch.setenv('RATING_HUB_DB',store.path)
    monkeypatch.setenv('RATING_HUB_TARGETS',','.join(GLOBAL_TARGETS))
    from hub import app as app_module
    from hub.settings import HubSettings
    monkeypatch.setattr(app_module,'store',hub)
    monkeypatch.setattr(app_module,'settings',HubSettings.from_env())
    result=app_module.remove_rating('movie:tmdb:550')
    assert result.removed and result.queued_targets==list(GLOBAL_TARGETS)


ARGS=['--apply-removal-event','3','--expect-content-key',KEY,'--expect-generation','6',
      '--expect-old-rating','9','--expect-canonical-revision','7',
      '--expect-canonical-source','trakt-inbound:2','--confirm-live-import']


@pytest.mark.parametrize('omit',['--apply-removal-event','--expect-content-key','--expect-generation',
                               '--expect-old-rating','--expect-canonical-revision',
                               '--expect-canonical-source','--confirm-live-import'])
def test_cli_requires_all_guards(store,monkeypatch,omit):
    monkeypatch.setenv('RATING_HUB_DB',store.path)
    args=ARGS.copy();index=args.index(omit);del args[index:index+(1 if omit=='--confirm-live-import' else 2)]
    before=dump(store)
    try:result=main(args)
    except SystemExit as exc:result=exc.code
    assert result!=0 and dump(store)==before


@pytest.mark.parametrize('extra',[['--once'],['--baseline'],['--apply-event','3'],['--reclassify-event','3'],
                                 ['--reset'],['--observe-only'],['--confirm-reclassification'],
                                 ['--expect-rating','9'],['--expect-event-type','removed']])
def test_cli_removal_cannot_mix_other_operations(store,monkeypatch,extra):
    monkeypatch.setenv('RATING_HUB_DB',store.path);before=dump(store)
    try:result=main(ARGS+extra)
    except SystemExit as exc:result=exc.code
    assert result!=0 and dump(store)==before


def test_cli_removal_never_calls_provider_or_http(store,monkeypatch,capsys):
    monkeypatch.setenv('RATING_HUB_DB',store.path)
    monkeypatch.setenv('RATING_HUB_TARGETS',','.join(GLOBAL_TARGETS))
    monkeypatch.setenv('TRAKT_INBOUND_ENABLED','false')
    import httpx
    import hub.providers.registry
    monkeypatch.setattr(httpx,'Client',lambda **k:pytest.fail('no HTTP in removal importer'))
    monkeypatch.setattr(hub.providers.registry,'get_provider',lambda *a:pytest.fail('no provider in removal importer'))
    assert main(ARGS)==0
    output=capsys.readouterr().out
    assert 'revision=8' in output and 'direct_provider_writes=0' in output
    assert "queued_targets=['tmdb', 'simkl', 'mdblist']" in output


def test_cli_unexpected_failure_is_sanitized(store,monkeypatch,capsys):
    monkeypatch.setenv('RATING_HUB_DB',store.path)
    monkeypatch.setattr(removal,'apply_removal_event',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('private-secret-marker')))
    before=dump(store)
    assert main(ARGS)==1
    output=capsys.readouterr().out
    assert 'private-secret-marker' not in output and 'inspect canonical/event audit' in output
    assert dump(store)==before


def test_external_delete_commits_only_when_caller_commits(store):
    hub=RatingStore(store.path)
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        hub.delete_rating(KEY,TARGETS,source='caller-source',connection=conn)
        assert conn.in_transaction and rows(store,'ratings')[0]['deleted']==0
        conn.commit()
    assert rows(store,'ratings')[0]['source']=='caller-source'
    assert {j['target'] for j in rows(store,'outbox') if j['revision']==8}==set(TARGETS)


def test_owned_delete_accepts_explicit_source(store):
    RatingStore(store.path).delete_rating(KEY,TARGETS,source='owned-source')
    assert rows(store,'ratings')[0]['source']=='owned-source'


def test_fingerprint_changed_in_audit_gap_refuses_event_mark(store,monkeypatch):
    original=removal._mark_applied
    def change_before_mark(*args,**kwargs):
        mutate(store,"UPDATE inbound_events SET fingerprint=? WHERE id=3",('e'*64,))
        return original(*args,**kwargs)
    monkeypatch.setattr(removal,'_mark_applied',change_before_mark)
    with pytest.raises(InboundError,match='fingerprint changed'):apply(store)
    assert rows(store,'ratings')[0]['revision']==8
    assert rows(store,'inbound_events')[2]['status']=='observed'
    assert len([j for j in rows(store,'outbox') if j['revision']==8])==3
