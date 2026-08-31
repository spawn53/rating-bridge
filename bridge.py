from __future__ import annotations

from config import Config
from core.diff import build_plan
from core.models import RatingItem
from providers.mdblist import MDBListClient
from providers.simkl import SimklClient


def _label(item: RatingItem) -> str:
    return f"{item.title or item.key} [{item.media_type} tmdb:{item.tmdb_id}] = {item.rating}/10"


def main() -> int:
    cfg = Config.from_env()

    print(f"rating-bridge: starting (dry_run={cfg.dry_run})")
    source = MDBListClient(cfg.mdblist_api_key).get_ratings()
    simkl = SimklClient(cfg.simkl_client_id, cfg.simkl_access_token)
    simkl.validate()
    target = simkl.get_ratings()
    watched = simkl.get_watched_keys()

    plan = build_plan(source, target, watched)

    print(f"MDBList ratings: {len(source)}")
    print(f"Simkl ratings: {len(target)}")
    print(f"Simkl watched guards: {len(watched)}")
    print(f"Would upsert: {len(plan.upserts)}")
    print(f"Already equal: {len(plan.already_equal)}")
    print(f"Skipped until Simkl has watch activity: {len(plan.skipped_unwatched)}")
    print("Rating removals: disabled in public/stateless V1")

    for item in plan.upserts[:25]:
        print(f"  UPSERT  {_label(item)}")
    for item in plan.skipped_unwatched[:10]:
        print(f"  WAIT    {_label(item)}")

    if cfg.dry_run:
        print("DRY_RUN=true: no ratings were changed.")
        return 0

    successful_upserts = simkl.upsert_ratings(plan.upserts, cfg.batch_size)
    print(f"Applied: {len(successful_upserts)} upserts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
