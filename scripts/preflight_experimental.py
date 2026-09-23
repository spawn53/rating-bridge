from __future__ import annotations

import os

from hub.providers.registry import get_provider
from hub.providers.base import ProviderNotConfigured


def _flag(name: str) -> str:
    return os.getenv(name, "").strip() or "<unset>"


def check(name: str) -> None:
    try:
        provider = get_provider(name)
    except ProviderNotConfigured as exc:
        print(f"{name}: DISABLED/NOT READY - {exc}")
        return

    dry_run = getattr(provider, "dry_run", None)
    print(
        f"{name}: READY - dry_run={dry_run!s} "
        f"(no credential values are printed)"
    )


def main() -> int:
    print("Rating Hub experimental-provider preflight")
    print(f"IMDB_V2_ENABLED={_flag('IMDB_V2_ENABLED')}")
    print(f"IMDB_V2_DRY_RUN={_flag('IMDB_V2_DRY_RUN')}")
    print(f"LETTERBOXD_ENABLED={_flag('LETTERBOXD_ENABLED')}")
    print(f"LETTERBOXD_DRY_RUN={_flag('LETTERBOXD_DRY_RUN')}")
    check("imdb")
    check("letterboxd")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
