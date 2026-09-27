from __future__ import annotations

from contextlib import nullcontext
import math
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
    def _episode_coordinates(
        series_id: object, season_number: object, episode_number: object
    ) -> tuple[int, int, int]:
        if type(series_id) is not int or series_id <= 0:
            raise ProviderError("TMDb episode series ID was invalid")
        if type(season_number) is not int or season_number < 0:
            raise ProviderError("TMDb episode season number was invalid")
        if type(episode_number) is not int or episode_number <= 0:
            raise ProviderError("TMDb episode number was invalid")
        return series_id, season_number, episode_number

    @classmethod
    def _episode_account_states_path(
        cls, series_id: object, season_number: object, episode_number: object
    ) -> str:
        series, season, episode = cls._episode_coordinates(
            series_id, season_number, episode_number
        )
        return f"/tv/{series}/season/{season}/episode/{episode}/account_states"

    def read_episode_rating(
        self,
        series_id: object,
        season_number: object,
        episode_number: object,
        *,
        client: httpx.Client | None = None,
    ) -> float | None:
        """Read one authenticated TMDb episode personal rating without mutation."""
        path = self._episode_account_states_path(series_id, season_number, episode_number)
        if (not isinstance(self.api_read_token, str) or not self.api_read_token.strip()
                or not isinstance(self.session_id, str) or not self.session_id.strip()):
            raise ProviderError("TMDb episode account state authentication was unavailable")
        try:
            context = (httpx.Client(timeout=self.timeout, follow_redirects=False)
                       if client is None else nullcontext(client))
            with context as session:
                response = session.get(
                    f"{BASE_URL}{path}",
                    params={"session_id": self.session_id},
                    headers=self.headers,
                    timeout=self.timeout,
                    follow_redirects=False,
                )
        except Exception:
            raise ProviderError("TMDb episode account state read failed") from None
        if not response.is_success or response.is_redirect:
            raise ProviderError(
                f"TMDb episode account state read failed ({response.status_code})"
            )
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("TMDb episode account state response was invalid") from None
        if not isinstance(data, dict) or "rated" not in data:
            raise ProviderError("TMDb episode account state response was invalid")
        rated = data["rated"]
        if rated is False:
            return None
        if not isinstance(rated, dict) or "value" not in rated:
            raise ProviderError("TMDb episode account state response was invalid")
        value = rated["value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProviderError("TMDb episode account state rating was invalid")
        rating = float(value)
        if (not math.isfinite(rating) or not 0.5 <= rating <= 10
                or not (rating * 2).is_integer()):
            raise ProviderError("TMDb episode account state rating was invalid")
        return rating

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
