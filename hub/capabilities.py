from __future__ import annotations

from collections.abc import Iterable

CAPABILITIES: dict[str, frozenset[str]] = {
    "mdblist": frozenset({"movie", "show"}),
    "trakt": frozenset({"movie", "show", "episode"}),
    "simkl": frozenset({"movie", "show"}),
    "tmdb": frozenset({"movie", "show", "episode"}),
    "imdb": frozenset({"movie", "show", "episode"}),
    "letterboxd": frozenset({"movie"}),
}


def split_supported(
    media_type: str, targets: Iterable[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    supported: list[str] = []
    skipped: list[str] = []
    for target in dict.fromkeys(targets):
        if media_type in CAPABILITIES.get(target, frozenset()):
            supported.append(target)
        else:
            skipped.append(target)
    return tuple(supported), tuple(skipped)
