"""Offline guarded show import, refusal and audit-gap recovery contracts."""
import json
from datetime import datetime

import pytest

from hub.inbound import importer
from hub.inbound.importer import apply_event, GLOBAL_TARGETS, TARGETS
from hub.inbound.models import InboundError, MovieRating, ShowRating, Snapshot
from hub.inbound.storage import InboundStore
from hub.inbound.trakt import main, observe
from hub.models import RatingWrite
from hub.store import RatingStore
from tests.test_trakt_importer import dump, rows, mutate

KEY = 'show:tmdb:123'
DATE = '2026-09-26T20:35:52.000000+00:00'


@pytest.fixture(params=['added', 'changed'])
def store(tmp_path, request):
    path = str(tmp_path / 'show.sqlite3')
    canonical = RatingStore(path)
    s = InboundStore(path, media_type='show')
    old = ()
    if request.param == 'changed':
        canonical.upsert_rating(RatingWrite(media_type='show', tmdb_id=123, rating=7), ())
        old = (ShowRating(7, DATE, 123, 456, 'tt1234567'),)
    observe(s, lambda: Snapshot(old, media_type='show'), baseline=True)
    observe(s, lambda: Snapshot((ShowRating(8, DATE, 123, 456, 'tt1234567'),), media_type='show'))
    return s


def apply(s, **changes):
    values = dict(event_id=1, expected_key=KEY, expected_rating=8, expected_generation=2,
                  expected_revision=1 if rows(s, 'inbound_events')[0]['event_type']=='changed' else 0,
                  confirmed=True)
    values.update(changes)
    return apply_event(s, GLOBAL_TARGETS, **values)


def test_show_commit_payload_source_exclusion_and_idempotency(store):
    before = dump(store)
    result = apply(store)
    canonical = rows(store, 'ratings')[0]
    assert canonical['content_key']==KEY and canonical['media_type']=='show'
    assert (canonical['rating'],canonical['tmdb_id'],canonical['trakt_id'],canonical['imdb_id'])==(8,123,456,'tt1234567')
    assert canonical['source']=='trakt-inbound:1' and datetime.fromisoformat(canonical['rated_at'])==datetime.fromisoformat(DATE) and canonical['deleted']==0
    assert canonical['revision']==result['revision'] and result['queued_targets']==list(TARGETS)
    assert result['skipped_targets']==['trakt'] and not result['already_applied']
    jobs = rows(store, 'outbox')
    assert len(jobs)==3 and {j['target'] for j in jobs}==set(TARGETS)
    assert all(j['action']=='upsert' and json.loads(j['payload_json'])==canonical for j in jobs)
    event=rows(store,'inbound_events')[0]
    assert event['status']=='applied' and event['canonical_revision']==canonical['revision']
    assert datetime.fromisoformat(event['applied_at']).utcoffset().total_seconds()==0
    for table in ('inbound_state','inbound_snapshots','inbound_unmapped'):assert rows(store,table)==before[table]
    committed=dump(store);again=apply(store)
    assert again['already_applied'] and again['queued_targets']==[] and again['revision']==result['revision']
    assert dump(store)==committed


@pytest.mark.parametrize('changes', [dict(confirmed=False),dict(expected_key='movie:tmdb:123'),dict(expected_key='show:tmdb:0123'),dict(expected_key='show:tmdb:124'),dict(expected_rating=9),dict(expected_generation=1),dict(expected_revision=99)])
def test_show_expectation_guards(store, changes):
    before=dump(store)
    with pytest.raises(InboundError):apply(store,**changes)
    assert dump(store)==before


@pytest.mark.parametrize('sql', [
    "UPDATE inbound_events SET media_type='movie'",
    "UPDATE inbound_events SET provider='simkl'",
    "UPDATE inbound_events SET fingerprint='invalid'",
    "UPDATE inbound_events SET classification='echo'",
    "UPDATE inbound_events SET future_action='delete'",
    "UPDATE inbound_snapshots SET rating=9",
    "UPDATE inbound_snapshots SET rated_at='2026-09-26T21:00:00Z'",
    "DELETE FROM inbound_snapshots",
    "UPDATE inbound_state SET generation=3",
])
def test_show_event_and_snapshot_guards(store, sql):
    mutate(store,sql);before=dump(store)
    with pytest.raises(InboundError):apply(store)
    assert dump(store)==before


