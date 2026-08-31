from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MediaType = Literal["movie", "show"]


@dataclass(frozen=True)
class RatingItem:
    media_type: MediaType
    tmdb_id: int
    rating: int
    title: str | None = None
    imdb_id: str | None = None
    rated_at: str | None = None

    @property
    def key(self) -> str:
        return f"{self.media_type}:tmdb:{self.tmdb_id}"


def clamp_rating(value: object) -> int | None:
    try:
        rating = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return rating if 1 <= rating <= 10 else None
