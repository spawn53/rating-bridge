from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from scripts.live_pilot import PilotBlocked, State, main, pilot, read_state


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


def test_cli_refuses_without_explicit_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(['--provider', 'tmdb', '--content', 'movie:tmdb:550']) == 2
    assert 'confirm-live-write' in capsys.readouterr().out


def test_unrated_movie_is_removed_after_pilot() -> None:
    fake = FakeDelivery(State(None, watchlist=False))
    events = pilot('tmdb', {'media_type': 'movie', 'tmdb_id': 550},
                   fake, lambda: fake.state)
    assert fake.calls == [('upsert', 7), ('upsert', 9), ('remove', None)]
    assert fake.state.rating is None
    assert events[-1] == 'original rating verified restored'


def test_existing_half_step_rating_is_restored() -> None:
    fake = FakeDelivery(State(8.5, watchlist=False))
    pilot('tmdb', {'media_type': 'movie', 'tmdb_id': 550}, fake, lambda: fake.state)
    assert fake.calls[-1] == ('upsert', 8.5)
    assert fake.state.rating == 8.5


def test_original_trakt_timestamp_is_passed_to_restoration() -> None:
    fake = FakeDelivery(State(6, rated_at='2020-01-01T00:00:00Z'))
    payloads: list[dict[str, object]] = []
    def deliver(action: str, payload: dict[str, object]) -> None:
        payloads.append(payload)
        fake.deliver(action, payload)
    wrapper = SimpleNamespace(deliver=deliver)
    pilot('trakt', {'media_type': 'movie', 'tmdb_id': 550}, wrapper, lambda: fake.state)
    assert payloads[-1]['rated_at'] == '2020-01-01T00:00:00Z'


def test_verification_mismatch_stops_then_rolls_back() -> None:
    fake = FakeDelivery(State(None, watchlist=False))
    fake.ignore_first = True
    with pytest.raises(PilotBlocked, match='pilot stopped'):
        pilot('tmdb', {'media_type': 'movie', 'tmdb_id': 550},
              fake, lambda: fake.state)
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
        return httpx.Response(200, headers={'X-Pagination-Page-Count': '1'}, json=[
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
    assert 'read contract is unverified' in capsys.readouterr().out


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
