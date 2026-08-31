from __future__ import annotations

from config import Config
from core.diff import build_plan
from providers.mdblist import MDBListClient
from providers.simkl import SimklClient


def main() -> int:
    cfg = Config.from_env()

    print(f"rating-bridge: starting (dry_run={cfg.dry_run})")
    mdblist = MDBListClient(cfg.mdblist_api_key)
    source = mdblist.get_ratings()
    simkl = SimklClient(cfg.simkl_client_id, cfg.simkl_access_token)
    simkl.validate()
    target = simkl.get_ratings()
    watched = simkl.get_watched_keys()

    plan = build_plan(source, target, watched)

    print(f"MDBList movie/show ratings used for Simkl: {len(source)}")
    print(
        "MDBList rating buckets: "
        f"movies={mdblist.last_rating_counts['movies']} "
        f"shows={mdblist.last_rating_counts['shows']} "
        f"seasons={mdblist.last_rating_counts['seasons']} "
        f"episodes={mdblist.last_rating_counts['episodes']}"
    )
    if mdblist.last_rating_counts["episodes"]:
        print("Episode ratings detected in MDBList; they are diagnostic-only and are not sent to Simkl.")
    print(f"Simkl ratings: {len(target)}")
    print(f"Simkl watched guards: {len(watched)}")
    print(f"Would upsert: {len(plan.upserts)}")
    print(f"Already equal: {len(plan.already_equal)}")
    print(f"Skipped until Simkl has watch activity: {len(plan.skipped_unwatched)}")
    print("Rating removals: disabled in public/stateless V1")

    if cfg.dry_run:
        print("DRY_RUN=true: no ratings were changed.")
        return 0

    successful_upserts = simkl.upsert_ratings(plan.upserts, cfg.batch_size)
    print(f"Applied: {len(successful_upserts)} upserts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
