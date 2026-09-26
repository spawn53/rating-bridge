import json
import sqlite3

import pytest

from hub.inbound.classification import Classification, classify
from hub.inbound.importer import apply_event, GLOBAL_TARGETS
from hub.inbound.models import InboundError, MovieRating, Snapshot
from hub.inbound.reclassification import reclassify_event, OLD, NEW, AUDIT_FIELDS
from hub.inbound.storage import InboundStore
from hub.inbound.trakt import main, observe
from hub.models import RatingWrite
from hub.store import RatingStore

KEY = "movie:tmdb:265189"
DATE = "2026-09-26T13:02:32.000000+00:00"


def canonical(score=9, deleted=0, revision=7):
    return {"rating":score, "deleted":deleted, "revision":revision}


def job(revision=5, status="done", action="remove", score=None, identity=16):
    return {"id":identity, "content_key":KEY, "target":"trakt", "action":action,
            "revision":revision, "status":status, "payload_json":json.dumps({"rating":score})}


def test_exact_phase4b_rev5_remove_does_not_explain_rev7_user_removal():
    result = classify(KEY, None, canonical(), [job()])
    assert result == Classification("candidate", "different_provider_state", "delete")


def test_historical_completed_upsert_is_not_current_echo():
    result = classify(KEY, 8, canonical(), [job(action="upsert",score=8)])
    assert result == Classification("candidate", "different_provider_state", "upsert")


@pytest.mark.parametrize("status", ["pending", "processing", "failed", "done", "superseded"])
def test_stale_jobs_are_history_only_even_with_unusable_payloads(status):
    old = job(status=status)
    old.update(id="invalid old id", payload_json="unparseable", action="historical")
    assert classify(KEY, None, canonical(), [old]).future_action == "delete"
    assert classify(KEY, 9, canonical(), [old]).reason == "same_as_canonical"
    assert classify(KEY, None, canonical(None,1), [old]).reason == "both_unrated"


@pytest.mark.parametrize("status", ["pending", "processing", "failed"])
def test_only_current_unresolved_job_defers(status):
    assert classify(KEY,None,canonical(),[job(7,status)] ) == Classification("defer","trakt_outbound_unsettled")


@pytest.mark.parametrize("status", ["done", "pending", "processing", "failed", "superseded"])
def test_ahead_revision_fails_closed_even_if_provider_matches_canonical(status):
    assert classify(KEY,9,canonical(),[job(8,status)]) == Classification("defer","trakt_outbound_audit_ahead")


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 7.5, "7"])
def test_canonical_revision_must_be_positive_strict_integer(value):
    assert classify(KEY,None,canonical(revision=value),[job()]) == Classification("defer","invalid_canonical_revision")


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 5.5, "5"])
def test_job_revision_malformed_fails_closed(value):
    assert classify(KEY,None,canonical(),[job(value)]) == Classification("defer","invalid_outbound_audit")


def test_current_completed_matching_state_keeps_echo_semantics():
    assert classify(KEY,None,canonical(),[job(7)]) == Classification("echo","matches_latest_completed_trakt_job")
    assert classify(KEY,8,canonical(),[job(7,action="upsert",score=8)]) == Classification("echo","matches_latest_completed_trakt_job")
    assert classify(KEY,9,canonical(),[job(7,action="upsert",score=9)]).reason == "same_as_canonical"


@pytest.mark.parametrize("bad", ["json", "rating_bool", "rating_none", "action", "id", "status"])
def test_bad_current_completed_audit_fails_closed_including_same_score(bad):
    current = job(7,action="upsert",score=9)
    if bad == "json": current["payload_json"] = "private-secret-marker"
    if bad == "rating_bool": current["payload_json"] = '{"rating":true}'
    if bad == "rating_none": current["payload_json"] = '{"rating":null}'
    if bad == "action": current["action"] = "unknown"
    if bad == "id": current["id"] = True
    if bad == "status": current["status"] = "unknown"
    assert classify(KEY,9,canonical(),[current]) == Classification("defer","invalid_outbound_audit")


@pytest.mark.parametrize("status", ["done", "pending", "processing", "failed"])
def test_contradictory_current_jobs_fail_closed_without_selection(status):
    current = [job(7),job(7,status,action="upsert",score=9,identity=17)]
    for ordering in (current,list(reversed(current))):
        assert classify(KEY,None,canonical(),ordering) == Classification("defer","invalid_outbound_audit")


def test_other_targets_and_other_titles_are_not_causal_evidence():
    unrelated = [dict(job(8),target="tmdb"),dict(job(8),content_key="movie:tmdb:550")]
    assert classify(KEY,None,canonical(),unrelated).future_action == "delete"


