"""Network-free show observation, lossless migration and movie isolation."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from hub.inbound.auto_apply import auto_apply, AutoApplyError
from hub.inbound.importer import apply_event, GLOBAL_TARGETS
from hub.inbound.models import InboundError, MovieRating, ShowRating, Snapshot, normalize
from hub.inbound.reclassification import reclassify_event
from hub.inbound.removal import apply_removal_event
from hub.inbound.scheduled import scheduled_observe
from hub.inbound.storage import InboundStore
from hub.inbound.trakt import fetch_snapshot, main, observe
from hub.models import RatingWrite
from hub.store import RatingStore

DATE = '2026-09-26T10:00:00Z'
TABLES = ('inbound_state','inbound_snapshots','inbound_unmapped','inbound_events')


def item(rating=8, tmdb=123, **ids):
    return {'rating':rating,'rated_at':DATE,'show':{'ids':{'tmdb':tmdb,'trakt':456,'imdb':'tt1234567',**ids}}}


def headers(page=1,pages=1,count=1,limit=250):
    return {'X-Pagination-Page':str(page),'X-Pagination-Page-Count':str(pages),
            'X-Pagination-Item-Count':str(count),'X-Pagination-Limit':str(limit)}


def fetch(pages, **kwargs):
    seen=[]
    media=kwargs.pop('media_type','show')
    def handler(request):
        assert request.method=='GET' and request.url.path=='/users/me/ratings/shows'
        assert request.url.params['limit']=='250'
        seen.append(int(request.url.params['page']))
        body,hdr=pages[len(seen)-1]
        return httpx.Response(200,json=body,headers=hdr)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=fetch_snapshot(SimpleNamespace(headers={'Authorization':'offline-marker'}),client,media_type=media,**kwargs)
    return result,seen


def snapshot(media='show', *ratings):
    model=ShowRating if media=='show' else MovieRating
    return Snapshot(tuple(model(score,DATE,key,key) for key,score in ratings),media_type=media)


def rows(store, name, media=None):
    with store.connect() as c:
        sql='SELECT * FROM '+name
        if media is not None:sql+=' WHERE media_type=?'
        return [dict(r) for r in c.execute(sql+' ORDER BY 1',(media,) if media else ())]


def dump(store,media=None):return {t:rows(store,t,media) for t in TABLES}

def canonical_dump(store):return {t:rows(store,t) for t in ('ratings','outbox')}


@pytest.fixture
def store(tmp_path):
    path=str(tmp_path/'hub.sqlite3');RatingStore(path)
    return InboundStore(path,media_type='show')


def test_show_normalization_and_content_key():
    r=normalize(item(),media_type='show')
    assert isinstance(r,ShowRating) and r.content_key=='show:tmdb:123'
    assert r.rated_at=='2026-09-26T10:00:00.000000+00:00'
    assert (r.rating,r.trakt_id,r.imdb_id)==(8,456,'tt1234567')


@pytest.mark.parametrize('rating',[1,10])
def test_show_rating_boundaries(rating):assert normalize(item(rating),media_type='show').rating==rating


@pytest.mark.parametrize('rating',[None,True,False,0,11,8.0,8.5,'8'])
def test_show_invalid_ratings(rating):
    with pytest.raises(InboundError):normalize(item(rating),media_type='show')


@pytest.mark.parametrize('field',['tmdb','trakt'])
@pytest.mark.parametrize('value',[True,False,0,-1,1.0,'123'])
def test_show_strict_ids(field,value):
    data=item();data['show']['ids'][field]=value
    with pytest.raises(InboundError):normalize(data,media_type='show')


@pytest.mark.parametrize('value',['tt','tt123','bad',123,True])
def test_show_strict_imdb(value):
    with pytest.raises(InboundError):normalize(item(imdb=value),media_type='show')


@pytest.mark.parametrize('value',[None,'','2026-09-26','2026-09-26T10:00:00','2026-02-30T10:00:00Z'])
def test_show_strict_timestamp(value):
    data=item();data['rated_at']=value
    with pytest.raises(InboundError):normalize(data,media_type='show')


@pytest.mark.parametrize('media',['episode','season','unknown','',None])
def test_unsupported_media_is_rejected(media):
    with pytest.raises(InboundError):normalize(item(),media_type=media)
    with pytest.raises(InboundError):Snapshot((),media_type=media)
    with pytest.raises(InboundError):fetch([],media_type=media)


@pytest.mark.parametrize('other',['movie','episode','season'])
def test_mixed_payload_rejected(other):
    data=item();data[other]={}
    with pytest.raises(InboundError):normalize(data,media_type='show')


@pytest.mark.parametrize('media',['movie','show'])
def test_snapshots_cannot_mix_media(media):
    with pytest.raises(InboundError):Snapshot((MovieRating(8,DATE,123),ShowRating(8,DATE,123)),media_type=media)
    with pytest.raises(InboundError):Snapshot((),(ShowRating(8,DATE),),media_type='movie')


def test_show_fetch_and_complete_pagination():
    result,seen=fetch([([item(tmdb=2)],headers(1,2,2)),([item(tmdb=1)],headers(2,2,2))])
    assert seen==[1,2] and result.media_type=='show'
    assert [r.tmdb_id for r in result.eligible]==[1,2]


def test_show_one_page():
    result,seen=fetch([([item()],headers())]);assert seen==[1] and result.movies==1


@pytest.mark.parametrize('pages',[0,1])
def test_show_empty_fetch(pages):assert fetch([([],headers(pages=pages,count=0))])[0].movies==0


@pytest.mark.parametrize('failure',['missing','wrong_page','changed_count','wrong_total','zero_limit','large_limit','non_list','bad_header'])
def test_show_pagination_failure(failure):
    first=headers(1,2,2);second=headers(2,2,2);body=[item(tmdb=2)]
    if failure=='missing':del second['X-Pagination-Page']
    if failure=='wrong_page':second['X-Pagination-Page']='1'
    if failure=='changed_count':second['X-Pagination-Item-Count']='3'
    if failure=='wrong_total':first['X-Pagination-Item-Count']=second['X-Pagination-Item-Count']='3'
    if failure=='zero_limit':second['X-Pagination-Limit']='0'
    if failure=='large_limit':second['X-Pagination-Limit']='251'
    if failure=='non_list':body={}
    if failure=='bad_header':second['X-Pagination-Page']='2.0'
    with pytest.raises(InboundError):fetch([([item()],first),(body,second)])


def test_show_duplicate_identity_rejected():
    with pytest.raises(InboundError,match='duplicate'):fetch([([item()],headers(1,2,2)),([item()],headers(2,2,2))])


def test_show_unmapped_baseline(store):
    s,_=fetch([([item(tmdb=None)],headers())]);r=observe(store,lambda:s,baseline=True)
    assert r['skipped']==1 and len(rows(store,'inbound_unmapped'))==1
    assert not rows(store,'inbound_events') and not rows(store,'ratings') and not rows(store,'outbox')


def test_show_http_error_is_sanitized():
    marker='private-token-response-url'
    def handler(request):raise RuntimeError(marker)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InboundError) as e:fetch_snapshot(SimpleNamespace(headers={'Authorization':marker}),client,media_type='show')
    assert marker not in str(e.value)


def test_show_deadline_applies_to_all_pages():
    now=[0.0];seen=[]
    def handler(request):
        seen.append(request.extensions['timeout']['read']);now[0]+=0.75
        page=int(request.url.params['page']);return httpx.Response(200,json=[item(tmdb=page)],headers=headers(page,3,3))
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(InboundError,match='deadline'):fetch_snapshot(SimpleNamespace(headers={}),c,timeout=1,clock=lambda:now[0],media_type='show')
    assert seen==[1,0.25]


def old_store(tmp_path):
    path=str(tmp_path/'old.sqlite3');RatingStore(path)
    c=sqlite3.connect(path)
    c.executescript((Path(__file__).parent/'fixtures/trakt_movie_schema.sql').read_text())
    c.execute('INSERT INTO inbound_state VALUES (?,?,?,?,?,?,?,?)',('trakt','movie',DATE,DATE,'a'*64,17,2,1))
    c.execute('INSERT INTO inbound_snapshots VALUES (?,?,?,?,?,?,?,?,?)',('trakt','movie','movie:tmdb:123',8,DATE,123,456,'tt1234567',DATE))
    c.execute('INSERT INTO inbound_unmapped VALUES (?,?,?,?,?,?,?,?)',('trakt','movie',0,7,DATE,789,'tt7654321',DATE))
    for identity,status,classification in ((1,'observed','candidate'),(3,'ignored','echo'),(8,'applied','candidate')):
        c.execute('INSERT INTO inbound_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (identity,hex(identity)[2:]*64,'trakt','movie','movie:tmdb:123',17,'added',None,8,DATE,DATE,status,'different_provider_state',classification,'upsert',DATE if status=='applied' else None,6 if status=='applied' else None))
    c.execute("UPDATE sqlite_sequence SET seq=50 WHERE name='inbound_events'");c.commit();c.close()
    return InboundStore(path,initialize=False)


def db_schema(store):
    with store.connect() as c:return [tuple(r) for r in c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name')]


def test_exact_old_schema_lossless_migration_and_idempotency(tmp_path):
    old=old_store(tmp_path);before=dump(old);canonical=canonical_dump(old);seq=rows(old,'sqlite_sequence')
    migrated=InboundStore(old.path)
    assert dump(migrated)==before and canonical_dump(migrated)==canonical and rows(migrated,'sqlite_sequence')==seq
    ddl=db_schema(migrated)
    assert dump(InboundStore(old.path))==before and db_schema(migrated)==ddl
    show=InboundStore(old.path,media_type='show');observe(show,lambda:snapshot('show'),baseline=True)
    observe(show,lambda:snapshot('show',(123,8)))
    assert dump(show,'movie')==before and show.state()['generation']==2 and show.state('movie')['generation']==17
    assert rows(show,'inbound_events','show')[0]['id']==51


def test_concurrent_migration_preserves_rows(tmp_path):
    old=old_store(tmp_path);before=dump(old)
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:InboundStore(old.path),range(2)))
    assert dump(old)==before and rows(old,'sqlite_sequence')[0]['seq']==50


@pytest.mark.parametrize('change',["ALTER TABLE inbound_state ADD COLUMN mystery TEXT", "CREATE INDEX mystery_index ON inbound_events(status)", "CREATE TABLE inbound_snapshots_migration(x INTEGER)", "DROP TABLE inbound_unmapped", "CREATE TRIGGER mystery_trigger AFTER UPDATE ON inbound_state BEGIN SELECT 1; END"])
def test_unknown_schema_fails_without_partial_changes(tmp_path,change):
    old=old_store(tmp_path)
    with old.connect() as c:c.execute(change)
    ddl=db_schema(old)
    with pytest.raises(InboundError):InboundStore(old.path)
    assert db_schema(old)==ddl


def test_migration_mid_rebuild_failure_rolls_back_all_tables(tmp_path):
    from hub.inbound.schema import migrate_inbound
    old=old_store(tmp_path);before=dump(old);ddl=db_schema(old);seq=rows(old,'sqlite_sequence')
    with old.connect() as c:
        drops=[]
        def deny_second(action,name,*args):
            if action==sqlite3.SQLITE_DROP_TABLE:
                drops.append(name)
                if len(drops)==2:return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        c.set_authorizer(deny_second)
        with pytest.raises(sqlite3.DatabaseError):
            with c:migrate_inbound(c)
    assert dump(old)==before and db_schema(old)==ddl and rows(old,'sqlite_sequence')==seq


def test_movie_show_same_id_isolated_independent_versions(store):
    movie=InboundStore(store.path)
    observe(movie,lambda:snapshot('movie',(123,8)),baseline=True)
    for _ in range(16):observe(movie,lambda:snapshot('movie',(123,8)),baseline=True,reset=True)
    before=dump(movie,'movie')
    observe(store,lambda:snapshot('show',(123,9)),baseline=True)
    assert movie.state()['generation']==17 and store.state()['generation']==1 and dump(movie,'movie')==before
    show_before=dump(store,'show');observe(movie,lambda:snapshot('movie',(123,9)))
    assert dump(store,'show')==show_before
    movie_before=dump(movie,'movie');observe(store,lambda:snapshot('show'))
    assert dump(movie,'movie')==movie_before


@pytest.mark.parametrize('media',['movie','show'])
def test_stable_generation_and_snapshot_rows(media,store):
    scoped=InboundStore(store.path,media_type=media);s=snapshot(media,(123,8));observe(scoped,lambda:s,baseline=True)
    before=dump(scoped,media)
    for _ in range(3):
        r=observe(scoped,lambda:s);assert not r['snapshot_changed'] and r['generation']==1
    after=dump(scoped,media)
    for name in ('inbound_snapshots','inbound_unmapped','inbound_events'):assert after[name]==before[name]
    assert after['inbound_state'][0]['last_successful_poll_at']!=before['inbound_state'][0]['last_successful_poll_at']


@pytest.mark.parametrize('media',['movie','show'])
@pytest.mark.parametrize('change',['timestamp','identity','unmapped'])
def test_non_score_content_change_versions_only_selected_media(media,change,store):
    scoped=InboundStore(store.path,media_type=media);s=snapshot(media,(123,8));observe(scoped,lambda:s,baseline=True)
    if change=='timestamp':s=replace(s,eligible=(replace(s.eligible[0],rated_at='2026-09-27T10:00:00Z'),))
    if change=='identity':s=replace(s,eligible=(replace(s.eligible[0],trakt_id=789),))
    if change=='unmapped':s=replace(s,unmapped=((ShowRating if media=='show' else MovieRating)(8,DATE,None,789),))
    r=observe(scoped,lambda:s);assert r['snapshot_changed'] and r['generation']==2 and r['events']==0


def test_movie_hash_and_fingerprint_compatibility(store):
    # Frozen serialization contract predating Phase 6A; media is not a field.
    movie=MovieRating(8,DATE,123,456,'tt1234567')
    expected={'eligible':[{'rating':8,'rated_at':'2026-09-26T10:00:00.000000+00:00','tmdb_id':123,'trakt_id':456,'imdb_id':'tt1234567'}],'unmapped':[]}
    legacy_hash=hashlib.sha256(json.dumps(expected,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert legacy_hash=='ec93a44bb88aedf378f2cba15ce555507de042a20b8b26eaaa35a90b513f5617'
    s=Snapshot((movie,));assert s.snapshot_hash==legacy_hash
    scoped=InboundStore(store.path);observe(scoped,lambda:Snapshot(()),baseline=True);observe(scoped,lambda:s)
    identity=['trakt','movie',2,'movie:tmdb:123','added',None,8,legacy_hash]
    assert rows(scoped,'inbound_events')[0]['fingerprint']==hashlib.sha256(json.dumps(identity,separators=(',',':')).encode()).hexdigest()


def test_show_delta_lifecycle_never_mutates_canonical_or_outbox(store):
    observe(store,lambda:snapshot('show'),baseline=True);before=canonical_dump(store)
    for state,kind,old,new in ((snapshot('show',(123,8)),'added',None,8),(snapshot('show',(123,9)),'changed',8,9),(snapshot('show'),'removed',9,None)):
        r=observe(store,lambda:state);e=rows(store,'inbound_events')[-1]
        assert r[kind]==1 and r['canonical_mutations']==r['provider_writes']==0
        assert (e['event_type'],e['old_rating'],e['new_rating'])==(kind,old,new)
        assert e['media_type']=='show' and e['content_key']=='show:tmdb:123' and e['status']!='applied'
        assert canonical_dump(store)==before


def test_show_candidate_removal_retains_active_canonical(store):
    hub=RatingStore(store.path);hub.upsert_rating(RatingWrite(media_type='show',tmdb_id=123,rating=9),())
    observe(store,lambda:snapshot('show',(123,9)),baseline=True);before=canonical_dump(store)
    observe(store,lambda:snapshot('show'))
    e=rows(store,'inbound_events')[-1];assert e['classification']=='candidate' and e['future_action']=='delete' and e['status']=='observed'
    assert canonical_dump(store)==before


def test_show_cannot_enter_any_mutation_engine(store):
    before=canonical_dump(store)
    with pytest.raises(InboundError,match='not found'):apply_event(store,GLOBAL_TARGETS,event_id=1,expected_key='show:tmdb:123',expected_rating=8,expected_generation=1,expected_revision=0,confirmed=True)
    with pytest.raises(InboundError,match='observe-only'):apply_removal_event(store,GLOBAL_TARGETS,event_id=1,expected_key='show:tmdb:123',expected_generation=1,expected_old_rating=9,expected_revision=1,expected_source='nuvio',confirmed=True)
    with pytest.raises(InboundError,match='observe-only'):reclassify_event(store,event_id=1,expected_key='show:tmdb:123',expected_generation=1,expected_event_type='removed',expected_old_rating=9,expected_revision=1,confirmed=True)
    with pytest.raises(AutoApplyError):auto_apply(store,GLOBAL_TARGETS,generation=1)
    assert canonical_dump(store)==before


@pytest.mark.parametrize('argv',[['--apply-removal-event','1'],['--reclassify-event','1'],['--scheduled-observe']])
def test_show_cli_mutations_and_scheduler_refused_before_io(tmp_path,monkeypatch,argv,capsys):
    db=tmp_path/'must-not-exist.sqlite3';monkeypatch.setenv('RATING_HUB_DB',str(db))
    assert main([*argv,'--media-type','show'])==2 and not db.exists()
    assert 'observe-only' in capsys.readouterr().out


def test_scheduled_show_snapshot_rejected_without_any_publication(store):
    movie=InboundStore(store.path);observe(movie,lambda:Snapshot(()),baseline=True);before=dump(movie)
    with pytest.raises(InboundError,match='media'):scheduled_observe(store.path,lambda:snapshot('show',(123,8)),enabled=True,auto_apply_enabled=True)
    assert dump(movie)==before and not rows(movie,'ratings') and not rows(movie,'outbox')


def test_movie_auto_apply_does_not_select_show_candidates(store):
    movie=InboundStore(store.path);observe(movie,lambda:Snapshot(()),baseline=True)
    observe(store,lambda:snapshot('show'),baseline=True);observe(store,lambda:snapshot('show',(123,8)))
    before=dump(store);r=auto_apply(movie,GLOBAL_TARGETS,generation=1)
    assert r['auto_candidates']==r['auto_applied']==r['canonical_mutations']==0 and dump(store)==before


def test_show_manual_cli_uses_gets_only_even_with_movie_auto_apply_true(store,monkeypatch,capsys):
    import hub.providers.registry as registry
    monkeypatch.setenv('RATING_HUB_DB',store.path);monkeypatch.setenv('TRAKT_INBOUND_AUTO_APPLY','true')
    monkeypatch.setenv('TRAKT_INBOUND_MEDIA_TYPES','movie');original=httpx.Client;calls=[]
    def handler(request):
        calls.append(request.method);assert request.url.path=='/users/me/ratings/shows'
        return httpx.Response(200,json=[item()],headers=headers())
    monkeypatch.setattr('hub.inbound.trakt.httpx.Client',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    monkeypatch.setattr(registry,'get_provider',lambda name:SimpleNamespace(headers={'Authorization':'offline-secret'},deliver=lambda *a:pytest.fail('provider write')))
    assert main(['--baseline','--media-type','show'])==0
    assert main(['--once','--observe-only','--media-type','show'])==0
    output=capsys.readouterr().out
    assert calls==['GET','GET'] and 'shows=1' in output and 'snapshot_changed=false' in output
    assert 'offline-secret' not in output and 'show:tmdb:' not in output
    assert not rows(store,'inbound_events') and not rows(store,'ratings') and not rows(store,'outbox')


@pytest.mark.parametrize('data',[None,[],{}, {'rating':8,'rated_at':DATE,'show':{}}, {'rating':8,'rated_at':DATE,'show':{'ids':{}}}, {'rating':8,'rated_at':DATE,'movie':{'ids':{'tmdb':123}}}])
def test_show_malformed_objects_refused(data):
    with pytest.raises(InboundError):normalize(data,media_type='show')


def test_show_failed_complete_fetch_retains_trusted_state(store):
    observe(store,lambda:snapshot('show',(123,8)),baseline=True);before=dump(store)
    def read():return fetch([([item(tmdb=456)],headers(1,2,2)),([item(tmdb=789)],headers(2,3,2))])[0]
    with pytest.raises(InboundError):observe(store,read)
    assert dump(store)==before and not rows(store,'ratings') and not rows(store,'outbox')


def test_show_http_status_error_hides_response_body():
    marker='private-response-token'
    with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(401,text=marker))) as c:
        with pytest.raises(InboundError) as error:fetch_snapshot(SimpleNamespace(headers={}),c,media_type='show')
    assert marker not in str(error.value) and 'http' not in str(error.value).lower()


@pytest.mark.parametrize('change',["CHECK(media_type IN ('movie','show','episode'))", "CHECK(media_type='show')", "CHECK(media_type='MOVIE')", "CHECK(media_type='m ovie')"])
def test_unknown_media_constraint_is_refused(tmp_path,change):
    path=str(tmp_path/'unknown.sqlite3');ddl=(Path(__file__).parent/'fixtures/trakt_movie_schema.sql').read_text()
    with sqlite3.connect(path) as c:c.executescript(ddl.replace("CHECK(media_type='movie')",change,1))
    with sqlite3.connect(path) as c:before=c.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
    with pytest.raises(InboundError):InboundStore(path)
    with sqlite3.connect(path) as c:assert c.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()==before
