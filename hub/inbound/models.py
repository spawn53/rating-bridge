from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re


class InboundError(RuntimeError):
    """Fixed, credential-free observer failure."""


def timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise InboundError("Trakt rating timestamp was invalid")
    try:
        # Require a full ISO date-time with an explicit timezone.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        raise InboundError("Trakt rating timestamp was invalid") from None


@dataclass(frozen=True)
class MovieRating:
    rating: int
    rated_at: str
    tmdb_id: int | None = None
    trakt_id: int | None = None
    imdb_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.rating) is not int or not 1 <= self.rating <= 10:
            raise InboundError("Trakt rating must be an integer from 1 to 10")
        for value in (self.tmdb_id, self.trakt_id):
            if value is not None and (type(value) is not int or value <= 0):
                raise InboundError(f"Trakt {self.media_type} ID was invalid")
        if self.imdb_id is not None and (
            not isinstance(self.imdb_id, str)
            or not re.fullmatch(r"tt[0-9]{7,}", self.imdb_id)
        ):
            raise InboundError("Trakt IMDb ID was invalid")
        object.__setattr__(self, "rated_at", timestamp(self.rated_at))

    @property
    def media_type(self) -> str:
        return "movie"

    @property
    def content_key(self) -> str | None:
        return f"{self.media_type}:tmdb:{self.tmdb_id}" if self.tmdb_id is not None else None


@dataclass(frozen=True)
class ShowRating(MovieRating):
    @property
    def media_type(self) -> str:
        return "show"


def validate_media_type(value: str) -> str:
    if value not in ("movie", "show"):
        raise InboundError("Trakt inbound media type must be movie or show")
    return value


def normalize(item: object, *, media_type: str = "movie") -> MovieRating:
    validate_media_type(media_type)
    if (not isinstance(item, dict) or not isinstance(item.get(media_type), dict)
            or any(name in item for name in ("movie", "show", "episode", "season") if name != media_type)):
        raise InboundError("Trakt rating item was malformed")
    ids = item[media_type].get("ids")
    if not isinstance(ids, dict) or not ids:
        raise InboundError("Trakt rating item IDs were malformed")
    model = MovieRating if media_type == "movie" else ShowRating
    return model(
        rating=item.get("rating"),
        rated_at=item.get("rated_at"),
        tmdb_id=ids.get("tmdb"),
        trakt_id=ids.get("trakt"),
        imdb_id=ids.get("imdb"),
    )


@dataclass(frozen=True)
class Snapshot:
    eligible: tuple[MovieRating, ...]
    unmapped: tuple[MovieRating, ...] = ()
    media_type: str = "movie"

    def __post_init__(self) -> None:
        validate_media_type(self.media_type)
        if any(not isinstance(r, MovieRating) or r.media_type != self.media_type or r.tmdb_id is None for r in self.eligible):
            raise InboundError(f"Trakt snapshot contained an ineligible {self.media_type}")
        if any(not isinstance(r, MovieRating) or r.media_type != self.media_type or r.tmdb_id is not None for r in self.unmapped):
            raise InboundError(f"Trakt snapshot contained an invalid unmapped {self.media_type}")
        keys = [r.content_key for r in self.eligible]
        if len(keys) != len(set(keys)):
            raise InboundError(f"Trakt snapshot contained duplicate {self.media_type} identities")
        object.__setattr__(self, "eligible", tuple(sorted(self.eligible, key=lambda r: r.tmdb_id)))
        object.__setattr__(self, "unmapped", tuple(sorted(
            self.unmapped, key=lambda r: json.dumps(asdict(r), sort_keys=True)
        )))

    @property
    def snapshot_hash(self) -> str:
        data = {"eligible": [asdict(r) for r in self.eligible],
                "unmapped": [asdict(r) for r in self.unmapped]}
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def movies(self) -> int:
        return len(self.eligible) + len(self.unmapped)
