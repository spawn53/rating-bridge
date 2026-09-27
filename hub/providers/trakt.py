from __future__ import annotations

from typing import Any, Callable

import httpx

from hub.providers.base import ProviderError, UnsupportedDelivery

BASE_URL = "https://api.trakt.tv"


class TraktProvider:
    name = "trakt"

    def __init__(
        self,
        client_id: str,
        access_token: str,
        timeout: float = 30.0,
        token_supplier: Callable[[], str] | None = None,
    ):
        self.client_id = client_id
        self.access_token = access_token
        self.timeout = timeout
        self.token_supplier = token_supplier

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
            "Authorization": f"Bearer {self.token_supplier() if self.token_supplier else self.access_token}",
            "User-Agent": "nuvio-rating-hub/2",
        }

    @staticmethod
    def _ids(payload: dict[str, Any]) -> dict[str, Any]:
        ids: dict[str, Any] = {}
        if payload.get("trakt_id"):
            ids["trakt"] = payload["trakt_id"]
        if payload.get("tmdb_id"):
            ids["tmdb"] = payload["tmdb_id"]
        if payload.get("imdb_id"):
            ids["imdb"] = payload["imdb_id"]
        return ids

    def deliver(self, action: str, payload: dict[str, Any]) -> None:
        media_type = str(payload.get("media_type"))
        bucket = {
            "movie": "movies",
            "show": "shows",
            "episode": "episodes",
        }.get(media_type)
        if bucket is None:
            raise UnsupportedDelivery(f"Trakt does not support media type {media_type}")

        ids = self._ids(payload)
        if not ids:
            raise ProviderError("Trakt delivery needs a Trakt, TMDb, or IMDb ID")

        item: dict[str, Any] = {"ids": ids}
        if action == "upsert":
            item["rating"] = int(payload["rating"])
            if payload.get("rated_at"):
                item["rated_at"] = payload["rated_at"]
            path = "/sync/ratings"
        elif action == "remove":
            path = "/sync/ratings/remove"
        else:
            raise UnsupportedDelivery(f"Unknown rating action {action}")

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{BASE_URL}{path}",
                headers=self.headers,
                json={bucket: [item]},
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()[:500]
            raise ProviderError(
                f"Trakt {action} failed ({response.status_code}): {detail}"
            ) from exc

        data = response.json()
        not_found = data.get("not_found", {}) if isinstance(data, dict) else {}
        if isinstance(not_found, dict) and not_found.get(bucket):
            raise ProviderError(f"Trakt could not match {payload.get('content_key')}")
