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
Trakt outbound jobs as echo candidates and identifies differing ratings as
candidates for the guarded manual importer. Outbound evidence is scoped to the
current canonical revision: older jobs are history only, current unresolved
jobs defer, and jobs ahead of canonical fail closed. Canonical and job revisions
must be positive integers. Multiple current jobs are treated as ambiguous, and
malformed current completed audit also defers. Without a canonical revision,
existing outbound audit cannot safely establish causality.
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
mixed with baseline/reset/observer flags. This upsert mode refuses removed events; Phase 5H provides a separate removal mode.
The importer performs no HTTP/provider calls. Normal outbox workers deliver
exactly `tmdb,simkl,mdblist`; global targets still include Trakt.

Under `BEGIN IMMEDIATE`, the importer revalidates the exact persisted event,
current generation, matching score/timestamp/movie identity in the trusted
snapshot, absence of superseding events, canonical revision/state, and current
Trakt outbound classification. Current-revision pending/processing/failed Trakt jobs refuse
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


### Phase 5G: repair one historical-outbound echo classification

```bash
python -m hub.inbound.trakt --reclassify-event 3 \
  --expect-content-key movie:tmdb:265189 --expect-generation 6 \
  --expect-event-type removed --expect-old-rating 9 \
  --expect-canonical-revision 7 --confirm-reclassification
```

An old Trakt removal from revision 5 cannot explain a user removal while
canonical is active at revision 7. The same revision scope is used by importer
crash recovery; a historical unresolved job cannot block a later committed
inbound revision's audit completion.

The manual repair supports only Trakt movie removals with the known false
`ignored/echo/matches_latest_completed_trakt_job` signature. It requires valid
event identity/timestamps, the explicit expected key/generation/old score and
canonical revision, active canonical score matching the removal's old score,
absence in the trusted snapshot at that generation, no superseding event,
historical completed-removal evidence, and recomputation to exactly
`candidate/different_provider_state/delete`. A single write transaction holds
all checks through the update. Only status, classification, reason and future
action change; all identity/transition fields and the snapshot remain intact.
Repeat repair safely reports already reclassified after the same checks.

Reclassification has no HTTP, canonical, or outbox write path. It does not run
the observer, advance generation, reset a baseline, or import a removal. The
upsert importer still refuses removed events; the separately guarded removal
operation is described below. Automatic inbound remains disabled.

### Phase 5H: explicitly import one observed Trakt movie removal

```bash
python -m hub.inbound.trakt --apply-removal-event 3 \
  --expect-content-key movie:tmdb:265189 --expect-generation 6 \
  --expect-old-rating 9 --expect-canonical-revision 7 \
  --expect-canonical-source trakt-inbound:2 --confirm-live-import
```

This dedicated operation requires every expectation and explicit confirmation;
upsert, reclassification and observer modes cannot be combined with it. It
performs no HTTP or direct provider writes. Automatic inbound remains disabled,
and there is no bulk removal, automatic application, or scheduler.

Under one `BEGIN IMMEDIATE`, the importer verifies the persisted removal's
identity, fingerprint format, timestamps, candidate/delete classification,
unapplied audit, current trusted generation, snapshot absence, and absence of
any newer event. The active canonical movie must match the expected old score,
revision, source, TMDb identity and retained source rating timestamp. The fixed
revision-scoped classifier must return exactly
`candidate/different_provider_state/delete` before deletion. Reappearance in the
trusted snapshot, changed canonical state, or unsafe Trakt audit refuses import.

`RatingStore.delete_rating()` now accepts optional `source` and `connection`.
An external connection must already have a transaction; the method never
commits it. Without a connection, deletion retains its owned transaction.
Without a source, ordinary API deletion retains existing provenance. Both paths
queue payloads from the final canonical tombstone, including its new update
time. Canonical and outbox insertion failures roll back together.

For event 3, canonical revision 7 becomes revision 8 with `rating=NULL`,
`deleted=1` and `source=trakt-inbound:3`. Identity metadata and `rated_at` remain
unchanged because the snapshot contract supplies no deletion timestamp. Source
exclusion derives exactly `tmdb,simkl,mdblist` from unchanged global targets
`tmdb,trakt,simkl,mdblist`; exactly three remove jobs are committed with the
tombstone and no Trakt job is generated.

Event marking follows the canonical/outbox commit. Crash-gap replay requires
that same event and trusted absence, exact tombstone revision/provenance/movie
identity/retained timestamp, and exactly three remove jobs whose payloads equal
the complete tombstone. Corrupted audit or an extra Trakt job refuses recovery.
Successful recovery completes event audit without another delete or duplicate
job. Already-applied replay returns its original revision and empty queued
targets without changing later canonical state.