def test_newer_same_show_event_refuses_old_intent(store):
    observe(store,lambda:Snapshot((ShowRating(9,DATE,123),),media_type='show'))
    # Restore the generation/score to prove newer-event protection independently.
    mutate(store,"UPDATE inbound_state SET generation=2")
    mutate(store,"UPDATE inbound_snapshots SET rating=8")
    before=dump(store)
    with pytest.raises(InboundError,match='newer'):apply(store)
    assert dump(store)==before


@pytest.mark.parametrize('field,value',[('media_type','movie'),('tmdb_id',124)])
def test_show_canonical_identity_guard(store,field,value):
    if not rows(store,'ratings'):
        RatingStore(store.path).upsert_rating(RatingWrite(media_type='show',tmdb_id=123,rating=7),())
    mutate(store,f'UPDATE ratings SET {field}=?',(value,));before=dump(store)
    with pytest.raises(InboundError,match='identity'):apply(store)
    assert dump(store)==before


@pytest.mark.parametrize('corruption',[None,'source','rating','rated_at','revision','media_type','tmdb_id','trakt_id','imdb_id','target','payload','missing_job'])
def test_show_audit_gap_recovery_requires_exact_commit(store,monkeypatch,corruption):
    original=importer._mark_applied
    monkeypatch.setattr(importer,'_mark_applied',lambda *args, **kw:(_ for _ in ()).throw(RuntimeError('simulated exit')))
    with pytest.raises(RuntimeError):apply(store)
    monkeypatch.setattr(importer,'_mark_applied',original)
    if corruption in {'source','rating','rated_at','revision','media_type','tmdb_id','trakt_id','imdb_id'}:
        value={'source':'other','rating':9,'rated_at':'2026-09-26T21:00:00Z','revision':99,'media_type':'movie','tmdb_id':124,'trakt_id':457,'imdb_id':'tt7654321'}[corruption]
        mutate(store,f'UPDATE ratings SET {corruption}=?',(value,))
    if corruption=='target':mutate(store,"UPDATE outbox SET target='trakt' WHERE target='tmdb'")
    if corruption=='payload':mutate(store,"UPDATE outbox SET payload_json='{}' WHERE target='tmdb'")
    if corruption=='missing_job':mutate(store,"DELETE FROM outbox WHERE target='tmdb'")
    before=dump(store)
    if corruption:
        with pytest.raises(InboundError):apply(store)
        assert dump(store)==before
    else:
        result=apply(store)
        assert result['already_applied'] and result['queued_targets']==[]
        assert rows(store,'ratings')==before['ratings'] and rows(store,'outbox')==before['outbox']
        assert rows(store,'inbound_events')[0]['status']=='applied'


def test_same_numeric_movie_id_never_supersedes_or_changes_show(store):
    movie=InboundStore(store.path)
    observe(movie,lambda:Snapshot(()),baseline=True)
    observe(movie,lambda:Snapshot((MovieRating(9,DATE,123),)))
    before={t:[r for r in rows(store,t) if r.get('media_type')=='movie'] for t in ('inbound_state','inbound_snapshots','inbound_events')}
    apply(store)
    assert before=={t:[r for r in rows(store,t) if r.get('media_type')=='movie'] for t in before}


def test_show_guarded_cli_requires_every_expectation(store,monkeypatch):
    monkeypatch.setenv('RATING_HUB_DB',store.path)
    monkeypatch.setenv('RATING_HUB_TARGETS',','.join(GLOBAL_TARGETS))
    argv=['--apply-event','1','--media-type','show','--expect-content-key',KEY,'--expect-rating','8','--expect-generation','2','--expect-canonical-revision',str(1 if rows(store,'ratings') else 0),'--confirm-live-import']
    for flag in ('--expect-content-key','--expect-rating','--expect-generation','--expect-canonical-revision','--confirm-live-import'):
        incomplete=argv[:];i=incomplete.index(flag);del incomplete[i:i+(1 if flag=='--confirm-live-import' else 2)]
        before=dump(store);assert main(incomplete)==2;assert dump(store)==before
    assert main(argv)==0 and rows(store,'inbound_events')[0]['status']=='applied'
