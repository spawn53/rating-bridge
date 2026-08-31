from core.diff import build_plan
from core.models import RatingItem


def r(kind: str, tmdb: int, rating: int) -> RatingItem:
    return RatingItem(media_type=kind, tmdb_id=tmdb, rating=rating)  # type: ignore[arg-type]


def test_missing_target_is_upserted_only_when_watched():
    source = {r("movie", 1, 8).key: r("movie", 1, 8), r("movie", 2, 7).key: r("movie", 2, 7)}
    plan = build_plan(source, {}, {"movie:tmdb:1"})
    assert [i.key for i in plan.upserts] == ["movie:tmdb:1"]
    assert [i.key for i in plan.skipped_unwatched] == ["movie:tmdb:2"]


def test_equal_target_is_not_written():
    item = r("show", 42, 9)
    plan = build_plan({item.key: item}, {item.key: item}, {item.key})
    assert not plan.upserts
    assert [i.key for i in plan.already_equal] == [item.key]


def test_stateless_v1_never_plans_removals():
    target_item = r("movie", 5, 7)
    plan = build_plan({}, {target_item.key: target_item}, {target_item.key})
    assert not plan.upserts
    assert not plan.skipped_unwatched
