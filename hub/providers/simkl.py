from __future__ import annotations

from typing import Any

import httpx

from hub.providers.base import ProviderError, UnsupportedDelivery

BASE_URL = "https://api.simkl.com"


class SimklProvider:
    name = "simkl"

    def __init__(
        self,
        client_id: str,
        access_token: str,
        timeout: float = 30.0,
    ):
        self.client_id = client_id
        self.access_token = access_token
        self.timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "simkl-api-key": self.client_id,
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": "nuvio-rating-hub/2",
        }

    def deliver(self, action: str, payload: dict[str, Any]) -> None:
        media_type = str(payload.get("media_type"))
        bucket = {"movie": "movies", "show": "shows"}.get(media_type)
        if bucket is None:
            raise UnsupportedDelivery(
                "Simkl episode ratings are disabled until API support is verified"
            )

        ids: dict[str, Any] = {}
        if payload.get("tmdb_id"):
            ids["tmdb"] = payload["tmdb_id"]
        if payload.get("imdb_id"):
            ids["imdb"] = payload["imdb_id"]
        if not ids:
            raise ProviderError("Simkl delivery needs a TMDb or IMDb ID")

        item: dict[str, Any] = {"ids": ids}
        if action == "upsert":
            item["rating"] = int(payload["rating"])
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
                f"Simkl {action} failed ({response.status_code}): {detail}"
            ) from exc
