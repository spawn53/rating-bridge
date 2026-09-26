# Phase 4A operator pilot

Run `scripts/live_pilot.py` manually for one approved movie and one provider at a time. It requires `--confirm-live-write`, a `movie:tmdb:<id>` content key, and valid local credentials. It reads the original personal rating, verifies two temporary ratings, and attempts to restore the original rating. The script never runs from the API or worker.

The MDBList pilot is gated by the same settle verifier as the other providers. Its published API documents GET /sync/ratings with cursor pagination; the read helper fails closed on unrecognized item shapes, invalid half-step ratings (using `rating_precise` when present), duplicate matches, cursor loops, and deadline exhaustion. Do not infer that a title is unrated from a metadata rating or an unverified response. An original half-step rating blocks the pilot before its first write because the canonical integer model cannot restore it exactly.

| Provider | Existing personal rating read | Write/remove | Rating scale | Guard |
| --- | --- | --- | --- | --- |
| TMDb | [Movie account states](https://developer.themoviedb.org/reference/movie-account-states) | [Add](https://developer.themoviedb.org/reference/movie-add-rating) / [delete](https://developer.themoviedb.org/reference/movie-delete-rating) | 0.5–10, half steps | Refuse movies on watchlist because an add rating can remove a watchlist entry. |
| Trakt | [User movie ratings](https://docs.trakt.tv/reference/getusersratingsmovies) | Existing adapter `/sync/ratings` and `/sync/ratings/remove` | 1–10 integer | Read every page; retain `rated_at` for restoration. |
| Simkl | [User ratings and library state](https://api.simkl.org/api-reference/simkl/get-user-ratings) | [Add](https://api.simkl.org/api-reference/simkl/add-ratings) / [remove](https://api.simkl.org/api-reference/simkl/remove-ratings) | 1–10 integer | Require the movie already in the library and verify its list status stays unchanged. |
| MDBList | [GET /sync/ratings](https://api.mdblist.com/schema/) with cursor pagination | Existing adapter `/sync/ratings` and `/sync/ratings/remove` | 1–10 in 0.5 steps for reads; pilot writes remain integers | Requires three stable reads for each upsert and a five-read, full-window rollback; original half-step ratings are ineligible. |

The production capability matrix remains unchanged: MDBList and Simkl support movie/show; Trakt and TMDb support movie/show/episode. This tool is movie-only. No episode or four-provider fan-out run should start until each provider's individual movie pilot succeeds and its original state can be restored. Before an API pilot, make a SQLite backup and choose a movie known to be unrated on all four providers if their original ratings differ.

The current adapters require at least one of IMDb/TMDb/Trakt/MDBList IDs for MDBList, one of IMDb/TMDb/Trakt for Trakt, and IMDb or TMDb for Simkl. TMDb requires its own movie or show ID; episodes require the TMDb series ID plus season and episode coordinates. MDBList and Simkl episode writes remain disabled. Those are code-level requirements, not a claim that the live endpoint contracts have been verified.

The tool sanitizes operator output. A failed verification stops further test ratings and attempts rollback. If rollback cannot be verified, it reports manual review is required. It does not promise recovery from a provider outage. IMDb and Letterboxd remain outside this phase.


MDBList terminal pagination accepts an explicit null cursor. When the cursor key
is omitted, fewer than 1000 movie rows are required; a full page remains
ambiguous. If the observed `has_more` flag is present it must be exactly false,
so a short movie collection cannot hide continuation for other media types.
An empty pagination object uses the authorized short-page fallback. Explicit
invalid cursor values never use that fallback. All pages are still validated
before absence or a unique target match is accepted.
