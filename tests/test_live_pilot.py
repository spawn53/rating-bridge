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
    assert operator_failure(PilotBlocked('Simkl library state could not be verified')) == (
        'Pilot blocked: Simkl library state could not be verified'
    )
    assert operator_failure(PilotBlocked('Simkl settle verifier is required')) == (
        'Pilot blocked: Simkl settle verification is unavailable'
    )
    assert operator_failure(PilotBlocked(
        'Simkl movie must already exist exactly once in the library'
    )) == 'Pilot blocked: Simkl movie is not uniquely present in the library'
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


def simkl_state(rating=None, status='completed'):
    return State(rating, library_status=status)


def simkl_settle(values, *, original=None, expected=7, rollback=False, timeout=5):
    timer = FakeClock()
    original = original or simkl_state()
    required = 5 if rollback else 3
    stable_seconds = 4 if rollback else 2
    reader = Samples(values)
    result = wait_for_state(
        'simkl', original, expected, reader,
        policy=SettlePolicy(timeout, 1, required, stable_seconds, full_window=rollback),
        rollback=rollback, clock=timer.clock, sleep=timer.sleep,
    )
    return result, timer.now, reader.index


def simkl_library_item(*, rating=None, status='completed', tmdb_id=265189):
    return {
        'status': status,
        'user_rating': rating,
        'movie': {'ids': {'tmdb': tmdb_id}},
    }


def test_simkl_settle_verifier_is_required_before_any_write():
    fake = FakeDelivery(simkl_state())
    with pytest.raises(PilotBlocked, match='Simkl settle verifier') as error:
        pilot('simkl', {'media_type': 'movie', 'tmdb_id': 265189},
              fake, lambda: fake.state)
    assert fake.calls == []
    assert operator_failure(error.value) == (
        'Pilot blocked: Simkl settle verification is unavailable'
    )


def test_simkl_pilot_verifies_both_upserts_and_rollback():
    fake = FakeDelivery(simkl_state())
    verified = []
    events = pilot('simkl', {'media_type': 'movie', 'tmdb_id': 265189},
                   fake, lambda: fake.state, verify=fake_settle(fake, verified))
    assert verified == [(7, False), (9, False), (None, True)]
    assert events[-1] == 'original rating verified restored'


def test_simkl_pilot_restores_existing_integer_rating():
    fake = FakeDelivery(simkl_state(6))
    verified = []
    pilot('simkl', {'media_type': 'movie', 'tmdb_id': 265189},
          fake, lambda: fake.state, verify=fake_settle(fake, verified))
    assert verified == [(7, False), (9, False), (6, True)]
    assert fake.calls[-1] == ('upsert', 6)
    assert fake.state == simkl_state(6)


def test_simkl_rollback_timeout_uses_fixed_restoration_failure():
    fake = FakeDelivery(simkl_state())

    def fail_rollback(expected, original, rollback):
        if rollback:
            raise PilotBlocked('provider settle verification timed out')
        return fake.state

    with pytest.raises(PilotBlocked, match='Simkl original rating restoration') as error:
        pilot('simkl', {'media_type': 'movie', 'tmdb_id': 265189},
              fake, lambda: fake.state, verify=fail_rollback)
    assert operator_failure(error.value) == (
        'Pilot blocked: Simkl original rating restoration could not be verified'
    )


def test_simkl_settle_delayed_rating_visibility():
    states = [simkl_state(value) for value in (None, None, 7, 7, 7)]
    result, elapsed, calls = simkl_settle(states)
    assert result == simkl_state(7)
    assert elapsed == 4 and calls == 5


def test_simkl_settle_upsert_rebound_does_not_pass_early():
    states = [simkl_state(value) for value in (None, 7, 7, None, 7)]
    with pytest.raises(PilotBlocked, match='timed out'):
        simkl_settle(states)


def test_simkl_settle_stable_unrated_rollback_uses_full_window():
    states = [simkl_state(value) for value in (9, 9, None, None, None, None, None)]
    result, elapsed, calls = simkl_settle(
        states, expected=None, rollback=True, timeout=7,
    )
    assert result == simkl_state()
    assert elapsed == 7 and calls == 7


