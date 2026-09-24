"""Explicitly gated, one-provider rating pilot; never run by the API or worker."""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class PilotBlocked(RuntimeError):
    pass


class Delivery(Protocol):
    def deliver(self, action: str, payload: dict[str, object]) -> None: ...


@dataclass(frozen=True)
class State:
    rating: float | None
    rated_at: str | None = None
    watchlist: bool | None = None
    library_status: str | None = None


def _rating(value: object) -> float | None:
    if value is None or value is False:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PilotBlocked('provider rating response was not numeric')
    score = float(value)
    if not 0.5 <= score <= 10:
        raise PilotBlocked('provider rating response was outside the expected scale')
    return score


def _json(response: object) -> object:
    # Never surface HTTP errors, URLs, or response bodies to the operator.
    response.raise_for_status()
    return response.json()


def read_state(name: str, tmdb_id: int, provider: object, client: object) -> State:
    """Read only documented GET endpoints; fail if absence is ambiguous."""
    if name == 'tmdb':
        from hub.providers.tmdb import BASE_URL
        data = _json(client.get(
            f'{BASE_URL}/movie/{tmdb_id}/account_states',
            params={'session_id': provider.session_id}, headers=provider.headers,
        ))
        if (not isinstance(data, dict) or 'rated' not in data
                or not isinstance(data.get('watchlist'), bool)):
            raise PilotBlocked('TMDb account state was incomplete')
        rated = data['rated']
        if isinstance(rated, dict):
            rated = rated.get('value')
            if rated is None:
                raise PilotBlocked('TMDb rated value was missing')
        score = _rating(rated)
        if score is not None and not (score * 2).is_integer():
            raise PilotBlocked('TMDb rating was not a half-step value')
        return State(score, watchlist=data['watchlist'])

    if name == 'trakt':
        from hub.providers.trakt import BASE_URL
        matches = []
        page = 1
        while True:
            response = client.get(
                f'{BASE_URL}/users/me/ratings/movies',
                params={'page': str(page), 'limit': '250'}, headers=provider.headers,
            )
            items = _json(response)
            if not isinstance(items, list):
                raise PilotBlocked('Trakt ratings response was incomplete')
            for item in items:
                if not isinstance(item, dict):
                    raise PilotBlocked('Trakt rating item was malformed')
                movie = item.get('movie')
                ids = movie.get('ids') if isinstance(movie, dict) else None
                if isinstance(ids, dict) and str(ids.get('tmdb')) == str(tmdb_id):
                    matches.append(item)
            pages = response.headers.get('X-Pagination-Page-Count')
            if pages is not None:
                try:
                    page_count = int(pages)
                except ValueError as exc:
                    raise PilotBlocked('Trakt pagination was invalid') from exc
                if page_count < page:
                    raise PilotBlocked('Trakt pagination was inconsistent')
                if page >= page_count:
                    break
            elif len(items) < 250:
                break
            page += 1
            if page > 1000:
                raise PilotBlocked('Trakt ratings exceeded safe pagination limit')
        if len(matches) > 1:
            raise PilotBlocked('Trakt returned duplicate matching ratings')
        if not matches:
            return State(None)
        if 'rating' not in matches[0]:
            raise PilotBlocked('Trakt rating value was missing')
        score = _rating(matches[0].get('rating'))
        if score is None or not score.is_integer():
            raise PilotBlocked('Trakt rating was not an integer')
        return State(score, rated_at=matches[0].get('rated_at'))

    if name == 'simkl':
        from hub.providers.simkl import BASE_URL
        data = _json(client.get(
            f'{BASE_URL}/sync/all-items/movies',
            params={'client_id': provider.client_id}, headers=provider.headers,
        ))
        items = data.get('movies') if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise PilotBlocked('Simkl library response was incomplete')
        matches = []
        for item in items:
            if not isinstance(item, dict):
                raise PilotBlocked('Simkl library item was malformed')
            movie = item.get('movie')
            ids = movie.get('ids') if isinstance(movie, dict) else None
            if isinstance(ids, dict) and str(ids.get('tmdb')) == str(tmdb_id):
                matches.append(item)
        if len(matches) != 1:
            raise PilotBlocked('Simkl movie must already exist exactly once in the library')
        status = matches[0].get('status')
        if not isinstance(status, str) or not status:
            raise PilotBlocked('Simkl library status was missing')
        if 'user_rating' not in matches[0]:
            raise PilotBlocked('Simkl user rating field was missing')
        score = _rating(matches[0].get('user_rating'))
        if score is not None and not score.is_integer():
            raise PilotBlocked('Simkl rating was not an integer')
        return State(score, library_status=status)

    if name == 'mdblist':
        from hub.auth_flows import mdblist_movie_rating
        authorization = provider.headers.get('Authorization', '')
        if not authorization.startswith('Bearer ') or len(authorization) <= 7:
            raise PilotBlocked('MDBList OAuth access token is unavailable')
        return State(mdblist_movie_rating(tmdb_id, authorization[7:], client))

    raise PilotBlocked('unknown provider')


