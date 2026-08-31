from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

from core.models import RatingItem, clamp_rating

BASE_URL = "https://api.simkl.com"


class SimklError(RuntimeError):
    pass


class SimklClient:
    def __init__(self, client_id: str, access_token: str, timeout: float = 60.0):
        self.client_id = client_id
        self.access_token = access_token
        self.timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "simkl-api-key": self.client_id,
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": "rating-bridge/0.1.0",
        }

    def validate(self) -> None:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{BASE_URL}/users/settings", headers=self.headers)
            self._raise(response, "validate token")

    def get_ratings(self) -> dict[str, RatingItem]:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{BASE_URL}/sync/ratings/",
                params={"extended": "full"},
                headers=self.headers,
            )
            self._raise(response, "get ratings")
            data = response.json()
        if isinstance(data, list):
            typed = {
                "movies": [r for r in data if isinstance(r, dict) and r.get("movie")],
                "shows": [r for r in data if isinstance(r, dict) and r.get("show")],
            }
        elif isinstance(data, dict):
            typed = data
        else:
            raise SimklError("Simkl ratings response had an unexpected shape")

        out: dict[str, RatingItem] = {}
        for bucket, media_type, nested_key in (
            ("movies", "movie", "movie"),
            ("shows", "show", "show"),
        ):
            rows = typed.get(bucket, [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item = self._parse_rating_row(row, media_type, nested_key)
                if item is not None:
                    out[item.key] = item
        return out

    def get_watched_keys(self) -> set[str]:
        """Return movie/show keys that Simkl itself shows as having watch progress.

        This intentionally uses Simkl as the guard. Rating an unseen title can mark it
        watched on Simkl, so the bridge waits until Nuvio/another tracker has already
        produced watch activity on Simkl before pushing the rating.
        """
        with httpx.Client(timeout=max(self.timeout, 120.0)) as client:
            response = client.get(
                f"{BASE_URL}/sync/all-items/",
                params={"extended": "full", "episode_watched_at": "yes"},
                headers=self.headers,
            )
            self._raise(response, "get all-items")
            data = response.json()
        if not isinstance(data, dict):
            raise SimklError("Simkl all-items response had an unexpected shape")

        watched: set[str] = set()
        for bucket, media_type, nested_key in (
            ("movies", "movie", "movie"),
            ("shows", "show", "show"),
            ("anime", "show", "show"),
        ):
            rows = data.get(bucket, [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                media = row.get(nested_key)
                if not isinstance(media, dict):
                    continue
                ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
                try:
                    tmdb_id = int(ids.get("tmdb"))
                except (TypeError, ValueError):
                    continue
                if tmdb_id <= 0:
                    continue
                status = str(row.get("status") or "").strip().lower().replace("_", " ")
                last_watched = row.get("last_watched_at") or row.get("watched_at")
                has_episode_watch = False
                seasons = row.get("seasons")
                if isinstance(seasons, list):
                    for season in seasons:
                        if not isinstance(season, dict):
                            continue
                        episodes = season.get("episodes")
                        if isinstance(episodes, list) and any(
                            isinstance(ep, dict) and (ep.get("watched_at") or ep.get("last_watched_at"))
                            for ep in episodes
                        ):
                            has_episode_watch = True
                            break
                status_is_watched = status in {
                    "completed", "watching", "on hold", "dropped",
                }
                if last_watched or has_episode_watch or status_is_watched:
                    watched.add(f"{media_type}:tmdb:{tmdb_id}")
        return watched

    def upsert_ratings(self, items: Iterable[RatingItem], batch_size: int = 50) -> list[str]:
        items = list(items)
        successful: list[str] = []
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            payload: dict[str, list[dict[str, Any]]] = {}
            movies = [i for i in batch if i.media_type == "movie"]
            shows = [i for i in batch if i.media_type == "show"]
            if movies:
                payload["movies"] = [
                    {"rating": i.rating, "ids": {"tmdb": i.tmdb_id}} for i in movies
                ]
            if shows:
                payload["shows"] = [
                    {"rating": i.rating, "ids": {"tmdb": i.tmdb_id}} for i in shows
                ]
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(f"{BASE_URL}/sync/ratings", json=payload, headers=self.headers)
                self._raise(response, "upsert ratings")
            successful.extend(i.key for i in batch)
        return successful

    def remove_ratings(self, items: Iterable[RatingItem], batch_size: int = 50) -> list[str]:
        items = list(items)
        successful: list[str] = []
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            payload: dict[str, list[dict[str, Any]]] = {}
            movies = [i for i in batch if i.media_type == "movie"]
            shows = [i for i in batch if i.media_type == "show"]
            if movies:
                payload["movies"] = [{"ids": {"tmdb": i.tmdb_id}} for i in movies]
            if shows:
                payload["shows"] = [{"ids": {"tmdb": i.tmdb_id}} for i in shows]
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{BASE_URL}/sync/ratings/remove", json=payload, headers=self.headers
                )
                self._raise(response, "remove ratings")
            successful.extend(i.key for i in batch)
        return successful

    @staticmethod
    def _parse_rating_row(row: dict[str, Any], media_type: str, nested_key: str) -> RatingItem | None:
        media = row.get(nested_key)
        if not isinstance(media, dict):
            return None
        ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
        try:
            tmdb_id = int(ids.get("tmdb"))
        except (TypeError, ValueError):
            return None
        rating = clamp_rating(row.get("user_rating", row.get("rating")))
        if tmdb_id <= 0 or rating is None:
            return None
        title = media.get("title") or media.get("name")
        imdb = ids.get("imdb")
        return RatingItem(
            media_type=media_type,  # type: ignore[arg-type]
            tmdb_id=tmdb_id,
            rating=rating,
            title=str(title).strip() if title else None,
            imdb_id=str(imdb).strip() if imdb else None,
            rated_at=str(row.get("user_rated_at")) if row.get("user_rated_at") else None,
        )

    @staticmethod
    def _raise(response: httpx.Response, action: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()[:300]
            raise SimklError(f"Simkl {action} failed ({response.status_code}): {detail}") from exc
