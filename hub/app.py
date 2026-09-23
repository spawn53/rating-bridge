from __future__ import annotations

import hmac

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from hub.capabilities import CAPABILITIES, split_supported
from hub.models import DeleteResponse, RatingResponse, RatingWrite
from hub.settings import HubSettings
from hub.store import RatingStore

VERSION = "2.0.0-alpha.1"
settings = HubSettings.from_env()
store = RatingStore(settings.db_path)

app = FastAPI(
    title="Nuvio Rating Hub",
    version=VERSION,
    description="Single-source rating API for Nuvio with multi-provider fan-out.",
)


def require_api_key(x_rating_hub_key: str | None = Header(default=None)) -> None:
    if not settings.api_key:
        raise HTTPException(
            status_code=503,
            detail="RATING_HUB_API_KEY is not configured",
        )
    if not x_rating_hub_key or not hmac.compare_digest(
        x_rating_hub_key, settings.api_key
    ):
        raise HTTPException(status_code=401, detail="invalid rating hub key")


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": VERSION,
        "configured_targets": list(settings.targets),
        "capabilities": {key: sorted(value) for key, value in CAPABILITIES.items()},
        "api_key_configured": bool(settings.api_key),
    }


@app.post(
    "/api/v1/ratings",
    response_model=RatingResponse,
    dependencies=[Depends(require_api_key)],
)
def put_rating(command: RatingWrite) -> RatingResponse:
    requested = tuple(command.targets) if command.targets is not None else settings.targets
    supported, skipped = split_supported(command.media_type, requested)
    result = store.upsert_rating(command, supported)
    result["skipped_targets"] = list(skipped)
    return RatingResponse(**result)


@app.get(
    "/api/v1/ratings",
    dependencies=[Depends(require_api_key)],
)
def list_ratings(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, object]]:
    return store.list_ratings(limit=limit, offset=offset)


@app.get(
    "/api/v1/ratings/lookup",
    dependencies=[Depends(require_api_key)],
)
def get_rating(content_key: str) -> dict[str, object]:
    rating = store.get_rating(content_key)
    if rating is None:
        raise HTTPException(status_code=404, detail="rating not found")
    return rating


@app.delete(
    "/api/v1/ratings/{content_key:path}",
    response_model=DeleteResponse,
    dependencies=[Depends(require_api_key)],
)
def remove_rating(content_key: str) -> DeleteResponse:
    current = store.get_rating(content_key)
    if current is None:
        return DeleteResponse(content_key=content_key, removed=False)
    supported, skipped = split_supported(str(current["media_type"]), settings.targets)
    result = store.delete_rating(content_key, supported)
    result["skipped_targets"] = list(skipped)
    return DeleteResponse(**result)


@app.get(
    "/api/v1/outbox",
    dependencies=[Depends(require_api_key)],
)
def outbox(
    status: str = Query(default="pending", pattern="^(pending|processing|done|failed)$"),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, object]]:
    return store.list_outbox(status=status, limit=limit)