def test_absent_canonical_with_unanchored_outbound_audit_defers():
    assert classify(KEY,9,None,[job()]) == Classification("defer","invalid_outbound_audit")
    assert classify(KEY,None,None).reason == "removal_no_canonical_state"
    assert classify(KEY,None,None,[job()]).reason == "removal_no_canonical_state"


def rows(store, name):
    with store.connect() as c:
        return [dict(r) for r in c.execute('SELECT * FROM '+name+' ORDER BY 1')]


def dump(store):
    return {name:rows(store,name) for name in (
        "ratings","outbox","inbound_events","inbound_state","inbound_snapshots","inbound_unmapped"
    )}


def mutate(store, sql, args=()):
    with store.connect() as c: c.execute(sql,args)


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = InboundStore(str(tmp_path / 'hub.sqlite3'))
    hub = RatingStore(store.path)
    for score in (7,8,7,9):
        hub.upsert_rating(RatingWrite(media_type='movie',tmdb_id=265189,rating=score,
                                     source='phase4b-e2e'),GLOBAL_TARGETS)
    hub.delete_rating(KEY,GLOBAL_TARGETS)
    mutate(store,"UPDATE outbox SET status='done'")
    observe(store,lambda:Snapshot(()),baseline=True)
    for _ in range(2): observe(store,lambda:Snapshot(()))
    observe(store,lambda:Snapshot((MovieRating(8,DATE,265189),)))
    first = apply_event(store,GLOBAL_TARGETS,event_id=1,expected_key=KEY,expected_rating=8,
                        expected_generation=4,expected_revision=5,confirmed=True)
    assert first['revision']==6 and first['queued_targets']==['tmdb','simkl','mdblist']
    mutate(store,"UPDATE outbox SET status='done'")
    observe(store,lambda:Snapshot((MovieRating(9,DATE,265189),)))
    second = apply_event(store,GLOBAL_TARGETS,event_id=2,expected_key=KEY,expected_rating=9,
                         expected_generation=5,expected_revision=6,confirmed=True)
    assert second['revision']==7 and second['queued_targets']==['tmdb','simkl','mdblist']
    mutate(store,"UPDATE outbox SET status='done'")
    # Reproduce only the old classifier at detection time; persist a real event
    # fingerprint and transition using the normal transactional observer.
    with monkeypatch.context() as patch:
        patch.setattr('hub.inbound.storage.classify',lambda *args:Classification('echo','matches_latest_completed_trakt_job'))
        observe(store,lambda:Snapshot(()))
    assert rows(store,'inbound_events')[2]['status']=='ignored'
    return store


def repair(store, **changes):
    guards=dict(event_id=3,expected_key=KEY,expected_generation=6,expected_event_type='removed',
                expected_old_rating=9,expected_revision=7,confirmed=True)
    guards.update(changes)
    return reclassify_event(store,**guards)


def test_repair_preserves_entire_transition_and_all_other_tables(store):
    before=dump(store)
    result=repair(store)
    assert result['old']==dict(zip(AUDIT_FIELDS,OLD)) and result['new']==dict(zip(AUDIT_FIELDS,NEW))
    assert not result['already_reclassified']
    assert result['canonical_mutations']==result['outbox_mutations']==result['provider_writes']==0
    after=dump(store)
    for name in before:
        if name!='inbound_events': assert after[name]==before[name]
    assert after['inbound_events'][:2]==before['inbound_events'][:2]
    expected={**before['inbound_events'][2],**dict(zip(AUDIT_FIELDS,NEW))}
    assert after['inbound_events'][2]==expected
    assert expected['applied_at'] is None and expected['canonical_revision'] is None


def test_repeated_repair_is_safe_and_read_only(store):
    repair(store)
    before=dump(store)
    assert repair(InboundStore(store.path))['already_reclassified']
    assert dump(store)==before


@pytest.mark.parametrize('field,value',[
    ('confirmed',False),('event_id',0),('event_id',True),('event_id',1),('event_id',2),('event_id',999),
    ('expected_key','show:tmdb:265189'),('expected_key','movie:tmdb:550'),
    ('expected_generation',5),('expected_generation',True),('expected_event_type','changed'),
    ('expected_old_rating',8),('expected_old_rating',True),('expected_revision',6),('expected_revision',True),
])
def test_reclassification_expectation_guards_refuse_without_mutation(store,field,value):
    before=dump(store)
    with pytest.raises(InboundError): repair(store,**{field:value})
    assert dump(store)==before


