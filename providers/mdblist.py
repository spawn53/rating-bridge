from __future__ import annotations

from typing import Any

import httpx

from core.models import RatingItem, clamp_rating

BASE_URL = "https://api.mdblist.com"
PAGE_SIZE = 1000


class MDBListError(RuntimeError):
    pass


class MDBListClient:
    def __init__(self, api_key: str, timeout: float = 60.0):
        self.api_key = api_key
        self.timeout = timeout

    def _get_all(self, path: str) -> dict[str, list[dict[str, Any]]]:
        merged: dict[str, list[dict[str, Any]]] = {
            "movies": [], "shows": [], "seasons": [], "episodes": []
        }
        cursor: str | None = None
        total_seen = 0
        seen_cursors: set[str] = set()
        with httpx.Client(timeout=self.timeout) as client:
            while True:
                params: dict[str, Any] = {"apikey": self.api_key, "limit": PAGE_SIZE}
                if cursor:
                    params["cursor"] = cursor
                elif total_seen:
                    params["offset"] = total_seen
                response = client.get(f"{BASE_URL}{path}", params=params)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    detail = response.text.strip()[:300]
                    raise MDBListError(f"MDBList GET {path} failed ({response.status_code}): {detail}") from exc
                data = response.json()
                if not isinstance(data, dict):
                    raise MDBListError(f"MDBList GET {path} returned invalid JSON")

                page_count = 0
                for kind in merged:
                    rows = data.get(kind)
                    if isinstance(rows, list):
                        merged[kind].extend(row for row in rows if isinstance(row, dict))
                        page_count += len(rows)
                total_seen += page_count

                pagination = data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
                next_cursor = pagination.get("next_cursor")
                if next_cursor:
                    next_cursor = str(next_cursor)
                    if next_cursor in seen_cursors:
                        raise MDBListError("MDBList returned a repeated pagination cursor")
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                    continue
                if pagination.get("has_more"):
                    if page_count == 0:
                        raise MDBListError("MDBList reported more pages but returned no items")
                    cursor = None
                    continue
                return merged

    def get_ratings(self) -> dict[str, RatingItem]:
        raw = self._get_all("/sync/ratings")
        out: dict[str, RatingItem] = {}
        for bucket, media_type, nested_key in (
            ("movies", "movie", "movie"),
            ("shows", "show", "show"),
        ):
            for row in raw.get(bucket, []):
                item = self._parse_rating_row(row, media_type, nested_key)
                if item is not None:
                    out[item.key] = item
        return out

    @staticmethod
    def _parse_rating_row(row: dict[str, Any], media_type: str, nested_key: str) -> RatingItem | None:
        media = row.get(nested_key)
        if not isinstance(media, dict):
            return None
        ids = media.get("ids") if isinstance(media.get("ids"), dict) else media
        tmdb = ids.get("tmdb") if isinstance(ids, dict) else None
        try:
            tmdb_id = int(tmdb)
        except (TypeError, ValueError):
            return None
        if tmdb_id <= 0:
            return None
        rating = clamp_rating(row.get("rating"))
        if rating is None:
            return None
        imdb = ids.get("imdb") if isinstance(ids, dict) else None
        if imdb is not None:
            imdb = str(imdb).strip()
            if not (imdb.startswith("tt") and imdb[2:].isdigit()):
                imdb = None
        title = media.get("title") or media.get("name")
        return RatingItem(
            media_type=media_type,  # type: ignore[arg-type]
            tmdb_id=tmdb_id,
            rating=rating,
            title=str(title).strip() if title else None,
            imdb_id=imdb,
            rated_at=str(row.get("rated_at")) if row.get("rated_at") else None,
        )
