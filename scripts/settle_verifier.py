"""Bounded convergence checks for the explicitly gated live pilot.

Readers receive the remaining seconds and must bound their own I/O. No writer,
credentials, provider responses, or exception text are exposed by this module.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

from scripts.live_pilot import PilotBlocked, State, _ensure_safe, _json, _rating


@dataclass(frozen=True)
class SettlePolicy:
    timeout: float
    poll_interval: float
    required_consecutive_matches: int
    stable_seconds: float
    full_window: bool = False

    def __post_init__(self) -> None:
        values = (self.timeout, self.poll_interval, self.stable_seconds)
        if (any(not math.isfinite(x) for x in values)
                or self.timeout <= 0 or self.poll_interval <= 0
                or self.stable_seconds < 0
                or type(self.required_consecutive_matches) is not int
                or self.required_consecutive_matches < 2
                or max(self.stable_seconds,
                       (self.required_consecutive_matches - 1) * self.poll_interval) > self.timeout):
            raise ValueError('invalid settle policy')


# Operational review defaults, not claims about provider convergence SLAs.
UPSERT_POLICY = SettlePolicy(60, 2, 3, 4)
ROLLBACK_POLICY = SettlePolicy(120, 5, 5, 20, full_window=True)


def wait_for_state(
    name: str,
    original: State,
    expected_rating: float | None,
    read: Callable[[float], State],
    *,
    policy: SettlePolicy = UPSERT_POLICY,
    rollback: bool = False,
    read_secondary: Callable[[float], float | None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> State:
    """Require a stable matching streak with provider-specific rollback checks.

    Rollback uses its entire observation budget. An error or disagreement resets
    the streak; watchlist/library safety failures abort immediately. A deadline
    is not proof of permanent convergence, even when this function succeeds.
    """
    if rollback and not policy.full_window:
        raise PilotBlocked('rollback requires full-window verification')
    if rollback and name == 'tmdb' and read_secondary is None:
        raise PilotBlocked('TMDb rollback requires both read surfaces')
    deadline = clock() + policy.timeout
    streak = 0
    matching_since: float | None = None
    last: State | None = None
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            if (policy.full_window and last is not None and streak >= policy.required_consecutive_matches
                    and matching_since is not None
                    and clock() - matching_since >= policy.stable_seconds):
                return last
            raise PilotBlocked('provider settle verification timed out')
        try:
            current = read(remaining)
        except Exception:
            current = None
        matched = False
        if current is not None:
            _ensure_safe(name, original, current)
            matched = current.rating == expected_rating
            if read_secondary is not None:
                try:
                    remaining = deadline - clock()
                    if remaining <= 0:
                        raise TimeoutError()
                    secondary = read_secondary(remaining)
                    matched = matched and secondary == expected_rating
                except Exception:
                    matched = False
            if name == 'trakt' and rollback and original.rating is not None:
                matched = matched and current.rated_at == original.rated_at
        now = clock()
        if now > deadline:
            raise PilotBlocked('provider settle verification timed out')
        if matched:
            streak += 1
            if matching_since is None:
                matching_since = now
            last = current
        else:
            streak = 0
            matching_since = None
            last = None
        if (not policy.full_window and streak >= policy.required_consecutive_matches
                and matching_since is not None and now - matching_since >= policy.stable_seconds):
            return current
        sleep(min(policy.poll_interval, max(0, deadline - clock())))


def _tmdb_get(
    path: str, provider: object, client: object, timeout: float,
    *, params: dict[str, object] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    from hub.providers.tmdb import BASE_URL
    started = clock()
    try:
        data = _json(client.get(
            BASE_URL + path, headers=provider.headers,
            params={'session_id': provider.session_id, **(params or {})},
            timeout=min(10.0, timeout),
        ))
    except Exception:
        raise PilotBlocked('TMDb read could not be verified') from None
    if clock() - started > timeout or not isinstance(data, dict):
        raise PilotBlocked('TMDb read could not be verified')
    return data


def tmdb_account_id(provider: object, client: object, timeout: float) -> int:
    """Resolve only the authenticated numeric account identity, without logging."""
    data = _tmdb_get('/account', provider, client, timeout)
    identity = data.get('id')
    if type(identity) is not int or identity <= 0:
        raise PilotBlocked('TMDb account identity could not be verified')
    return identity


def tmdb_rated_movie_rating(
    tmdb_id: int, account_id: int, provider: object, client: object, timeout: float,
    *, clock: Callable[[], float] = time.monotonic,
) -> float | None:
    """GET authenticated identity and all rated pages; ambiguous absence fails.

    Uses the same provider/session as account_states. Pagination must complete
    before absence is accepted. Nothing from responses is printed or cached.
    """
    if type(account_id) is not int or account_id <= 0:
        raise PilotBlocked('TMDb account identity could not be verified')
    deadline = clock() + timeout

    def get(path: str, **params: object) -> dict:
        remaining = deadline - clock()
        if remaining <= 0:
            raise PilotBlocked('provider settle verification timed out')
        return _tmdb_get(path, provider, client, remaining, params=params, clock=clock)
    matches: list[float] = []
    page = 1
    total_pages: int | None = None
    while True:
        data = get(f'/account/{account_id}/rated/movies', page=page)
        pages, items = data.get('total_pages'), data.get('results')
        if (type(pages) is not int or not 0 <= pages <= 1000
                or type(data.get('page')) is not int or data['page'] != page
                or not isinstance(items, list)
                or (total_pages is not None and pages != total_pages)
                or (pages == 0 and items) or (pages > 0 and page > pages)):
            raise PilotBlocked('TMDb rated movies pagination could not be verified')
        total_pages = pages
        for item in items:
            if not isinstance(item, dict) or type(item.get('id')) is not int:
                raise PilotBlocked('TMDb rated movies response was incomplete')
            if item['id'] == tmdb_id:
                rating = _rating(item.get('rating'))
                if rating is None or not (rating * 2).is_integer():
                    raise PilotBlocked('TMDb rated movie rating could not be verified')
                matches.append(rating)
        if page >= pages:
            break
        page += 1
    if len(matches) > 1:
        raise PilotBlocked('TMDb rated movie appeared more than once')
    return matches[0] if matches else None
