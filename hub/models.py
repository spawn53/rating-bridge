from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

MediaType = Literal["movie", "show", "episode"]
TargetService = Literal["mdblist", "trakt", "simkl", "tmdb", "imdb", "letterboxd"]


class RatingWrite(BaseModel):
    """Canonical rating command received from Nuvio or another trusted client."""

    media_type: MediaType
    rating: int = Field(ge=1, le=10)

    # tmdb_id is the ID of the rated entity itself. For an episode this is the
    # TMDb episode ID when available; tmdb_series_id identifies its parent show.
    tmdb_id: int | None = Field(default=None, gt=0)
    tmdb_series_id: int | None = Field(default=None, gt=0)
    season_number: int | None = Field(default=None, ge=0)
    episode_number: int | None = Field(default=None, gt=0)

    imdb_id: str | None = None
    trakt_id: int | None = Field(default=None, gt=0)
    mdblist_id: str | None = None
    title: str | None = None

    rated_at: datetime | None = None
    source: str = "nuvio"
    targets: list[TargetService] | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> "RatingWrite":
        if self.media_type == "episode":
            if (
                self.tmdb_series_id is None
                or self.season_number is None
                or self.episode_number is None
            ):
                raise ValueError(
                    "episode ratings require tmdb_series_id, season_number and episode_number"
                )
            return self

        if self.tmdb_id is None and not self.imdb_id:
            raise ValueError("movie/show ratings require tmdb_id or imdb_id")
        return self

    @property
    def content_key(self) -> str:
        if self.media_type == "episode":
            return (
                f"episode:tmdb:{self.tmdb_series_id}:"
                f"s{self.season_number}:e{self.episode_number}"
            )
        if self.tmdb_id is not None:
            return f"{self.media_type}:tmdb:{self.tmdb_id}"
        return f"{self.media_type}:imdb:{self.imdb_id}"


class RatingResponse(BaseModel):
    content_key: str
    media_type: MediaType
    rating: int
    revision: int
    queued_targets: list[str]
    rated_at: str
    updated_at: str


class DeleteResponse(BaseModel):
    content_key: str
    removed: bool
    revision: int | None = None
    queued_targets: list[str] = []
