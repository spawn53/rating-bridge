# rating-bridge

A small, VPS-free rating synchronizer for this flow:

```text
Reeel -> MDBList -> Trakt (MDBList's official connection)
                 \
                  -> rating-bridge -> Simkl
                  -> rating-bridge -> IMDb (experimental, opt-in)
```

V1 intentionally does **not** call the Trakt API. MDBList is the rating source of truth and Simkl is the automated target. IMDb is optional and disabled by default.

## Public-repository safety model

This repository is designed to be safe to keep public:

- Secrets live only in GitHub Actions repository secrets.
- `DRY_RUN` should start as `true` for Simkl setup.
- No personal rating snapshot/state is committed to the repository.
- V1 is stateless and only **adds/updates** Simkl ratings.
- V1 does **not remove** ratings from Simkl. Safe removal propagation needs private state/ownership tracking and is deferred.
- A rating is written to Simkl only after Simkl itself already shows watch activity for that movie/show. This prevents a rating write from creating an accidental watched item.
- Existing matching Simkl ratings are left untouched.
- Simkl sync handles movies and whole-show ratings only. Seasons/episodes are deliberately ignored.
- Public Action logs show aggregate counts, not title-level rating details.

## Required GitHub Actions secrets

In **Settings -> Secrets and variables -> Actions**, create these repository secrets:

- `MDBLIST_API_KEY`
- `SIMKL_CLIENT_ID`
- `SIMKL_ACCESS_TOKEN`

Then create a repository variable:

- `DRY_RUN=true`

After checking the first Action log, change it to `false`.

Never commit API keys or access tokens.

## Experimental IMDb rating sync

IMDb does not expose an official public API for writing a user's personal ratings. The optional provider therefore uses the same internal GraphQL rating mutation used by IMDb's web experience and authenticates with your logged-in IMDb browser cookie. This can break if IMDb changes its private web API.

Safety defaults:

- `IMDB_ENABLED=false`
- `IMDB_DRY_RUN=true`
- rating removals are never sent
- `IMDB_MAX_WRITES=25` stops unexpectedly large write batches
- only MDBList movie/show ratings with a valid IMDb title ID are considered initially

To test it:

1. Log into IMDb in a browser.
2. From browser developer tools, copy the full `Cookie` request-header value from an authenticated IMDb request. Treat this cookie like your password.
3. Add it directly to GitHub Actions repository secrets as `IMDB_COOKIE`. Do **not** paste it into issues, logs, commits, or chat.
4. Add repository variables `IMDB_ENABLED=true` and `IMDB_DRY_RUN=true`.
5. Run **Rating Sync** manually and inspect only the aggregate IMDb counts.
6. If the proposed write count looks correct, change `IMDB_DRY_RUN=false` and run once manually.

The hourly workflow may remain enabled; IMDb does nothing unless `IMDB_ENABLED=true`.

Episode-level IMDb sync is intentionally deferred until the MDBList episode payload/ID mapping is verified independently.

## Simkl authorization

1. Create a Simkl developer app and copy its Client ID.
2. Locally set `SIMKL_CLIENT_ID`.
3. Install requirements and run:

```bash
python scripts/simkl_auth.py
```

4. Open the URL shown, enter the PIN, approve the app, and store the returned token as the `SIMKL_ACCESS_TOKEN` GitHub secret.

## Local dry run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
export MDBLIST_API_KEY='...'
export SIMKL_CLIENT_ID='...'
export SIMKL_ACCESS_TOKEN='...'
export DRY_RUN=true
pytest -q
python bridge.py
```

## GitHub Actions

The workflow runs hourly at minute 37 and can also be launched manually.

The workflow has only `contents: read` permission and does not commit user data back into this public repository.

## Planned next phases

- V1: MDBList -> Simkl rating add/update
- V1.1: safe removal propagation using private/encrypted state
- V1.5: optional IMDb target behind an explicit feature flag
- V1.6: investigate MDBList episode IDs -> IMDb episode ratings
- V2: Letterboxd only after a sufficiently reliable non-VPS method is confirmed
