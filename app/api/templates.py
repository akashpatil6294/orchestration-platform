"""Workflow template gallery: list the built-in packs and install them."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.services import workflow_packs
from app.services import workflow_service

router = APIRouter(prefix="/api/v1/templates", tags=["templates"])


@router.get("")
def list_templates() -> dict:
    """The gallery: every pack with its metadata (no definitions)."""
    return {"items": workflow_packs.list_packs()}


@router.post("/{pack_id}/install", status_code=201)
def install_template(pack_id: str, user: CurrentUser, db: DbSession) -> dict:
    """Install a pack as a new workflow for the current user (idempotent)."""
    workflow = workflow_packs.install_pack(db, user.id, pack_id)
    return workflow_service.summarize(db, workflow)


__all__ = ["router"]