@pytest.mark.parametrize('sql',[
    "UPDATE inbound_events SET status='observed' WHERE id=3",
    "UPDATE inbound_events SET classification='candidate' WHERE id=3",
    "UPDATE inbound_events SET reason='other' WHERE id=3",
    "UPDATE inbound_events SET future_action='delete' WHERE id=3",
    "UPDATE inbound_events SET fingerprint='bad' WHERE id=3",
    "UPDATE inbound_events SET provider='simkl' WHERE id=3",
    "UPDATE inbound_events SET media_type='show' WHERE id=3",
    "UPDATE inbound_events SET new_rating=8 WHERE id=3",
    "UPDATE inbound_events SET provider_rated_at='bad' WHERE id=3",
    "UPDATE inbound_events SET detected_at='bad' WHERE id=3",
    "UPDATE inbound_events SET status='applied',applied_at='2026-09-26T13:02:32Z',canonical_revision=7 WHERE id=3",
    "UPDATE inbound_state SET generation=7",
    "UPDATE ratings SET rating=8",
    "UPDATE ratings SET deleted=1,rating=NULL",
    "UPDATE ratings SET revision=8",
    "UPDATE ratings SET tmdb_id=550",
    "DELETE FROM ratings",
    "DELETE FROM outbox WHERE target='trakt' AND action='remove'",
    "UPDATE outbox SET revision=8 WHERE target='trakt' AND revision=5",
])
def test_bad_event_snapshot_canonical_or_audit_refuses_without_mutation(store,sql):
    with store.connect() as c:
        c.execute('PRAGMA ignore_check_constraints=ON')
        c.execute(sql)
    before=dump(store)
    with pytest.raises(InboundError): repair(store)
    assert dump(store)==before


def test_snapshot_unexpectedly_contains_removed_movie_refuses(store):
    mutate(store,"INSERT INTO inbound_snapshots VALUES ('trakt','movie',?,9,?,265189,NULL,NULL,?)",(KEY,DATE,DATE))
    before=dump(store)
    with pytest.raises(InboundError,match='snapshot'): repair(store)
    assert dump(store)==before


