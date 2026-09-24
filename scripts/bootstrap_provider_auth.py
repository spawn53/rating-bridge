"""Interactive, no-rating provider authorization for the single-user VPS."""
from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub.auth import AuthError, TokenStore, auth_status, save_oauth_pair
from hub.auth_flows import (poll_device, poll_simkl_pin, start_device,
                            start_simkl_pin, tmdb_request_token, tmdb_session)


CONFIG_NAMES = {
    "TRAKT_CLIENT_ID", "TRAKT_CLIENT_SECRET", "TRAKT_ACCESS_TOKEN",
    "TRAKT_REFRESH_TOKEN", "TRAKT_TOKEN_EXPIRES_AT", "TRAKT_REDIRECT_URI",
    "MDBLIST_CLIENT_ID", "MDBLIST_CLIENT_SECRET", "SIMKL_CLIENT_ID",
    "SIMKL_ACCESS_TOKEN", "TMDB_API_READ_TOKEN", "TMDB_SESSION_ID",
    "RATING_HUB_AUTH_DIR",
}


def load_config(path: Path) -> None:
    if path.is_symlink():
        raise AuthError("local configuration file permissions are unsafe")
    if not path.is_file():
        if os.getenv("RATING_HUB_DB") == "/data/rating-hub.sqlite3":
            return  # Docker Compose already supplied the mode-600 env file.
        raise AuthError("local configuration file is missing")
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
        raise AuthError("local configuration file permissions are unsafe")
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() in CONFIG_NAMES:
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def bootstrap(name: str, store: TokenStore) -> None:
    import httpx

    client_id = os.getenv(f"{name.upper()}_CLIENT_ID", "").strip()
    if name == "mdblist" and not client_id:
        raise AuthError("MDBList OAuth client registration required")
    if name in {"trakt", "simkl"} and not client_id:
        raise AuthError(f"{name} client registration required")
    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        if name in {"trakt", "mdblist"}:
            secret = os.getenv("TRAKT_CLIENT_SECRET", "").strip() if name == "trakt" else ""
            if name == "trakt" and not secret:
                raise AuthError("Trakt client secret is required")
            device = start_device(name, client_id, client)
            print(f"Open: {device.verification_url}")
            print(f"Enter code: {device.user_code}")
            print("Waiting for browser authorization...")
            data = poll_device(name, client_id, device, client, client_secret=secret)
            save_oauth_pair(name, data, store)
        elif name == "tmdb":
            api_token = os.getenv("TMDB_API_READ_TOKEN", "").strip()
            if not api_token:
                raise AuthError("TMDb API Read Access Token is required")
            request_token = tmdb_request_token(api_token, client)
            print(f"Approve: https://www.themoviedb.org/authenticate/{request_token}")
            input("Press Enter after browser approval: ")
            session = tmdb_session(api_token, request_token, client)
            store.save("tmdb", {"session_id": session})
        elif name == "simkl":
            device = start_simkl_pin(client_id, client)
            print(f"Open: {device.verification_url}")
            print(f"Enter code: {device.user_code}")
            print("Waiting for browser authorization...")
            token = poll_simkl_pin(client_id, device, client)
            store.save("simkl", {"access_token": token, "auth_version": 1})
        else:
            raise AuthError("unsupported provider")
    print(f"{name} authorization stored securely")
    print(f"{name} read-only status: {auth_status(name, store=store)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive provider authorization; no rating writes")
    parser.add_argument("provider", choices=("trakt", "mdblist", "tmdb", "simkl"))
    parser.add_argument("--env-file", type=Path, default=Path("/srv/stacks/rating-hub/.env.v2"))
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Interactive terminal required", file=sys.stderr)
        return 2
    try:
        load_config(args.env_file)
        bootstrap(args.provider, TokenStore())
        return 0
    except (AuthError, OSError, EOFError) as exc:
        # No HTTP URLs, bodies, token values, or nested exception text reach stdout.
        message = str(exc) if isinstance(exc, AuthError) else "authorization failed"
        print(message, file=sys.stderr)
        return 1
    except Exception:
        print("authorization failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
