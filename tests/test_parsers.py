from providers.mdblist import MDBListClient
from providers.simkl import SimklClient


def test_mdblist_movie_rating_parser():
    item = MDBListClient._parse_rating_row(
        {"rating": 9, "rated_at": "2026-08-31T10:00:00Z", "movie": {"title": "Test", "ids": {"tmdb": 603, "imdb": "tt0133093"}}},
        "movie",
        "movie",
    )
    assert item is not None
    assert item.key == "movie:tmdb:603"
    assert item.rating == 9
    assert item.imdb_id == "tt0133093"


def test_simkl_rating_parser_accepts_user_rating():
    item = SimklClient._parse_rating_row(
        {"user_rating": 8, "show": {"title": "Test Show", "ids": {"tmdb": 1399}}},
        "show",
        "show",
    )
    assert item is not None
    assert item.key == "show:tmdb:1399"
    assert item.rating == 8
