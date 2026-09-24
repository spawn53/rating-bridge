from __future__ import annotations

import os

from hub.auth import auth_status, oauth_access_token, stored_value

from hub.providers.base import ProviderNotConfigured, RatingProvider
from hub.providers.imdb_v2 import IMDbProvider
from hub.providers.letterboxd import LetterboxdProvider
from hub.providers.mdblist import MDBListProvider
from hub.providers.simkl import SimklProvider
from hub.providers.tmdb import TMDbProvider
from hub.providers.trakt import TraktProvider


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_provider(name: str) -> RatingProvider:
    if name == "trakt":
        client_id = _env("TRAKT_CLIENT_ID")
        if auth_status("trakt", offline=True) in {"UNCONFIGURED", "NEEDS_AUTHORIZATION", "AUTH_FAILED"}:
            raise ProviderNotConfigured(
                "Trakt OAuth authorization and refresh configuration are required"
            )
        return TraktProvider(client_id, "", token_supplier=lambda: oauth_access_token("trakt"))

    if name == "tmdb":
        api_read_token = _env("TMDB_API_READ_TOKEN")
        session_id = stored_value("tmdb", "session_id", "TMDB_SESSION_ID")
        if not api_read_token or not session_id:
            raise ProviderNotConfigured(
                "TMDb requires TMDB_API_READ_TOKEN and TMDB_SESSION_ID"
            )
        return TMDbProvider(api_read_token, session_id)

    if name == "mdblist":
        if auth_status("mdblist", offline=True) in {"UNCONFIGURED", "NEEDS_AUTHORIZATION", "AUTH_FAILED"}:
            raise ProviderNotConfigured(
                "MDBList rating sync requires its own OAuth device authorization"
            )
        return MDBListProvider("", token_supplier=lambda: oauth_access_token("mdblist"))

    if name == "simkl":
        client_id = _env("SIMKL_CLIENT_ID")
        access_token = stored_value("simkl", "access_token", "SIMKL_ACCESS_TOKEN")
        if not client_id or not access_token:
            raise ProviderNotConfigured(
                "Simkl requires SIMKL_CLIENT_ID and SIMKL_ACCESS_TOKEN"
            )
        return SimklProvider(client_id, access_token)

    if name == "imdb":
        if not _bool_env("IMDB_V2_ENABLED", False):
            raise ProviderNotConfigured("IMDb V2 provider is disabled")
        dry_run = _bool_env("IMDB_V2_DRY_RUN", True)
        cookie = _env("IMDB_COOKIE")
        if not dry_run and not cookie:
            raise ProviderNotConfigured("IMDb V2 live writes require IMDB_COOKIE")
        return IMDbProvider(cookie, dry_run=dry_run)

    if name == "letterboxd":
        if not _bool_env("LETTERBOXD_ENABLED", False):
            raise ProviderNotConfigured("Letterboxd provider is disabled")
        dry_run = _bool_env("LETTERBOXD_DRY_RUN", True)
        client_id = _env("LETTERBOXD_CLIENT_ID")
        client_secret = _env("LETTERBOXD_CLIENT_SECRET")
        refresh_token = _env("LETTERBOXD_REFRESH_TOKEN")
        if not dry_run and not all((client_id, client_secret, refresh_token)):
            raise ProviderNotConfigured(
                "Letterboxd live writes require client ID, client secret and refresh token"
            )
        return LetterboxdProvider(
            client_id,
            client_secret,
            refresh_token,
            dry_run=dry_run,
        )

    raise ProviderNotConfigured(f"Provider {name} is not implemented yet")
