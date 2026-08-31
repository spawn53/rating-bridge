from __future__ import annotations

import os
from dataclasses import dataclass


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    mdblist_api_key: str
    simkl_client_id: str
    simkl_access_token: str
    dry_run: bool = True
    batch_size: int = 50

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            mdblist_api_key=os.getenv("MDBLIST_API_KEY", "").strip(),
            simkl_client_id=os.getenv("SIMKL_CLIENT_ID", "").strip(),
            simkl_access_token=os.getenv("SIMKL_ACCESS_TOKEN", "").strip(),
            dry_run=_bool_env("DRY_RUN", True),
            batch_size=max(1, min(200, int(os.getenv("BATCH_SIZE", "50")))),
        )
        missing = [
            name
            for name, value in (
                ("MDBLIST_API_KEY", cfg.mdblist_api_key),
                ("SIMKL_CLIENT_ID", cfg.simkl_client_id),
                ("SIMKL_ACCESS_TOKEN", cfg.simkl_access_token),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        return cfg
