"""Demo seeding endpoint for the dashboard quick action.

Installs the baseline and advanced example workflows for the signed-in owner.
``install_demo_workflows`` is idempotent per owner/workflow name, so pressing
the button twice never duplicates anything.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.services import demo_data

router = APIRouter(prefix="/api/v1/demo", tags=["operations"])


@router.post("/install")
def install_demo(user: CurrentUser, db: DbSession) -> dict:
    installed = demo_data.install_demo_workflows(db, user.id)
    db.commit()
    return {
        "installed": [
            {"id": workflow.id, "name": workflow.name, "latest_version": workflow.latest_version}
            for workflow in installed
        ]
    }


__all__ = ["router"]
