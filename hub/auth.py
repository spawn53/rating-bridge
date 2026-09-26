"""Small local credential store and coordinated OAuth refresh for one VPS user.

Only provider tokens and TMDb's session ID live here. The rating database never
contains credentials. All public errors deliberately omit provider responses.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

import httpx


class AuthError(RuntimeError):
    pass


REFRESH_SKEW = 300
OAUTH = {
    "trakt": "https://auth.trakt.tv/oauth/token",
    "mdblist": "https://api.mdblist.com/oauth/token/",
}


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _expiry(value: object) -> float:
    if isinstance(value, bool):
        raise AuthError("credential expiry is invalid")
    try:
        if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            return float(value)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError) as exc:
        raise AuthError("credential expiry is invalid") from exc


class TokenStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or _env("RATING_HUB_AUTH_DIR") or (Path(_env("RATING_HUB_DB") or "/srv/data/rating-hub/rating-hub.sqlite3").parent / "auth"))

    def _directory(self, create: bool) -> bool:
        if not self.root.exists() and not self.root.is_symlink():
            if not create:
                return False
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or (info.st_uid != os.geteuid() and os.geteuid() != 0):
            raise AuthError("credential directory permissions are unsafe")
        return True

    def _path(self, name: str) -> Path:
        if name not in {"trakt", "mdblist", "simkl", "tmdb"}:
            raise AuthError("unknown credential name")
        return self.root / f"{name}.json"

    def read(self, name: str) -> dict[str, object] | None:
        if not self._directory(False):
            return None
        path = self._path(name)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != self.root.stat().st_uid:
                raise AuthError("credential file permissions are unsafe")
            with os.fdopen(fd, "r") as file:
                fd = -1
                data = json.load(file)
        except (ValueError, UnicodeError) as exc:
            raise AuthError("credential file is invalid") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(data, dict):
            raise AuthError("credential file is invalid")
        return data

    @contextmanager
    def locked(self, name: str) -> Iterator[None]:
        self._directory(True)
        lock = self.root / f"{name}.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.geteuid() == 0:
                os.fchown(fd, self.root.stat().st_uid, -1)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != self.root.stat().st_uid:
                raise AuthError("credential lock permissions are unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def save_locked(self, name: str, data: dict[str, object]) -> None:
        self._directory(True)
        path = self._path(name)
        fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            if os.geteuid() == 0:
                os.fchown(fd, self.root.stat().st_uid, -1)
            with os.fdopen(fd, "w") as file:
                json.dump(data, file, separators=(",", ":"))
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save(self, name: str, data: dict[str, object]) -> None:
        with self.locked(name):
            self.save_locked(name, data)


def _legacy(name: str) -> dict[str, object] | None:
    if name != "trakt":
        return None
    access, refresh = _env("TRAKT_ACCESS_TOKEN"), _env("TRAKT_REFRESH_TOKEN")
    if not access or not refresh:
        return None
    expires = _env("TRAKT_TOKEN_EXPIRES_AT")
    if not expires:
        raise AuthError("Trakt token expiry is required")
    return {"access_token": access, "refresh_token": refresh, "expires_at": _expiry(expires)}


def token_state(name: str, store: TokenStore | None = None) -> dict[str, object] | None:
    store = store or TokenStore()
    saved = store.read(name)
    return saved if saved is not None else _legacy(name)


def _validate_pair(data: object, now: float) -> dict[str, object]:
    if not isinstance(data, dict):
        raise AuthError("OAuth response was incomplete")
    access, refresh, duration = data.get("access_token"), data.get("refresh_token"), data.get("expires_in")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise AuthError("OAuth response was incomplete")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise AuthError("OAuth expiry was invalid")
    return {"access_token": access, "refresh_token": refresh, "expires_at": now + float(duration)}


def save_oauth_pair(name: str, data: object, store: TokenStore | None = None, now: float | None = None) -> None:
    if name not in OAUTH:
        raise AuthError("unknown OAuth provider")
    pair = _validate_pair(data, time.time() if now is None else now)
    if name == "mdblist" and (not isinstance(data, dict) or "write" not in str(data.get("scope", "")).split()):
        raise AuthError("MDBList write scope was not granted")
    (store or TokenStore()).save(name, pair)


def oauth_access_token(name: str, *, store: TokenStore | None = None,
                       client: httpx.Client | None = None, now: float | None = None) -> str:
    if name not in OAUTH:
        raise AuthError("unknown OAuth provider")
    store = store or TokenStore()
    current_time = time.time() if now is None else now
    state = token_state(name, store)
    if state is None:
        raise AuthError("OAuth authorization is required")
    if (not isinstance(state.get("access_token"), str) or not state["access_token"]
            or not isinstance(state.get("refresh_token"), str) or not state["refresh_token"]):
        raise AuthError("OAuth credentials are incomplete")
    if _expiry(state.get("expires_at")) > current_time + REFRESH_SKEW:
        return str(state["access_token"])
    client_id = _env(f"{name.upper()}_CLIENT_ID")
    client_secret = _env("TRAKT_CLIENT_SECRET") if name == "trakt" else _env("MDBLIST_CLIENT_SECRET")
    if not client_id or (name == "trakt" and not client_secret):
        raise AuthError("OAuth client configuration is incomplete")
    with store.locked(name):
        # A peer may have refreshed while this caller waited for the lock.
        state = token_state(name, store)
        if state is None:
            raise AuthError("OAuth authorization is required")
        if (not isinstance(state.get("access_token"), str) or not state["access_token"]
                or not isinstance(state.get("refresh_token"), str) or not state["refresh_token"]):
            raise AuthError("OAuth credentials are incomplete")
        if _expiry(state.get("expires_at")) > current_time + REFRESH_SKEW:
            return str(state["access_token"])
        payload = {"grant_type": "refresh_token", "refresh_token": state["refresh_token"],
                   "client_id": client_id}
        if client_secret:
            payload["client_secret"] = client_secret
        own_client = client is None
        client = client or httpx.Client(timeout=10.0, follow_redirects=False)
        try:
            if name == "trakt":
                payload["redirect_uri"] = _env("TRAKT_REDIRECT_URI") or "urn:ietf:wg:oauth:2.0:oob"
                response = client.post(OAUTH[name], json=payload)
            else:
                response = client.post(OAUTH[name], data=payload)
            if not response.is_success:
                raise AuthError("OAuth refresh failed")
            pair = _validate_pair(response.json(), current_time)
            if pair["refresh_token"] == state["refresh_token"]:
                raise AuthError("OAuth refresh did not rotate the refresh token")
            store.save_locked(name, pair)
            return str(pair["access_token"])
        except (httpx.HTTPError, ValueError) as exc:
            raise AuthError("OAuth refresh failed") from exc
        finally:
            if own_client:
                client.close()


def stored_value(name: str, key: str, env_name: str, store: TokenStore | None = None) -> str:
    state = (store or TokenStore()).read(name)
    value = state.get(key) if state else None
    if state is not None:
        return value if isinstance(value, str) else ""
    return _env(env_name)


def auth_status(name: str, *, offline: bool = False,
                store: TokenStore | None = None, client: httpx.Client | None = None) -> str:
    """Return only a relative state. Offline mode never opens a socket."""
    store = store or TokenStore()
    try:
        if name in OAUTH:
            client_id = _env(f"{name.upper()}_CLIENT_ID")
            if not client_id or (name == "trakt" and not _env("TRAKT_CLIENT_SECRET")):
                return "UNCONFIGURED"
            state = token_state(name, store)
            if state is None:
                return "NEEDS_AUTHORIZATION"
            if (not isinstance(state.get("access_token"), str) or not state["access_token"]
                    or not isinstance(state.get("refresh_token"), str) or not state["refresh_token"]):
                return "NEEDS_AUTHORIZATION"
            due = _expiry(state.get("expires_at")) <= time.time() + REFRESH_SKEW
            if offline:
                return "REFRESH_REQUIRED" if due else "CONFIGURED"
            token = oauth_access_token(name, store=store, client=client)
            url = "https://api.trakt.tv/users/settings" if name == "trakt" else "https://api.mdblist.com/user"
            headers = {"Authorization": f"Bearer {token}"}
            if name == "trakt":
                headers.update({"trakt-api-version": "2", "trakt-api-key": client_id})
        elif name == "simkl":
            client_id, token = _env("SIMKL_CLIENT_ID"), stored_value("simkl", "access_token", "SIMKL_ACCESS_TOKEN", store)
            if not client_id:
                return "UNCONFIGURED"
            if not token:
                return "NEEDS_AUTHORIZATION"
            if offline:
                return "CONFIGURED"
            url, headers = "https://api.simkl.com/users/settings", {"Authorization": f"Bearer {token}", "simkl-api-key": client_id}
        elif name == "tmdb":
            token, session = _env("TMDB_API_READ_TOKEN"), stored_value("tmdb", "session_id", "TMDB_SESSION_ID", store)
            if not token:
                return "UNCONFIGURED"
            if not session:
                return "NEEDS_AUTHORIZATION"
            if offline:
                return "CONFIGURED"
            url, headers = "https://api.themoviedb.org/3/account", {"Authorization": f"Bearer {token}"}
        else:
            return "DISABLED"
        own_client = client is None
        client = client or httpx.Client(timeout=10.0, follow_redirects=False)
        try:
            response = client.get(url, headers=headers,
                                  params={"session_id": session} if name == "tmdb" else None)
            return "READY" if response.is_success else "AUTH_FAILED"
        finally:
            if own_client:
                client.close()
    except (AuthError, httpx.HTTPError, ValueError, OSError):
        return "AUTH_FAILED"
