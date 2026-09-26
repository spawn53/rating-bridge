"""One observe-only cycle with an OS lock. Recurrence belongs to systemd."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from typing import Callable, Iterator

from hub.inbound.models import InboundError, Snapshot
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


def scheduled_observe(db_path: str, read: Callable[[], Snapshot], *, enabled: bool) -> dict:
    if enabled is not True:
        raise InboundError("Scheduled observation requires inbound enabled")
    with scheduled_lock(db_path) as acquired:
        if not acquired:
            return {"skipped_overlap": True, "canonical_mutations": 0, "provider_writes": 0}
        store = InboundStore(db_path, initialize=False)
        # Existing observe enforces a trusted baseline and atomic generation CAS.
        from hub.inbound.trakt import observe
        result = observe(store, read)
        return {**result, "skipped_overlap": False}