Live removal validation waits for the three jobs to finish and observes each
target for the full 120-second removal window, polling every five seconds,
requiring at least five final consecutive matches spanning at least 20 seconds.
TMDb authenticated rating surfaces must agree and watchlist remain false;
Simkl must remain uniquely present with its completed library status; MDBList
must be absent from ratings. A final read checks Trakt remains unrated. A failed
downstream delivery leaves the tombstone intact for outbox convergence.


### Phase 5I: recurring observe-only Trakt movie polling

`python -m hub.inbound.trakt --scheduled-observe` performs exactly one poll and
exits. It requires `TRAKT_INBOUND_ENABLED=true` and the existing trusted baseline
and current schema. It does not initialize/migrate tables, create/reset a
baseline, or automatically reclassify events. With auto-apply off it invokes no
importer. Safe source defaults remain disabled, movie-only and 300 seconds;
Phase 5J adds a separate opt-in application flag described below.

A nonblocking OS `flock` on the database directory's `trakt-inbound.lock`
(`/data/trakt-inbound.lock` in Compose, `/srv/data/rating-hub/trakt-inbound.lock`
on the VPS) covers baseline loading, complete fetch, validation and publication.
An overlap exits successfully with a sanitized skip and no database mutations.
The lock inode is retained; completion, exception or process exit releases the
kernel lock without stale PID cleanup. SQLite still protects manual imports.

Scheduled output contains only generation, change/application counts and mutation counts.
Failures use a fixed sanitized message and nonzero exit. Existing atomic
publication retains trusted state on malformed/incomplete/failed reads and
SQLite failure. Generation represents snapshot content; identical snapshots
update only poll metadata and retain snapshot rows and their observed times.
With auto-apply off, candidates remain observed; echoes/noops remain ignored.
Existing applied audit is untouched. OAuth refresh continues through the normal
provider lifecycle; secrets and response histories are never logged.

Repository-managed units are in `deploy/systemd/`. Install both files into
`/etc/systemd/system/` and run `sudo systemctl daemon-reload`. The oneshot runs as
`ubuntu` through the deployed Compose image and its existing environment; units
contain no credentials. Systemd enforces one active instance; the persistent
storage lock also protects direct overlapping CLI invocations. `Restart=no`
prevents failure loops, and a 120-second start timeout bounds the oneshot.

Before enabling the timer, take a mode-0600 SQLite API backup, set only
`TRAKT_INBOUND_ENABLED=true` in `/srv/stacks/rating-hub/.env.v2` while preserving
owner/mode and outbound targets, and validate one service start with
`sudo systemctl start rating-hub-trakt-inbound.service`. Compare deterministic
ratings/outbox hashes and event audit before/after. Then enable with
`sudo systemctl enable --now rating-hub-trakt-inbound.timer` and verify two real
timer activations, zero canonical/outbox/provider side effects and sanitized
journal output. To stop recurrence, disable/stop the timer; keep manual import
commands separate and operator-confirmed.

The timer uses `OnBootSec=2min`, `OnUnitActiveSec=5min`, `AccuracySec=1s` and
`Persistent=true`. Cadence is owned by the timer, not by an internal Python loop
or the configuration's poll-seconds value. Each activation performs one poll;
there is no missed-interval replay loop. `Persistent=true` affects calendar
timers; this monotonic timer instead gets one overdue boot activation after
startup, then resumes five-minute recurrence. Application remains off by default.


### Phase 5J: content versions and default-off guarded auto-apply

Identical non-baseline snapshots preserve generation, snapshot rows, event
fingerprints and waiting candidates. Only last-successful-poll time and counts
update. A different hash increments generation and atomically publishes content
and score deltas. Timestamp, identity and unmapped changes also version content,
without inventing score events. Explicit baseline/reset retains its versioning.
No existing generation or event is rewritten during deployment.

Defaults are `TRAKT_INBOUND_AUTO_APPLY=false`,
`TRAKT_INBOUND_AUTO_APPLY_MAX_EVENTS=10` (1..100), and
`TRAKT_INBOUND_ECHO_GRACE_SECONDS=600` (0..86400). Invalid booleans, noninteger
values or out-of-range numbers fail closed. The same scheduled command, oneshot,
timer and flock cover one fetch, publication and optional application. No second
observer read, service, lock, direct provider writer or settle loop is added.

Opt-in application selects only current-generation Trakt movie events with
observed/candidate/different-provider-state audit and upsert/delete intent.
The complete eligible count is checked before imports; exceeding the configured
limit applies none. Events process by ascending ID. A failure stops subsequent
events, retains previous commits, logs fixed sanitized counters and exits
nonzero. Ignored, echo, noop, defer, applied and stale-generation events are never
automatically reclassified or selected.