def test_simkl_settle_late_rating_rebound_fails_rollback():
    states = [simkl_state(value) for value in (None, None, None, 7)]
    with pytest.raises(PilotBlocked, match='timed out'):
        simkl_settle(states, expected=None, rollback=True, timeout=7)


def test_simkl_library_status_mutation_aborts_immediately():
    timer = FakeClock()
    reader = Samples([simkl_state(7, 'watching'), simkl_state(7)])
    with pytest.raises(PilotBlocked, match='library status changed') as error:
        wait_for_state(
            'simkl', simkl_state(), 7, reader,
            policy=SettlePolicy(5, 1, 3, 2),
            clock=timer.clock, sleep=timer.sleep,
        )
    assert timer.now == 0
    assert reader.index == 1
    assert operator_failure(error.value) == (
        'Pilot blocked: Simkl library status changed during the pilot'
    )


def test_simkl_movie_disappearance_cannot_verify_unrated_rollback():
    timer = FakeClock()
    reads = 0

    def missing(remaining):
        nonlocal reads
        reads += 1
        raise PilotBlocked('Simkl movie must already exist exactly once in the library')

    with pytest.raises(PilotBlocked, match='timed out'):
        wait_for_state(
            'simkl', simkl_state(), None, missing,
            policy=SettlePolicy(3, 1, 2, 1, full_window=True),
            rollback=True, clock=timer.clock, sleep=timer.sleep,
        )
    assert reads == 3


