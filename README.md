# rating-bridge

A small, VPS-free rating synchronizer for this flow:

```text
Reeel -> MDBList -> Trakt (MDBList's official connection)
                 \
                  -> rating-bridge -> Simkl
```

V1 intentionally does **not** call the Trakt API. MDBList is the rating source of truth and Simkl is the automated target.

## Public-repository safety model

This repository is designed to be safe to keep public:

- Secrets live only in GitHub Actions repository secrets.
- `DRY_RUN` should start as `true`.
- No personal rating snapshot/state is committed to the repository.
- V1 is stateless and only **adds/updates** Simkl ratings.
- V1 does **not remove** ratings from Simkl. Safe removal propagation needs private state/ownership tracking and is deferred.
- A rating is written to Simkl only after Simkl itself already shows watch activity for that movie/show. This prevents a rating write from creating an accidental watched item.
- Existing matching Simkl ratings are left untouched.
- Movies and whole-show ratings only. Seasons/episodes are deliberately ignored.

## Required GitHub Actions secrets

In **Settings -> Secrets and variables -> Actions**, create these repository secrets:

- `MDBLIST_API_KEY`
- `SIMKL_CLIENT_ID`
- `SIMKL_ACCESS_TOKEN`

Then create a repository variable:

- `DRY_RUN=true`

After checking the first Action log, change it to `false`.

Never commit API keys or access tokens.

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

During initial setup, `.github/workflows/rating-sync.yml` is **manual-only**. This avoids scheduled failures before the required secrets exist.

After the first successful dry run, enable the hourly schedule (minute 37) and then change `DRY_RUN` to `false` when the proposed changes look correct.

The workflow has only `contents: read` permission and does not commit user data back into this public repository.

## Planned next phases

- V1: MDBList -> Simkl rating add/update
- V1.1: safe removal propagation using private/encrypted state
- V1.5: optional IMDb target behind an explicit feature flag
- V2: Letterboxd only after a sufficiently reliable non-VPS method is confirmed
