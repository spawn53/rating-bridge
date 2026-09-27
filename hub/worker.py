from __future__ import annotations

import os
import time
from collections.abc import Callable

from hub.providers.base import (
    ProviderNotConfigured, RatingProvider, UnsupportedDelivery,
)
from hub.providers.registry import get_provider
from hub.settings import HubSettings
from hub.store import RatingStore


def process_one(
    store: RatingStore,
    provider_factory: Callable[[str], RatingProvider] = get_provider,
) -> bool:
    """Process one claim; provider_factory allows an in-memory test provider."""
    job = store.claim_next_job()
    if job is None:
        return False
    job_id, attempts = int(job['id']), int(job['attempts'])
    if not store.is_current_job(job_id, attempts):
        store.supersede_job(job_id, attempts)
        return True

    try:
        provider = provider_factory(str(job['target']))
        # Recheck immediately before delivery; a newer canonical revision may
        # have been committed while the provider was being constructed.
        if not store.is_current_job(job_id, attempts):
            store.supersede_job(job_id, attempts)
            return True
        provider.deliver(str(job['action']), job['payload'])
    except (UnsupportedDelivery, ProviderNotConfigured) as exc:
        store.fail_job(job_id, type(exc).__name__, permanent=True, attempts=attempts)
    except Exception as exc:
        # Exception text can echo an access token or a provider response body.
        # Store/log only the exception class, which still identifies the failure.
        store.fail_job(job_id, type(exc).__name__, permanent=False, attempts=attempts)
    else:
        store.complete_job(job_id, attempts=attempts)
    return True


def main() -> int:
    settings = HubSettings.from_env()
    store = RatingStore(settings.db_path)
    idle_seconds = max(0.25, float(os.getenv('RATING_HUB_WORKER_IDLE', '2')))
    store.requeue_stale_processing()

    print('rating-hub worker: started')
    while True:
        if not process_one(store):
            # Recover a crashed peer even when this worker keeps running.
            store.requeue_stale_processing()
            time.sleep(idle_seconds)


if __name__ == '__main__':
    raise SystemExit(main())
