from __future__ import annotations

import os

from hub.providers.base import ProviderNotConfigured, RatingProvider
from hub.providers.mdblist import MDBListProvider
from hub.providers.simkl import SimklProvider
from hub.providers.tmdb import TMDbProvider
from hub.providers.trakt import TraktProvider


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def get_provider(name: str) -> RatingProvider:
    if name == "trakt":
        client_id = _env("TRAKT_CLIENT_ID")
        access_token = _env("TRAKT_ACCESS_TOKEN")
        if not client_id or not access_token:
            raise ProviderNotConfigured(
                "Trakt requires TRAKT_CLIENT_ID and TRAKT_ACCESS_TOKEN"
            )
        return TraktProvider(client_id, access_token)

    if name == "tmdb":
        api_read_token = _env("TMDB_API_READ_TOKEN")
        session_id = _env("TMDB_SESSION_ID")
        if not api_read_token or not session_id:
            raise ProviderNotConfigured(
                "TMDb requires TMDB_API_READ_TOKEN and TMDB_SESSION_ID"
            )
        return TMDbProvider(api_read_token, session_id)

    if name == "mdblist":
        access_token = _env("MDBLIST_ACCESS_TOKEN")
        if not access_token:
            raise ProviderNotConfigured(
                "MDBList V2 writes require MDBLIST_ACCESS_TOKEN"
            )
        return MDBListProvider(access_token)

    if name == "simkl":
        client_id = _env("SIMKL_CLIENT_ID")
        access_token = _env("SIMKL_ACCESS_TOKEN")
        if not client_id or not access_token:
            raise ProviderNotConfigured(
                "Simkl requires SIMKL_CLIENT_ID and SIMKL_ACCESS_TOKEN"
            )
        return SimklProvider(client_id, access_token)

    raise ProviderNotConfigured(f"Provider {name} is not implemented yet")
