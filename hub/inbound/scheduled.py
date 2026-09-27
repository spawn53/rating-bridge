"""One poll and optional guarded apply under an OS lock; systemd owns recurrence."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from typing import Callable, Iterator

from hub.inbound.models import InboundError, Snapshot, validate_media_type
from hub.inbound.storage import InboundStore


@contextmanager
def scheduled_lock(db_path: str) -> Iterator[bool]:
    """Never unlink the lock inode; process exit releases its kernel-owned lock."""
    lock_path = Path(db_path).expanduser().resolve().parent / "trakt-inbound.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise InboundError("Scheduled observer lock is invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class ScheduledManyError(InboundError):
    """Earlier media commits remain authoritative; no later media are run."""

    def __init__(self, result: dict, failed_media: str):
        super().__init__("Trakt scheduled media processing failed")
        self.result = {**result, "failed_media": failed_media}


def _scheduled_observe_unlocked(db_path: str, read: Callable[[], Snapshot], *,
                                media_type: str, auto_apply_enabled: bool,
                                max_events: int, echo_grace_seconds: int,
                                targets: tuple[str, ...]) -> dict:
    """Execute one medium only while the caller owns the shared invocation lock."""
    store = InboundStore(db_path, initialize=False, media_type=media_type)
    # Existing observe enforces baseline, snapshot media and atomic generation CAS.
    from hub.inbound.trakt import observe
    result = observe(store, read)
    from hub.inbound.auto_apply import auto_apply, counters, AutoApplyError
    try:
        applied = (auto_apply(store, targets, generation=result["generation"],
                              max_events=max_events, echo_grace_seconds=echo_grace_seconds)
                   if auto_apply_enabled is True else counters(False))
    except AutoApplyError as exc:
        raise AutoApplyError({**result, **exc.result, "skipped_overlap": False}) from None
    return {**result, **applied, "skipped_overlap": False}


def scheduled_observe(db_path: str, read: Callable[[], Snapshot], *, enabled: bool,
                      auto_apply_enabled: bool = False, max_events: int = 10,
                      echo_grace_seconds: int = 600,
                      media_type: str = "movie",
                      targets: tuple[str, ...] = ("tmdb", "trakt", "simkl", "mdblist")) -> dict:
    media_type = validate_media_type(media_type)
    if enabled is not True:
        raise InboundError("Scheduled observation requires inbound enabled")
    with scheduled_lock(db_path) as acquired:
        if not acquired:
            return {"skipped_overlap": True, "canonical_mutations": 0, "provider_writes": 0}
        return _scheduled_observe_unlocked(
            db_path, read, media_type=media_type, auto_apply_enabled=auto_apply_enabled,
            max_events=max_events, echo_grace_seconds=echo_grace_seconds, targets=targets,
        )


def _many_result(results: dict[str, dict]) -> dict:
    return {"media_results": dict(results), "skipped_overlap": False,
            "canonical_mutations": sum(r["canonical_mutations"] for r in results.values()),
            "provider_writes": sum(r["provider_writes"] for r in results.values())}


def scheduled_observe_many(db_path: str, read: Callable[[str], Snapshot], *, enabled: bool,
                           media_types: tuple[str, ...] = ("movie",),
                           auto_apply_enabled: bool = False, max_events: int = 10,
                           echo_grace_seconds: int = 600,
                           targets: tuple[str, ...] = ("tmdb", "trakt", "simkl", "mdblist")) -> dict:
    """One flock, ordered movie then show; failure retains earlier commits.

    Movie-only retains the single-media result and exception contract. Limits
    apply separately to each medium; overlap skips the entire invocation.
    """
    if type(media_types) is not tuple or media_types not in (("movie",), ("movie", "show")):
        raise InboundError("Scheduled media must be movie or movie,show in canonical order")
    if enabled is not True:
        raise InboundError("Scheduled observation requires inbound enabled")
    with scheduled_lock(db_path) as acquired:
        if not acquired:
            return {"skipped_overlap": True, "canonical_mutations": 0, "provider_writes": 0}
        results = {}
        for media in media_types:
            try:
                result = _scheduled_observe_unlocked(
                    db_path, lambda: read(media), media_type=media,
                    auto_apply_enabled=auto_apply_enabled, max_events=max_events,
                    echo_grace_seconds=echo_grace_seconds, targets=targets,
                )
            except Exception as exc:
                if media_types == ("movie",):
                    raise
                from hub.inbound.auto_apply import AutoApplyError
                if isinstance(exc, AutoApplyError):
                    results[media] = exc.result
                raise ScheduledManyError(_many_result(results), media) from None
            results[media] = result
        return results["movie"] if media_types == ("movie",) else _many_result(results)
