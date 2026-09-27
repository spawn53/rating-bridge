from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hub.providers.base import ProviderNotConfigured
from hub.providers.registry import get_provider

ROOT = Path(__file__).resolve().parents[1]


def test_default_experimental_providers_are_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMDB_V2_ENABLED", raising=False)
    monkeypatch.delenv("LETTERBOXD_ENABLED", raising=False)
    with pytest.raises(ProviderNotConfigured):
        get_provider("imdb")
    with pytest.raises(ProviderNotConfigured):
        get_provider("letterboxd")


def test_offline_preflight_does_not_make_network_calls(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import scripts.preflight_providers as preflight

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("network call in offline mode")

    monkeypatch.setattr("hub.auth.httpx.Client", fail)
    assert preflight.main(["--offline"]) == 0
    output = capsys.readouterr().out
    assert "MDBList" in output and "IMDb" in output and "Letterboxd" in output


def test_preflight_never_prints_credentials(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    secret = "super-secret-token"
    monkeypatch.setenv("MDBLIST_ACCESS_TOKEN", secret)
    import scripts.preflight_providers as preflight

    assert preflight.main(["--offline"]) == 0
    assert secret not in capsys.readouterr().out


def test_optional_missing_providers_do_not_fail_preflight() -> None:
    env = {key: value for key, value in os.environ.items() if key not in {
        "MDBLIST_ACCESS_TOKEN", "TRAKT_CLIENT_ID", "TRAKT_ACCESS_TOKEN",
        "SIMKL_CLIENT_ID", "SIMKL_ACCESS_TOKEN", "TMDB_API_READ_TOKEN",
        "TMDB_SESSION_ID", "IMDB_V2_ENABLED", "IMDB_COOKIE",
        "LETTERBOXD_ENABLED", "LETTERBOXD_CLIENT_ID", "LETTERBOXD_CLIENT_SECRET",
        "LETTERBOXD_REFRESH_TOKEN",
    }}
    result = subprocess.run(
        [sys.executable, "scripts/preflight_providers.py", "--offline"],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0
    assert "UNCONFIGURED" in result.stdout


def test_default_targets_exclude_experimental_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RATING_HUB_TARGETS", raising=False)
    from hub.settings import HubSettings

    assert HubSettings.from_env().targets == ("mdblist", "trakt", "simkl", "tmdb")



def test_imdb_live_preflight_requires_exact_read_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.preflight_providers as preflight

    monkeypatch.setenv("IMDB_V2_ENABLED", "true")
    monkeypatch.setenv("IMDB_V2_DRY_RUN", "false")
    monkeypatch.setenv("IMDB_COOKIE", "private-secret-marker")
    monkeypatch.setenv("IMDB_PREFLIGHT_TITLE_ID", "tt0137523")
    monkeypatch.setenv("IMDB_PREFLIGHT_EXPECTED_RATING", "9")
    calls = []

    def read(self, imdb_id):
        calls.append(imdb_id)
        return 9

    monkeypatch.setattr(preflight.IMDbProvider, "read_personal_rating", read)
    assert preflight._check_imdb(False) == "READY"
    assert calls == ["tt0137523"]


def test_imdb_preflight_mismatch_never_claims_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.preflight_providers as preflight

    monkeypatch.setenv("IMDB_V2_ENABLED", "true")
    monkeypatch.setenv("IMDB_V2_DRY_RUN", "false")
    monkeypatch.setenv("IMDB_COOKIE", "private-secret-marker")
    monkeypatch.setenv("IMDB_PREFLIGHT_TITLE_ID", "tt0137523")
    monkeypatch.setenv("IMDB_PREFLIGHT_EXPECTED_RATING", "9")
    monkeypatch.setattr(preflight.IMDbProvider, "read_personal_rating", lambda self, imdb_id: None)
    assert preflight._check_imdb(False) == "AUTH_UNVERIFIED"


def test_imdb_offline_preflight_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.preflight_providers as preflight

    monkeypatch.setenv("IMDB_V2_ENABLED", "true")
    monkeypatch.setenv("IMDB_V2_DRY_RUN", "false")
    monkeypatch.setenv("IMDB_COOKIE", "private-secret-marker")
    monkeypatch.setenv("IMDB_PREFLIGHT_TITLE_ID", "tt0137523")
    monkeypatch.setenv("IMDB_PREFLIGHT_EXPECTED_RATING", "9")
    monkeypatch.setattr(
        preflight.IMDbProvider,
        "read_personal_rating",
        lambda *a, **k: pytest.fail("offline preflight must not read"),
    )
    assert preflight._check_imdb(True) == "CONFIGURED"


def test_imdb_live_preflight_needs_a_rated_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.preflight_providers as preflight

    monkeypatch.setenv("IMDB_V2_ENABLED", "true")
    monkeypatch.setenv("IMDB_V2_DRY_RUN", "false")
    monkeypatch.setenv("IMDB_COOKIE", "private-secret-marker")
    monkeypatch.delenv("IMDB_PREFLIGHT_TITLE_ID", raising=False)
    monkeypatch.delenv("IMDB_PREFLIGHT_EXPECTED_RATING", raising=False)
    assert preflight._check_imdb(False) == "NEEDS READ PROBE"
