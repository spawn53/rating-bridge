from __future__ import annotations

import concurrent.futures
import json
import threading

import httpx
import pytest

from hub.auth import (AuthError, TokenStore, auth_status, oauth_access_token,
                      save_oauth_pair)
from hub.auth_flows import (DeviceCode, mdblist_movie_rating, poll_device,
                            poll_simkl_pin, start_device, tmdb_request_token,
                            tmdb_session)


def store(tmp_path) -> TokenStore:
    root = tmp_path / "auth"
    root.mkdir(mode=0o700)
    return TokenStore(root)


def test_valid_trakt_token_never_refreshes(tmp_path, monkeypatch) -> None:
    local = store(tmp_path)
    local.save("trakt", {"access_token": "current", "refresh_token": "original", "expires_at": 5000})
    monkeypatch.setenv("TRAKT_CLIENT_ID", "app")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "secret")
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected HTTP"))) as client:
        assert oauth_access_token("trakt", store=local, client=client, now=1000) == "current"


def test_trakt_refresh_rotates_both_tokens_atomically(tmp_path, monkeypatch) -> None:
    local = store(tmp_path)
    local.save("trakt", {"access_token": "old", "refresh_token": "spent", "expires_at": 1100})
    monkeypatch.setenv("TRAKT_CLIENT_ID", "app")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "secret")
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/token"
        assert json.loads(request.content)["refresh_token"] == "spent"
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert oauth_access_token("trakt", store=local, client=client, now=1000) == "new"
    assert local.read("trakt") == {"access_token": "new", "refresh_token": "rotated", "expires_at": 4600.0}
    assert (local.root / "trakt.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("name", ["trakt", "mdblist"])
def test_failed_refresh_keeps_last_known_pair(name, tmp_path, monkeypatch) -> None:
    local = store(tmp_path)
    original = {"access_token": "old", "refresh_token": "still-here", "expires_at": 1100}
    local.save(name, original)
    monkeypatch.setenv(f"{name.upper()}_CLIENT_ID", "app")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "secret")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "invalid_grant"}))) as client:
        with pytest.raises(AuthError, match="refresh failed"):
            oauth_access_token(name, store=local, client=client, now=1000)
    assert local.read(name) == original


@pytest.mark.parametrize("name", ["trakt", "mdblist"])
def test_two_callers_use_one_rotated_refresh_token(name, tmp_path, monkeypatch) -> None:
    local = store(tmp_path)
    local.save(name, {"access_token": "old", "refresh_token": "once", "expires_at": 1100})
    monkeypatch.setenv(f"{name.upper()}_CLIENT_ID", "app")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "secret")
    calls = []
    guard = threading.Lock()
    def handler(request: httpx.Request) -> httpx.Response:
        with guard:
            calls.append(request.url.path)
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: oauth_access_token(name, store=local, client=client, now=1000), range(2)))
    assert results == ["new", "new"]
    assert len(calls) == 1
    assert local.read(name)["refresh_token"] == "rotated"


def test_mdblist_device_pending_slow_down_then_success() -> None:
    responses = [
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(429, json={"error": "slow_down"}),
        httpx.Response(200, json={"access_token": "new", "refresh_token": "next", "expires_in": 3600}),
    ]
    waits = []
    now = [0.0]
    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/token/"
        assert b"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code" in request.content
        return responses.pop(0)
    device = DeviceCode("device", "CODE", "https://mdblist.com/oauth/device/", 5, 40)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        data = poll_device("mdblist", "app", device, client, sleep=sleep, monotonic=lambda: now[0])
    assert data["refresh_token"] == "next"
    assert waits == [5, 5, 10]


@pytest.mark.parametrize("error", ["access_denied", "expired_token"])
def test_mdblist_device_terminal_errors(error) -> None:
    device = DeviceCode("device", "CODE", "https://mdblist.com/oauth/device/", 1, 3)
    now = [0.0]
    def sleep(n): now[0] += n
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(400, json={"error": error}))) as client:
        with pytest.raises(AuthError, match="ended"):
            poll_device("mdblist", "app", device, client, sleep=sleep, monotonic=lambda: now[0])