def test_simkl_reader_rejects_duplicate_target():
    def handler(request):
        item = simkl_library_item()
        return httpx.Response(200, json={'movies': [item, item]})

    provider = SimpleNamespace(client_id='test-id', headers={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked, match='exactly once'):
            read_state('simkl', 265189, provider, client)


def test_simkl_reader_rejects_missing_rating_and_invalid_ratings():
    invalid_items = [
        {'status': 'completed', 'movie': {'ids': {'tmdb': 265189}}},
        simkl_library_item(rating=7.5),
        simkl_library_item(rating='7'),
        simkl_library_item(rating=0),
        simkl_library_item(rating=11),
    ]
    provider = SimpleNamespace(client_id='test-id', headers={})
    for item in invalid_items:
        def handler(request):
            return httpx.Response(200, json={'movies': [item]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(PilotBlocked):
                read_state('simkl', 265189, provider, client)


def test_simkl_reader_rejects_malformed_items_and_ids():
    invalid_items = [
        None,
        {'status': 'completed', 'user_rating': None},
        simkl_library_item(tmdb_id=None),
        simkl_library_item(tmdb_id=True),
        simkl_library_item(tmdb_id=0),
        simkl_library_item(tmdb_id='0265189'),
        simkl_library_item(tmdb_id='not-a-number'),
    ]
    provider = SimpleNamespace(client_id='test-id', headers={})
    for item in invalid_items:
        def handler(request):
            return httpx.Response(200, json={'movies': [item]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(PilotBlocked):
                read_state('simkl', 265189, provider, client)


def test_simkl_reader_passes_remaining_budget_to_httpx():
    seen = []

    def handler(request):
        seen.append(request.extensions['timeout'])
        return httpx.Response(200, json={'movies': [simkl_library_item()]})

    provider = SimpleNamespace(client_id='test-id', headers={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = read_state('simkl', 265189, provider, client, timeout=3.5)
    assert state == simkl_state()
    assert all(value == 3.5 for value in seen[0].values())


def test_simkl_reader_rejects_response_finishing_after_budget(monkeypatch):
    readings = iter((10.0, 14.0))
    monkeypatch.setattr('scripts.live_pilot.time.monotonic', lambda: next(readings))

    def handler(request):
        return httpx.Response(200, json={'movies': [simkl_library_item()]})

    provider = SimpleNamespace(client_id='test-id', headers={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked, match='could not be verified'):
            read_state('simkl', 265189, provider, client, timeout=3)


def test_simkl_read_failure_does_not_expose_body_url_or_headers():
    body_marker = 'private-simkl-response-body'
    header_marker = 'private-bearer-token'

    def handler(request):
        return httpx.Response(401, text=body_marker)

    provider = SimpleNamespace(
        client_id='private-client-id',
        headers={'Authorization': 'Bearer ' + header_marker},
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            read_state('simkl', 265189, provider, client, timeout=3)
    message = operator_failure(error.value)
    assert body_marker not in message
    assert header_marker not in message
    assert 'api.simkl.com' not in message
    assert message == 'Pilot blocked or failed; inspect state manually'


def mdblist_item(rating=7, tmdb_id=550):
    return {'ids': {'tmdb': tmdb_id}, 'rating': rating}


def mdblist_page(items, next_cursor=None):
    return {'movies': items, 'pagination': {'next_cursor': next_cursor}}


@pytest.mark.parametrize('rating', [x / 2 for x in range(2, 21)])
def test_mdblist_reader_accepts_every_half_step(rating):
    def handler(request):
        return httpx.Response(200, json=mdblist_page([mdblist_item(rating)]))

    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert read_state('mdblist', 550, provider, client) == State(rating)


def test_mdblist_reader_prefers_precise_rating_without_integer_truncation():
    def handler(request):
        return httpx.Response(200, json=mdblist_page([{
            'movie': {'ids': {'tmdb': 550}},
            'rating': 7,
            'rating_precise': 7.5,
        }]))

    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert read_state('mdblist', 550, provider, client) == State(7.5)


@pytest.mark.parametrize('rating', [7.2, 8.75, True, False])
def test_mdblist_reader_rejects_invalid_precise_rating_even_with_integer_field(rating):
    def handler(request):
        item = mdblist_item(7)
        item['rating_precise'] = rating
        return httpx.Response(200, json=mdblist_page([item]))

    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked, match='could not be verified'):
            read_state('mdblist', 550, provider, client)


@pytest.mark.parametrize('rating', [7.2, 8.75, True, False])
def test_mdblist_reader_rejects_invalid_decimal_and_boolean(rating):
    def handler(request):
        return httpx.Response(200, json=mdblist_page([mdblist_item(rating)]))

    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked, match='could not be verified'):
            read_state('mdblist', 550, provider, client)


@pytest.mark.parametrize('item', [
    None,
    {},
    {'ids': {}, 'rating': 7},
    {'ids': {'tmdb': True}, 'rating': 7},
    {'ids': {'tmdb': 0}, 'rating': 7},
    {'ids': {'tmdb': '0550'}, 'rating': 7},
    {'ids': {'tmdb': 550}},
    {'ids': {'imdb': 'tt0000001'}},
])
def test_mdblist_reader_rejects_malformed_items_and_ids(item):
    def handler(request):
        return httpx.Response(200, json=mdblist_page([item]))

    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked, match='could not be verified'):
            read_state('mdblist', 550, provider, client)


@pytest.mark.parametrize('body', [
    {'pagination': {'next_cursor': None}},
    {'movies': [], 'pagination': None},
    {'movies': [], 'pagination': {'next_cursor': 1}},
    {'movies': [], 'pagination': {'next_cursor': ''}},
])
def test_mdblist_reader_rejects_malformed_collection_and_pagination(body):
    def handler(request):
        return httpx.Response(200, json=body)

    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked, match='could not be verified'):
            read_state('mdblist', 550, provider, client)


def test_mdblist_reader_rejects_duplicate_match_across_pages():
    def handler(request):
        cursor = request.url.params.get('cursor')
        return httpx.Response(
            200,
            json=mdblist_page(
                [mdblist_item(7)],
                'second-page' if cursor is None else None,
            ),
        )

    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked, match='could not be verified'):
            read_state('mdblist', 550, provider, client)


def test_mdblist_reader_rejects_cursor_loop():
    cursors = []

    def handler(request):
        cursors.append(request.url.params.get('cursor'))
        return httpx.Response(200, json=mdblist_page([], 'loop'))

    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked, match='could not be verified'):
            read_state('mdblist', 550, provider, client)
    assert cursors == [None, 'loop']


def test_mdblist_reader_stops_when_pagination_exhausts_budget(monkeypatch):
    from hub import auth_flows

    timer = FakeClock()
    seen_timeouts = []

    def handler(request):
        seen_timeouts.append(request.extensions['timeout']['read'])
        timer.now += 1.1
        return httpx.Response(200, json=mdblist_page([], 'next'))

    monkeypatch.setattr(auth_flows.time, 'monotonic', timer.clock)
    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked, match='could not be verified'):
            read_state('mdblist', 550, provider, client, timeout=2)
    assert len(seen_timeouts) == 2
    assert seen_timeouts[0] == 2
    assert 0 < seen_timeouts[1] <= 0.9


def test_mdblist_read_failure_suppresses_token_url_and_body():
    token = 'private-mdblist-token'
    body = 'private-mdblist-response-body'

    def handler(request):
        assert token in request.headers['Authorization']
        return httpx.Response(401, text=body)

    provider = SimpleNamespace(headers={'Authorization': 'Bearer ' + token})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked) as error:
            read_state('mdblist', 550, provider, client, timeout=3)
    message = operator_failure(error.value)
    assert token not in message
    assert body not in message
    assert 'api.mdblist.com' not in message
    assert message == 'Pilot blocked: MDBList rating state could not be verified'


def mdblist_settle(values, *, expected=7, rollback=False):
    timer = FakeClock()
    reader = Samples([
        value if isinstance(value, Exception) else State(value)
        for value in values
    ])
    result = wait_for_state(
        'mdblist', State(None), expected, reader,
        policy=ROLLBACK_POLICY if rollback else UPSERT_POLICY,
        rollback=rollback, clock=timer.clock, sleep=timer.sleep,
    )
    return result, timer.now, reader.index


def test_mdblist_settle_delayed_rating_visibility():
    result, elapsed, reads = mdblist_settle([None, None, 7, 7, 7])
    assert result == State(7)
    assert elapsed == 8
    assert reads == 5


def test_mdblist_settle_upsert_rebound_resets_streak():
    result, elapsed, reads = mdblist_settle([None, None, 7, None, 7, 7, 7])
    assert result == State(7)
    assert elapsed == 12
    assert reads == 7


def test_mdblist_settle_read_error_resets_streak():
    result, elapsed, reads = mdblist_settle([
        7, 7, RuntimeError('private-read-error'), 7, 7, 7,
    ])
    assert result == State(7)
    assert elapsed == 10
    assert reads == 6


def test_mdblist_settle_stable_unrated_rollback_observes_full_window():
    result, elapsed, reads = mdblist_settle(
        [9, 9] + [None] * 22, expected=None, rollback=True,
    )
    assert result == State(None)
    assert elapsed == 120
    assert reads == 24


def test_mdblist_settle_late_rollback_rebound_fails():
    values = [None] * 22 + [7, None]
    with pytest.raises(PilotBlocked, match='timed out'):
        mdblist_settle(values, expected=None, rollback=True)


def test_mdblist_pilot_restores_original_integer_rating_exactly():
    fake = FakeDelivery(State(6))
    verified = []
    events = pilot(
        'mdblist', {'media_type': 'movie', 'tmdb_id': 550},
        fake, lambda: fake.state, verify=fake_settle(fake, verified),
    )
    assert verified == [(7, False), (9, False), (6, True)]
    assert fake.calls == [('upsert', 7), ('upsert', 9), ('upsert', 6)]
    assert events[-1] == 'original rating verified restored'


def test_mdblist_half_step_original_blocks_before_any_write():
    fake = FakeDelivery(State(7.5))
    with pytest.raises(PilotBlocked, match='half-step') as error:
        pilot(
            'mdblist', {'media_type': 'movie', 'tmdb_id': 550},
            fake, lambda: fake.state, verify=fake_settle(fake),
        )
    assert fake.calls == []
    assert operator_failure(error.value) == (
        'MDBLIST PILOT BLOCKED — ORIGINAL HALF-STEP RATING'
    )


def test_mdblist_has_no_half_step_integer_truncation_path():
    fake = FakeDelivery(State(7.5))
    verifier_calls = []
    with pytest.raises(PilotBlocked, match='half-step'):
        pilot(
            'mdblist', {'media_type': 'movie', 'tmdb_id': 550},
            fake, lambda: fake.state,
            verify=fake_settle(fake, verifier_calls),
        )
    assert fake.calls == []
    assert verifier_calls == []


def test_mdblist_settle_verifier_is_required_before_any_write():
    fake = FakeDelivery(State(None))
    with pytest.raises(PilotBlocked, match='MDBList settle verifier') as error:
        pilot(
            'mdblist', {'media_type': 'movie', 'tmdb_id': 550},
            fake, lambda: fake.state,
        )
    assert fake.calls == []
    assert operator_failure(error.value) == (
        'Pilot blocked: MDBList settle verification is unavailable'
    )


def test_mdblist_rollback_timeout_uses_fixed_restoration_failure():
    fake = FakeDelivery(State(None))

    def fail_rollback(expected, original, rollback):
        if rollback:
            raise PilotBlocked('provider settle verification timed out')
        return fake.state

    with pytest.raises(PilotBlocked, match='MDBList original rating restoration') as error:
        pilot(
            'mdblist', {'media_type': 'movie', 'tmdb_id': 550},
            fake, lambda: fake.state, verify=fail_rollback,
        )
    assert operator_failure(error.value) == (
        'Pilot blocked: MDBList original rating restoration could not be verified'
    )

@pytest.mark.parametrize('pagination', [{}, {'next_cursor': None}, {'has_more': False}])
def test_mdblist_terminal_short_page_accepts_absent_target(pagination):
    def handler(request):
        assert request.url.params['limit'] == '1000'
        return httpx.Response(200, json={'movies': [mdblist_item(8, 1)],
                                        'pagination': pagination})
    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert read_state('mdblist', 550, provider, client) == State(None)


@pytest.mark.parametrize('pagination', [{}, {'has_more': False}])
def test_mdblist_full_page_without_cursor_is_ambiguous(pagination):
    def handler(request):
        return httpx.Response(200, json={'movies': [mdblist_item(8, 1)] * 1000,
                                        'pagination': pagination})
    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked):
            read_state('mdblist', 550, provider, client)


