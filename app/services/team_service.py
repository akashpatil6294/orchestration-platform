"""Team management: create teams, manage memberships, resolve access.

Teams group users and workflows. Every user gets a personal team (backfilled);
workflows may be assigned to a team to share them with its members.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Invalid, NotFound
from app.models.team import TEAM_ROLES, Team, TeamMembership, role_at_least
from app.models.user import User


def create_team(db: Session, *, name: str, creator: User) -> Team:
    """Create a team and make the creator its admin."""
    if not name or not name.strip() or len(name.strip()) > 200:
        raise Invalid("Team name must be 1-200 characters", code="team_name_invalid")
    team = Team(name=name.strip())
    db.add(team)
    db.flush()
    db.add(TeamMembership(team_id=team.id, user_id=creator.id, role="admin"))
    db.flush()
    return team


def get_team(db: Session, team_id: str) -> Team:
    team = db.get(Team, team_id)
    if team is None:
        raise NotFound("That team does not exist", code="team_not_found")
    return team


def membership(db: Session, team_id: str, user_id: str) -> TeamMembership | None:
    return db.scalar(
        select(TeamMembership).where(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
    )


def require_membership(db: Session, team_id: str, user_id: str, *, minimum_role: str = "viewer") -> TeamMembership:
    """Return the membership or raise; enforces the minimum role level."""
    get_team(db, team_id)  # 404 when the team does not exist
    member = membership(db, team_id, user_id)
    if member is None or not role_at_least(member.role, minimum_role):
        raise NotFound("That team does not exist", code="team_not_found")
    return member


def user_team_ids(db: Session, user_id: str) -> list[str]:
    """IDs of all teams the user belongs to (any role)."""
    return list(
        db.scalars(select(TeamMembership.team_id).where(TeamMembership.user_id == user_id)).all()
    )


def user_role(db: Session, team_id: str, user_id: str) -> str | None:
    """The user's role on a team, or None if not a member."""
    return db.scalar(
        select(TeamMembership.role).where(
            TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
        )
    )


def user_teams(db: Session, user_id: str) -> list[dict[str, Any]]:
    """Teams the user belongs to, with their role on each."""
    rows = db.scalars(
        select(TeamMembership).where(TeamMembership.user_id == user_id).order_by(TeamMembership.created_at)
    ).all()
    result = []
    for row in rows:
        team = db.get(Team, row.team_id)
        if team is None:
            continue
        member_count = db.scalar(
            select(func.count()).select_from(TeamMembership).where(TeamMembership.team_id == team.id)
        ) or 0
        result.append(
            {
                "id": team.id,
                "name": team.name,
                "role": row.role,
                "member_count": member_count,
                "created_at": team.created_at,
            }
        )
    return result


def add_member(db: Session, team_id: str, *, user_id: str, role: str, actor: User) -> TeamMembership:
    """Add a user to the team (or change their role). Actor must be admin."""
    require_membership(db, team_id, actor.id, minimum_role="admin")
    if role not in TEAM_ROLES:
        raise Invalid(f"Role must be one of {', '.join(TEAM_ROLES)}", code="team_role_invalid")
    target = db.get(User, user_id)
    if target is None or not target.is_active:
        raise NotFound("That user does not exist", code="user_not_found")
    existing = membership(db, team_id, user_id)
    if existing:
        existing.role = role
        return existing
    member = TeamMembership(team_id=team_id, user_id=user_id, role=role)
    db.add(member)
    db.flush()
    return member


def remove_member(db: Session, team_id: str, *, user_id: str, actor: User) -> None:
    """Remove a member. Actor must be admin; the last admin cannot be removed."""
    require_membership(db, team_id, actor.id, minimum_role="admin")
    member = membership(db, team_id, user_id)
    if member is None:
        raise NotFound("That user is not a member of the team", code="team_membership_not_found")
    if member.role == "admin":
        admins = db.scalars(
            select(TeamMembership).where(TeamMembership.team_id == team_id, TeamMembership.role == "admin")
        ).all()
        if len(admins) <= 1:
            raise Invalid("A team must keep at least one admin", code="team_last_admin")
    db.delete(member)


def team_members(db: Session, team_id: str, *, viewer: User) -> list[dict[str, Any]]:
    require_membership(db, team_id, viewer.id)
    rows = db.scalars(select(TeamMembership).where(TeamMembership.team_id == team_id).order_by(TeamMembership.created_at)).all()
    result = []
    for row in rows:
        user = db.get(User, row.user_id)
        result.append(
            {
                "user_id": row.user_id,
                "email": user.email if user else "",
                "display_name": user.display_name if user else "",
                "role": row.role,
                "created_at": row.created_at,
            }
        )
    return result


__all__ = [
    "add_member",
    "create_team",
    "get_team",
    "membership",
    "remove_member",
    "require_membership",
    "team_members",
    "user_teams",
]
