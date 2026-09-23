# Rating Hub V2 architecture

## Goal

Nuvio should be the place where a rating is entered. The VPS-hosted Rating Hub is
the canonical source of truth and fans that rating out to external services.

```text
Nuvio UI
   |
   v
Rating Hub API (VPS)
   |
   +-- SQLite canonical rating state
   +-- transactional outbox
           |
           +--> MDBList
           +--> Trakt
           +--> Simkl
           +--> TMDb
           +--> IMDb       (experimental)
           +--> Letterboxd (experimental)
```

The hub, not an external provider, owns the user's canonical rating. This avoids
provider-to-provider loops and keeps one provider outage from blocking the rest.

## Canonical rating scale

The hub stores an integer score from 1 through 10. Provider adapters are
responsible for converting that value only when a service uses a different
scale.

## Canonical identities

Movies and shows prefer their TMDb ID and retain IMDb/Trakt/MDBList IDs when
known.

Episodes use the parent TMDb series ID plus season and episode number as the
stable hub key:

```text
episode:tmdb:<series_id>:s<season>:e<episode>
```

The TMDb episode ID and IMDb episode ID can additionally be stored for provider
mapping.

## Delivery semantics

Every write updates the canonical rating row and creates one outbox job per
target in the same SQLite transaction. A future worker will claim these jobs and
perform idempotent provider writes with retry/backoff. Failed providers therefore
do not roll back or lose the user's rating.

Rating removal is represented as a tombstone plus `remove` outbox jobs so it can
be propagated safely.

## Phase plan

### Phase 1 — foundation (this branch)

- FastAPI rating API
- API-key authentication
- movie/show/episode canonical model
- SQLite WAL source of truth
- transactional outbox
- Docker/VPS deployment definition

### Phase 2 — official/stable provider adapters

- MDBList
- Trakt
- TMDb
- Simkl (movie/show first; verify episode-rating support before enabling it)

Each adapter must have read/compare/write tests and must not block other targets.

### Phase 3 — Nuvio client

Add a rating action to movie/show detail pages and an episode rating action to
the episode UI or post-play flow. Nuvio sends one request only: to Rating Hub.

### Phase 4 — experimental providers

- IMDb V2 is implemented as an isolated private-web GraphQL adapter behind
  `IMDB_V2_ENABLED`. It supports movie/show/episode writes when a canonical
  IMDb `tt...` ID is present. Both upsert and remove mutations are implemented.
  Live writes remain disabled by default.
- Letterboxd is implemented using OAuth2 refresh tokens and the documented
  `PATCH /me/rate/{id}` endpoint. The hub converts 1–10 to 0.5–5.0 exactly,
  with no rounding loss. Live writes remain disabled by default.
- Letterboxd's current API models Film, Show, Season and Episode as Production
  types and the rating endpoint accepts a generic rateable object. V2 still
  enables movie delivery only until production-ID resolution for TV/episodes
  is verified against a live account.
- Letterboxd documents that setting a rating also marks that production watched.
  This is expected provider behavior and must be considered during preflight.

## Security

- The public repository contains no credentials.
- Provider tokens/cookies live only on the VPS.
- Rating Hub is bound to localhost by default in Compose and should be exposed
  only through the existing authenticated/reverse-proxy setup.
- IMDb cookies, if used, are treated as password-equivalent secrets.