def test_mdblist_device_scope_and_rating_cursor() -> None:
    requests = []
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth/device-authorization/":
            assert b"scope=write" in request.content
            return httpx.Response(200, json={"device_code": "device", "user_code": "CODE", "verification_uri": "https://mdblist.com/oauth/device/", "interval": 5, "expires_in": 300})
        if request.url.params.get("cursor") is None:
            return httpx.Response(200, json={"movies": [], "pagination": {"next_cursor": "next"}})
        return httpx.Response(200, json={"movies": [{"ids": {"tmdb": 550}, "rating": 8}], "pagination": {"next_cursor": None}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        device = start_device("mdblist", "app", client)
        assert device.user_code == "CODE"
        assert mdblist_movie_rating(550, "token", client) == 8
        assert mdblist_movie_rating(551, "token", client) is None


def test_tmdb_bootstrap_uses_api_token_and_does_not_print_session(capsys) -> None:
    paths = []
    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer app-token"
        if request.url.path == "/3/authentication/token/new":
            return httpx.Response(200, json={"success": True, "request_token": "request"})
        if request.url.path == "/3/authentication/session/new":
            return httpx.Response(200, json={"success": True, "session_id": "private-session"})
        return httpx.Response(200, json={"success": True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        request = tmdb_request_token("app-token", client)
        assert tmdb_session("app-token", request, client) == "private-session"
    assert paths == ["/3/authentication", "/3/authentication/token/new", "/3/authentication/session/new"]
    assert "private-session" not in capsys.readouterr().out


def test_tmdb_unapproved_request_token_is_rejected() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(422, json={"status_message": "unapproved"}))) as client:
        with pytest.raises(AuthError, match="session creation failed"):
            tmdb_session("app-token", "request", client)


def test_simkl_v1_pin_has_no_refresh_pair() -> None:
    device = DeviceCode("device", "CODE", "https://simkl.com/pin/", 1, 3)
    now = [0.0]
    def sleep(n): now[0] += n
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"result": "OK", "access_token": "v1-token"}))) as client:
        assert poll_simkl_pin("app", device, client, sleep=sleep, monotonic=lambda: now[0]) == "v1-token"


def test_offline_status_never_calls_network(tmp_path, monkeypatch) -> None:
    local = store(tmp_path)
    monkeypatch.setenv("TRAKT_CLIENT_ID", "app")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "secret")
    local.save("trakt", {"access_token": "current", "refresh_token": "next", "expires_at": 9999999999})
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("network in offline mode"))) as client:
        assert auth_status("trakt", offline=True, store=local, client=client) == "CONFIGURED"


def test_bootstrap_requires_interactive_terminal(capsys, monkeypatch) -> None:
    from scripts.bootstrap_provider_auth import main
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["trakt"]) == 2
    assert "Interactive terminal required" in capsys.readouterr().err


def test_store_rejects_unsafe_permissions(tmp_path) -> None:
    local = store(tmp_path)
    local.save("trakt", {"access_token": "a", "refresh_token": "b", "expires_at": 1})
    (local.root / "trakt.json").chmod(0o644)
    with pytest.raises(AuthError, match="permissions"):
        local.read("trakt")


def test_mdblist_bootstrap_rejects_missing_write_scope(tmp_path) -> None:
    local = store(tmp_path)
    with pytest.raises(AuthError, match='write scope'):
        save_oauth_pair('mdblist', {
            'access_token': 'new', 'refresh_token': 'next',
            'expires_in': 3600, 'scope': 'read',
        }, local, now=1000)
    assert local.read('mdblist') is None


def test_refresh_rejects_unrotated_token(tmp_path, monkeypatch) -> None:
    local = store(tmp_path)
    original = {'access_token': 'old', 'refresh_token': 'same', 'expires_at': 1100}
    local.save('trakt', original)
    monkeypatch.setenv('TRAKT_CLIENT_ID', 'app')
    monkeypatch.setenv('TRAKT_CLIENT_SECRET', 'secret')
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            'access_token': 'new', 'refresh_token': 'same', 'expires_in': 3600,
        })
    )) as client:
        with pytest.raises(AuthError, match='did not rotate'):
            oauth_access_token('trakt', store=local, client=client, now=1000)
    assert local.read('trakt') == original


def test_trakt_accepts_documented_verification_host() -> None:
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            'device_code': 'private', 'user_code': 'CODE',
            'verification_url': 'https://trakt.tv/activate',
            'interval': 5, 'expires_in': 600,
        })
    )) as client:
        assert start_device('trakt', 'app', client).verification_url == 'https://trakt.tv/activate'


def test_tmdb_cli_stores_session_without_printing_it(tmp_path, monkeypatch, capsys) -> None:
    import scripts.bootstrap_provider_auth as cli
    local = store(tmp_path)
    monkeypatch.setenv('TMDB_API_READ_TOKEN', 'app-token')
    monkeypatch.setattr(cli, 'tmdb_request_token', lambda token, client: 'request')
    monkeypatch.setattr(cli, 'tmdb_session', lambda token, request, client: 'private-session')
    monkeypatch.setattr(cli, 'auth_status', lambda name, store: 'READY')
    monkeypatch.setattr('builtins.input', lambda prompt: '')
    cli.bootstrap('tmdb', local)
    assert local.read('tmdb') == {'session_id': 'private-session'}
    output = capsys.readouterr().out
    assert 'https://www.themoviedb.org/authenticate/request' in output
    assert 'private-session' not in output
