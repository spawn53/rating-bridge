"""No-write provider configuration and authentication preflight."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _value(name: str) -> str:
    return os.getenv(name, "").strip()


def _enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _configured(*names: str) -> bool:
    return all(_value(name) for name in names)


def _get(url: str, *, headers: dict[str, str] | None = None, params: dict[str, str] | None = None) -> bool:
    """Perform only a read-only request; discard bodies and error details."""
    try:
        import httpx
    except ImportError:
        return False
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            response = client.get(url, headers=headers, params=params)
        return response.is_success
    except httpx.HTTPError:
        return False


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _check_mdblist(offline: bool) -> str:
    token = _value("MDBLIST_ACCESS_TOKEN")
    if not token:
        return "MISSING"
    if offline:
        return "CONFIGURED — offline auth not tested"
    return "READY" if _get("https://api.mdblist.com/user", headers=_auth_header(token)) else "CONFIGURED — live auth check failed"


def _check_trakt(offline: bool) -> str:
    client_id, token = _value("TRAKT_CLIENT_ID"), _value("TRAKT_ACCESS_TOKEN")
    if not client_id or not token:
        return "MISSING"
    if offline:
        return "CONFIGURED — offline auth not tested"
    headers = {**_auth_header(token), "trakt-api-version": "2", "trakt-api-key": client_id}
    return "READY" if _get("https://api.trakt.tv/users/settings", headers=headers) else "CONFIGURED — live auth check failed"


def _check_simkl(offline: bool) -> str:
    client_id, token = _value("SIMKL_CLIENT_ID"), _value("SIMKL_ACCESS_TOKEN")
    if not client_id or not token:
        return "MISSING"
    if offline:
        return "CONFIGURED — offline auth not tested"
    headers = {**_auth_header(token), "simkl-api-key": client_id}
    return "READY" if _get("https://api.simkl.com/users/settings", headers=headers) else "CONFIGURED — live auth check failed"


def _check_tmdb(offline: bool) -> str:
    token, session = _value("TMDB_API_READ_TOKEN"), _value("TMDB_SESSION_ID")
    if not token or not session:
        return "MISSING"
    if offline:
        return "CONFIGURED — offline auth not tested"
    return "READY" if _get("https://api.themoviedb.org/3/account", headers=_auth_header(token), params={"session_id": session}) else "CONFIGURED — live auth check failed"


def _experimental(enabled_name: str, dry_run_name: str, credentials: tuple[str, ...]) -> str:
    if not _enabled(enabled_name):
        return "DISABLED"
    if _enabled(dry_run_name, True):
        return "DRY-RUN"
    if not _configured(*credentials):
        return "MISSING LIVE CREDENTIALS"
    return "READY — live auth not tested"


CHECKS: tuple[tuple[str, Callable[[bool], str]], ...] = (
    ("MDBList", _check_mdblist),
    ("Trakt", _check_trakt),
    ("Simkl", _check_simkl),
    ("TMDb", _check_tmdb),
    ("IMDb", lambda offline: _experimental("IMDB_V2_ENABLED", "IMDB_V2_DRY_RUN", ("IMDB_COOKIE",))),
    ("Letterboxd", lambda offline: _experimental("LETTERBOXD_ENABLED", "LETTERBOXD_DRY_RUN", ("LETTERBOXD_CLIENT_ID", "LETTERBOXD_CLIENT_SECRET", "LETTERBOXD_REFRESH_TOKEN"))),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a no-write Rating Hub provider preflight")
    parser.add_argument("--offline", action="store_true", help="validate configuration without making network requests")
    args = parser.parse_args(argv)
    print("Rating Hub provider preflight" + (" (offline)" if args.offline else ""))
    for label, check in CHECKS:
        print(f"{label:<12} {check(args.offline)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
