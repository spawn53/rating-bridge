# Stable-provider authentication (operator runbook)

This phase only prepares authentication. Do not run a rating pilot until the
operator has reviewed and deployed this code, registered the required apps,
and confirmed read-only status for each provider.

Configuration stays in `/srv/stacks/rating-hub/.env.v2` (mode `0600`). Runtime
tokens and the TMDb session are stored as mode `0600` files under
`/srv/data/rating-hub/auth/` (mode `0700`), mounted as `/data/auth/` in the two
Rating Hub containers. The store uses an interprocess file lock, refreshes
before expiry, and atomically replaces each rotated token pair. Do not copy
these files into the repository or Docker build context. Back up the auth
directory as a secret, separately from rating data.

Run an interactive bootstrap in a temporary container using the deployed image:

```sh
docker compose -f /srv/stacks/rating-hub/docker-compose.v2.yml run --rm --no-deps -it rating-hub python scripts/bootstrap_provider_auth.py tmdb
docker compose -f /srv/stacks/rating-hub/docker-compose.v2.yml run --rm --no-deps -it rating-hub python scripts/bootstrap_provider_auth.py trakt
docker compose -f /srv/stacks/rating-hub/docker-compose.v2.yml run --rm --no-deps -it rating-hub python scripts/bootstrap_provider_auth.py simkl
docker compose -f /srv/stacks/rating-hub/docker-compose.v2.yml run --rm --no-deps -it rating-hub python scripts/bootstrap_provider_auth.py mdblist
```

The command needs a real terminal and prints only the provider's browser URL,
user code where applicable, and final status. It never prints access tokens,
refresh tokens, or the TMDb session ID. It does not run at container startup.
If the command is run directly on the host, it reads the mode `0600` env file
from its default path and writes the same host auth directory.

Before bootstrap, set only the appropriate app configuration in `.env.v2`:

| Provider | Required configuration | Authorization and renewal |
| --- | --- | --- |
| Trakt | `TRAKT_CLIENT_ID`, `TRAKT_CLIENT_SECRET` | Official device code flow. Access and single-use refresh tokens go into the local store. The legacy `TRAKT_ACCESS_TOKEN`, `TRAKT_REFRESH_TOKEN`, and `TRAKT_TOKEN_EXPIRES_AT` env fields can be imported by the runtime until the first refresh; a stored pair takes precedence. `TRAKT_REDIRECT_URI` is optional and defaults to the device/OOB URI; set it to the app's registered URI if different. |
| MDBList | `MDBLIST_CLIENT_ID` for a Rating Hub **Device Code App** | Device authorization requests `write` scope. Access and refresh tokens go into the store. An ordinary API key or another app's client ID is not a substitute. If registration is unavailable, MDBList OAuth client registration is the remaining blocker. |
| TMDb | `TMDB_API_READ_TOKEN` | The app token is validated first. Browser approval of a request token produces a user session ID, stored locally. App authentication and user authentication are separate. |
| Simkl | `SIMKL_CLIENT_ID` for the currently supported AUTH V1 app | Verified PIN flow stores the user token locally. AUTH V1 tokens are long lived and have no refresh grant. Simkl is migrating to AUTH V2, which uses seven-day access tokens and refresh tokens; a future migration needs a separate V2 app and refresh implementation. Do not put a V2 token into this V1 path. |

After each provider is authorized, run only the read-only preflight from the
deployed image. `--offline` reads local configuration and makes no network
calls. Normal mode may refresh an expiring OAuth token and then uses only the
provider's documented GET account endpoint. Statuses are `UNCONFIGURED`,
`NEEDS_AUTHORIZATION`, `CONFIGURED` (offline, not checked remotely),
`REFRESH_REQUIRED` (offline), `READY`, `AUTH_FAILED`, or `DISABLED`.

```sh
docker compose -f /srv/stacks/rating-hub/docker-compose.v2.yml run --rm --no-deps rating-hub python scripts/preflight_providers.py --offline
docker compose -f /srv/stacks/rating-hub/docker-compose.v2.yml run --rm --no-deps rating-hub python scripts/preflight_providers.py
```

MDBList's published `GET /sync/ratings` supports cursor pagination. The
read helper checks all pages, validates a 1–10 rating, and refuses ambiguous
item shapes or duplicate matches. The write pilot remains gated in this auth
phase; a future live pilot must validate the actual response shape against the
provider account before relying on an unrated result.

Sources: [Trakt authentication](https://docs.trakt.tv/reference/auth),
[Trakt single-use refresh](https://docs.trakt.tv/reference/postoauthtoken),
[MDBList authentication](https://api.mdblist.com/docs/authentication/),
[MDBList OpenAPI schema](https://api.mdblist.com/schema/),
[TMDb session guide](https://developer.themoviedb.org/reference/authentication-how-do-i-generate-a-session-id),
[Simkl migration guide](https://api.simkl.org/guides/migrating-v1-to-v2).


## Experimental IMDb V2 preflight

IMDb V2 is deliberately excluded from the interactive OAuth bootstrap because it
uses an authenticated IMDb browser cookie rather than a supported public user
API. Keep `IMDB_V2_ENABLED=false` and `IMDB_V2_DRY_RUN=true` until a controlled
pilot. Never commit the cookie or paste it into logs; store it only in the
mode-0600 `.env.v2` file.

Before any live write, choose one IMDb title that the account has already rated
and configure `IMDB_PREFLIGHT_TITLE_ID=tt...` plus
`IMDB_PREFLIGHT_EXPECTED_RATING=1..10`. The normal online provider preflight
then performs only the authenticated GraphQL `userRating` read and returns
`READY` only when the observed value exactly matches the expected value.
An unrated probe, an expired/invalid cookie, or a mismatched value never grants
READY. Offline preflight validates configuration only and makes no network call.

Live IMDb mutations use a conservative configurable delay
(`IMDB_V2_WRITE_DELAY_SECONDS`, default 2 seconds) and read-after-write
verification by default (`IMDB_V2_VERIFY_WRITES=true`). Missing canonical IMDb
IDs are treated as permanent unsupported deliveries rather than retried.


### Trakt inbound IMDb target-plan gate

The guarded Trakt importer does not derive automatic fan-out from the global
capability table. It accepts only two exact, ordered plans: the established
`tmdb,trakt,simkl,mdblist` plan and the separately audited
`tmdb,trakt,simkl,mdblist,imdb` plan. Trakt remains excluded as the source, so
the latter produces exactly four transactional outbox jobs:
TMDb, Simkl, MDBList and IMDb. Any missing, reordered, duplicated, unknown or
Letterboxd-containing plan fails closed before canonical mutation.

This code path may be merged/deployed while production remains on the four
stable global targets; IMDb is not activated until the environment and provider
flags are changed in a later controlled phase.
