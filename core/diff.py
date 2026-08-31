from __future__ import annotations

from dataclasses import dataclass, field

from core.models import RatingItem


@dataclass
class SyncPlan:
    upserts: list[RatingItem] = field(default_factory=list)
    skipped_unwatched: list[RatingItem] = field(default_factory=list)
    already_equal: list[RatingItem] = field(default_factory=list)


def build_plan(
    source: dict[str, RatingItem],
    target: dict[str, RatingItem],
    target_watched: set[str],
) -> SyncPlan:
    """Build a safe one-way MDBList -> Simkl plan.

    Public-repository V1 is deliberately stateless: it only adds/updates ratings.
    Rating removals are not propagated, because tracking bridge ownership would
    require persisting personal rating history somewhere private.
    """
    plan = SyncPlan()

    for key, item in source.items():
        target_item = target.get(key)
        if target_item and target_item.rating == item.rating:
            plan.already_equal.append(item)
            continue
        if key not in target_watched:
            plan.skipped_unwatched.append(item)
            continue
        plan.upserts.append(item)

    return plan
