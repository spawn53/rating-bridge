from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from hub.providers.base import ProviderError, UnsupportedDelivery

BASE_URL = "https://api.letterboxd.com/api/v0"


def rating_to_stars(rating: int) -> float:
    if not 1 <= int(rating) <= 10:
        raise ProviderError("Letterboxd source rating must be from 1 to 10")
    return int(rating) / 2.0


class LetterboxdProvider:
    """Letterboxd rating writer using OAuth2 and the public /me/rate endpoint.

    V2 initially enables movie delivery only. Letterboxd's current API models
    Shows/Seasons/Episodes as Production types and /me/rate accepts a generic
    rateable object, but TV identity resolution remains opt-in until we verify
    the exact production-ID mapping against the user's account.
    """

    name = "letterboxd"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        dry_run: bool = True,
        timeout: float = 30.0,
    ):
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.refresh_token = refresh_token.strip()
        self.dry_run = dry_run
        self.timeout = timeout
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

    def _get_access_token(self) -> str:
        now = datetime.now(timezone.utc)
        if (
            self._token
            and self._token_expires_at
            and self._token_expires_at > now + timedelta(seconds=60)
        ):
            return self._token

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{BASE_URL}/auth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Accept": "application/json"},
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()[:500]
            raise ProviderError(
                f"Letterboxd token refresh failed ({response.status_code}): {detail}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("Letterboxd token response was invalid JSON") from exc
        token = str(data.get("access_token") or "").strip() if isinstance(data, dict) else ""
        if not token:
            raise ProviderError("Letterboxd token response missing access_token")

        expires_in = data.get("expires_in", 300) if isinstance(data, dict) else 300
        try:
            seconds = max(60, int(expires_in))
        except (TypeError, ValueError):
            seconds = 300
        self._token = token
        self._token_expires_at = now + timedelta(seconds=seconds)
        return token

    @staticmethod
    def _extract_lid(data: Any) -> str | None:
        if isinstance(data, dict):
            direct = data.get("id")
            if direct:
                return str(direct)
            production = data.get("production")
            if isinstance(production, dict) and production.get("id"):
                return str(production["id"])
            items = data.get("items")
            if isinstance(items, list) and items:
                first = items[0]
                if isinstance(first, dict):
                    if first.get("id"):
                        return str(first["id"])
                    nested = first.get("film") or first.get("production")
                    if isinstance(nested, dict) and nested.get("id"):
                        return str(nested["id"])
        return None

    def _resolve_movie_lid(self, access_token: str, payload: dict[str, Any]) -> str:
        tmdb_id = payload.get("tmdb_id")
        imdb_id = str(payload.get("imdb_id") or "").strip()

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        with httpx.Client(timeout=self.timeout, headers=headers) as client:
            if tmdb_id:
                response = client.get(f"{BASE_URL}/film/tmdb:{tmdb_id}")
                if response.status_code < 400:
                    lid = self._extract_lid(response.json())
                    if lid:
                        return lid
                elif response.status_code not in {400, 404}:
                    response.raise_for_status()

            if imdb_id:
                response = client.get(
                    f"{BASE_URL}/films",
                    params={"filmId": f"imdb:{imdb_id}", "perPage": "1"},
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    detail = response.text.strip()[:500]
                    raise ProviderError(
                        f"Letterboxd film lookup failed ({response.status_code}): {detail}"
                    ) from exc
                lid = self._extract_lid(response.json())
                if lid:
                    return lid

        raise ProviderError(
            f"Letterboxd could not resolve movie {payload.get('content_key')}"
        )

    def deliver(self, action: str, payload: dict[str, Any]) -> None:
        if str(payload.get("media_type")) != "movie":
            raise UnsupportedDelivery(
                "Letterboxd TV/episode rating is held until production-ID mapping is live-verified"
            )
        if action not in {"upsert", "remove"}:
            raise UnsupportedDelivery(f"Unknown Letterboxd rating action {action}")
        if self.dry_run:
            if action == "upsert":
                rating_to_stars(int(payload["rating"]))
            return

        if not self.client_id or not self.client_secret or not self.refresh_token:
            raise ProviderError("Letterboxd OAuth credentials are incomplete")

        access_token = self._get_access_token()
        lid = self._resolve_movie_lid(access_token, payload)
        value = rating_to_stars(int(payload["rating"])) if action == "upsert" else None

        with httpx.Client(timeout=self.timeout) as client:
            response = client.patch(
                f"{BASE_URL}/me/rate/{lid}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"rating": value},
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()[:500]
            raise ProviderError(
                f"Letterboxd {action} failed ({response.status_code}): {detail}"
            ) from exc
