from __future__ import annotations

import os
from dataclasses import dataclass

ALLOWED_TARGETS = {"mdblist", "trakt", "simkl", "tmdb", "imdb", "letterboxd"}


def _targets(value: str) -> tuple[str, ...]:
    requested = [part.strip().lower() for part in value.split(",") if part.strip()]
    unknown = sorted(set(requested) - ALLOWED_TARGETS)
    if unknown:
        raise RuntimeError(f"Unknown RATING_HUB_TARGETS: {', '.join(unknown)}")
    return tuple(dict.fromkeys(requested))


@dataclass(frozen=True)
class HubSettings:
    db_path: str
    api_key: str
    targets: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "HubSettings":
        return cls(
            db_path=os.getenv("RATING_HUB_DB", "./data/rating-hub.sqlite3").strip(),
            api_key=os.getenv("RATING_HUB_API_KEY", "").strip(),
            targets=_targets(
                os.getenv("RATING_HUB_TARGETS", "mdblist,trakt,simkl,tmdb")
            ),
        )
