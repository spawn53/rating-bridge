from __future__ import annotations

from typing import Any, Callable

import httpx

from hub.providers.base import ProviderError, UnsupportedDelivery

BASE_URL = "https://api.mdblist.com"


class MDBListProvider:
    name = "mdblist"

    def __init__(self, access_token: str, timeout: float = 30.0,
                 token_supplier: Callable[[], str] | None = None):
        self.access_token = access_token
        self.timeout = timeout
        self.token_supplier = token_supplier

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token_supplier() if self.token_supplier else self.access_token}",
            "User-Agent": "nuvio-rating-hub/2",
        }

    @staticmethod
    def _ids(payload: dict[str, Any]) -> dict[str, Any]:
        ids: dict[str, Any] = {}
        for source, target in (
            ("imdb_id", "imdb"),
            ("tmdb_id", "tmdb"),
            ("trakt_id", "trakt"),
            ("mdblist_id", "mdblist"),
        ):
            if payload.get(source):
                ids[target] = payload[source]
        return ids

    def deliver(self, action: str, payload: dict[str, Any]) -> None:
        media_type = str(payload.get("media_type"))
        bucket = {"movie": "movies", "show": "shows"}.get(media_type)
        if bucket is None:
            raise UnsupportedDelivery(
                "MDBList episode rating is disabled until its write contract is verified"
            )
        ids = self._ids(payload)
        if not ids:
            raise ProviderError("MDBList delivery needs at least one provider ID")

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
                f"MDBList {action} failed ({response.status_code}): {detail}"
            ) from exc
