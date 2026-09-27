"""Strict episode observation models, separate from movie/show import models."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re

from hub.inbound.models import InboundError, timestamp


def _id(value: object) -> None:
    if value is not None and (type(value) is not int or value <= 0):
        raise InboundError("Trakt episode ID was invalid")


def _imdb(value: object) -> None:
    if value is not None and (not isinstance(value, str)
                              or not re.fullmatch(r"tt[0-9]{7,}", value)):
        raise InboundError("Trakt episode IMDb ID was invalid")


@dataclass(frozen=True)
class EpisodeRating:
    rating: int
    rated_at: str
    tmdb_series_id: int | None
    season_number: int
    episode_number: int
    trakt_id: int | None = None
    imdb_id: str | None = None
    # The episode's own TMDb ID is metadata, never its series identity.
    tmdb_id: int | None = None

    def __post_init__(self) -> None:
        if type(self.rating) is not int or not 1 <= self.rating <= 10:
            raise InboundError("Trakt episode rating must be an integer from 1 to 10")
        for value in (self.tmdb_series_id, self.tmdb_id, self.trakt_id):
            _id(value)
        _imdb(self.imdb_id)
        if (type(self.season_number) is not int or self.season_number < 0
                or type(self.episode_number) is not int or self.episode_number <= 0):
            raise InboundError("Trakt episode coordinates were invalid")
        object.__setattr__(self, "rated_at", timestamp(self.rated_at))

    @property
    def media_type(self) -> str:
        return "episode"

    @property
    def content_key(self) -> str | None:
        if self.tmdb_series_id is None:
            return None
        return (f"episode:tmdb:{self.tmdb_series_id}:"
                f"s{self.season_number}:e{self.episode_number}")


def normalize_episode(item: object) -> EpisodeRating:
    if (not isinstance(item, dict) or not isinstance(item.get("episode"), dict)
            or not isinstance(item.get("show"), dict)
            or any(name in item for name in ("movie", "season"))
            or "type" in item and item["type"] != "episode"):
        raise InboundError("Trakt episode rating item was malformed")
    episode, show = item["episode"], item["show"]
    ids, series_ids = episode.get("ids"), show.get("ids")
    if not isinstance(ids, dict) or not ids or not isinstance(series_ids, dict) or not series_ids:
        raise InboundError("Trakt episode rating IDs were malformed")
    # Validate supplied parent metadata without promoting it to episode IDs.
    for field in ("tmdb", "trakt"):
        _id(series_ids.get(field))
    _imdb(series_ids.get("imdb"))
    return EpisodeRating(
        rating=item.get("rating"), rated_at=item.get("rated_at"),
        tmdb_series_id=series_ids.get("tmdb"),
        season_number=episode.get("season"), episode_number=episode.get("number"),
        trakt_id=ids.get("trakt"), imdb_id=ids.get("imdb"), tmdb_id=ids.get("tmdb"),
    )


@dataclass(frozen=True)
class EpisodeSnapshot:
    eligible: tuple[EpisodeRating, ...]
    unmapped: tuple[EpisodeRating, ...] = ()

    @property
    def media_type(self) -> str:
        return "episode"

    def __post_init__(self) -> None:
        if (any(not isinstance(r, EpisodeRating) or r.content_key is None for r in self.eligible)
                or any(not isinstance(r, EpisodeRating) or r.content_key is not None for r in self.unmapped)):
            raise InboundError("Trakt episode snapshot contained invalid mapping")
        keys = [r.content_key for r in self.eligible]
        if len(keys) != len(set(keys)):
            raise InboundError("Trakt episode snapshot contained duplicate identities")
        # Metadata may not assign the same episode to different coordinates,
        # including conflicts between mapped and unmapped rows/pages.
        for field in ("trakt_id", "imdb_id", "tmdb_id"):
            values = [getattr(r, field) for r in (*self.eligible, *self.unmapped)
                      if getattr(r, field) is not None]
            if len(values) != len(set(values)):
                raise InboundError("Trakt episode snapshot contained duplicate metadata identities")
        serialize = lambda r: json.dumps(asdict(r), sort_keys=True, separators=(",", ":"))
        unmapped = [serialize(r) for r in self.unmapped]
        if len(unmapped) != len(set(unmapped)):
            raise InboundError("Trakt episode snapshot contained duplicate unmapped identities")
        object.__setattr__(self, "eligible", tuple(sorted(
            self.eligible, key=lambda r: (r.tmdb_series_id, r.season_number, r.episode_number))))
        object.__setattr__(self, "unmapped", tuple(sorted(self.unmapped, key=serialize)))

    @property
    def snapshot_hash(self) -> str:
        data = {"media_type": "episode", "eligible": [asdict(r) for r in self.eligible],
                "unmapped": [asdict(r) for r in self.unmapped]}
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def observed_count(self) -> int:
        return len(self.eligible) + len(self.unmapped)
