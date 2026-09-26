from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from scripts.live_pilot import PilotBlocked, State, main, operator_failure, pilot, read_state


class FakeDelivery:
    def __init__(self, state: State):
        self.state = state
        self.calls: list[tuple[str, object]] = []
        self.ignore_first = False

    def deliver(self, action: str, payload: dict[str, object]) -> None:
        self.calls.append((action, payload.get('rating')))
        if self.ignore_first and len(self.calls) == 1:
            return
        rating = None if action == 'remove' else float(payload['rating'])
        self.state = State(rating, rated_at=payload.get('rated_at'),
                           watchlist=self.state.watchlist,
                           library_status=self.state.library_status)


def fake_settle(fake: FakeDelivery, calls: list[tuple[float | None, bool]] | None = None):
    def verify(expected: float | None, original: State, rollback: bool) -> State:
        if calls is not None:
            calls.append((expected, rollback))
        if fake.state.rating != expected:
            raise PilotBlocked('provider settle verification timed out')
        return fake.state
    return verify


def trakt_headers(page: int, pages: int, item_count: int, limit: int = 250):
    return {
        'X-Pagination-Page': str(page),
        'X-Pagination-Page-Count': str(pages),
        'X-Pagination-Limit': str(limit),
        'X-Pagination-Item-Count': str(item_count),
    }


def test_cli_refuses_without_explicit_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(['--provider', 'tmdb', '--content', 'movie:tmdb:550']) == 2
    assert 'confirm-live-write' in capsys.readouterr().out


def test_unrated_movie_is_removed_after_pilot() -> None:
    fake = FakeDelivery(State(None, watchlist=False))
    events = pilot('tmdb', {'media_type': 'movie', 'tmdb_id': 550},
                   fake, lambda: fake.state, verify=fake_settle(fake))
    assert fake.calls == [('upsert', 7), ('upsert', 9), ('remove', None)]
    assert fake.state.rating is None
    assert events[-1] == 'original rating verified restored'


def test_existing_half_step_rating_is_restored() -> None:
    fake = FakeDelivery(State(8.5, watchlist=False))
    pilot('tmdb', {'media_type': 'movie', 'tmdb_id': 550}, fake, lambda: fake.state,
          verify=fake_settle(fake))
    assert fake.calls[-1] == ('upsert', 8.5)
    assert fake.state.rating == 8.5


def test_original_trakt_timestamp_is_passed_to_restoration() -> None:
    fake = FakeDelivery(State(6, rated_at='2020-01-01T00:00:00Z'))
    payloads: list[dict[str, object]] = []
    def deliver(action: str, payload: dict[str, object]) -> None:
        payloads.append(payload)
        fake.deliver(action, payload)
    wrapper = SimpleNamespace(deliver=deliver)
    pilot('trakt', {'media_type': 'movie', 'tmdb_id': 550}, wrapper, lambda: fake.state,
          verify=fake_settle(fake))
    assert payloads[-1]['rated_at'] == '2020-01-01T00:00:00Z'


def test_verification_mismatch_stops_then_rolls_back() -> None:
    fake = FakeDelivery(State(None, watchlist=False))
    fake.ignore_first = True
    with pytest.raises(PilotBlocked, match='pilot stopped'):
        pilot('tmdb', {'media_type': 'movie', 'tmdb_id': 550},
              fake, lambda: fake.state, verify=fake_settle(fake))
    assert fake.calls == [('upsert', 7), ('remove', None)]


def test_tmdb_watchlist_guard_prevents_any_write() -> None:
    fake = FakeDelivery(State(None, watchlist=True))
    with pytest.raises(PilotBlocked, match='watchlist'):
        pilot('tmdb', {'media_type': 'movie', 'tmdb_id': 550},
              fake, lambda: fake.state)
    assert fake.calls == []


