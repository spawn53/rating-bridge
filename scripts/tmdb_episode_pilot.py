"""Explicit provider-isolation harness for one TMDb episode rating pilot."""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class PilotBlocked(RuntimeError):
    pass


class EpisodeProvider(Protocol):
    def read_episode_rating(
        self, series_id: object, season_number: object, episode_number: object
    ) -> float | None: ...

    def deliver(self, action: str, payload: dict[str, object]) -> None: ...


_UNSET = object()
_SAFE_MESSAGES = {
    "confirmation required": "Pilot refused: --confirm-live-write is required",
    "expected state required": "Pilot refused: an explicit expected current state is required",
    "read-only guards invalid": "Pilot refused: read-only mode does not accept mutation guards",
    "remove requires rated state": "Pilot refused: remove requires an expected current rating",
    "rating invalid": "Pilot refused: rating must be a half-step from 0.5 through 10",
    "operation invalid": "Pilot refused: operation was invalid",
    "current state mismatch": "Pilot blocked: current TMDb episode rating did not match expectation",
    "verification mismatch": (
        "Pilot failed: TMDb episode write could not be verified; inspect provider state"
    ),
}


def operator_failure(exc: Exception) -> str:
    if type(exc) is PilotBlocked:
        return _SAFE_MESSAGES.get(str(exc), "Pilot blocked or failed; inspect provider state")
    return "Pilot blocked or failed; inspect provider state"


def _rating(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PilotBlocked("rating invalid")
    score = float(value)
    if (not math.isfinite(score) or not 0.5 <= score <= 10
            or not (score * 2).is_integer()):
        raise PilotBlocked("rating invalid")
    return score


def run_episode_pilot(
    provider: EpisodeProvider,
    *,
    series_id: object,
    season_number: object,
    episode_number: object,
    operation: str,
    rating: object = None,
    expected: object = _UNSET,
    confirmed: bool = False,
) -> float | None:
    """Run one isolated read or guarded write followed by exact verification."""
    from hub.providers.tmdb import TMDbProvider

    series, season, episode = TMDbProvider._episode_coordinates(
        series_id, season_number, episode_number
    )
    if operation == "read":
        if expected is not _UNSET or confirmed is not False or rating is not None:
            raise PilotBlocked("read-only guards invalid")
        return provider.read_episode_rating(series, season, episode)
    if operation not in {"set", "remove"}:
        raise PilotBlocked("operation invalid")
    if confirmed is not True:
        raise PilotBlocked("confirmation required")
    if expected is _UNSET:
        raise PilotBlocked("expected state required")
    expected_rating = None if expected is None else _rating(expected)
    if operation == "remove" and expected_rating is None:
        raise PilotBlocked("remove requires rated state")
    desired = _rating(rating) if operation == "set" else None
    current = provider.read_episode_rating(series, season, episode)
    if current != expected_rating:
        raise PilotBlocked("current state mismatch")
    payload: dict[str, object] = {
        "media_type": "episode",
        "tmdb_series_id": series,
        "season_number": season,
        "episode_number": episode,
    }
    if operation == "set":
        payload["rating"] = desired
    provider.deliver("upsert" if operation == "set" else "remove", payload)
    verified = provider.read_episode_rating(series, season, episode)
    if verified != desired:
        raise PilotBlocked("verification mismatch")
    return verified


def _display(value: float | None) -> str:
    return "unrated" if value is None else f"{value:g}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One TMDb episode provider-isolation pilot")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--read-only", action="store_true")
    mode.add_argument("--set-rating", type=float)
    mode.add_argument("--remove", action="store_true")
    parser.add_argument("--series-id", type=int, required=True)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--episode", type=int, required=True)
    expectation = parser.add_mutually_exclusive_group()
    expectation.add_argument("--expect-current-rating", type=float)
    expectation.add_argument("--expect-current-unrated", action="store_true")
    parser.add_argument("--confirm-live-write", action="store_true")
    args = parser.parse_args(argv)

    expected = (None if args.expect_current_unrated else args.expect_current_rating
                if args.expect_current_rating is not None else _UNSET)
    operation = "read" if args.read_only else "set" if args.set_rating is not None else "remove"
    try:
        if operation == "read":
            if expected is not _UNSET or args.confirm_live_write:
                raise PilotBlocked("read-only guards invalid")
        else:
            if not args.confirm_live_write:
                raise PilotBlocked("confirmation required")
            if expected is _UNSET:
                raise PilotBlocked("expected state required")
            if expected is not None:
                _rating(expected)
            if operation == "set":
                _rating(args.set_rating)
            elif expected is None:
                raise PilotBlocked("remove requires rated state")
        from hub.providers.registry import get_provider
        provider = get_provider("tmdb")
        result = run_episode_pilot(
            provider,
            series_id=args.series_id,
            season_number=args.season,
            episode_number=args.episode,
            operation=operation,
            rating=args.set_rating,
            expected=expected,
            confirmed=args.confirm_live_write,
        )
        label = "current" if operation == "read" else "verified"
        print(f"TMDb episode rating {label}: {_display(result)}")
        return 0
    except Exception as exc:
        print(operator_failure(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
