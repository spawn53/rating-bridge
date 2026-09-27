from __future__ import annotations

import time
from typing import Any

import httpx

from hub.providers.base import ProviderError, UnsupportedDelivery

API_URL = "https://api.graphql.imdb.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


class IMDbProvider:
    """Experimental IMDb personal-rating adapter.

    IMDb does not expose a supported public API for personal rating writes.
    This adapter uses the authenticated GraphQL surface used by the IMDb web
    experience and remains isolated behind IMDB_V2_ENABLED.
    """

    name = "imdb"

    def __init__(
        self,
        cookie: str,
        *,
        dry_run: bool = True,
        timeout: float = 30.0,
        write_delay_seconds: float = 2.0,
        verify_writes: bool = True,
    ):
        self.cookie = cookie.strip()
        self.dry_run = dry_run
        self.timeout = timeout
        self.write_delay_seconds = max(0.0, float(write_delay_seconds))
        self.verify_writes = verify_writes

    @property
    def headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "accept": "application/json",
            "cookie": self.cookie,
            "user-agent": USER_AGENT,
            "origin": "https://www.imdb.com",
            "referer": "https://www.imdb.com/",
        }

    @staticmethod
    def _validate_imdb_id(value: object) -> str:
        imdb_id = str(value or "").strip()
        if not (imdb_id.startswith("tt") and imdb_id[2:].isdigit()):
            raise UnsupportedDelivery(
                "IMDb delivery requires a canonical title/episode IMDb tt ID"
            )
        return imdb_id

    @classmethod
    def _imdb_id(cls, payload: dict[str, Any]) -> str:
        return cls._validate_imdb_id(payload.get("imdb_id"))

    @staticmethod
    def build_request(action: str, imdb_id: str, rating: int | None) -> dict[str, object]:
        if action == "upsert":
            if rating is None or not 1 <= int(rating) <= 10:
                raise ProviderError("IMDb rating must be an integer from 1 to 10")
            return {
                "query": (
                    "mutation UpdateTitleRating($rating: Int!, $titleId: ID!) { "
                    "rateTitle(input: {rating: $rating, titleId: $titleId}) { "
                    "rating { value } } }"
                ),
                "operationName": "UpdateTitleRating",
                "variables": {"rating": int(rating), "titleId": imdb_id},
            }
        if action == "remove":
            return {
                "query": (
                    "mutation DeleteTitleRating($titleId: ID!) { "
                    "deleteTitleRating(input: {titleId: $titleId}) { date } }"
                ),
                "operationName": "DeleteTitleRating",
                "variables": {"titleId": imdb_id},
            }
        raise UnsupportedDelivery(f"Unknown IMDb rating action {action}")

    @staticmethod
    def build_read_request(imdb_id: str) -> dict[str, object]:
        return {
            "query": (
                "query ReadPersonalTitleRating($titleId: ID!) { "
                "title(id: $titleId) { id userRating { value } } }"
            ),
            "operationName": "ReadPersonalTitleRating",
            "variables": {"titleId": imdb_id},
        }

    @staticmethod
    def _response_data(response: httpx.Response, *, action: str) -> dict[str, Any]:
        if response.status_code == 429:
            raise ProviderError("IMDb rate limit exceeded (HTTP 429)")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"IMDb {action} failed (HTTP {response.status_code})") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("IMDb returned invalid JSON") from exc

        errors = data.get("errors") if isinstance(data, dict) else None
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            message = str(first.get("message") or "IMDb GraphQL error")
            if "auth" in message.lower():
                raise ProviderError("IMDb authentication failed")
            raise ProviderError("IMDb GraphQL request failed")

        result = data.get("data") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise ProviderError("IMDb GraphQL response missing data")
        return result

    def read_personal_rating(self, imdb_id: str) -> int | None:
        """Read this account's rating for one IMDb title without mutating it."""
        imdb_id = self._validate_imdb_id(imdb_id)
        if not self.cookie:
            raise ProviderError("IMDb cookie is empty")

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                API_URL,
                json=self.build_read_request(imdb_id),
                headers=self.headers,
            )
        result = self._response_data(response, action="rating read")
        title = result.get("title")
        if not isinstance(title, dict) or str(title.get("id") or "") != imdb_id:
            raise ProviderError("IMDb did not return the requested title")
        user_rating = title.get("userRating")
        if user_rating is None:
            return None
        if not isinstance(user_rating, dict):
            raise ProviderError("IMDb returned an invalid personal rating")
        value = user_rating.get("value")
        if type(value) is not int or not 1 <= value <= 10:
            raise ProviderError("IMDb returned an invalid personal rating")
        return value

    def deliver(self, action: str, payload: dict[str, Any]) -> None:
        imdb_id = self._imdb_id(payload)
        request = self.build_request(action, imdb_id, payload.get("rating"))
        if self.dry_run:
            return
        if not self.cookie:
            raise ProviderError("IMDb cookie is empty")

        # The writer is intentionally conservative because this is an
        # undocumented web surface. The worker is serial, so a fixed delay
        # before every live mutation provides a simple process-independent cap
        # even though get_provider() constructs a fresh adapter per job.
        if self.write_delay_seconds:
            time.sleep(self.write_delay_seconds)

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(API_URL, json=request, headers=self.headers)

        result = self._response_data(response, action=action)
        expected = "rateTitle" if action == "upsert" else "deleteTitleRating"
        mutation = result.get(expected)
        if not isinstance(mutation, dict):
            raise ProviderError(f"IMDb did not confirm {action} for {imdb_id}")
        if action == "upsert":
            rating_data = mutation.get("rating")
            value = rating_data.get("value") if isinstance(rating_data, dict) else None
            if type(value) is not int or value != int(payload["rating"]):
                raise ProviderError("IMDb mutation returned an unexpected rating")

        if self.verify_writes:
            observed = self.read_personal_rating(imdb_id)
            wanted = int(payload["rating"]) if action == "upsert" else None
            if observed != wanted:
                raise ProviderError("IMDb write verification mismatch")
