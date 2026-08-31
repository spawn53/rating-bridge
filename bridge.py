from __future__ import annotations

from config import Config
from core.diff import build_plan
from providers.imdb import IMDbClient
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
        print("DRY_RUN=true: no Simkl ratings were changed.")
    else:
        successful_upserts = simkl.upsert_ratings(plan.upserts, cfg.batch_size)
        print(f"Applied to Simkl: {len(successful_upserts)} upserts")

    if not cfg.imdb_enabled:
        print("IMDb sync: disabled")
        return 0

    imdb_items = [item for item in source.values() if item.imdb_id]
    missing_imdb_id = len(source) - len(imdb_items)
    imdb = IMDbClient(cfg.imdb_cookie)
    current_imdb = imdb.get_user_ratings(item.imdb_id for item in imdb_items if item.imdb_id)
    imdb_upserts = [
        item
        for item in imdb_items
        if item.imdb_id and current_imdb.get(item.imdb_id) != item.rating
    ]
    imdb_equal = len(imdb_items) - len(imdb_upserts)

    print(f"IMDb eligible movie/show ratings: {len(imdb_items)}")
    print(f"IMDb missing source IMDb IDs: {missing_imdb_id}")
    print(f"IMDb already equal: {imdb_equal}")
    print(f"IMDb would upsert: {len(imdb_upserts)}")
    print("IMDb removals: disabled")

    if cfg.imdb_dry_run:
        print("IMDB_DRY_RUN=true: no IMDb ratings were changed.")
        return 0

    if len(imdb_upserts) > cfg.imdb_max_writes:
        raise RuntimeError(
            "IMDb safety stop: planned writes "
            f"({len(imdb_upserts)}) exceed IMDB_MAX_WRITES ({cfg.imdb_max_writes})."
        )

    applied_imdb = imdb.upsert_ratings(imdb_upserts)
    print(f"Applied to IMDb: {len(applied_imdb)} upserts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
