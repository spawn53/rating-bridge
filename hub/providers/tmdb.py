from __future__ import annotations

from typing import Any

import httpx

from hub.providers.base import ProviderError, UnsupportedDelivery

BASE_URL = "https://api.themoviedb.org/3"


class TMDbProvider:
    name = "tmdb"

    def __init__(
        self,
        api_read_token: str,
        session_id: str,
        timeout: float = 30.0,
    ):
        self.api_read_token = api_read_token
        self.session_id = session_id
        self.timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_read_token}",
            "User-Agent": "nuvio-rating-hub/2",
        }

    @staticmethod
    def _path(payload: dict[str, Any]) -> str:
        media_type = str(payload.get("media_type"))
        if media_type == "movie":
            movie_id = payload.get("tmdb_id")
            if not movie_id:
                raise ProviderError("TMDb movie rating requires tmdb_id")
            return f"/movie/{movie_id}/rating"
        if media_type == "show":
            series_id = payload.get("tmdb_id")
            if not series_id:
                raise ProviderError("TMDb show rating requires tmdb_id")
            return f"/tv/{series_id}/rating"
        if media_type == "episode":
            series_id = payload.get("tmdb_series_id")
            season = payload.get("season_number")
            episode = payload.get("episode_number")
            if series_id is None or season is None or episode is None:
                raise ProviderError(
                    "TMDb episode rating requires series, season and episode coordinates"
                )
            return f"/tv/{series_id}/season/{season}/episode/{episode}/rating"
        raise UnsupportedDelivery(f"TMDb does not support media type {media_type}")

    def deliver(self, action: str, payload: dict[str, Any]) -> None:
        path = self._path(payload)
        params = {"session_id": self.session_id}
        with httpx.Client(timeout=self.timeout) as client:
            if action == "upsert":
                response = client.post(
                    f"{BASE_URL}{path}",
                    params=params,
                    headers=self.headers,
                    json={"value": float(payload["rating"])},
                )
            elif action == "remove":
                response = client.delete(
                    f"{BASE_URL}{path}",
                    params=params,
                    headers=self.headers,
                )
            else:
                raise UnsupportedDelivery(f"Unknown rating action {action}")

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()[:500]
            raise ProviderError(
                f"TMDb {action} failed ({response.status_code}): {detail}"
            ) from exc

        try:
            data = response.json()
        except ValueError:
            data = {}
        if isinstance(data, dict) and data.get("success") is False:
            raise ProviderError(
                f"TMDb {action} was not accepted: {data.get('status_message') or data}"
            )
