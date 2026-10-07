"""Team routes: create teams, list memberships, manage members."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from fastapi import Request

from app.api.deps import CurrentUser, DbSession
from app.models.team import TEAM_ROLES
from app.services import audit_service, team_service

router = APIRouter(prefix="/api/v1/teams", tags=["teams"])


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class MemberUpsert(BaseModel):
    user_id: str
    role: str = Field(pattern=f"^({'|'.join(TEAM_ROLES)})$")


def _audit(request: Request, db, *, action: str, resource_id: str | None = None, details: dict | None = None) -> None:
    audit_service.record(
        db,
        action=action,
        actor_user_id=request.state.user_id if hasattr(request.state, "user_id") else None,
        actor_token_id=getattr(request.state, "api_token_id", None),
        resource_type="team",
        resource_id=resource_id,
        ip_address=request.client.host if request.client else None,
        details=details,
    )


def _team_view(team) -> dict:
    return {"id": team.id, "name": team.name, "created_at": team.created_at, "updated_at": team.updated_at}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_team(payload: TeamCreate, request: Request, user: CurrentUser, db: DbSession) -> dict:
    team = team_service.create_team(db, name=payload.name, creator=user)
    _audit(request, db, action="team.create", resource_id=team.id, details={"name": team.name})
    db.commit()
    return _team_view(team)


@router.get("")
def list_my_teams(user: CurrentUser, db: DbSession) -> dict:
    return {"items": team_service.user_teams(db, user.id)}


@router.get("/{team_id}")
def get_team(team_id: str, user: CurrentUser, db: DbSession) -> dict:
    member = team_service.require_membership(db, team_id, user.id)
    team = team_service.get_team(db, team_id)
    view = _team_view(team)
    view["role"] = member.role
    return view


@router.get("/{team_id}/members")
def list_members(team_id: str, user: CurrentUser, db: DbSession) -> dict:
    return {"items": team_service.team_members(db, team_id, viewer=user)}


@router.put("/{team_id}/members")
def upsert_member(team_id: str, payload: MemberUpsert, request: Request, user: CurrentUser, db: DbSession) -> dict:
    member = team_service.add_member(db, team_id, user_id=payload.user_id, role=payload.role, actor=user)
    _audit(request, db, action="team.member_upsert", resource_id=team_id, details={"user_id": member.user_id, "role": member.role})
    db.commit()
    return {"team_id": team_id, "user_id": member.user_id, "role": member.role}


@router.delete("/{team_id}/members/{member_user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_member(team_id: str, member_user_id: str, request: Request, user: CurrentUser, db: DbSession) -> None:
    team_service.remove_member(db, team_id, user_id=member_user_id, actor=user)
    _audit(request, db, action="team.member_remove", resource_id=team_id, details={"user_id": member_user_id})
    db.commit()


__all__ = ["router"]
