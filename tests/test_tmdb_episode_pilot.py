"""Offline TMDb episode account-state read and isolated pilot tests."""
from __future__ import annotations

import httpx
import pytest

from hub.inbound.episode_importer import (
    EPISODE_DELIVERY_TARGETS,
    EPISODE_SOURCE_TARGETS,
    validated_episode_targets,
)
from hub.inbound.models import InboundError, validate_media_type
from hub.providers.base import ProviderError
from hub.providers.tmdb import TMDbProvider
from scripts.tmdb_episode_pilot import PilotBlocked, main, run_episode_pilot


SERIES = 195339
SEASON = 1
EPISODE = 2


def provider(token: str = "app-token", session: str = "account-session") -> TMDbProvider:
    return TMDbProvider(token, session, timeout=7.0)


def read_with(handler, *, value=provider()):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return value.read_episode_rating(SERIES, SEASON, EPISODE, client=client)


def test_episode_account_states_path_and_authenticated_session() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/3/tv/195339/season/1/episode/2/account_states"
        assert dict(request.url.params) == {"session_id": "account-session"}
        assert request.headers["Authorization"] == "Bearer app-token"
        assert request.headers["Accept"] == "application/json"
        return httpx.Response(200, json={"rated": {"value": 7}})

    assert read_with(handler) == 7.0


@pytest.mark.parametrize("rated,expected", [
    ({"value": 1}, 1.0), ({"value": 7.5}, 7.5), ({"value": 10.0}, 10.0),
    (False, None),
])
def test_rated_unrated_and_fractional_responses(rated, expected) -> None:
    assert read_with(lambda request: httpx.Response(200, json={"rated": rated})) == expected


@pytest.mark.parametrize("data", [
    None, [], {}, {"rated": None}, {"rated": True}, {"rated": {}},
    {"rated": {"score": 7}}, {"rated": {"value": None}},
    {"rated": {"value": True}}, {"rated": {"value": "7"}},
    {"rated": {"value": 0}}, {"rated": {"value": 10.5}},
    {"rated": {"value": 7.25}}, {"rated": {"value": float("inf")}},
])
def test_malformed_account_state_or_rating_is_refused(data) -> None:
    with pytest.raises(ProviderError):
        read_with(lambda request: httpx.Response(200, json=data))


def test_malformed_json_is_refused_without_body_exposure() -> None:
    marker = "private-session-body"
    with pytest.raises(ProviderError) as error:
        read_with(lambda request: httpx.Response(200, content=("{" + marker).encode()))
    assert marker not in str(error.value)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_http_failures_are_fixed_and_sanitized(status: int) -> None:
    marker = "secret-token-and-session"
    with pytest.raises(ProviderError) as error:
        read_with(lambda request: httpx.Response(status, text=marker))
    assert str(status) in str(error.value)
    assert marker not in str(error.value)
    assert "account-session" not in str(error.value)


def test_transport_failure_is_sanitized() -> None:
    marker = "https://example.invalid/?session_id=private-session"
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(marker)
    with pytest.raises(ProviderError) as error:
        read_with(handler)
    assert marker not in str(error.value)


def test_client_construction_failure_is_sanitized(monkeypatch) -> None:
    marker = "private-token-from-client-construction"
    monkeypatch.setattr(
        "hub.providers.tmdb.httpx.Client",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(marker)),
    )
    with pytest.raises(ProviderError) as error:
        provider().read_episode_rating(SERIES, SEASON, EPISODE)
    assert marker not in str(error.value)


@pytest.mark.parametrize("field,value", [
    ("series", 0), ("series", -1), ("series", True), ("series", "195339"),
    ("season", -1), ("season", True), ("season", "1"),
    ("episode", 0), ("episode", -1), ("episode", True), ("episode", "2"),
])
def test_invalid_episode_coordinates_fail_before_http(field: str, value: object) -> None:
    values = {"series": SERIES, "season": SEASON, "episode": EPISODE}
    values[field] = value
    calls = 0
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"rated": False})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError):
            provider().read_episode_rating(
                values["series"], values["season"], values["episode"], client=client
            )
    assert calls == 0


@pytest.mark.parametrize("token,session", [
    ("", "session"), ("   ", "session"), ("token", ""), ("token", "   "),
])
def test_missing_authentication_fails_before_http(token: str, session: str) -> None:
    calls = 0
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"rated": False})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="authentication was unavailable"):
            provider(token, session).read_episode_rating(SERIES, SEASON, EPISODE, client=client)
    assert calls == 0


class FakeProvider:
    def __init__(self, states: list[float | None]):
        self.states = list(states)
        self.reads: list[tuple[int, int, int]] = []
        self.writes: list[tuple[str, dict[str, object]]] = []

    def read_episode_rating(self, series_id, season_number, episode_number):
        self.reads.append((series_id, season_number, episode_number))
        if not self.states:
            raise AssertionError("unexpected read")
        return self.states.pop(0)

    def deliver(self, action, payload):
        self.writes.append((action, dict(payload)))


def run(fake: FakeProvider, **changes):
    values = dict(provider=fake, series_id=SERIES, season_number=SEASON,
                  episode_number=EPISODE, operation="set", rating=8,
                  expected=7, confirmed=True)
    values.update(changes)
    return run_episode_pilot(**values)


def test_read_only_reads_once_and_causes_zero_writes() -> None:
    fake = FakeProvider([7.5])
    assert run_episode_pilot(
        fake, series_id=SERIES, season_number=SEASON, episode_number=EPISODE,
        operation="read",
    ) == 7.5
    assert fake.reads == [(SERIES, SEASON, EPISODE)] and fake.writes == []