The engine derives exact expectations and calls the existing upsert/removal
importers with confirmation from the explicitly enabled scheduler. Added events
require absent/tombstoned canonical; changed and removed events require active
canonical matching the old score. Removals use current source/revision. All
identity, snapshot, transition, source exclusion and delivery-audit guards stay
authoritative; manual commands still require their explicit confirmations.
Imported events fan out only to `tmdb,simkl,mdblist` from unchanged four-provider
global targets. Workers deliver asynchronously; scheduler direct writes are zero.

A differing current-revision completed Trakt delivery must be at least the grace
interval old. Creation/completion timestamps must be full UTC ISO timestamps,
ordered consistently and not ahead of the UTC clock. Missing/malformed timing
fails closed; older-revision audit never invokes grace. A grace-held event keeps
its original status/classification and is counted as deferred. Automation-only
guards recheck the plan and grace inside the importer's canonical write
transaction, closing races after initial planning without weakening manual paths.

A crash before apply leaves a candidate current after an identical next poll.
A crash after canonical commit derives the original revision from deterministic
event provenance and delegates exact payload-audit recovery to the existing
importer. Removal recovery uses a nonauthorizing old-source sentinel: it can
complete the proven audit gap but cannot authorize a new delete if state changes.
No duplicate canonical revision/jobs are created. Counters include canonical
commits even when subsequent event-audit marking fails.

A source change before retry increments generation and excludes older candidates.
The newest event is considered only if its own canonical old-score guards pass.
If an intermediate score never reached canonical, the newest changed event may
require operator review; automation does not manufacture the missing state.

For Phase 5J live validation, stop (do not disable) the timer, take a mode-0600
SQLite API backup, deploy the tested commit, and keep auto-apply false. Validate
one unchanged manual scheduled cycle. With no candidates and timer still stopped,
validate exactly one temporary true-flag cycle, then immediately restore false.
Restart the same timer and verify one real observe-only activation, unchanged
generation/snapshot rows/canonical/outbox/events, and no provider rating writes.
Production must finish with observation enabled and automatic application off.

### Phase 6A: manual Trakt show observation with isolated state

Trakt movie observation, guarded upsert/removal import and scheduled auto-apply
are production enabled. The existing timer, oneshot and flock remain movie-only:
`TRAKT_INBOUND_MEDIA_TYPES=movie`, `TRAKT_INBOUND_AUTO_APPLY=true`, max events 10,
echo grace 600 seconds, and global targets `tmdb,trakt,simkl,mdblist`. Imported
movie events continue to exclude Trakt from their outbound jobs.

Trakt shows now support complete validated snapshots, a baseline and added,
changed and removed delta audit. Manual commands use the deployed OAuth lifecycle:

```bash
python -m hub.inbound.trakt --baseline --media-type show
python -m hub.inbound.trakt --once --observe-only --media-type show
```

The default media selection remains movie. Guarded manual show added/changed
imports use `--apply-event <id> --media-type show` with explicit key, rating,
generation, existing revision and `--confirm-live-import` expectations. The shared
movie/show importer commits canonical state and exactly three jobs (TMDb, Simkl,
MDBList), excludes Trakt, and recovers audit-gap crashes only from exact
provenance and canonical/outbox payload audit. Guarded manual removal supports
movies and shows with the same explicit content key, generation, old rating,
canonical revision/source and confirmation flags. It commits one tombstone and
exactly three removal jobs (TMDb, Simkl and MDBList), excludes Trakt, and retains
identity and the original provider rating timestamp. The new canonical update
timestamp and event application timestamp record the import; detection time is
unchanged. Repeating an applied event is idempotent, and recovery of an interrupted
audit requires the exact committed tombstone and payloads. Show reclassification,
auto-apply and scheduled polling remain disabled and fail closed.
Episode/season inbound observation is unsupported. Existing show ratings captured
by the baseline are historical source state: they create no events, canonical
ratings or outbox jobs and are never automatically backfilled.

Show identities use `show:tmdb:<id>`; movie identities retain `movie:tmdb:<id>`.
Snapshots cannot mix media. Each medium has independent state, hash, generation,
snapshots and unmapped records. An identical poll preserves its generation and
snapshot/event rows, updating only safe poll metadata. Movie snapshot serialization
and fingerprint construction remain compatible with their pre-6A values.

Initialization migrates the four inbound tables under `BEGIN IMMEDIATE` from
recognized movie-only constraints to `media_type IN ('movie','show')`. It preserves
all logical rows, IDs, fingerprints, audit timestamps and event AUTOINCREMENT
sequence. Repeated initialization is idempotent; unknown/incomplete DDL or attached
schema objects are refused without partial migration. Canonical ratings and outbox
are never changed by migration. Scheduled commands open existing schemas without
initialization; deployment performs the migration explicitly while the timer is
stopped and after a mode-0600 SQLite API backup.
