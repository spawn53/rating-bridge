from __future__ import annotations

import time
from collections.abc import Iterable

import httpx

from core.models import RatingItem

API_URL = "https://api.graphql.imdb.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
READ_BATCH_SIZE = 20


class IMDbError(RuntimeError):
    pass


class IMDbClient:
    def __init__(self, cookie: str, timeout: float = 30.0):
        self.cookie = cookie.strip()
        self.timeout = timeout
        if not self.cookie:
            raise IMDbError("IMDb cookie is empty")

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

    def _post(self, payload: dict[str, object]) -> dict[str, object]:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(API_URL, json=payload, headers=self.headers)
        if response.status_code == 429:
            raise IMDbError("IMDb rate limit exceeded (HTTP 429)")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise IMDbError(f"IMDb request failed (HTTP {response.status_code})") from exc
        data = response.json()
        if not isinstance(data, dict):
            raise IMDbError("IMDb returned invalid JSON")
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            message = str(first.get("message") or "IMDb GraphQL error")
            if "auth" in message.lower():
                raise IMDbError("IMDb authentication failed; refresh IMDB_COOKIE")
            raise IMDbError(message)
        return data

    def get_user_ratings(self, imdb_ids: Iterable[str]) -> dict[str, int | None]:
        unique_ids = list(dict.fromkeys(i for i in imdb_ids if i.startswith("tt") and i[2:].isdigit()))
        out: dict[str, int | None] = {}
        for start in range(0, len(unique_ids), READ_BATCH_SIZE):
            batch = unique_ids[start:start + READ_BATCH_SIZE]
            variable_defs = ", ".join(f"$id{i}: ID!" for i in range(len(batch)))
            fields = "\n".join(
                f"t{i}: title(id: $id{i}) {{ userRating {{ value }} }}"
                for i in range(len(batch))
            )
            variables = {f"id{i}": imdb_id for i, imdb_id in enumerate(batch)}
            payload: dict[str, object] = {
                "query": f"query RatingBridgeUserRatings({variable_defs}) {{\n{fields}\n}}",
                "operationName": "RatingBridgeUserRatings",
                "variables": variables,
            }
            data = self._post(payload).get("data")
            if not isinstance(data, dict):
                raise IMDbError("IMDb user-rating query returned no data")
            for i, imdb_id in enumerate(batch):
                title = data.get(f"t{i}")
                value: int | None = None
                if isinstance(title, dict):
                    user_rating = title.get("userRating")
                    if isinstance(user_rating, dict):
                        raw_value = user_rating.get("value")
                        try:
                            candidate = int(raw_value)
                        except (TypeError, ValueError):
                            candidate = 0
                        if 1 <= candidate <= 10:
                            value = candidate
                out[imdb_id] = value
        return out

    def upsert_ratings(self, items: Iterable[RatingItem]) -> list[str]:
        applied: list[str] = []
        mutation = (
            "mutation UpdateTitleRating($rating: Int!, $titleId: ID!) { "
            "rateTitle(input: {rating: $rating, titleId: $titleId}) { "
            "rating { value } } }"
        )
        for item in items:
            if not item.imdb_id:
                continue
            payload: dict[str, object] = {
                "query": mutation,
                "operationName": "UpdateTitleRating",
                "variables": {"rating": item.rating, "titleId": item.imdb_id},
            }
            data = self._post(payload).get("data")
            if not isinstance(data, dict) or not isinstance(data.get("rateTitle"), dict):
                raise IMDbError(f"IMDb did not confirm rating update for {item.imdb_id}")
            applied.append(item.imdb_id)
            time.sleep(0.25)
        return applied