@pytest.mark.parametrize('cursor', ['', 0, False, [], {}])
def test_mdblist_explicit_invalid_cursor_never_uses_short_page_fallback(cursor):
    def handler(request):
        return httpx.Response(200, json=mdblist_page([], cursor))
    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked):
            read_state('mdblist', 550, provider, client)


@pytest.mark.parametrize('flag', [True, None, 0, '', []])
def test_mdblist_missing_cursor_rejects_nonterminal_or_invalid_has_more(flag):
    def handler(request):
        return httpx.Response(200, json={'movies': [], 'pagination': {'has_more': flag}})
    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PilotBlocked):
            read_state('mdblist', 550, provider, client)


@pytest.mark.parametrize('duplicate', [False, True])
def test_mdblist_traverses_to_omitted_terminal_cursor_and_checks_duplicates(duplicate):
    cursors = []
    def handler(request):
        cursor = request.url.params.get('cursor')
        cursors.append(cursor)
        if cursor is None:
            return httpx.Response(200, json=mdblist_page(
                [mdblist_item(7, 550 if duplicate else 1)], 'page-2'))
        return httpx.Response(200, json={
            'movies': [mdblist_item(9)], 'pagination': {}})
    provider = SimpleNamespace(headers={'Authorization': 'Bearer test-token'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        if duplicate:
            with pytest.raises(PilotBlocked):
                read_state('mdblist', 550, provider, client)
        else:
            assert read_state('mdblist', 550, provider, client) == State(9)
    assert cursors == [None, 'page-2']
