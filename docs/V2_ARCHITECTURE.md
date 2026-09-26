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

## Phase 5A — Trakt inbound observer

Outbound is **Hub canonical → providers**. Inbound is **provider watcher →
canonical candidate**. The inbound tables are independent of canonical ratings
and the transactional outbox. Baseline and observer commands remain **OBSERVE
ONLY**, movies only, with manual baseline and manual polling. Phase 5C adds an
explicitly confirmed importer for one persisted event. There are no recurring
Compose services, timers, bulk imports, or automatic applications.

Use the existing authenticated Trakt account to fetch all movie-rating pages:

```bash
python -m hub.inbound.trakt --baseline
python -m hub.inbound.trakt --once --observe-only
```

An explicit first baseline is a watermark: existing ratings produce zero events.
A subsequent baseline is refused unless `--baseline --reset` is explicitly
requested. Reset changes the watermark and trusted snapshot but preserves the
event audit. Missing TMDb mappings are validated, counted and stored separately
in `inbound_unmapped`; they cannot generate canonical candidates.

A complete snapshot validates every item, pagination header and item count,
normalizes timezone-aware timestamps to UTC, and rejects duplicate movie keys.
One bounded deadline covers every page. Publication of the snapshot, its state
generation and detected events uses a single SQLite transaction. Generation
checks reject concurrent stale polls; failed polls retain the prior snapshot
and create no partial events. Event fingerprints include the occurrence
generation, retaining genuine repeated score cycles while restart/replay of a
published snapshot produces no duplicate events. A timestamp-only change
updates the trusted snapshot without generating a rating-change event.

The pure classifier labels same-as-canonical scores and matching completed
Trakt outbound jobs as echo candidates, defers pending/processing/failed Trakt
jobs, and identifies differing ratings as candidates for the guarded manual importer.
These hints do not prove user intent and are not authorization to import.
Baseline-era removals without an active canonical rating cannot manufacture
canonical tombstones or provider remove jobs.

The loop rule for any applied event is: **the origin provider is never an
outbound target for that imported event**. With normal production targets
`tmdb,trakt,simkl,mdblist`, Trakt-originated events use
`targets_excluding_source(settings.targets, "trakt")`, yielding
`tmdb,simkl,mdblist`. Ordinary Nuvio writes retain all four targets.

`TRAKT_INBOUND_ENABLED=false`, `TRAKT_INBOUND_MEDIA_TYPES=movie` and
`TRAKT_INBOUND_POLL_SECONDS=300` are safe source defaults. Explicit manual
commands work while automatic inbound is disabled. Enabling that flag
does not install a service or automatically apply anything. The event table
supports `observed`, `ignored`, and `applied`, with `applied_at` and
`canonical_revision` for successful imports. Its transactional migration
preserves every existing event field, ID, fingerprint, and AUTOINCREMENT
sequence; the snapshot and generation tables are unchanged. Tokens, titles,
complete histories and raw provider bodies are excluded from logs and inbound
audit metadata.


### Phase 5C: explicitly import one observed Trakt movie event

```bash
python -m hub.inbound.trakt --apply-event 1 \
  --expect-content-key movie:tmdb:265189 --expect-rating 8 \
  --expect-generation 4 --expect-canonical-revision 5 \
  --confirm-live-import
```

The generation and canonical revision are required operator expectations. All
expectations and the explicit confirmation must be supplied; they cannot be
mixed with baseline/reset/observer flags. Removed events are unsupported.
The importer performs no HTTP/provider calls. Normal outbox workers deliver
exactly `tmdb,simkl,mdblist`; global targets still include Trakt.

Under `BEGIN IMMEDIATE`, the importer revalidates the exact persisted event,
current generation, matching score/timestamp/movie identity in the trusted
snapshot, absence of superseding events, canonical revision/state, and current
Trakt outbound classification. Pending/processing/failed Trakt jobs refuse
import. An added event requires absent or tombstoned canonical state; a changed
event requires the active canonical score to equal the event's old score.
No candidate classification alone can bypass these checks.

The existing canonical upsert runs inside that same write transaction and
commits one revision with three unique delivery jobs. Source provenance is
`trakt-inbound:<event_id>` and the provider timestamp is retained. After commit,
a separate transaction marks the event applied. If the process stops in that
gap, replay recognizes only the exact provenance, revision, identities,
timestamp and full three-job payload audit; it marks the original revision
applied without another canonical revision or job. An already-applied event
returns its audited original revision without changing later canonical state.
Incompatible state refuses replay. Failed downstream delivery leaves canonical
state and outbox convergence intact; the importer does not undo user ratings.