def _ensure_safe(name: str, original: State, current: State) -> None:
    if name == 'tmdb' and (original.watchlist is not False or current.watchlist is not False):
        raise PilotBlocked('TMDb watchlist state is unsafe for a rating pilot')
    if name == 'simkl' and current.library_status != original.library_status:
        raise PilotBlocked('Simkl library status changed during the pilot')


def pilot(name: str, payload: dict[str, object], provider: Delivery,
          read: Callable[[], State], first: int = 7, second: int = 9) -> list[str]:
    """Verify each transition and attempt rollback even after uncertain writes."""
    original = read()
    _ensure_safe(name, original, original)
    events = ['original state read']
    attempted = False
    failure: Exception | None = None
    try:
        for score in (first, second):
            attempted = True  # A timed-out write may have reached the provider.
            provider.deliver('upsert', {**payload, 'rating': score})
            current = read()
            _ensure_safe(name, original, current)
            if current.rating != score:
                raise PilotBlocked('provider verification disagreed with requested rating')
            events.append(f'rating {score} verified')
    except Exception as exc:
        failure = exc
    finally:
        if attempted:
            try:
                if original.rating is None:
                    provider.deliver('remove', payload)
                else:
                    provider.deliver('upsert', {
                        **payload, 'rating': original.rating,
                        'rated_at': original.rated_at,
                    })
                restored = read()
                _ensure_safe(name, original, restored)
                if restored.rating != original.rating:
                    raise PilotBlocked('original rating was not restored')
                if (name == 'trakt' and original.rating is not None
                        and restored.rated_at != original.rated_at):
                    raise PilotBlocked('original Trakt rated_at was not restored')
                events.append('original rating verified restored')
            except Exception as exc:
                raise PilotBlocked('rollback could not be verified; manual review required') from exc
    if failure is not None:
        raise PilotBlocked('pilot stopped after failed write or verification') from failure
    return events


def _load_env_file(path: str) -> None:
    file = Path(path)
    if not file.is_file() or file.stat().st_mode & 0o077:
        raise PilotBlocked('local env file is missing or has unsafe permissions')
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise PilotBlocked('local env file has an invalid line')
        name, value = line.split('=', 1)
        os.environ[name.strip()] = value.strip().strip('"').strip("'")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='One-provider controlled rating pilot')
    parser.add_argument('--provider', choices=('tmdb', 'trakt', 'mdblist', 'simkl'), required=True)
    parser.add_argument('--content', required=True, help='movie:tmdb:<numeric ID>')
    parser.add_argument('--confirm-live-write', action='store_true')
    parser.add_argument('--env-file', help='local mode-600 provider configuration file')
    args = parser.parse_args(argv)
    if not args.confirm_live_write:
        print('Live write refused: --confirm-live-write is required')
        return 2
    match = re.fullmatch(r'movie:tmdb:([1-9][0-9]*)', args.content)
    if match is None:
        print('Pilot requires one movie:tmdb:<numeric ID> content key')
        return 2
    if args.provider == 'mdblist':
        print('Pilot blocked: MDBList writes remain disabled until authorized Phase 4A')
        return 2
    try:
        if args.env_file:
            _load_env_file(args.env_file)
        from hub.providers.registry import get_provider
        import httpx
        provider = get_provider(args.provider)
        tmdb_id = int(match.group(1))
        payload = {'media_type': 'movie', 'tmdb_id': tmdb_id, 'content_key': args.content}
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            events = pilot(args.provider, payload, provider,
                           lambda: read_state(args.provider, tmdb_id, provider, client))
        for event in events:
            print(event)
        return 0
    except Exception as exc:
        # HTTP exception text may include URLs, response bodies, or credentials.
        print(f'Pilot blocked or failed ({type(exc).__name__}); inspect state manually')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
