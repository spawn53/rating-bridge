"""Offline episode observation and hard zero-mutation boundaries."""
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from hub.inbound.episode_models import EpisodeRating, EpisodeSnapshot, normalize_episode
from hub.inbound.episode_storage import EpisodeStore, SCHEMAS
from hub.inbound.episodes import fetch_episode_snapshot, observe_episode, main
from hub.inbound.models import InboundError, MovieRating, ShowRating, Snapshot
from hub.inbound.storage import InboundStore
from hub.inbound.trakt import InboundSettings, observe, main as existing_main
from hub.models import RatingWrite
from hub.store import RatingStore

DATE = '2026-09-26T10:00:00Z'
KEY = 'episode:tmdb:195339:s1:e1'


def item(score=8, series=195339, season=1, episode=1, trakt=194117, imdb='tt11680642', tmdb=42):
    return {'rating': score, 'rated_at': DATE,
            'show': {'ids': {'tmdb': series, 'trakt': 99, 'imdb': 'tt7654321'}},
            'episode': {'season': season, 'number': episode,
                        'ids': {'trakt': trakt, 'imdb': imdb, 'tmdb': tmdb}}}


def rating(**kwargs):
    return normalize_episode(item(**kwargs))


def snapshot(*ratings):
    return EpisodeSnapshot(tuple(r for r in ratings if r.content_key is not None),
                           tuple(r for r in ratings if r.content_key is None))


def headers(page=1, pages=1, count=1, limit=250):
    return {'X-Pagination-Page': str(page), 'X-Pagination-Page-Count': str(pages),
            'X-Pagination-Item-Count': str(count), 'X-Pagination-Limit': str(limit)}


def fetch(pages, **kwargs):
    seen = []
    def handle(request):
        assert request.method == 'GET' and request.url.path == '/users/me/ratings/episodes'
        assert request.headers['Authorization'] == 'Bearer offline'
        assert request.url.params['limit'] == '250'
        page = int(request.url.params['page']); seen.append(page)
        body, hdr = pages[page-1]
        return httpx.Response(200, json=body, headers=hdr)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = fetch_episode_snapshot(SimpleNamespace(headers={'Authorization': 'Bearer offline'}),
                                        client, **kwargs)
    return result, seen


