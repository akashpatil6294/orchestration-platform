"""Workflow routes: drafts, validation, publishing, versions, secrets, runs."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status

from app.api.deps import CurrentUser, DbSession
from app.core.errors import Conflict
from app.core.pagination import Pagination, page_of, pagination
from app.schemas.run import RunSummary, StartRunRequest, TestRunRequest
from app.schemas.workflow import (
    PublishRequest,
    PublishResult,
    SecretUpsert,
    SecretView,
    ValidateRequest,
    ValidationResult,
    WorkflowCreate,
    WorkflowDetail,
    WorkflowDraftPatch,
    WorkflowSummary,
    WorkflowUpdate,
    WorkflowVersionDetail,
    WorkflowVersionSummary,
)
from app.services import quota_service, run_service, workflow_service

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


def _version_summary(version, *, latest: int) -> dict:
    return {
        "version": version.version,
        "published_at": version.published_at,
        "published_by": version.published_by,
        "publish_note": version.publish_note,
        "step_count": len(version.definition.get("steps", [])),
        "definition_hash": version.definition_hash,
        "is_latest": version.version == latest,
    }


def _detail(db, workflow, *, user_id: str | None = None) -> dict:
    versions = workflow_service.list_versions(db, workflow.id, limit=100)
    counts = workflow_service.run_counts_by_status(db, [workflow.id]).get(workflow.id, {})
    last_run = workflow_service.latest_runs(db, [workflow.id]).get(workflow.id)
    schedules = workflow_service.schedule_counts(db, [workflow.id]).get(workflow.id, 0)
    summary = workflow_service.summarize(db, workflow, counts=counts, last_run=last_run, schedules=schedules)
    summary["draft"] = workflow.draft
    summary["draft_version"] = workflow.draft_version
    summary["versions"] = [_version_summary(version, latest=workflow.latest_version) for version in versions]
    summary["team_id"] = workflow.team_id
    if user_id:
        from app.services import team_service

        if workflow.owner_id == user_id:
            summary["user_role"] = "owner"
        elif workflow.team_id:
            summary["user_role"] = team_service.user_role(db, workflow.team_id, user_id)
        else:
            summary["user_role"] = None
    return summary


@router.get("", response_model=dict)
def list_workflows(
    user: CurrentUser,
    db: DbSession,
    page: Annotated[Pagination, Depends(pagination)],
    search: str | None = Query(default=None, max_length=200, description="Match name or description"),
    status_filter: str | None = Query(default=None, alias="status", max_length=24, description="Latest run status"),
    include_archived: bool = Query(default=False),
    sort: str = Query(default="updated", pattern="^(updated|created|name)$"),
) -> dict:
    rows, total = workflow_service.list_workflows(
        db, user.id, search=search, status=status_filter, include_archived=include_archived,
        limit=page.limit, offset=page.offset, sort=sort,
    )
    ids = [row.id for row in rows]
    counts = workflow_service.run_counts_by_status(db, ids)
    latest = workflow_service.latest_runs(db, ids)
    schedules = workflow_service.schedule_counts(db, ids)
    items = [
        workflow_service.summarize(
            db, row, counts=counts.get(row.id, {}), last_run=latest.get(row.id), schedules=schedules.get(row.id, 0)
        )
        for row in rows
    ]
    return page_of(items, total, page)


@router.post("", response_model=WorkflowSummary, status_code=status.HTTP_201_CREATED)
def create_workflow(payload: WorkflowCreate, user: CurrentUser, db: DbSession) -> dict:
    workflow = workflow_service.create_workflow(db, user.id, payload)
    db.commit()
    db.refresh(workflow)
    return workflow_service.summarize(db, workflow)


@router.get("/{workflow_id}", response_model=WorkflowDetail)
def get_workflow(workflow_id: str, user: CurrentUser, db: DbSession) -> dict:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    return _detail(db, workflow, user_id=user.id)


@router.patch("/{workflow_id}", response_model=WorkflowDetail)
def update_workflow(
    workflow_id: str,
    payload: WorkflowDraftPatch,
    user: CurrentUser,
    db: DbSession,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict:
    """Partial draft update used by the editor's autosave.

    Requires ``If-Match: <draft_version>`` for optimistic concurrency. A
    stale version returns 409 with the current draft and version so the
    builder can offer reload-or-overwrite instead of silently clobbering
    another editor's changes.
    """
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    workflow_service.require_workflow_mutation(db, workflow, user.id)
    if if_match is not None:
        try:
            expected = int(if_match.strip().strip('"'))
        except ValueError:
            expected = None
        if expected != workflow.draft_version:
            raise Conflict(
                "Draft changed since you loaded it (expected version "
                f"{if_match}, current is {workflow.draft_version})",
                code="draft_version_conflict",
                details={
                    "expected_version": expected,
                    "current_version": workflow.draft_version,
                    "current_draft": workflow.draft,
                },
            )
    workflow_service.update_workflow(db, workflow, payload)
    db.commit()
    db.refresh(workflow)
    return _detail(db, workflow, user_id=user.id)


@router.put("/{workflow_id}", response_model=WorkflowDetail)
def replace_workflow(workflow_id: str, payload: WorkflowUpdate, user: CurrentUser, db: DbSession) -> dict:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    workflow_service.require_workflow_mutation(db, workflow, user.id)
    workflow_service.update_workflow(db, workflow, payload)
    db.commit()
    db.refresh(workflow)
    return _detail(db, workflow, user_id=user.id)


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(workflow_id: str, user: CurrentUser, db: DbSession) -> None:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    workflow_service.require_workflow_mutation(db, workflow, user.id, minimum_role="admin")
    workflow_service.delete_workflow(db, workflow)
    db.commit()


@router.post("/{workflow_id}/archive", response_model=WorkflowDetail)
def archive_workflow(workflow_id: str, user: CurrentUser, db: DbSession, archived: bool = Query(default=True)) -> dict:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    workflow_service.archive_workflow(db, workflow, archived=archived)
    db.commit()
    db.refresh(workflow)
    return _detail(db, workflow, user_id=user.id)


@router.post("/{workflow_id}/validate", response_model=ValidationResult)
def validate_workflow(workflow_id: str, user: CurrentUser, db: DbSession, payload: ValidateRequest | None = None) -> dict:
    """Validate the saved draft, or a candidate definition sent by the editor."""
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    definition = payload.definition if payload and payload.definition else None
    result = workflow_service.validate_definition(definition, workflow=workflow)
    if result["valid"]:
        known = workflow_service.secret_names(db, workflow.id)
        referenced = workflow_service.referenced_secret_names(
            definition.model_dump(mode="json") if definition else workflow.draft
        )
        missing = sorted(referenced - known)
        if missing:
            result["valid"] = False
            result["errors"] = list(result["errors"]) + [
                {
                    "code": "secret.missing",
                    "message": f"Step input references an undefined secret: {', '.join(missing)}",
                    "step_id": None,
                    "field": "input",
                }
            ]
    return result


@router.get("/{workflow_id}/diff", response_model=dict)
def diff_workflow(
    workflow_id: str,
    user: CurrentUser,
    db: DbSession,
    from_version: str = Query(default="draft", description="'draft' or 'version:N'"),
    to_version: str = Query(default="draft", description="'draft' or 'version:N'"),
) -> dict:
    """Structural diff between two definitions (Stage H1).

    Used by the builder's publish flow: ``from_version=draft&to_version=version:3``
    shows what changed since v3. Either side may be ``draft`` or ``version:N``.
    """

    def _resolve(spec: str) -> dict[str, Any]:
        if spec == "draft":
            return dict(workflow.draft or {})
        if spec.startswith("version:"):
            try:
                number = int(spec.split(":", 1)[1])
            except ValueError:
                from app.core.errors import Invalid

                raise Invalid(f"Bad version spec '{spec}'; use 'draft' or 'version:N'", code="bad_version_spec")
            return dict(workflow_service.get_version(db, workflow.id, number).definition)
        from app.core.errors import Invalid

        raise Invalid(f"Bad version spec '{spec}'; use 'draft' or 'version:N'", code="bad_version_spec")

    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    diff = workflow_service.diff_definitions(_resolve(from_version), _resolve(to_version))
    return {"from": from_version, "to": to_version, **diff}


@router.post("/{workflow_id}/publish", response_model=PublishResult)
def publish_workflow(workflow_id: str, user: CurrentUser, db: DbSession, payload: PublishRequest | None = None) -> dict:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    version = workflow_service.publish(
        db, workflow, note=(payload.note if payload else ""), published_by=user.email
    )
    db.commit()
    db.refresh(version)
    return {
        "workflow_id": workflow.id,
        "version": version.version,
        "published_at": version.published_at,
        "definition_hash": version.definition_hash,
        "summary": workflow_service.validate_definition(version.definition)["summary"],
    }


@router.get("/{workflow_id}/versions", response_model=list[WorkflowVersionSummary])
def list_versions(workflow_id: str, user: CurrentUser, db: DbSession) -> list[dict]:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    versions = workflow_service.list_versions(db, workflow.id, limit=200)
    return [_version_summary(version, latest=workflow.latest_version) for version in versions]


@router.get("/{workflow_id}/versions/{version}", response_model=WorkflowVersionDetail)
def get_version(workflow_id: str, version: int, user: CurrentUser, db: DbSession) -> dict:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    row = workflow_service.get_version(db, workflow.id, version)
    return {**_version_summary(row, latest=workflow.latest_version), "definition": row.definition}


@router.post("/{workflow_id}/runs", response_model=RunSummary, status_code=status.HTTP_201_CREATED)
def start_run(workflow_id: str, payload: StartRunRequest, user: CurrentUser, db: DbSession) -> dict:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    workflow_service.require_workflow_operate(db, workflow, user.id)
    quota_service.check_run_quota(db, triggered_by=user.email)
    run, replayed = run_service.create_run(
        db,
        workflow,
        version=payload.version,
        input_data=payload.input,
        idempotency_key=payload.idempotency_key,
        trigger=payload.trigger,
        triggered_by=user.email,
        max_parallel=payload.max_parallel,
        priority=payload.priority,
        queue_name=payload.queue,
    )
    db.commit()
    db.refresh(run)
    summary = run_service.run_summary_view(db, run, workflow_name=workflow.name)
    summary["idempotent_replay"] = replayed
    return summary


@router.post("/{workflow_id}/test-run", response_model=RunSummary, status_code=status.HTTP_201_CREATED)
def test_run_workflow(workflow_id: str, payload: TestRunRequest, user: CurrentUser, db: DbSession) -> dict:
    """Run the current draft (or a single step) as a builder test run.

    Test runs are marked ``is_test``: they never fire triggers, never send
    notifications, and never consume production quotas. Connectors honour
    dry-run mode. Use ``step_id`` + ``pinned_outputs`` to test one step in
    isolation with sample upstream data.
    """
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    workflow_service.require_workflow_operate(db, workflow, user.id)
    # NOTE: no quota check — test runs are quota-exempt by design.
    draft = dict(workflow.draft or {})
    steps = draft.get("steps", [])
    if payload.step_id:
        wanted = [step for step in steps if step.get("id") == payload.step_id]
        if not wanted:
            from app.core.errors import NotFound

            raise NotFound(f"Step '{payload.step_id}' is not in the draft", code="step_not_found")
        step = dict(wanted[0])
        # Pin the sample dependency outputs into the step input so the
        # handler sees them as upstream results.
        step_input = dict(step.get("input") or {})
        if payload.pinned_outputs:
            step_input["_pinned_dependency_outputs"] = dict(payload.pinned_outputs)
        step["input"] = step_input
        step["depends_on"] = []
        draft = {**draft, "steps": [step]}
    run, _ = run_service.create_run(
        db,
        workflow,
        input_data=payload.input,
        trigger="test",
        triggered_by=user.email,
        queue_name=payload.queue,
        is_test=True,
        draft_definition=draft,
    )
    db.commit()
    db.refresh(run)
    summary = run_service.run_summary_view(db, run, workflow_name=workflow.name)
    summary["is_test"] = True
    return summary


@router.get("/{workflow_id}/runs", response_model=dict)
def list_workflow_runs(
    workflow_id: str,
    user: CurrentUser,
    db: DbSession,
    page: Annotated[Pagination, Depends(pagination)],
    status_filter: str | None = Query(default=None, alias="status", max_length=24),
) -> dict:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    rows, total = run_service.list_runs(db, user.id, workflow_id=workflow.id, status=status_filter, limit=page.limit, offset=page.offset)
    steps = {}
    for run in rows:
        steps[run.id] = run_service.steps_for(db, run.id)
    items = [run_service.run_summary_view(db, run, workflow_name=workflow.name, steps=steps.get(run.id, [])) for run in rows]
    return page_of(items, total, page)


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
@router.get("/{workflow_id}/secrets", response_model=list[SecretView])
def list_secrets(workflow_id: str, user: CurrentUser, db: DbSession) -> list[dict]:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    return [
        {"name": row.name, "created_at": row.created_at, "updated_at": row.updated_at}
        for row in workflow_service.list_secrets(db, workflow.id)
    ]


@router.put("/{workflow_id}/secrets/{name}", response_model=SecretView)
def upsert_secret(workflow_id: str, name: str, payload: SecretUpsert, user: CurrentUser, db: DbSession) -> dict:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    if payload.name != name:
        from app.core.errors import Invalid

        raise Invalid("The secret name in the path and body must match", code="secret_name_mismatch")
    record = workflow_service.set_secret(db, workflow, name, payload.value)
    db.commit()
    db.refresh(record)
    return {"name": record.name, "created_at": record.created_at, "updated_at": record.updated_at}


@router.delete("/{workflow_id}/secrets/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_secret(workflow_id: str, name: str, user: CurrentUser, db: DbSession) -> None:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    workflow_service.delete_secret(db, workflow, name)
    db.commit()


@router.get("/{workflow_id}/activity", response_model=list)
def workflow_activity(workflow_id: str, user: CurrentUser, db: DbSession, limit: int = Query(default=25, ge=1, le=100)) -> list[dict]:
    from app.services import event_service

    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    events = event_service.list_workflow_events(db, workflow.id, limit=limit)
    return [
        {
            "seq": event.seq,
            "type": event.event_type,
            "level": event.level,
            "message": event.message,
            "payload": event.payload,
            "actor": event.actor,
            "created_at": event.created_at,
        }
        for event in events
    ]


__all__ = ["router"]