def test_simkl_requires_existing_library_item() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'movies': []})
    provider = SimpleNamespace(client_id='test-id', headers={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked, match='already exist'):
            read_state('simkl', 550, provider, client)


def test_tmdb_account_state_reads_half_step_rating() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == 'GET'
        assert request.url.path == '/3/movie/550/account_states'
        return httpx.Response(200, json={'rated': {'value': 8.5}, 'watchlist': False})
    provider = SimpleNamespace(session_id='test-session', headers={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert read_state('tmdb', 550, provider, client) == State(8.5, watchlist=False)


def test_tmdb_incomplete_account_state_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'watchlist': False})
    provider = SimpleNamespace(session_id='test-session', headers={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked, match='incomplete'):
            read_state('tmdb', 550, provider, client)


def test_trakt_read_uses_personal_rating_and_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == 'GET'
        assert request.url.path == '/users/me/ratings/movies'
        return httpx.Response(200, headers=trakt_headers(1, 1, 1), json=[
            {'rating': 6, 'rated_at': '2020-01-01T00:00:00Z',
             'movie': {'ids': {'tmdb': 550}}},
        ])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = read_state('trakt', 550, SimpleNamespace(headers={}), client)
    assert state.rating == 6 and state.rated_at == '2020-01-01T00:00:00Z'


def test_simkl_read_requires_library_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == 'GET'
        assert request.url.path == '/sync/all-items/movies'
        return httpx.Response(200, json={'movies': [
            {'status': 'completed', 'user_rating': None,
             'movie': {'ids': {'tmdb': '550'}}},
        ]})
    provider = SimpleNamespace(client_id='test-id', headers={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = read_state('simkl', 550, provider, client)
    assert state == State(None, library_status='completed')


def test_mdblist_pilot_fails_closed(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(['--provider', 'mdblist', '--content', 'movie:tmdb:550',
                 '--confirm-live-write']) == 2
    assert 'writes remain disabled' in capsys.readouterr().out


def test_cli_does_not_echo_provider_exception_or_env_values(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hub.providers import registry
    marker = 'sensitive-marker-123'
    env_file = tmp_path / 'local.env'
    env_file.write_text('TMDB_API_READ_TOKEN=' + marker + '\nTMDB_SESSION_ID=other\n')
    env_file.chmod(0o600)
    def fail(name: str) -> None:
        raise RuntimeError(marker)
    monkeypatch.setattr(registry, 'get_provider', fail)
    assert main(['--provider', 'tmdb', '--content', 'movie:tmdb:550',
                 '--confirm-live-write', '--env-file', str(env_file)]) == 1
    assert marker not in capsys.readouterr().out


def test_operator_failure_exposes_only_allowlisted_pilot_reasons() -> None:
    assert operator_failure(PilotBlocked('TMDb watchlist state is unsafe for a rating pilot')) == (
        'Pilot blocked: TMDb movie is currently in watchlist'
    )
    assert operator_failure(PilotBlocked('TMDb account state was incomplete')) == (
        'Pilot blocked: TMDb account state could not be verified'
    )
    assert operator_failure(PilotBlocked('Trakt rating state could not be verified')) == (
        'Pilot blocked: Trakt rating state could not be verified'
    )
    assert operator_failure(PilotBlocked('Trakt settle verifier is required')) == (
        'Pilot blocked: Trakt settle verification is unavailable'
    )
    marker = 'https://example.invalid/?session_id=sensitive-marker-123'
    for error in (PilotBlocked(marker), RuntimeError(marker)):
        message = operator_failure(error)
        assert marker not in message
        assert message == 'Pilot blocked or failed; inspect state manually'


def test_mdblist_read_uses_personal_ratings_without_writing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == 'GET'
        assert request.url.path == '/sync/ratings'
        assert request.headers['Authorization'] == 'Bearer test-token'
        return httpx.Response(200, json={
            'movies': [{'ids': {'tmdb': 550}, 'rating': 7}],
            'pagination': {'next_cursor': None},
        })
    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert read_state('mdblist', 550, provider, client) == State(7)


# Fake time and mock transports keep convergence regressions entirely offline.
from scripts.settle_verifier import (
    ROLLBACK_POLICY, UPSERT_POLICY, SettlePolicy, tmdb_account_id,
    tmdb_rated_movie_rating, wait_for_state,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Samples:
    def __init__(self, values):
        self.values = list(values)
        self.index = 0

    def __call__(self, remaining):
        assert remaining > 0
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        if isinstance(value, Exception):
            raise value
        return value


def settle(values, secondary=None, *, rollback=False, timeout=5):
    timer = FakeClock()
    reader = Samples([State(value, watchlist=False) for value in values])
    result = wait_for_state(
        'tmdb', State(None, watchlist=False), None if rollback else 7,
        reader, policy=SettlePolicy(timeout, 1, 3, 2, full_window=rollback),
        rollback=rollback, read_secondary=Samples(secondary) if secondary is not None else None,
        clock=timer.clock, sleep=timer.sleep,
    )
    return result, timer.now, reader.index


def test_tmdb_settle_verifier_is_required_before_any_write():
    fake = FakeDelivery(State(None, watchlist=False))
    with pytest.raises(PilotBlocked, match='settle verifier'):
        pilot('tmdb', {'media_type': 'movie', 'tmdb_id': 550},
              fake, lambda: fake.state)
    assert fake.calls == []


def test_tmdb_pilot_verifies_both_upserts_and_rollback_before_reporting_events():
    fake = FakeDelivery(State(None, watchlist=False))
    verified = []
    events = pilot('tmdb', {'media_type': 'movie', 'tmdb_id': 550},
                   fake, lambda: fake.state, verify=fake_settle(fake, verified))
    assert verified == [(7, False), (9, False), (None, True)]
    assert events == ['original state read', 'rating 7 verified',
                      'rating 9 verified', 'original rating verified restored']


def test_tmdb_rollback_timeout_never_reports_restoration():
    fake = FakeDelivery(State(None, watchlist=False))
    def fail_rollback(expected, original, rollback):
        if rollback:
            raise PilotBlocked('provider settle verification timed out')
        return fake.state
    with pytest.raises(PilotBlocked, match='rollback could not be verified') as error:
        pilot('tmdb', {'media_type': 'movie', 'tmdb_id': 550},
              fake, lambda: fake.state, verify=fail_rollback)
    message = operator_failure(error.value)
    assert message == 'Pilot blocked: original rating restoration could not be verified'
    assert 'restored' not in message


def test_unexpected_delivery_failure_remains_generic_after_verified_rollback():
    class FailingDelivery(FakeDelivery):
        def deliver(self, action, payload):
            if action == 'upsert':
                self.calls.append((action, payload.get('rating')))
                raise RuntimeError('private-provider-detail')
            super().deliver(action, payload)
    fake = FailingDelivery(State(None, watchlist=False))
    with pytest.raises(RuntimeError, match='pilot failed') as error:
        pilot('tmdb', {'media_type': 'movie', 'tmdb_id': 550},
              fake, lambda: fake.state, verify=fake_settle(fake))
    assert operator_failure(error.value) == 'Pilot blocked or failed; inspect state manually'
    assert 'private-provider-detail' not in operator_failure(error.value)


def test_tmdb_failed_upsert_reports_restoration_only_after_settle_success():
    fake = FakeDelivery(State(None, watchlist=False))
    fake.ignore_first = True
    with pytest.raises(PilotBlocked, match='pilot stopped') as error:
        pilot('tmdb', {'media_type': 'movie', 'tmdb_id': 550},
              fake, lambda: fake.state, verify=fake_settle(fake))
    assert operator_failure(error.value) == (
        'Pilot stopped after a failed write or verification; original rating restoration verified'
    )


def test_default_settle_policies_match_reviewed_budgets():
    assert UPSERT_POLICY == SettlePolicy(60, 2, 3, 4)
    assert ROLLBACK_POLICY == SettlePolicy(120, 5, 5, 20, full_window=True)


def test_settle_stable_upsert():
    result, elapsed, calls = settle([7, 7, 7])
    assert result.rating == 7 and elapsed == 2 and calls == 3


def test_settle_delayed_write_visibility():
    result, elapsed, calls = settle([None, None, 7, 7, 7])
    assert result.rating == 7 and elapsed == 4 and calls == 5


def test_settle_delayed_delete_rebound_is_not_restored():
    with pytest.raises(PilotBlocked, match='timed out'):
        settle([None, None, 7, 7], [None, None, 7, 7], rollback=True)


def test_settle_eventually_stable_delete():
    result, elapsed, calls = settle([7, 7, None, None, None],
                                   [7, 7, None, None, None], rollback=True)
    assert result.rating is None and elapsed == 5 and calls == 5


def test_settle_conflicting_surfaces_never_restore():
    with pytest.raises(PilotBlocked, match='timed out'):
        settle([None], [7], rollback=True)


def test_settle_stable_restoration_waits_full_budget():
    result, elapsed, calls = settle([None], [None], rollback=True)
    assert result.rating is None and elapsed == 5 and calls == 5


def test_settle_late_rebound_after_matching_streak_is_not_restored():
    with pytest.raises(PilotBlocked, match='timed out'):
        settle([None, None, None, 7, 7], [None, None, None, 7, 7], rollback=True)


def test_settle_rollback_requires_cross_check_and_full_window():
    timer = FakeClock()
    with pytest.raises(PilotBlocked, match='both read surfaces'):
        wait_for_state('tmdb', State(None, watchlist=False), None,
                       Samples([State(None, watchlist=False)]), rollback=True,
                       policy=SettlePolicy(5, 1, 3, 2, True),
                       clock=timer.clock, sleep=timer.sleep)
    with pytest.raises(PilotBlocked, match='full-window'):
        wait_for_state('tmdb', State(None, watchlist=False), None,
                       Samples([State(None, watchlist=False)]), rollback=True,
                       read_secondary=Samples([None]),
                       clock=timer.clock, sleep=timer.sleep)


def test_settle_read_error_resets_streak_without_echoing_exception():
    timer = FakeClock()
    reader = Samples([State(7, watchlist=False), RuntimeError('secret-marker'),
                      State(7, watchlist=False), State(7, watchlist=False)])
    with pytest.raises(PilotBlocked, match='timed out') as error:
        wait_for_state('tmdb', State(None, watchlist=False), 7, reader,
                       policy=SettlePolicy(4, 1, 3, 2),
                       clock=timer.clock, sleep=timer.sleep)
    assert 'secret-marker' not in str(error.value)


def test_settle_watchlist_safety_aborts_immediately():
    timer = FakeClock()
    with pytest.raises(PilotBlocked, match='watchlist'):
        wait_for_state('tmdb', State(None, watchlist=False), 7,
                       Samples([State(7, watchlist=True)]),
                       clock=timer.clock, sleep=timer.sleep)
    assert timer.now == 0


def test_settle_reads_finishing_after_deadline_fail_closed():
    timer = FakeClock()
    def slow_read(remaining):
        timer.sleep(remaining + 1)
        return State(7, watchlist=False)
    with pytest.raises(PilotBlocked, match='timed out'):
        wait_for_state('tmdb', State(None, watchlist=False), 7, slow_read,
                       policy=SettlePolicy(4, 1, 3, 2),
                       clock=timer.clock, sleep=timer.sleep)


@pytest.mark.parametrize('values', [(0, 1, 3, 2), (5, 0, 3, 2),
                                    (5, 1, 1, 0), (2, 1, 4, 0),
                                    (float('inf'), 1, 3, 2)])
def test_settle_invalid_policy_is_rejected(values):
    with pytest.raises(ValueError, match='invalid settle policy'):
        SettlePolicy(*values)


def test_tmdb_account_id_is_resolved_without_exposing_profile_fields():
    def handler(request):
        assert request.method == 'GET'
        assert request.url.path == '/3/account'
        assert request.url.params['session_id'] == 'offline-test-session'
        return httpx.Response(200, json={'id': 42, 'username': 'do-not-log'})
    provider = SimpleNamespace(session_id='offline-test-session', headers={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert tmdb_account_id(provider, client, 10) == 42


@pytest.mark.parametrize('present', [True, False])
def test_tmdb_rated_movies_cross_check_scans_all_pages_same_session(present):
    paths = []
    def handler(request):
        assert request.method == 'GET'
        assert request.url.params['session_id'] == 'offline-test-session'
        paths.append(request.url.path)
        assert request.url.path == '/3/account/42/rated/movies'
        page = int(request.url.params['page'])
        items = [{'id': 550, 'rating': 7}] if page == 2 and present else []
        return httpx.Response(200, json={'page': page, 'total_pages': 2, 'results': items})
    provider = SimpleNamespace(session_id='offline-test-session', headers={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert tmdb_rated_movie_rating(550, 42, provider, client, 10) == (7 if present else None)
    assert len(paths) == 2


@pytest.mark.parametrize('failure', ['missing-page', 'duplicate', 'malformed', 'http'])
def test_tmdb_rated_movies_ambiguous_absence_fails_closed(failure):
    def handler(request):
        assert request.method == 'GET'
        if failure == 'http':
            return httpx.Response(503)
        items = [{'id': 550, 'rating': 7}] * 2 if failure == 'duplicate' else []
        data = {'page': 1, 'total_pages': 1, 'results': items}
        if failure == 'missing-page':
            del data['total_pages']
        if failure == 'malformed':
            data['results'] = [{'id': 550}]
        return httpx.Response(200, json=data)
    provider = SimpleNamespace(session_id='offline-test-session', headers={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises((PilotBlocked, httpx.HTTPStatusError)):
            tmdb_rated_movie_rating(550, 42, provider, client, 10)


def test_tmdb_cross_check_http_failure_does_not_expose_session_or_body():
    marker = 'private-offline-session'
    def handler(request):
        return httpx.Response(401, text='private-response-body')
    provider = SimpleNamespace(session_id=marker, headers={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked) as error:
            tmdb_rated_movie_rating(550, 42, provider, client, 10)
    assert marker not in str(error.value)
    assert 'private-response-body' not in str(error.value)
    assert error.value.__suppress_context__


def trakt_settle(values, *, original=None, expected=7, rollback=False, timeout=5):
    timer = FakeClock()
    original = original or State(None)
    required = 5 if rollback else 3
    stable_seconds = 4 if rollback else 2
    reader = Samples(values)
    result = wait_for_state(
        'trakt', original, expected, reader,
        policy=SettlePolicy(timeout, 1, required, stable_seconds, full_window=rollback),
        rollback=rollback, clock=timer.clock, sleep=timer.sleep,
    )
    return result, timer.now, reader.index


def test_trakt_settle_verifier_is_required_before_any_write():
    fake = FakeDelivery(State(None))
    with pytest.raises(PilotBlocked, match='Trakt settle verifier') as error:
        pilot('trakt', {'media_type': 'movie', 'tmdb_id': 550},
              fake, lambda: fake.state)
    assert fake.calls == []
    assert operator_failure(error.value) == (
        'Pilot blocked: Trakt settle verification is unavailable'
    )


def test_trakt_pilot_verifies_both_upserts_and_rollback():
    fake = FakeDelivery(State(None))
    verified = []
    events = pilot('trakt', {'media_type': 'movie', 'tmdb_id': 550},
                   fake, lambda: fake.state, verify=fake_settle(fake, verified))
    assert verified == [(7, False), (9, False), (None, True)]
    assert events[-1] == 'original rating verified restored'


def test_trakt_pilot_rejects_wrong_restored_timestamp():
    original = State(6, rated_at='2024-01-01T00:00:00Z')
    fake = FakeDelivery(original)
    def verify(expected, initial, rollback):
        if rollback:
            return State(6, rated_at='2024-01-02T00:00:00Z')
        return fake.state
    with pytest.raises(PilotBlocked, match='rated_at restoration') as error:
        pilot('trakt', {'media_type': 'movie', 'tmdb_id': 550},
              fake, lambda: fake.state, verify=verify)
    assert operator_failure(error.value) == (
        'Pilot blocked: Trakt rated_at restoration could not be verified'
    )


def test_trakt_settle_delayed_write_visibility():
    states = [State(None), State(None), State(7), State(7), State(7)]
    result, elapsed, calls = trakt_settle(states)
    assert result.rating == 7 and elapsed == 4 and calls == 5


def test_trakt_settle_rebound_does_not_pass_early():
    states = [State(None), State(7), State(7), State(None), State(7)]
    with pytest.raises(PilotBlocked, match='timed out'):
        trakt_settle(states)


def test_trakt_settle_stable_removal_uses_full_window():
    states = [State(7), State(7), State(None), State(None), State(None),
              State(None), State(None)]
    result, elapsed, calls = trakt_settle(
        states, expected=None, rollback=True, timeout=7,
    )
    assert result.rating is None and elapsed == 7 and calls == 7


def test_trakt_settle_late_reappearance_fails_rollback():
    states = [State(None), State(None), State(None), State(7)]
    with pytest.raises(PilotBlocked, match='timed out'):
        trakt_settle(states, expected=None, rollback=True, timeout=7)


def test_trakt_settle_existing_rating_requires_original_timestamp():
    original = State(6, rated_at='2024-01-01T00:00:00Z')
    wrong = [State(6, rated_at='2024-01-02T00:00:00Z')]
    with pytest.raises(PilotBlocked, match='timed out'):
        trakt_settle(wrong, original=original, expected=6, rollback=True)
    correct = [State(6, rated_at=original.rated_at)]
    result, elapsed, calls = trakt_settle(
        correct, original=original, expected=6, rollback=True,
    )
    assert result == original and elapsed == 5 and calls == 5


def test_trakt_settle_read_error_resets_streak():
    states = [State(7), RuntimeError('private-read-detail'), State(7), State(7)]
    with pytest.raises(PilotBlocked, match='timed out') as error:
        trakt_settle(states, timeout=4)
    assert 'private-read-detail' not in str(error.value)


def test_trakt_settle_timeout_fails():
    with pytest.raises(PilotBlocked, match='timed out'):
        trakt_settle([State(None)], timeout=4)


def test_trakt_reader_finds_target_beyond_first_page():
    pages_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params['page'])
        pages_seen.append(page)
        item = ({'rating': 8, 'rated_at': '2024-02-03T04:05:06Z',
                 'movie': {'ids': {'tmdb': 550}}}
                if page == 2 else
                {'rating': 5, 'rated_at': '2024-01-01T00:00:00Z',
                 'movie': {'ids': {'tmdb': 1}}})
        return httpx.Response(
            200, headers=trakt_headers(page, 2, 2), json=[item],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = read_state('trakt', 550, SimpleNamespace(headers={}), client)
    assert pages_seen == [1, 2]
    assert state == State(8, rated_at='2024-02-03T04:05:06Z')


def test_trakt_reader_rejects_duplicate_target_across_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params['page'])
        item = {'rating': 7, 'rated_at': f'2024-01-0{page}T00:00:00Z',
                'movie': {'ids': {'tmdb': 550}}}
        return httpx.Response(
            200, headers=trakt_headers(page, 2, 2), json=[item],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked, match='duplicate'):
            read_state('trakt', 550, SimpleNamespace(headers={}), client)


def test_trakt_reader_rejects_malformed_pagination():
    failures = ('missing-header', 'wrong-page', 'oversized-limit',
                'wrong-item-count', 'changed-total')
    for failure in failures:
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params['page'])
            pages = 2 if failure == 'changed-total' and page == 1 else 1
            headers = trakt_headers(page, pages, 0)
            if failure == 'missing-header':
                del headers['X-Pagination-Item-Count']
            elif failure == 'wrong-page':
                headers['X-Pagination-Page'] = '2'
            elif failure == 'oversized-limit':
                headers['X-Pagination-Limit'] = '251'
            elif failure == 'wrong-item-count':
                headers['X-Pagination-Item-Count'] = '1'
            elif page == 2:
                headers['X-Pagination-Page-Count'] = '3'
            return httpx.Response(200, headers=headers, json=[])

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(PilotBlocked, match='pagination'):
                read_state('trakt', 550, SimpleNamespace(headers={}), client)


def test_trakt_reader_accepts_authoritative_empty_result():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers=trakt_headers(1, 0, 0), json=[],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert read_state(
            'trakt', 550, SimpleNamespace(headers={}), client,
        ) == State(None)


def test_trakt_reader_rejects_malformed_rating_and_timestamp():
    failures = (
        {'rating': 7.5, 'rated_at': '2024-01-01T00:00:00Z'},
        {'rating': 7, 'rated_at': None},
    )
    for fields in failures:
        def handler(request: httpx.Request) -> httpx.Response:
            item = {**fields, 'movie': {'ids': {'tmdb': 550}}}
            return httpx.Response(
                200, headers=trakt_headers(1, 1, 1), json=[item],
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(PilotBlocked):
                read_state('trakt', 550, SimpleNamespace(headers={}), client)


def test_trakt_read_failure_does_not_expose_body_or_url():
    body_marker = 'private-trakt-response-body'
    url_marker = 'private-query-marker'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=body_marker)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            read_state(
                'trakt', 550,
                SimpleNamespace(headers={'X-Private-Test': url_marker}), client,
            )
    message = operator_failure(error.value)
    assert body_marker not in message
    assert url_marker not in message
    assert 'api.trakt.tv' not in message
    assert message == 'Pilot blocked or failed; inspect state manually'
