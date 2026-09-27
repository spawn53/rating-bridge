from __future__ import annotations

import httpx
import pytest

from hub.providers.base import ProviderError, UnsupportedDelivery
from hub.providers.imdb_v2 import IMDbProvider


def mock_http(monkeypatch: pytest.MonkeyPatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_read_personal_rating_uses_cookie_and_user_rating(monkeypatch):
    def handler(request):
        assert request.method == "POST"
        assert request.headers["cookie"] == "session=secret"
        body = __import__("json").loads(request.content)
        assert body["operationName"] == "ReadPersonalTitleRating"
        assert body["variables"] == {"titleId": "tt0137523"}
        assert "userRating" in body["query"]
        return httpx.Response(
            200,
            json={"data": {"title": {"id": "tt0137523", "userRating": {"value": 9}}}},
        )

    mock_http(monkeypatch, handler)
    provider = IMDbProvider("session=secret", dry_run=False, write_delay_seconds=0)
    assert provider.read_personal_rating("tt0137523") == 9


def test_read_personal_rating_allows_unrated_title(monkeypatch):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"data": {"title": {"id": "tt0137523", "userRating": None}}}
        ),
    )
    provider = IMDbProvider("session=secret", dry_run=False, write_delay_seconds=0)
    assert provider.read_personal_rating("tt0137523") is None


def test_live_upsert_is_paced_and_read_after_write_verified(monkeypatch):
    calls = []
    sleeps = []

    def handler(request):
        body = __import__("json").loads(request.content)
        calls.append(body["operationName"])
        if body["operationName"] == "UpdateTitleRating":
            return httpx.Response(
                200, json={"data": {"rateTitle": {"rating": {"value": 9}}}}
            )
        return httpx.Response(
            200,
            json={"data": {"title": {"id": "tt0137523", "userRating": {"value": 9}}}},
        )

    mock_http(monkeypatch, handler)
    monkeypatch.setattr("hub.providers.imdb_v2.time.sleep", sleeps.append)
    provider = IMDbProvider(
        "session=secret",
        dry_run=False,
        write_delay_seconds=2,
        verify_writes=True,
    )
    provider.deliver("upsert", {"imdb_id": "tt0137523", "rating": 9})
    assert sleeps == [2]
    assert calls == ["UpdateTitleRating", "ReadPersonalTitleRating"]


def test_live_remove_is_read_after_write_verified(monkeypatch):
    calls = []

    def handler(request):
        body = __import__("json").loads(request.content)
        calls.append(body["operationName"])
        if body["operationName"] == "DeleteTitleRating":
            return httpx.Response(200, json={"data": {"deleteTitleRating": {"date": "now"}}})
        return httpx.Response(
            200, json={"data": {"title": {"id": "tt0137523", "userRating": None}}}
        )

    mock_http(monkeypatch, handler)
    provider = IMDbProvider(
        "session=secret", dry_run=False, write_delay_seconds=0, verify_writes=True
    )
    provider.deliver("remove", {"imdb_id": "tt0137523", "rating": None})
    assert calls == ["DeleteTitleRating", "ReadPersonalTitleRating"]


def test_missing_imdb_identity_is_permanent_unsupported():
    provider = IMDbProvider("", dry_run=True)
    with pytest.raises(UnsupportedDelivery):
        provider.deliver("upsert", {"imdb_id": None, "rating": 8})


def test_dry_run_never_needs_cookie_or_network(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("no network"))
    IMDbProvider("", dry_run=True).deliver(
        "upsert", {"imdb_id": "tt0137523", "rating": 8}
    )


def test_live_read_requires_cookie():
    with pytest.raises(ProviderError, match="cookie"):
        IMDbProvider("", dry_run=False).read_personal_rating("tt0137523")


def test_graphql_auth_error_is_sanitized(monkeypatch):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"errors": [{"message": "Authentication private-secret-marker"}]}
        ),
    )
    with pytest.raises(ProviderError, match="authentication failed") as caught:
        IMDbProvider("session=secret", dry_run=False).read_personal_rating("tt0137523")
    assert "private-secret-marker" not in str(caught.value)


def test_write_verification_mismatch_fails(monkeypatch):
    def handler(request):
        body = __import__("json").loads(request.content)
        if body["operationName"] == "UpdateTitleRating":
            return httpx.Response(
                200, json={"data": {"rateTitle": {"rating": {"value": 8}}}}
            )
        raise AssertionError("read must not run after mutation mismatch")

    mock_http(monkeypatch, handler)
    with pytest.raises(ProviderError, match="unexpected rating"):
        IMDbProvider(
            "session=secret",
            dry_run=False,
            write_delay_seconds=0,
            verify_writes=True,
        ).deliver("upsert", {"imdb_id": "tt0137523", "rating": 9})