def test_newer_event_for_same_content_refuses_repair(store):
    mutate(store,"""INSERT INTO inbound_events
        (fingerprint,provider,media_type,content_key,generation,event_type,old_rating,new_rating,
         provider_rated_at,detected_at,status,reason,classification,future_action)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ('a'*64,'trakt','movie',KEY,7,'added',None,9,DATE,DATE,'observed','different_provider_state','candidate','upsert'))
    before=dump(store)
    with pytest.raises(InboundError,match='newer'): repair(store)
    assert dump(store)==before


@pytest.mark.parametrize('status',['pending','processing','failed','done'])
def test_current_revision_job_prevents_false_echo_repair(store,status):
    mutate(store,"UPDATE outbox SET revision=7,status=? WHERE target='trakt' AND revision=5",(status,))
    before=dump(store)
    with pytest.raises(InboundError,match='Recomputed'): repair(store)
    assert dump(store)==before


def test_guard_recompute_must_be_candidate_delete(store,monkeypatch):
    monkeypatch.setattr('hub.inbound.reclassification.classify',lambda *args:Classification('candidate','different_provider_state','upsert'))
    before=dump(store)
    with pytest.raises(InboundError): repair(store)
    assert dump(store)==before


def test_repair_holds_write_lock_from_validation_through_update(store,monkeypatch):
    original=classify
    def concurrent(*args):
        with sqlite3.connect(store.path,timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError,match='locked'):
                other.execute("UPDATE ratings SET rating=8")
        return original(*args)
    monkeypatch.setattr('hub.inbound.reclassification.classify',concurrent)
    assert not repair(store)['already_reclassified']


def test_repair_update_failure_rolls_back_all_audit_fields(store):
    mutate(store,"CREATE TRIGGER fail_repair BEFORE UPDATE ON inbound_events BEGIN SELECT RAISE(ABORT,'simulated failure'); END")
    before=dump(store)
    with pytest.raises(sqlite3.IntegrityError): repair(store)
    assert dump(store)==before


def test_added_changed_imports_and_four_target_nuvio_regressions(store):
    events=rows(store,'inbound_events')
    assert [(r['event_type'],r['status'],r['canonical_revision']) for r in events[:2]]==[('added','applied',6),('changed','applied',7)]
    current=rows(store,'outbox')
    for revision in (6,7): assert {j['target'] for j in current if j['revision']==revision}=={'tmdb','simkl','mdblist'}
    result=RatingStore(store.path).upsert_rating(RatingWrite(media_type='movie',tmdb_id=550,rating=7),GLOBAL_TARGETS)
    assert result['queued_targets']==list(GLOBAL_TARGETS)


def test_reclassified_removal_is_not_enabled_for_import(store):
    repair(store)
    before=dump(store)
    with pytest.raises(InboundError):
        apply_event(store,GLOBAL_TARGETS,event_id=3,expected_key=KEY,expected_rating=9,
                    expected_generation=6,expected_revision=7,confirmed=True)
    assert dump(store)==before


ARGS=['--reclassify-event','3','--expect-content-key',KEY,'--expect-generation','6',
      '--expect-event-type','removed','--expect-old-rating','9','--expect-canonical-revision','7',
      '--confirm-reclassification']


@pytest.mark.parametrize('omit',['--reclassify-event','--expect-content-key','--expect-generation',
                               '--expect-event-type','--expect-old-rating','--expect-canonical-revision',
                               '--confirm-reclassification'])
def test_cli_requires_every_explicit_guard(store,monkeypatch,omit):
    monkeypatch.setenv('RATING_HUB_DB',store.path)
    args=ARGS.copy();index=args.index(omit)
    del args[index:index+(1 if omit=='--confirm-reclassification' else 2)]
    before=dump(store)
    try: result=main(args)
    except SystemExit as exc: result=exc.code
    assert result!=0 and dump(store)==before


@pytest.mark.parametrize('extra',[['--once'],['--baseline'],['--apply-event','3'],['--reset'],
                                 ['--observe-only'],['--confirm-live-import'],['--expect-rating','9']])
def test_cli_cannot_mix_repair_with_import_or_observer(store,monkeypatch,extra):
    monkeypatch.setenv('RATING_HUB_DB',store.path)
    before=dump(store)
    try: result=main(ARGS+extra)
    except SystemExit as exc: result=exc.code
    assert result!=0 and dump(store)==before


def test_cli_repair_has_no_http_provider_or_canonical_calls(store,monkeypatch,capsys):
    import httpx
    from hub.providers import registry
    monkeypatch.setenv('RATING_HUB_DB',store.path)
    monkeypatch.setattr(httpx,'Client',lambda **kw:pytest.fail('HTTP forbidden'))
    monkeypatch.setattr(registry,'get_provider',lambda *a:pytest.fail('provider forbidden'))
    monkeypatch.setattr(RatingStore,'upsert_rating',lambda *a,**k:pytest.fail('canonical write forbidden'))
    monkeypatch.setattr(RatingStore,'delete_rating',lambda *a,**k:pytest.fail('canonical delete forbidden'))
    assert main(ARGS)==0
    output=capsys.readouterr().out
    assert 'canonical_mutations=0' in output and 'outbox_mutations=0' in output and 'provider_writes=0' in output


def test_added_replay_ignores_historical_pending_job_after_canonical_commit(tmp_path,monkeypatch):
    from hub.inbound import importer
    store=InboundStore(str(tmp_path/'replay.sqlite3'))
    hub=RatingStore(store.path)
    hub.upsert_rating(RatingWrite(media_type='movie',tmdb_id=265189,rating=7),GLOBAL_TARGETS)
    hub.delete_rating(KEY,GLOBAL_TARGETS)
    mutate(store,"UPDATE outbox SET status='done'")
    observe(store,lambda:Snapshot(()),baseline=True)
    observe(store,lambda:Snapshot((MovieRating(8,DATE,265189),)))
    args=dict(event_id=1,expected_key=KEY,expected_rating=8,expected_generation=2,expected_revision=2,confirmed=True)
    original=importer._mark_applied
    monkeypatch.setattr(importer,'_mark_applied',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError): apply_event(store,GLOBAL_TARGETS,**args)
    monkeypatch.setattr(importer,'_mark_applied',original)
    mutate(store,"UPDATE outbox SET status='pending' WHERE target='trakt' AND revision=2")
    before_jobs=rows(store,'outbox')
    result=apply_event(store,GLOBAL_TARGETS,**args)
    assert result['revision']==3 and result['already_applied']
    assert rows(store,'outbox')==before_jobs and rows(store,'inbound_events')[0]['status']=='applied'



def test_cli_reclassification_hides_unexpected_secret_bearing_exception(store,monkeypatch,capsys):
    monkeypatch.setenv('RATING_HUB_DB',store.path)
    def fail(*args,**kwargs): raise RuntimeError('private-oauth-token raw-provider-body')
    monkeypatch.setattr('hub.inbound.reclassification.reclassify_event',fail)
    before=dump(store)
    assert main(ARGS)==1
    output=capsys.readouterr().out
    assert 'private-oauth-token' not in output and 'raw-provider-body' not in output
    assert dump(store)==before


def test_repeated_repair_refuses_if_canonical_changed_in_the_meantime(store):
    repair(store)
    mutate(store,"UPDATE ratings SET rating=8,revision=8")
    before=dump(store)
    with pytest.raises(InboundError): repair(store)
    assert dump(store)==before
