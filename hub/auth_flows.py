"""Interactive OAuth/PIN protocol steps. Callers own prompts and persistence."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import httpx

from hub.auth import AuthError


@dataclass(frozen=True)
class DeviceCode:
    code: str
    user_code: str
    verification_url: str
    interval: int
    expires_in: int


def _object(response: httpx.Response, label: str) -> dict[str, object]:
    if not response.is_success:
        raise AuthError(f"{label} failed")
    try:
        data = response.json()
    except ValueError as exc:
        raise AuthError(f"{label} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise AuthError(f"{label} returned an invalid response")
    return data


def _device(data: dict[str, object], hosts: set[str]) -> DeviceCode:
    code, user_code = data.get("device_code"), data.get("user_code")
    url = data.get("verification_uri") or data.get("verification_url")
    interval, expires = data.get("interval"), data.get("expires_in")
    if (not isinstance(code, str) or not code or not isinstance(user_code, str) or not user_code
            or not isinstance(url, str) or urlparse(url).scheme != "https"
            or urlparse(url).hostname not in hosts or not isinstance(interval, int) or interval < 1
            or not isinstance(expires, int) or expires < 1):
        raise AuthError("device authorization response was incomplete")
    return DeviceCode(code, user_code, url, interval, expires)


def start_device(name: str, client_id: str, client: httpx.Client) -> DeviceCode:
    if name == "trakt":
        data = _object(client.post("https://auth.trakt.tv/oauth/device/code",
                                   json={"client_id": client_id}), "Trakt device authorization")
        return _device(data, {"auth.trakt.tv", "trakt.tv"})
    if name == "mdblist":
        data = _object(client.post("https://api.mdblist.com/oauth/device-authorization/",
                                   data={"client_id": client_id, "scope": "write"}), "MDBList device authorization")
        return _device(data, {"mdblist.com"})
    raise AuthError("unsupported device provider")


def poll_device(name: str, client_id: str, device: DeviceCode, client: httpx.Client,
                client_secret: str = "", sleep: Callable[[float], None] = time.sleep,
                monotonic: Callable[[], float] = time.monotonic) -> dict[str, object]:
    if name not in {"trakt", "mdblist"}:
        raise AuthError("unsupported device provider")
    deadline = monotonic() + device.expires_in
    interval = device.interval
    while monotonic() + interval <= deadline:
        sleep(interval)
        if name == "trakt":
            response = client.post("https://auth.trakt.tv/oauth/device/token",
                                   json={"code": device.code, "client_id": client_id,
                                         "client_secret": client_secret})
            if response.status_code == 400:
                continue
            if response.status_code == 429:
                interval += 5
                continue
            if response.status_code in {404, 409, 410, 418}:
                raise AuthError("Trakt device authorization ended")
        else:
            response = client.post("https://api.mdblist.com/oauth/token/", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device.code, "client_id": client_id,
            })
            if not response.is_success:
                try:
                    error = response.json().get("error")
                except (ValueError, AttributeError):
                    error = None
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval += 5
                    continue
                if error in {"access_denied", "expired_token"}:
                    raise AuthError("MDBList device authorization ended")
        return _object(response, f"{name} device token")
    raise AuthError("device authorization expired")


def tmdb_request_token(api_token: str, client: httpx.Client) -> str:
    headers = {"Authorization": f"Bearer {api_token}"}
    _object(client.get("https://api.themoviedb.org/3/authentication", headers=headers),
            "TMDb API token validation")
    data = _object(client.get("https://api.themoviedb.org/3/authentication/token/new",
                              headers=headers), "TMDb request token")
    token = data.get("request_token")
    if data.get("success") is not True or not isinstance(token, str) or not token:
        raise AuthError("TMDb request token was incomplete")
    return token


def tmdb_session(api_token: str, request_token: str, client: httpx.Client) -> str:
    data = _object(client.post("https://api.themoviedb.org/3/authentication/session/new",
                               headers={"Authorization": f"Bearer {api_token}"},
                               json={"request_token": request_token}), "TMDb session creation")
    session = data.get("session_id")
    if data.get("success") is not True or not isinstance(session, str) or not session:
        raise AuthError("TMDb session response was incomplete")
    return session


def start_simkl_pin(client_id: str, client: httpx.Client) -> DeviceCode:
    data = _object(client.get("https://api.simkl.com/oauth/pin", params={"client_id": client_id}),
                   "Simkl PIN authorization")
    # Simkl AUTH V1 PIN replies may call the URL either url or verification_url.
    if "verification_url" not in data and isinstance(data.get("url"), str):
        data["verification_url"] = data["url"]
    return _device(data, {"simkl.com"})


def poll_simkl_pin(client_id: str, device: DeviceCode, client: httpx.Client,
                   sleep: Callable[[float], None] = time.sleep,
                   monotonic: Callable[[], float] = time.monotonic) -> str:
    deadline = monotonic() + device.expires_in
    interval = device.interval
    while monotonic() + interval <= deadline:
        sleep(interval)
        data = _object(client.get(f"https://api.simkl.com/oauth/pin/{device.user_code}",
                                  params={"client_id": client_id}), "Simkl PIN status")
        if data.get("result") == "OK" and isinstance(data.get("access_token"), str):
            return str(data["access_token"])
        if data.get("result") != "KO":
            raise AuthError("Simkl PIN status was unexpected")
        if data.get("message") == "Slow down":
            interval += 5
        elif data.get("message") != "Authorization pending":
            raise AuthError("Simkl PIN authorization ended")
    raise AuthError("Simkl PIN authorization expired")


def mdblist_movie_rating(tmdb_id: int, token: str, client: httpx.Client) -> int | None:
    """Read every cursor page; fail closed on unknown item shapes or pagination."""
    cursor: str | None = None
    seen: set[str] = set()
    matches: list[int] = []
    for _ in range(1000):
        params: dict[str, str] = {"limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        data = _object(client.get("https://api.mdblist.com/sync/ratings",
                                  params=params, headers={"Authorization": f"Bearer {token}"}),
                       "MDBList rating read")
        items, page = data.get("movies"), data.get("pagination")
        if not isinstance(items, list) or not isinstance(page, dict) or "next_cursor" not in page:
            raise AuthError("MDBList rating response was incomplete")
        for item in items:
            if not isinstance(item, dict):
                raise AuthError("MDBList rating item was incomplete")
            movie = item.get("movie")
            ids = item.get("ids") or (movie.get("ids") if isinstance(movie, dict) else None)
            if not isinstance(ids, dict):
                raise AuthError("MDBList rating item IDs were incomplete")
            if str(ids.get("tmdb")) != str(tmdb_id):
                continue
            rating = item.get("rating")
            if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 10:
                raise AuthError("MDBList rating value was invalid")
            matches.append(rating)
        next_cursor = page["next_cursor"]
        if next_cursor is None:
            if len(matches) > 1:
                raise AuthError("MDBList returned duplicate movie ratings")
            return matches[0] if matches else None
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
            raise AuthError("MDBList rating pagination was invalid")
        seen.add(next_cursor)
        cursor = next_cursor
    raise AuthError("MDBList ratings exceeded safe pagination limit")