def dump(path, tables):
    with sqlite3.connect(path) as c:
        c.row_factory = sqlite3.Row
        return {t: [dict(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY 1')] for t in tables}


@pytest.fixture
def store(tmp_path):
    path = str(tmp_path/'audit.sqlite3')
    canonical = RatingStore(path)
    for media in ('movie', 'show'):
        canonical.upsert_rating(RatingWrite(media_type=media, rating=6, tmdb_id=195339), ['tmdb'])
        inbound = InboundStore(path, media_type=media)
        model = MovieRating if media == 'movie' else ShowRating
        observe(inbound, lambda: Snapshot((model(6, DATE, 195339),), media_type=media), baseline=True)
    canonical.upsert_rating(RatingWrite(media_type='episode', rating=5, tmdb_series_id=195339,
                                      season_number=1, episode_number=1, trakt_id=194117), ['tmdb'])
    return EpisodeStore(path)


SHARED = ('ratings', 'outbox', 'inbound_state', 'inbound_snapshots', 'inbound_events', 'inbound_unmapped')


def test_mapping_matches_existing_canonical_identity():
    r = rating()
    assert r.content_key == KEY and r.media_type == 'episode'
    assert (r.tmdb_series_id, r.season_number, r.episode_number) == (195339, 1, 1)
    assert (r.tmdb_id, r.trakt_id, r.imdb_id) == (42, 194117, 'tt11680642')
    assert r.rated_at == '2026-09-26T10:00:00.000000+00:00'
    assert r.content_key == RatingWrite(media_type='episode', rating=8, tmdb_series_id=195339,
                                       season_number=1, episode_number=1).content_key


def test_special_season_zero_supported():
    assert rating(season=0).content_key == 'episode:tmdb:195339:s0:e1'


@pytest.mark.parametrize('field', ['tmdb_series_id', 'tmdb_id', 'trakt_id'])
@pytest.mark.parametrize('value', [True, False, 0, -1, 1.0, '123', {}, []])
def test_strict_model_ids(field, value):
    with pytest.raises(InboundError): replace(rating(), **{field: value})


@pytest.mark.parametrize('field', ['season_number', 'episode_number'])
@pytest.mark.parametrize('value', [None, True, False, -1, 1.0, '1', {}, []])
def test_strict_coordinates(field, value):
    with pytest.raises(InboundError): replace(rating(), **{field: value})


def test_episode_zero_is_invalid():
    with pytest.raises(InboundError): rating(episode=0)


@pytest.mark.parametrize('value', [None, True, False, 0, 11, 8.0, '8'])
def test_strict_score(value):
    with pytest.raises(InboundError): rating(score=value)


@pytest.mark.parametrize('value', ['', 'tt123', 'tt-1234567', 'nm1234567', 'tt1234567x', 123, True])
def test_strict_episode_imdb(value):
    with pytest.raises(InboundError): rating(imdb=value)


@pytest.mark.parametrize('value', [None, '', DATE[:10], '2026-09-26T10:00:00', '2026-02-30T10:00:00Z'])
def test_strict_timestamp(value):
    data = item(); data['rated_at'] = value
    with pytest.raises(InboundError): normalize_episode(data)


@pytest.mark.parametrize('path', [('episode','ids','tmdb'), ('episode','ids','trakt'),
                                   ('show','ids','tmdb'), ('show','ids','trakt')])
def test_normalizer_does_not_coerce_ids(path):
    data = item(); data[path[0]][path[1]][path[2]] = '42'
    with pytest.raises(InboundError): normalize_episode(data)


@pytest.mark.parametrize('field', ['episode', 'show'])
@pytest.mark.parametrize('value', [None, [], 'invalid', {}])
def test_malformed_objects(field, value):
    data = item(); data[field] = value
    with pytest.raises(InboundError): normalize_episode(data)


@pytest.mark.parametrize('field', ['episode', 'show'])
@pytest.mark.parametrize('value', [None, [], 'invalid', {}])
def test_malformed_id_objects(field, value):
    data = item(); data[field]['ids'] = value
    with pytest.raises(InboundError): normalize_episode(data)


@pytest.mark.parametrize('other', ['movie', 'season'])
def test_wrong_media_payloads(other):
    data = item(); data[other] = {}
    with pytest.raises(InboundError): normalize_episode(data)


def test_wrong_type_and_parent_imdb_rejected():
    data = item(); data['type'] = 'show'
    with pytest.raises(InboundError): normalize_episode(data)
    data = item(); data['show']['ids']['imdb'] = 'invalid'
    with pytest.raises(InboundError): normalize_episode(data)


def test_missing_parent_mapping_never_uses_episode_tmdb_id():
    r = rating(series=None, tmdb=195339)
    assert r.tmdb_id == 195339 and r.tmdb_series_id is None and r.content_key is None
    s = snapshot(r); assert not s.eligible and s.unmapped == (r,)


def test_missing_episode_metadata_never_uses_parent_ids():
    r = rating(trakt=None, imdb=None, tmdb=None)
    assert r.content_key == KEY and r.trakt_id is r.imdb_id is r.tmdb_id is None


@pytest.mark.parametrize('field', ['content_key', 'trakt_id', 'imdb_id', 'tmdb_id'])
def test_duplicate_or_conflicting_identity(field):
    a = rating(); b = rating(episode=2, trakt=194118, imdb='tt11680643', tmdb=43)
    if field == 'content_key': b = replace(b, episode_number=1)
    else: b = replace(b, **{field: getattr(a, field)})
    with pytest.raises(InboundError, match='duplicate'): EpisodeSnapshot((a,b))


def test_unmapped_duplicate_and_cross_mapping_conflict():
    a = rating(series=None)
    with pytest.raises(InboundError, match='duplicate'): EpisodeSnapshot((), (a,a))
    with pytest.raises(InboundError, match='duplicate'): EpisodeSnapshot((rating(),), (a,))


def test_snapshot_only_accepts_episode_models_and_correct_mapping():
    for values in [(MovieRating(8,DATE,195339),), (ShowRating(8,DATE,195339),), (rating(series=None),)]:
        with pytest.raises(InboundError): EpisodeSnapshot(values)
    with pytest.raises(InboundError): EpisodeSnapshot((), (rating(),))


def test_snapshot_order_and_hash_deterministic():
    a = rating(); b = rating(episode=2,trakt=194118,imdb='tt11680643',tmdb=43)
    assert snapshot(a,b) == snapshot(b,a)
    assert snapshot(a,b).snapshot_hash == snapshot(b,a).snapshot_hash
    assert snapshot(a).snapshot_hash != snapshot(replace(a, trakt_id=77)).snapshot_hash


def test_complete_three_page_authenticated_get_only():
    pages = [([item(episode=n,trakt=n,imdb=None,tmdb=None)], headers(n,3,3)) for n in (1,2,3)]
    s, seen = fetch(pages)
    assert seen == [1,2,3] and s.observed_count == 3 and len(s.eligible) == 3


@pytest.mark.parametrize('pages', [0,1])
def test_empty_snapshot(pages):
    s, seen = fetch([([],headers(pages=pages,count=0))]); assert seen == [1] and s.observed_count == 0


def test_fetch_unmapped_and_null_metadata():
    s, _ = fetch([([item(series=None)], headers())]); assert len(s.unmapped) == 1 and not s.eligible
    s, _ = fetch([([item(trakt=None,imdb=None,tmdb=None)], headers())]); assert len(s.eligible) == 1


@pytest.mark.parametrize('failure', ['missing','wrong_page','changed_pages','changed_count','changed_limit',
                                     'incomplete','overflow','zero_limit','large_limit','non_list','bad_header',
                                     'too_many_pages','negative_count','zero_pages_with_items'])
def test_pagination_fails_closed(failure):
    first, second, body = headers(1,2,2), headers(2,2,2), [item(episode=2,trakt=2,imdb=None,tmdb=None)]
    if failure == 'missing': del second['X-Pagination-Page']
    if failure == 'wrong_page': second['X-Pagination-Page'] = '1'
    if failure == 'changed_pages': second['X-Pagination-Page-Count'] = '3'
    if failure == 'changed_count': second['X-Pagination-Item-Count'] = '3'
    if failure == 'changed_limit': second['X-Pagination-Limit'] = '200'
    if failure == 'incomplete': first['X-Pagination-Item-Count'] = second['X-Pagination-Item-Count'] = '3'
    if failure == 'overflow': first['X-Pagination-Item-Count'] = '0'
    if failure == 'zero_limit': second['X-Pagination-Limit'] = '0'
    if failure == 'large_limit': second['X-Pagination-Limit'] = '251'
    if failure == 'non_list': body = {}
    if failure == 'bad_header': second['X-Pagination-Page'] = '2.0'
    if failure == 'too_many_pages': first['X-Pagination-Page-Count'] = '1001'
    if failure == 'negative_count': first['X-Pagination-Item-Count'] = '-1'
    if failure == 'zero_pages_with_items': first['X-Pagination-Page-Count'] = '0'
    with pytest.raises(InboundError): fetch([([item()],first),(body,second)])


def test_duplicate_across_pages_rejected():
    with pytest.raises(InboundError,match='duplicate'):
        fetch([([item()],headers(1,2,2)),([item()],headers(2,2,2))])


@pytest.mark.parametrize('status', [301,302,401,429,500])
def test_failed_http_and_redirects_no_follow(status):
    seen = []
    def handle(req):
        seen.append(req.url.path)
        return httpx.Response(status, headers={'Location':'https://other.invalid/secret'}, json={'secret':'private'})
    with httpx.Client(transport=httpx.MockTransport(handle),follow_redirects=True) as client:
        with pytest.raises(InboundError): fetch_episode_snapshot(SimpleNamespace(headers={}),client)
    assert seen == ['/users/me/ratings/episodes']


def test_http_exception_and_invalid_json_sanitized():
    marker='private-provider-token-body'
    for invalid_json in (False,True):
        def handle(req):
            if invalid_json: return httpx.Response(200,content=marker,headers=headers())
            raise RuntimeError(marker)
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(InboundError) as e: fetch_episode_snapshot(SimpleNamespace(headers={}),client)
        assert marker not in str(e.value)


def test_credential_supplier_failure_sanitized():
    class Provider:
        @property
        def headers(self): raise RuntimeError('private-provider-token')
    with httpx.Client(transport=httpx.MockTransport(lambda req:pytest.fail('must not fetch'))) as c:
        with pytest.raises(InboundError) as e: fetch_episode_snapshot(Provider(),c)
    assert 'private-provider-token' not in str(e.value)


@pytest.mark.parametrize('value', [0,-1,float('nan'),float('inf')])
def test_bad_deadlines_before_fetch(value):
    with httpx.Client(transport=httpx.MockTransport(lambda req:pytest.fail('must not fetch'))) as c:
        with pytest.raises(InboundError): fetch_episode_snapshot(SimpleNamespace(headers={}),c,timeout=value)


def test_global_deadline_and_remaining_timeout():
    now=[0.0];seen=[];timeouts=[]
    def handle(req):
        page=int(req.url.params['page']);seen.append(page);timeouts.append(req.extensions['timeout']['read']);now[0]+=0.75
        return httpx.Response(200,json=[item(episode=page,trakt=page,imdb=None,tmdb=None)],headers=headers(page,3,3))
    with httpx.Client(transport=httpx.MockTransport(handle)) as c:
        with pytest.raises(InboundError,match='deadline'):
            fetch_episode_snapshot(SimpleNamespace(headers={}),c,timeout=1,clock=lambda:now[0])
    assert seen==[1,2] and timeouts==[1,0.25]


def test_baseline_noop_and_movie_show_canonical_outbox_unchanged(store):
    before=dump(store.path,SHARED);s=snapshot(rating());r=observe_episode(store,lambda:s,baseline=True)
    assert r['generation']==1 and r['events']==0 and r['episodes']==1 and r['snapshot_changed']
    old=dump(store.path,SCHEMAS);r=observe_episode(store,lambda:s)
    new=dump(store.path,SCHEMAS)
    assert r['generation']==1 and not r['snapshot_changed'] and r['events']==0
    assert new['inbound_episode_snapshots']==old['inbound_episode_snapshots']
    assert new['inbound_episode_events']==old['inbound_episode_events']
    assert new['inbound_episode_state'][0]['last_successful_poll_at']>old['inbound_episode_state'][0]['last_successful_poll_at']
    assert dump(store.path,SHARED)==before
    assert r['canonical_mutations']==r['outbox_mutations']==r['provider_writes']==0 and not r['auto_apply_enabled']


def test_add_change_remove_and_no_duplicate_events(store):
    before=dump(store.path,SHARED)
    observe_episode(store,lambda:snapshot(),baseline=True)
    a=observe_episode(store,lambda:snapshot(rating()))
    c=observe_episode(store,lambda:snapshot(rating(score=7)))
    r=observe_episode(store,lambda:snapshot())
    assert (a['added'],c['changed'],r['removed'])==(1,1,1)
    assert (a['generation'],c['generation'],r['generation'])==(2,3,4)
    events=dump(store.path,SCHEMAS)['inbound_episode_events']
    assert [(e['event_type'],e['old_rating'],e['new_rating']) for e in events]==[('added',None,8),('changed',8,7),('removed',7,None)]
    assert len({e['fingerprint'] for e in events})==3
    for e in events:
        assert e['content_key']==KEY and e['status']=='observed' and e['future_action'] is None
        assert e['reason']=='episode_import_disabled' and e['media_type']=='episode'
        assert json.loads(e['rating_json'])['trakt_id']==194117
    assert events[-1]['provider_rated_at']==rating().rated_at
    assert observe_episode(store,lambda:snapshot())['events']==0
    assert dump(store.path,SCHEMAS)['inbound_episode_events']==events
    assert dump(store.path,SHARED)==before


def test_repeated_transition_new_fingerprint_and_coordinate_isolation(store):
    a=rating();b=rating(episode=2,trakt=194118,imdb='tt11680643',tmdb=43)
    observe_episode(store,lambda:snapshot(a,b),baseline=True)
    for score in (7,8,7): observe_episode(store,lambda:snapshot(replace(a,rating=score),b))
    events=dump(store.path,SCHEMAS)['inbound_episode_events']
    assert len(events)==3 and len({e['fingerprint'] for e in events})==3 and {e['content_key'] for e in events}=={KEY}


def test_metadata_only_change_and_unmapped_tracking(store):
    a=rating();unmapped=rating(series=None,trakt=194118,imdb='tt11680643',tmdb=43)
    observe_episode(store,lambda:snapshot(a,unmapped),baseline=True)
    r=observe_episode(store,lambda:snapshot(replace(a,imdb_id=None),unmapped))
    assert r['generation']==2 and r['events']==0 and r['skipped']==1
    rows=dump(store.path,SCHEMAS)
    assert json.loads(rows['inbound_episode_unmapped'][0]['rating_json'])['tmdb_series_id'] is None
    assert rows['inbound_episode_snapshots'][0]['imdb_id'] is None


def test_baseline_guard_reset_and_fetch_failure(store):
    never=lambda:pytest.fail('must refuse before fetch')
    with pytest.raises(InboundError):observe_episode(store,never)
    with pytest.raises(InboundError):observe_episode(store,never,reset=True)
    observe_episode(store,lambda:snapshot(rating()),baseline=True)
    with pytest.raises(InboundError):observe_episode(store,never,baseline=True)
    before=dump(store.path,SCHEMAS)
    def broken():raise InboundError('offline read failed')
    with pytest.raises(InboundError):observe_episode(store,broken)
    assert dump(store.path,SCHEMAS)==before
    r=observe_episode(store,lambda:snapshot(),baseline=True,reset=True)
    assert r['generation']==2 and r['events']==0


def test_media_mismatch_and_generation_cas_no_partial_publication(store):
    observe_episode(store,lambda:snapshot(rating()),baseline=True);before=dump(store.path,SCHEMAS)
    with pytest.raises(InboundError):store.publish(Snapshot(()),expected_generation=1)
    with pytest.raises(InboundError):store.publish(snapshot(),expected_generation=0)
    with pytest.raises(InboundError):store.publish(snapshot(),expected_generation=True)
    assert dump(store.path,SCHEMAS)==before
    second=EpisodeStore(store.path,initialize=False)
    def race():
        observe_episode(second,lambda:snapshot(rating(score=7)))
        return snapshot(rating(score=6))
    with pytest.raises(InboundError,match='concurrently'):observe_episode(store,race)
    assert store.state()['generation']==2
    assert [r['new_rating'] for r in dump(store.path,SCHEMAS)['inbound_episode_events']]==[7]


def test_atomic_publication_rolls_back_on_mid_event_failure(store):
    observe_episode(store,lambda:snapshot(),baseline=True);before=dump(store.path,SCHEMAS)
    second=rating(episode=2,trakt=194118,imdb='tt11680643',tmdb=43)
    class FailingStore(EpisodeStore):
        def connect(self, **kwargs):
            conn=super().connect(**kwargs)
            conn.set_authorizer(lambda action,table,col,db,source:sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_INSERT and table=='inbound_episode_snapshots' else sqlite3.SQLITE_OK)
            return conn
    failed=FailingStore(store.path,initialize=False)
    with pytest.raises(sqlite3.DatabaseError):observe_episode(failed,lambda:snapshot(rating(),second))
    assert dump(store.path,SCHEMAS)==before


@pytest.mark.parametrize('table',['ratings','outbox','inbound_state','inbound_snapshots','inbound_events','inbound_unmapped','sqlite_sequence'])
def test_connection_hard_denies_existing_table_writes(store,table):
    before=dump(store.path,SHARED+('sqlite_sequence',))
    with store.connect() as c:
        with pytest.raises(sqlite3.DatabaseError):c.execute('DELETE FROM '+table)
    assert dump(store.path,SHARED+('sqlite_sequence',))==before


def test_episode_schema_initialization_lossless_and_idempotent(store):
    before=dump(store.path,SHARED+('sqlite_sequence',));episode=dump(store.path,SCHEMAS)
    EpisodeStore(store.path);EpisodeStore(store.path,initialize=False)
    assert dump(store.path,SHARED+('sqlite_sequence',))==before and dump(store.path,SCHEMAS)==episode


def test_unknown_partial_schema_and_trigger_refused(tmp_path,store):
    path=str(tmp_path/'partial.sqlite3')
    with sqlite3.connect(path) as c:c.execute(SCHEMAS['inbound_episode_state'])
    with pytest.raises(InboundError,match='incomplete'):EpisodeStore(path)
    path=str(tmp_path/'unknown.sqlite3')
    with sqlite3.connect(path) as c:
        for name,sql in SCHEMAS.items():c.execute(sql.replace("CHECK(provider='trakt')","CHECK(provider IN ('trakt','other'))"))
    with pytest.raises(InboundError,match='recognized'):EpisodeStore(path)
    observe_episode(store,lambda:snapshot(),baseline=True)
    with sqlite3.connect(store.path) as c:
        c.execute('CREATE TRIGGER unsafe_episode AFTER INSERT ON inbound_episode_events BEGIN DELETE FROM ratings; END')
    before=dump(store.path,SHARED)
    with pytest.raises(InboundError,match='objects'):observe_episode(store,lambda:snapshot(rating()))
    assert dump(store.path,SHARED)==before


def test_existing_only_store_never_creates_database(tmp_path):
    p=tmp_path/'missing.sqlite3'
    with pytest.raises(sqlite3.Error):EpisodeStore(str(p),initialize=False)
    assert not p.exists()
    with pytest.raises(InboundError):EpisodeStore(':memory:')


def test_episode_cannot_enter_import_removal_auto_apply_or_scheduler(store):
    from hub.inbound.importer import apply_event,GLOBAL_TARGETS
    from hub.inbound.removal import apply_removal_event
    from hub.inbound.auto_apply import auto_apply,AutoApplyError
    from hub.inbound.scheduled import scheduled_observe,scheduled_observe_many
    before=dump(store.path,SHARED+tuple(SCHEMAS))
    with pytest.raises(InboundError):
        apply_event(store,GLOBAL_TARGETS,event_id=1,expected_key=KEY,expected_rating=8,expected_generation=1,expected_revision=0,confirmed=True)
    with pytest.raises(InboundError):
        apply_removal_event(store,GLOBAL_TARGETS,event_id=1,expected_key=KEY,expected_generation=1,expected_old_rating=8,expected_revision=1,expected_source='test',confirmed=True)
    with pytest.raises(AutoApplyError) as e:auto_apply(store,GLOBAL_TARGETS,generation=1)
    assert e.value.result['canonical_mutations']==e.value.result['provider_writes']==0
    with pytest.raises(InboundError):scheduled_observe(store.path,lambda:pytest.fail('fetch'),enabled=True,media_type='episode')
    with pytest.raises(InboundError):scheduled_observe_many(store.path,lambda media:pytest.fail('fetch'),enabled=True,media_types=('movie','show','episode'))
    with pytest.raises(InboundError):InboundStore(store.path,media_type='episode')
    assert dump(store.path,SHARED+tuple(SCHEMAS))==before


@pytest.mark.parametrize('value',['episode','movie,episode','movie,show,episode','season'])
def test_episode_recurring_config_stays_refused(monkeypatch,value):
    monkeypatch.setenv('TRAKT_INBOUND_MEDIA_TYPES',value)
    with pytest.raises(InboundError):InboundSettings.from_env()


@pytest.mark.parametrize('mode',['--baseline','--once','--scheduled-observe','--apply-event','--apply-removal-event'])
def test_existing_cli_still_refuses_episode_before_io(mode,tmp_path,monkeypatch):
    monkeypatch.setenv('RATING_HUB_DB',str(tmp_path/'must-not-create.sqlite3'))
    argv=[mode]+(['1'] if 'event' in mode else [])+['--media-type','episode']
    with pytest.raises(SystemExit) as e:existing_main(argv)
    assert e.value.code==2 and not (tmp_path/'must-not-create.sqlite3').exists()


@pytest.mark.parametrize('flag',['--scheduled-observe','--apply-event','--apply-removal-event','--auto-apply','--reclassify-event','--confirm-live-import'])
def test_episode_cli_has_no_write_or_schedule_flags(flag,tmp_path):
    db=tmp_path/'must-not-create.sqlite3'
    argv=['--baseline','--db',str(db),flag]+(['1'] if 'event' in flag else [])
    with pytest.raises(SystemExit) as e:main(argv)
    assert e.value.code==2 and not db.exists()


def test_episode_cli_requires_explicit_db_and_observe_only(tmp_path):
    with pytest.raises(SystemExit) as e:main(['--baseline'])
    assert e.value.code==2
    path=tmp_path/'must-not-create.sqlite3'
    assert main(['--once','--db',str(path)])==2
    assert main(['--baseline','--observe-only','--db',str(path)])==2
    assert not path.exists()


def test_episode_cli_baseline_observe_sanitized_and_canonical_untouched(store,monkeypatch,capsys):
    import hub.inbound.episodes as module
    import hub.providers.registry as registry
    provider=SimpleNamespace(headers={'Authorization':'private-marker'})
    monkeypatch.setattr(registry,'get_provider',lambda name:provider)
    calls=[]
    def read(p,c):
        assert p is provider;calls.append(True);return snapshot(rating())
    monkeypatch.setattr(module,'fetch_episode_snapshot',read)
    before=dump(store.path,SHARED)
    assert main(['--baseline','--db',store.path])==0
    assert main(['--once','--observe-only','--db',store.path])==0
    assert len(calls)==2 and dump(store.path,SHARED)==before
    output=capsys.readouterr().out
    assert 'auto_apply_enabled=false' in output and 'outbox_mutations=0' in output
    assert 'private-marker' not in output and KEY not in output and 'tt11680642' not in output


def test_cli_provider_failure_sanitized(store,monkeypatch,capsys):
    import hub.providers.registry as registry
    def broken(name):raise RuntimeError('private-provider-secret')
    monkeypatch.setattr(registry,'get_provider',broken)
    before=dump(store.path,SHARED)
    assert main(['--baseline','--db',store.path])==1
    assert 'private-provider-secret' not in capsys.readouterr().out and dump(store.path,SHARED)==before


def test_episode_state_independent_of_later_movie_show_observation(store):
    observe_episode(store,lambda:snapshot(rating()),baseline=True)
    episode=dump(store.path,SCHEMAS)
    for media in ('movie','show'):
        inbound=InboundStore(store.path,media_type=media)
        model=MovieRating if media=='movie' else ShowRating
        observe(inbound,lambda:Snapshot((model(7,DATE,195339),),media_type=media))
    assert dump(store.path,SCHEMAS)==episode


def test_same_numeric_ids_across_all_media_remain_independent(store):
    before=dump(store.path,SHARED)
    s=snapshot(rating(series=195339,tmdb=195339,trakt=195339))
    observe_episode(store,lambda:s,baseline=True)
    observe_episode(store,lambda:snapshot(replace(s.eligible[0],rating=9)))
    assert dump(store.path,SHARED)==before
    row=dump(store.path,SCHEMAS)['inbound_episode_snapshots'][0]
    assert row['content_key']==KEY and row['tmdb_id']==row['tmdb_series_id']==row['trakt_id']==195339
    keys={r['content_key'] for r in before['ratings']}
    assert {'movie:tmdb:195339','show:tmdb:195339',KEY} <= keys


def test_distinct_seasons_and_series_preserve_same_episode_number(store):
    a=rating();b=rating(season=0,trakt=194118,imdb='tt11680643',tmdb=43)
    c=rating(series=195340,trakt=194119,imdb='tt11680644',tmdb=44)
    observe_episode(store,lambda:snapshot(a,b,c),baseline=True)
    observe_episode(store,lambda:snapshot(a,c))
    events=dump(store.path,SCHEMAS)['inbound_episode_events']
    assert len(events)==1 and events[0]['content_key']=='episode:tmdb:195339:s0:e1'
    assert events[0]['event_type']=='removed'


def test_late_malformed_page_never_publishes_partial_state(store):
    observe_episode(store,lambda:snapshot(rating()),baseline=True)
    before=dump(store.path,tuple(SCHEMAS)+SHARED)
    bad=item(episode=2,trakt=194118,imdb='tt11680643',tmdb=43);bad['episode']['number']=True
    def read():return fetch([([item(score=7)],headers(1,2,2)),([bad],headers(2,2,2))])[0]
    with pytest.raises(InboundError):observe_episode(store,read)
    assert dump(store.path,tuple(SCHEMAS)+SHARED)==before


def test_episode_events_cannot_be_marked_applied_by_schema(store):
    observe_episode(store,lambda:snapshot(),baseline=True)
    observe_episode(store,lambda:snapshot(rating()))
    before=dump(store.path,SCHEMAS)
    with store.connect() as c:
        with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE inbound_episode_events SET status='applied'")
        with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE inbound_episode_events SET future_action='upsert'")
    assert dump(store.path,SCHEMAS)==before


def test_episode_event_ids_do_not_increment_other_sequences(store):
    observe_episode(store,lambda:snapshot(),baseline=True)
    before=dump(store.path,('sqlite_sequence',))['sqlite_sequence']
    observe_episode(store,lambda:snapshot(rating()))
    after=dump(store.path,('sqlite_sequence',))['sqlite_sequence']
    assert [r for r in after if r['name']!='inbound_episode_events']==before
    assert next(r for r in after if r['name']=='inbound_episode_events')['seq']==1


def test_episode_module_ignores_auto_apply_runtime_setting(store,monkeypatch):
    import hub.inbound.episodes as module
    import hub.providers.registry as registry
    monkeypatch.setenv('TRAKT_INBOUND_AUTO_APPLY','true')
    monkeypatch.setenv('TRAKT_INBOUND_MEDIA_TYPES','movie,show')
    monkeypatch.setattr(registry,'get_provider',lambda name:SimpleNamespace(headers={}))
    monkeypatch.setattr(module,'fetch_episode_snapshot',lambda p,c:snapshot(rating()))
    before=dump(store.path,SHARED)
    assert main(['--baseline','--db',store.path])==0
    assert dump(store.path,SHARED)==before


def test_store_initialization_does_not_alter_existing_shared_ddl(store):
    with sqlite3.connect(store.path) as c:
        before=c.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'inbound_episode_%' ORDER BY name").fetchall()
    EpisodeStore(store.path)
    with sqlite3.connect(store.path) as c:
        after=c.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'inbound_episode_%' ORDER BY name").fetchall()
    assert after==before


@pytest.mark.parametrize('tamper', ['rating','content_key','count','hash','unmapped'])
def test_corrupt_trusted_snapshot_refuses_even_noop(store,tamper):
    s=snapshot(rating())
    observe_episode(store,lambda:s,baseline=True)
    with sqlite3.connect(store.path) as c:
        if tamper=='rating':c.execute('UPDATE inbound_episode_snapshots SET rating=3')
        if tamper=='content_key':c.execute("UPDATE inbound_episode_snapshots SET content_key='movie:tmdb:195339'")
        if tamper=='count':c.execute('UPDATE inbound_episode_state SET observed_count=99')
        if tamper=='hash':c.execute("UPDATE inbound_episode_state SET snapshot_hash='bad'")
        if tamper=='unmapped':c.execute("INSERT INTO inbound_episode_unmapped VALUES (0,'invalid-json','audit')")
    before=dump(store.path,tuple(SCHEMAS)+SHARED)
    with pytest.raises(InboundError,match='trusted'):observe_episode(store,lambda:s)
    assert dump(store.path,tuple(SCHEMAS)+SHARED)==before


@pytest.mark.parametrize('timeout',[None,True,False,'1'])
def test_malformed_deadline_type_refused_before_get(timeout):
    with httpx.Client(transport=httpx.MockTransport(lambda req:pytest.fail('fetch'))) as c:
        with pytest.raises(InboundError):fetch_episode_snapshot(SimpleNamespace(headers={}),c,timeout=timeout)
