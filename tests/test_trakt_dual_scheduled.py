"""Sequential dual-media scheduling, strict selection and one process-wide flock."""
import json
import select
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from hub.inbound.models import InboundError, MovieRating, ShowRating, Snapshot
from hub.inbound.scheduled import ScheduledManyError, scheduled_lock, scheduled_observe, scheduled_observe_many
from hub.inbound.storage import InboundStore
from hub.inbound.trakt import InboundSettings, main, observe
from hub.store import RatingStore

DATE = '2026-09-26T10:00:00Z'
TARGETS = ('tmdb', 'trakt', 'simkl', 'mdblist')
TABLES = ('ratings', 'outbox', 'inbound_state', 'inbound_snapshots', 'inbound_unmapped', 'inbound_events', 'sqlite_sequence')


def snapshot(media, scores=(), unmapped=False):
    model = MovieRating if media == 'movie' else ShowRating
    return Snapshot(tuple(model(score, DATE, key, key) for key, score in scores),
                    (model(6, DATE, None, 999),) if unmapped else (), media_type=media)


def dump(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return {t: [dict(r) for r in conn.execute('SELECT * FROM '+t+' ORDER BY 1')] for t in TABLES}


@pytest.fixture
def stores(tmp_path):
    path = str(tmp_path/'hub.sqlite3')
    RatingStore(path)
    result = {m: InboundStore(path, media_type=m) for m in ('movie', 'show')}
    for media, store in result.items():
        observe(store, lambda: snapshot(media), baseline=True)
        with store.connect() as conn:
            conn.execute('UPDATE inbound_state SET generation=? WHERE media_type=?',
                         (19 if media == 'movie' else 7, media))
    return result


def many(stores, read, **kwargs):
    return scheduled_observe_many(stores['movie'].path, read, enabled=True,
                                  media_types=('movie', 'show'), **kwargs)


@pytest.mark.parametrize('raw,expected', [('movie', ('movie',)), (' movie ', ('movie',)),
                                        ('movie,show', ('movie', 'show')), (' movie , show ', ('movie', 'show'))])
def test_configuration_accepts_only_normalized_canonical_order(monkeypatch, raw, expected):
    monkeypatch.setenv('TRAKT_INBOUND_MEDIA_TYPES', raw)
    assert InboundSettings.from_env().media_types == expected


@pytest.mark.parametrize('raw', ['', ' ', 'show', 'show,movie', 'movie,movie', 'show,show',
                                'movie,show,movie', 'movie,', ',movie', 'movie,,show', 'movie, ,show',
                                'movie,show,', 'movie,unknown', 'unknown', 'episode', 'season',
                                'movie,episode', 'movie,season', 'Movie', 'movie,SHOW'])
def test_malformed_configuration_fails_closed(monkeypatch, raw):
    monkeypatch.setenv('TRAKT_INBOUND_MEDIA_TYPES', raw)
    with pytest.raises(InboundError):
        InboundSettings.from_env()


def test_missing_media_configuration_retains_movie_default(monkeypatch):
    monkeypatch.delenv('TRAKT_INBOUND_MEDIA_TYPES', raising=False)
    assert InboundSettings.from_env().media_types == ('movie',)


@pytest.mark.parametrize('selection', [(), ('show',), ('show', 'movie'), ('movie', 'movie'),
                                      ('movie', 'show', 'movie'), ('episode',), ('movie', ''),
                                      ['movie', 'show'], 'movie,show', None])
def test_invalid_internal_selection_refused_before_lock_or_database(tmp_path, selection):
    path = tmp_path/'missing.sqlite3'
    with pytest.raises(InboundError):
        scheduled_observe_many(str(path), lambda m: pytest.fail('no fetch'), enabled=True, media_types=selection)
    assert not path.exists() and not path.with_name('trakt-inbound.lock').exists()


def test_disabled_many_refuses_before_lock_or_fetch(stores):
    path = stores['movie'].path; before = dump(path)
    with pytest.raises(InboundError):
        scheduled_observe_many(path, lambda m: pytest.fail('no fetch'), enabled=False, media_types=('movie', 'show'))
    assert dump(path) == before and not Path(path).with_name('trakt-inbound.lock').exists()


def test_movie_only_python_result_and_failures_remain_compatible(stores):
    path = stores['movie'].path
    expected = scheduled_observe(path, lambda: snapshot('movie'), enabled=True)
    actual = scheduled_observe_many(path, snapshot, enabled=True)
    assert actual == expected and 'media_results' not in actual
    error = RuntimeError('private-secret-marker')
    def read(media): raise error
    with pytest.raises(RuntimeError) as caught:
        scheduled_observe_many(path, read, enabled=True)
    assert caught.value is error


def test_sequential_order_one_fetch_publish_apply_per_media(stores, monkeypatch):
    import hub.inbound.auto_apply as engine
    original_publish = InboundStore.publish; original_apply = engine.auto_apply; stages = []
    def read(media): stages.append((media, 'fetch')); return snapshot(media)
    def publish(self, snap, **kw):
        stages.append((self.media_type, 'publish')); return original_publish(self, snap, **kw)
    def apply(store, *args, **kw):
        stages.append((store.media_type, 'apply')); return original_apply(store, *args, **kw)
    monkeypatch.setattr(InboundStore, 'publish', publish); monkeypatch.setattr(engine, 'auto_apply', apply)
    result = many(stores, read, auto_apply_enabled=True)
    assert stages == [(m, stage) for m in ('movie', 'show') for stage in ('fetch', 'publish', 'apply')]
    assert list(result['media_results']) == ['movie', 'show']
    assert [result['media_results'][m]['generation'] for m in ('movie', 'show')] == [19, 7]
    assert result['canonical_mutations'] == result['provider_writes'] == 0


def test_auto_apply_false_observes_both_without_importers(stores, monkeypatch):
    import hub.inbound.auto_apply as engine
    monkeypatch.setattr(engine, 'auto_apply', lambda *a, **kw: pytest.fail('auto apply disabled'))
    before = dump(stores['movie'].path)
    result = many(stores, lambda m: snapshot(m, [(195339, 8)]), auto_apply_enabled=False)
    after = dump(stores['movie'].path)
    assert after['ratings'] == before['ratings'] and after['outbox'] == before['outbox']
    assert {(e['media_type'], e['classification'], e['status']) for e in after['inbound_events']} == {
        ('movie', 'candidate', 'observed'), ('show', 'candidate', 'observed')}
    assert all(not r['auto_apply_enabled'] and r['auto_applied'] == 0 for r in result['media_results'].values())


def test_per_media_limit_allows_eight_plus_eight_and_excludes_trakt(stores):
    scores = [(195339+i, 8) for i in range(8)]
    result = many(stores, lambda m: snapshot(m, scores), auto_apply_enabled=True, max_events=10)
    data = dump(stores['movie'].path)
    assert result['canonical_mutations'] == 16 and result['provider_writes'] == 0
    assert all(r['auto_candidates'] == r['auto_applied'] == 8 for r in result['media_results'].values())
    assert len(data['ratings']) == 16 and len(data['outbox']) == 48
    assert {j['target'] for j in data['outbox']} == {'tmdb', 'simkl', 'mdblist'}
    assert {e['content_key'] for e in data['inbound_events']} == {
        f'{m}:tmdb:{key}' for m in ('movie', 'show') for key, _ in scores}
    assert all(e['status'] == 'applied' and e['canonical_revision'] == 1 for e in data['inbound_events'])
    assert all(json.loads(j['payload_json']) == next(r for r in data['ratings'] if r['content_key'] == j['content_key'])
               for j in data['outbox'])


@pytest.mark.parametrize('overflow', ['movie', 'show'])
def test_limit_fails_only_overflow_medium_before_its_application(stores, overflow):
    seen = []
    def read(media):
        seen.append(media)
        return snapshot(media, [(195339+i, 8) for i in range(11 if media == overflow else 8)])
    with pytest.raises(ScheduledManyError) as caught:
        many(stores, read, auto_apply_enabled=True, max_events=10)
    data = dump(stores['movie'].path)
    assert caught.value.result['failed_media'] == overflow
    assert seen == (['movie'] if overflow == 'movie' else ['movie', 'show'])
    assert len(data['ratings']) == (0 if overflow == 'movie' else 8)
    assert not any(r['media_type'] == overflow for r in data['ratings'])
    assert all(e['status'] == 'observed' for e in data['inbound_events'] if e['media_type'] == overflow)


@pytest.mark.parametrize('changing', ['movie', 'show'])
def test_independent_generations_snapshots_unmapped_and_events(stores, changing):
    other = 'show' if changing == 'movie' else 'movie'; before = dump(stores['movie'].path)
    result = many(stores, lambda m: snapshot(m, [(195339, 8)], True) if m == changing else snapshot(m))
    after = dump(stores['movie'].path)
    assert result['media_results'][other]['generation'] == (19 if other == 'movie' else 7)
    assert result['media_results'][changing]['generation'] == (20 if changing == 'movie' else 8)
    for table in ('inbound_snapshots', 'inbound_unmapped', 'inbound_events'):
        assert [x for x in after[table] if x['media_type'] == other] == [x for x in before[table] if x['media_type'] == other]
    old = next(s for s in before['inbound_state'] if s['media_type'] == other)
    new = next(s for s in after['inbound_state'] if s['media_type'] == other)
    assert new == {**old, 'last_successful_poll_at': new['last_successful_poll_at']}
    assert after['ratings'] == after['outbox'] == []


def test_same_numeric_tmdb_changed_movie_does_not_select_show(stores):
    many(stores, lambda m: snapshot(m, [(195339, 8)]), auto_apply_enabled=True)
    with stores['movie'].connect() as conn:
        conn.execute("UPDATE outbox SET status='done',created_at=?,updated_at=?", (DATE, DATE))
    before = dump(stores['movie'].path)
    result = many(stores, lambda m: snapshot(m, [(195339, 9 if m == 'movie' else 8)]), auto_apply_enabled=True)
    after = dump(stores['movie'].path)
    assert result['media_results']['show']['auto_candidates'] == 0
    assert next(r for r in after['ratings'] if r['media_type'] == 'show') == next(r for r in before['ratings'] if r['media_type'] == 'show')
    assert [e for e in after['inbound_events'] if e['media_type'] == 'show'] == [e for e in before['inbound_events'] if e['media_type'] == 'show']
    assert next(r for r in after['ratings'] if r['media_type'] == 'movie')['revision'] == 2


@pytest.mark.parametrize('media', ['movie', 'show'])
def test_snapshot_media_mismatch_fails_before_that_publication(stores, media):
    before = dump(stores['movie'].path)
    with pytest.raises(ScheduledManyError) as caught:
        many(stores, lambda m: snapshot('show' if m == 'movie' else 'movie') if m == media else snapshot(m))
    after = dump(stores['movie'].path)
    assert caught.value.result['failed_media'] == media
    for table in ('ratings', 'outbox', 'inbound_snapshots', 'inbound_events', 'inbound_unmapped', 'sqlite_sequence'):
        assert after[table] == before[table]
    assert next(s for s in after['inbound_state'] if s['media_type'] == media) == next(s for s in before['inbound_state'] if s['media_type'] == media)


@pytest.mark.parametrize('failure', ['movie', 'show'])
def test_fetch_failure_stops_later_media_and_preserves_snapshots(stores, failure):
    before = dump(stores['movie'].path); seen = []
    def read(media):
        seen.append(media)
        if media == failure: raise RuntimeError('private-secret-marker')
        return snapshot(media)
    with pytest.raises(ScheduledManyError) as caught: many(stores, read, auto_apply_enabled=True)
    after = dump(stores['movie'].path)
    assert seen == (['movie'] if failure == 'movie' else ['movie', 'show'])
    assert caught.value.result['failed_media'] == failure and 'private-secret-marker' not in str(caught.value)
    for table in TABLES:
        if table != 'inbound_state': assert after[table] == before[table]
    if failure == 'movie': assert after == before
    else:
        assert stores['show'].state() == next(s for s in before['inbound_state'] if s['media_type'] == 'show')
        assert stores['movie'].state()['last_successful_poll_at'] != next(s for s in before['inbound_state'] if s['media_type'] == 'movie')['last_successful_poll_at']


def test_show_publication_failure_retains_committed_movie_work(stores):
    with stores['show'].connect() as c:
        c.execute("CREATE TRIGGER fail_show BEFORE INSERT ON inbound_snapshots WHEN NEW.media_type='show' BEGIN SELECT RAISE(ABORT,'private-secret-marker'); END")
    with pytest.raises(ScheduledManyError):
        many(stores, lambda m: snapshot(m, [(195339, 8)]), auto_apply_enabled=True)
    data = dump(stores['movie'].path)
    assert stores['movie'].state()['generation'] == 20 and stores['show'].state()['generation'] == 7
    assert len(data['ratings']) == 1 and data['ratings'][0]['media_type'] == 'movie'
    assert len(data['outbox']) == 3 and all(e['media_type'] == 'movie' for e in data['inbound_events'])


def test_show_import_failure_retains_published_event_and_earlier_commits(stores, monkeypatch):
    import hub.inbound.auto_apply as engine
    original = engine.apply_event
    def apply(store, *a, **kw):
        if store.media_type == 'show' and kw['expected_key'] == 'show:tmdb:195340': raise RuntimeError('private-secret-marker')
        return original(store, *a, **kw)
    monkeypatch.setattr(engine, 'apply_event', apply)
    with pytest.raises(ScheduledManyError) as caught:
        many(stores, lambda m: snapshot(m, [(195339, 8)] if m == 'movie' else [(195339, 8), (195340, 8), (195341, 8)]), auto_apply_enabled=True)
    data = dump(stores['movie'].path); result = caught.value.result
    assert result['failed_media'] == 'show' and result['canonical_mutations'] == 2 and result['provider_writes'] == 0
    assert result['media_results']['show']['auto_applied'] == result['media_results']['show']['auto_failed'] == 1
    assert stores['show'].state()['generation'] == 8
    assert [e['status'] for e in data['inbound_events'] if e['media_type'] == 'show'] == ['applied', 'observed', 'observed']
    assert {r['content_key'] for r in data['ratings']} == {'movie:tmdb:195339', 'show:tmdb:195339'}
    assert len(data['outbox']) == 6


def child_skip(path):
    code = """import json,sys
from hub.inbound.scheduled import scheduled_observe_many
from hub.inbound.models import Snapshot
def read(m):print('FORBIDDEN_FETCH',flush=True);return Snapshot((),media_type=m)
print(json.dumps(scheduled_observe_many(sys.argv[1],read,enabled=True,media_types=('movie','show'))),flush=True)
"""
    result = subprocess.run([sys.executable, '-c', code, path], capture_output=True, text=True, timeout=10, check=True)
    assert 'FORBIDDEN_FETCH' not in result.stdout
    assert json.loads(result.stdout) == {'skipped_overlap': True, 'canonical_mutations': 0, 'provider_writes': 0}


def test_external_process_lock_skips_entire_dual_invocation(stores):
    code = """import sys
from hub.inbound.scheduled import scheduled_lock
with scheduled_lock(sys.argv[1]) as acquired:
 print(acquired,flush=True)
 sys.stdin.readline()
"""
    path = stores['movie'].path; before = dump(path)
    proc = subprocess.Popen([sys.executable, '-c', code, path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert select.select([proc.stdout], [], [], 10)[0] and proc.stdout.readline().strip() == 'True'
        result = many(stores, lambda m: pytest.fail('neither media may fetch'))
        assert result == {'skipped_overlap': True, 'canonical_mutations': 0, 'provider_writes': 0}
        assert dump(path) == before
    finally:
        proc.communicate('\n', timeout=10)


def test_same_inode_locked_during_movie_boundary_and_show(stores, monkeypatch):
    import hub.inbound.scheduled as module
    path = stores['movie'].path; lock = Path(path).with_name('trakt-inbound.lock'); checked = []; inode = []
    original = module._scheduled_observe_unlocked
    def check(stage):
        inode.append(lock.stat().st_ino); child_skip(path); checked.append(stage)
    def one(*a, **kw):
        result = original(*a, **kw)
        if kw['media_type'] == 'movie': check('boundary')
        return result
    def read(media): check(media); return snapshot(media)
    monkeypatch.setattr(module, '_scheduled_observe_unlocked', one)
    many(stores, read, auto_apply_enabled=True)
    assert checked == ['movie', 'boundary', 'show'] and len(set(inode)) == 1
    assert [p.name for p in Path(path).parent.glob('*.lock')] == ['trakt-inbound.lock']
    with scheduled_lock(path) as acquired: assert acquired and lock.stat().st_ino == inode[0]


@pytest.fixture
def cli(stores, monkeypatch):
    monkeypatch.setenv('RATING_HUB_DB', stores['movie'].path)
    monkeypatch.setenv('RATING_HUB_TARGETS', ','.join(TARGETS))
    monkeypatch.setenv('TRAKT_INBOUND_ENABLED', 'true')
    monkeypatch.setenv('TRAKT_INBOUND_MEDIA_TYPES', 'movie,show')
    monkeypatch.setenv('TRAKT_INBOUND_AUTO_APPLY', 'true')
    return stores


def mock_http(monkeypatch, handler):
    import hub.providers.registry as registry
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(registry, 'get_provider', lambda n: SimpleNamespace(headers={'Authorization': 'Bearer private-secret-marker'}))


def response(media, delta=False):
    data = [{'rating': 8, 'rated_at': DATE, media: {'title': 'private-title-marker', 'ids': {'tmdb': 195339, 'trakt': 195339}}}] if delta else []
    return httpx.Response(200, json=data, headers={'X-Pagination-Page': '1', 'X-Pagination-Page-Count': '1', 'X-Pagination-Limit': '250', 'X-Pagination-Item-Count': str(len(data))})


@pytest.mark.parametrize('auto', ['false', 'true'])
@pytest.mark.parametrize('delta', [False, True])
def test_env_driven_cli_fetches_media_explicitly_and_logs_safe_prefixed_counts(cli, monkeypatch, capsys, auto, delta):
    monkeypatch.setenv('TRAKT_INBOUND_AUTO_APPLY', auto); seen = []
    def handler(req):
        assert req.method == 'GET'; media = 'show' if req.url.path.endswith('/shows') else 'movie'
        seen.append(media); return response(media, delta)
    mock_http(monkeypatch, handler)
    assert main(['--scheduled-observe']) == 0 and seen == ['movie', 'show']
    output = capsys.readouterr().out
    assert 'movie_generation=' in output and 'show_generation=' in output
    assert f'canonical_mutations={2 if delta and auto == "true" else 0}\nprovider_writes=0\n' in output
    assert 'private-secret-marker' not in output and 'private-title-marker' not in output


@pytest.mark.parametrize('failure', ['movie', 'show'])
def test_dual_cli_fetch_failure_exits_failure_and_sanitizes(cli, monkeypatch, capsys, failure):
    seen = []
    def handler(req):
        media = 'show' if req.url.path.endswith('/shows') else 'movie'; seen.append(media)
        if media == failure: raise httpx.ReadTimeout('private-secret-marker')
        return response(media)
    mock_http(monkeypatch, handler)
    assert main(['--scheduled-observe']) == 1
    output = capsys.readouterr().out
    assert f'failed_media={failure}' in output and 'private-secret-marker' not in output
    assert seen == (['movie'] if failure == 'movie' else ['movie', 'show'])


def test_dual_cli_auto_apply_failure_exits_failure_without_disclosing_errors(cli, monkeypatch, capsys):
    import hub.inbound.auto_apply as engine
    original = engine.apply_event
    def apply(store, *a, **kw):
        if store.media_type == 'show': raise RuntimeError('private-secret-marker')
        return original(store, *a, **kw)
    monkeypatch.setattr(engine, 'apply_event', apply)
    mock_http(monkeypatch, lambda req: response('show' if req.url.path.endswith('/shows') else 'movie', True))
    assert main(['--scheduled-observe']) == 1
    output = capsys.readouterr().out
    assert 'failed_media=show' in output and 'show_auto_failed=1' in output and 'canonical_mutations=1' in output
    assert 'private-secret-marker' not in output and 'private-title-marker' not in output


def test_explicit_show_cli_cannot_bypass_dual_env(cli, monkeypatch, capsys):
    monkeypatch.setattr(InboundSettings, 'from_env', lambda: pytest.fail('refuse before settings'))
    before = dump(cli['movie'].path)
    assert main(['--scheduled-observe', '--media-type', 'show']) == 2
    assert 'scheduling refused' in capsys.readouterr().out and dump(cli['movie'].path) == before


def test_dual_cli_overlap_logs_zero_mutations_and_no_fetch(cli, monkeypatch, capsys):
    monkeypatch.setattr(httpx, 'Client', lambda **kw: pytest.fail('no overlapping fetch'))
    before = dump(cli['movie'].path)
    with scheduled_lock(cli['movie'].path) as acquired:
        assert acquired and main(['--scheduled-observe']) == 0
    assert 'canonical_mutations=0\nprovider_writes=0' in capsys.readouterr().out
    assert dump(cli['movie'].path) == before
