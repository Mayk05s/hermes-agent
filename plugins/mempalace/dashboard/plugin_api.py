"""MemPalace dashboard plugin API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from hermes_cli import mempalace


router = APIRouter()


def _api_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


class GenerateBody(BaseModel):
    profile: str = "default"
    dry_run: bool = False
    auto_clean: bool = True
    clean_max_delete: int = mempalace.AUTO_CLEAN_MAX_DELETE


class RebuildBody(BaseModel):
    profile: str = "default"
    all_profiles: bool = False
    backup: bool = True
    auto_clean: bool = True
    clean_max_delete: int = mempalace.AUTO_CLEAN_MAX_DELETE


class RefreshBody(BaseModel):
    profile: str = "default"
    force: bool = False


class CleanNoiseBody(BaseModel):
    profile: str = "default"
    palace: str = ""
    dry_run: bool = True
    backup: bool = True
    max_delete: int = 250


@router.get("/profiles")
async def profiles():
    try:
        rows = []
        for row in mempalace.list_profiles():
            palaces = mempalace.list_palaces(profile=row["name"], include_stats=False)
            rows.append({**row, "palace_count": len(palaces)})
        return {"profiles": rows}
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/palaces")
async def palaces(profile: str = "default"):
    try:
        return {"profile": profile, "palaces": mempalace.list_palaces(profile=profile)}
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/matrix")
async def matrix():
    try:
        return mempalace.profile_matrix()
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/graph")
async def graph(
    profile: str = "default",
    palace: str = "",
    center: str = "",
    depth: int = Query(default=1, ge=1, le=3),
    query: str = "",
    node_limit: int = Query(default=180, ge=10, le=1000),
    edge_limit: int = Query(default=420, ge=10, le=3000),
    min_confidence: float = 0.0,
):
    try:
        if center:
            return mempalace.load_subgraph(
                center,
                profile=profile,
                palace=palace,
                depth=depth,
                node_limit=node_limit,
                edge_limit=edge_limit,
                min_confidence=min_confidence,
            )
        return mempalace.load_graph(
            palace,
            profile=profile,
            query=query,
            node_limit=node_limit,
            edge_limit=edge_limit,
            min_confidence=min_confidence,
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/node")
async def node(
    node_id: str = Query(..., min_length=1),
    profile: str = "default",
    palace: str = "",
    depth: int = Query(default=2, ge=1, le=4),
    limit: int = Query(default=100, ge=10, le=300),
):
    try:
        return mempalace.node_tree(
            node_id,
            profile=profile,
            palace=palace,
            depth=depth,
            limit=limit,
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    profile: str = "default",
    palace: str = "",
    limit: int = Query(default=30, ge=1, le=100),
):
    try:
        return mempalace.search(q, profile=profile, palace=palace, limit=limit)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/generate")
async def generate(body: GenerateBody):
    try:
        return mempalace.generate_from_markdown(
            profile=body.profile,
            dry_run=body.dry_run,
            auto_clean=body.auto_clean,
            clean_max_delete=body.clean_max_delete,
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/rebuild")
async def rebuild(body: RebuildBody):
    try:
        if body.all_profiles:
            return mempalace.rebuild_all_profiles_from_history(
                backup=body.backup,
                include_markdown=True,
                auto_clean=body.auto_clean,
                clean_max_delete=body.clean_max_delete,
            )
        return mempalace.rebuild_from_history(
            profile=body.profile,
            backup=body.backup,
            include_markdown=True,
            auto_clean=body.auto_clean,
            clean_max_delete=body.clean_max_delete,
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/refresh")
async def refresh(body: RefreshBody):
    try:
        return mempalace.refresh_if_due(profile=body.profile, force=body.force)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/clean-noise")
async def clean_noise(body: CleanNoiseBody):
    try:
        return mempalace.clean_noise(
            profile=body.profile,
            palace=body.palace,
            dry_run=body.dry_run,
            backup=body.backup,
            max_delete=body.max_delete,
        )
    except Exception as exc:
        raise _api_error(exc) from exc