def test_mutation_requires_exact_confirmation_before_io() -> None:
    fake = FakeProvider([])
    with pytest.raises(PilotBlocked, match="confirmation required"):
        run(fake, confirmed=False)
    with pytest.raises(PilotBlocked, match="confirmation required"):
        run(fake, confirmed=1)
    assert fake.reads == fake.writes == []


def test_mutation_requires_expected_state_before_io() -> None:
    fake = FakeProvider([])
    with pytest.raises(PilotBlocked, match="expected state required"):
        run_episode_pilot(fake, series_id=SERIES, season_number=SEASON,
                          episode_number=EPISODE, operation="set", rating=8,
                          confirmed=True)
    assert fake.reads == fake.writes == []


def test_expected_state_mismatch_causes_zero_writes() -> None:
    fake = FakeProvider([6])
    with pytest.raises(PilotBlocked, match="current state mismatch"):
        run(fake)
    assert len(fake.reads) == 1 and fake.writes == []


def test_update_writes_exact_episode_then_reads_verified_state() -> None:
    fake = FakeProvider([None, 7.5])
    result = run(fake, rating=7.5, expected=None)
    assert result == 7.5
    assert fake.reads == [(SERIES, SEASON, EPISODE)] * 2
    assert fake.writes == [("upsert", {
        "media_type": "episode", "tmdb_series_id": SERIES,
        "season_number": SEASON, "episode_number": EPISODE, "rating": 7.5,
    })]


def test_remove_writes_delete_then_reads_verified_unrated_state() -> None:
    fake = FakeProvider([7, None])
    assert run(fake, operation="remove", rating=None) is None
    assert fake.reads == [(SERIES, SEASON, EPISODE)] * 2
    assert fake.writes == [("remove", {
        "media_type": "episode", "tmdb_series_id": SERIES,
        "season_number": SEASON, "episode_number": EPISODE,
    })]


def test_remove_refuses_unrated_expectation_without_io() -> None:
    fake = FakeProvider([])
    with pytest.raises(PilotBlocked, match="requires rated state"):
        run(fake, operation="remove", rating=None, expected=None)
    assert fake.reads == fake.writes == []


@pytest.mark.parametrize("operation,states,rating,expected", [
    ("set", [7, 9], 8, 7), ("remove", [7, 7], None, 7),
])
def test_read_after_write_verification_mismatch_fails(
    operation, states, rating, expected
) -> None:
    fake = FakeProvider(states)
    with pytest.raises(PilotBlocked, match="verification mismatch"):
        run(fake, operation=operation, rating=rating, expected=expected)
    assert len(fake.writes) == 1 and len(fake.reads) == 2


@pytest.mark.parametrize("rating", [0, 10.5, 7.25, True, float("nan"), float("inf")])
def test_invalid_pilot_rating_fails_before_io(rating: object) -> None:
    fake = FakeProvider([])
    with pytest.raises(PilotBlocked, match="rating invalid"):
        run(fake, rating=rating)
    assert fake.reads == fake.writes == []


def test_cli_read_only_and_mutation_output_are_sanitized(monkeypatch, capsys) -> None:
    read = FakeProvider([7.5])
    monkeypatch.setattr("hub.providers.registry.get_provider", lambda name: read)
    coordinates = ["--series-id", str(SERIES), "--season", "1", "--episode", "2"]
    assert main(["--read-only", *coordinates]) == 0
    assert capsys.readouterr().out.strip() == "TMDb episode rating current: 7.5"

    update = FakeProvider([7.5, 8])
    monkeypatch.setattr("hub.providers.registry.get_provider", lambda name: update)
    assert main(["--set-rating", "8", "--expect-current-rating", "7.5",
                 "--confirm-live-write", *coordinates]) == 0
    assert capsys.readouterr().out.strip() == "TMDb episode rating verified: 8"


@pytest.mark.parametrize("arguments,reason", [
    (["--set-rating", "8", "--expect-current-rating", "7"], "confirm-live-write"),
    (["--set-rating", "8", "--confirm-live-write"], "expected current state"),
    (["--remove", "--expect-current-unrated", "--confirm-live-write"], "expected current rating"),
])
def test_cli_guards_fail_before_provider_lookup(monkeypatch, capsys, arguments, reason) -> None:
    monkeypatch.setattr("hub.providers.registry.get_provider",
                        lambda name: pytest.fail("provider lookup forbidden"))
    coordinates = ["--series-id", str(SERIES), "--season", "1", "--episode", "2"]
    assert main([*arguments, *coordinates]) == 1
    assert reason in capsys.readouterr().out


def test_cli_never_exposes_provider_exception(monkeypatch, capsys) -> None:
    marker = "private-token-session-url"
    monkeypatch.setattr("hub.providers.registry.get_provider",
                        lambda name: (_ for _ in ()).throw(RuntimeError(marker)))
    assert main(["--read-only", "--series-id", str(SERIES),
                 "--season", "1", "--episode", "2"]) == 1
    assert marker not in capsys.readouterr().out


def test_movie_show_paths_are_unchanged() -> None:
    assert TMDbProvider._path({"media_type": "movie", "tmdb_id": 550}) == "/movie/550/rating"
    assert TMDbProvider._path({"media_type": "show", "tmdb_id": 1399}) == "/tv/1399/rating"


def test_episode_inbound_contract_remains_tmdb_only() -> None:
    assert EPISODE_SOURCE_TARGETS == ("trakt", "tmdb")
    assert validated_episode_targets(EPISODE_SOURCE_TARGETS) == EPISODE_DELIVERY_TARGETS == ("tmdb",)


def test_episode_auto_apply_remains_impossible() -> None:
    with pytest.raises(InboundError, match="movie or show"):
        validate_media_type("episode")
