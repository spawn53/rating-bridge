from __future__ import annotations

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
    """Experimental IMDb personal-rating writer.

    IMDb does not expose a supported public API for personal rating writes.
    This adapter uses the same authenticated GraphQL mutations as the IMDb
    web experience, isolated behind IMDB_V2_ENABLED.
    """

    name = "imdb"

    def __init__(
        self,
        cookie: str,
        *,
        dry_run: bool = True,
        timeout: float = 30.0,
    ):
        self.cookie = cookie.strip()
        self.dry_run = dry_run
        self.timeout = timeout

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
    def _imdb_id(payload: dict[str, Any]) -> str:
        imdb_id = str(payload.get("imdb_id") or "").strip()
        if not (imdb_id.startswith("tt") and imdb_id[2:].isdigit()):
            raise ProviderError(
                "IMDb delivery requires a canonical title/episode IMDb tt ID"
            )
        return imdb_id

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

    def deliver(self, action: str, payload: dict[str, Any]) -> None:
        imdb_id = self._imdb_id(payload)
        request = self.build_request(action, imdb_id, payload.get("rating"))
        if self.dry_run:
            return
        if not self.cookie:
            raise ProviderError("IMDb cookie is empty")

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(API_URL, json=request, headers=self.headers)

        if response.status_code == 429:
            raise ProviderError("IMDb rate limit exceeded (HTTP 429)")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()[:500]
            raise ProviderError(
                f"IMDb {action} failed ({response.status_code}): {detail}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("IMDb returned invalid JSON") from exc

        errors = data.get("errors") if isinstance(data, dict) else None
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            message = str(first.get("message") or "IMDb GraphQL error")
            raise ProviderError(message)

        result = data.get("data") if isinstance(data, dict) else None
        expected = "rateTitle" if action == "upsert" else "deleteTitleRating"
        if not isinstance(result, dict) or result.get(expected) is None:
            raise ProviderError(f"IMDb did not confirm {action} for {imdb_id}")
