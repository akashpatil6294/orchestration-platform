"""Shared router dependencies."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.auth import current_user, current_worker, optional_user, registering_worker, require_admin
from app.database import get_db
from app.models.user import User
from app.models.worker import Worker

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(current_user)]
MaybeUser = Annotated[User | None, Depends(optional_user)]
CurrentWorker = Annotated[Worker, Depends(current_worker)]
RegisteringWorker = Annotated[Worker, Depends(registering_worker)]
AdminUser = Annotated[User, Depends(require_admin)]

__all__ = ["AdminUser", "CurrentUser", "CurrentWorker", "DbSession", "MaybeUser", "RegisteringWorker"]
