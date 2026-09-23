from __future__ import annotations

import os
import time

from hub.providers.base import ProviderNotConfigured, UnsupportedDelivery
from hub.providers.registry import get_provider
from hub.settings import HubSettings
from hub.store import RatingStore


def main() -> int:
    settings = HubSettings.from_env()
    store = RatingStore(settings.db_path)
    idle_seconds = max(0.25, float(os.getenv("RATING_HUB_WORKER_IDLE", "2")))
    store.requeue_stale_processing()

    print("rating-hub worker: started")
    while True:
        job = store.claim_next_job()
        if job is None:
            time.sleep(idle_seconds)
            continue

        try:
            provider = get_provider(str(job["target"]))
            provider.deliver(str(job["action"]), job["payload"])
        except UnsupportedDelivery as exc:
            store.fail_job(int(job["id"]), str(exc), permanent=True)
            print(f"job {job['id']} permanently skipped: {exc}")
        except ProviderNotConfigured as exc:
            store.fail_job(int(job["id"]), str(exc), permanent=True)
            print(f"job {job['id']} provider not configured: {exc}")
        except Exception as exc:
            status = store.fail_job(int(job["id"]), str(exc), permanent=False)
            print(f"job {job['id']} delivery error ({status}): {exc}")
        else:
            store.complete_job(int(job["id"]))
            print(
                f"job {job['id']} delivered "
                f"{job['action']} -> {job['target']} ({job['content_key']})"
            )


if __name__ == "__main__":
    raise SystemExit(main())
