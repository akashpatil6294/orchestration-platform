"""The sample workflow shipped with the platform.

It demonstrates every capability a reviewer needs to see in one run:

* **Parallel branches.** ``fetch_primary`` and ``fetch_secondary`` have no
  dependencies, so both are ready at the same moment and run concurrently.
* **Dependencies.** ``aggregate`` waits for both fetches.
* **A deliberate failure and retry.** ``fetch_primary`` uses ``demo.fail_once``,
  which fails on attempt 1 and succeeds on attempt 2. Its step carries
  ``retries: 2`` with exponential backoff, so the retry is visible in the UI.
* **Downstream output passing.** ``aggregate`` and ``summarize`` receive their
  dependencies' outputs, and ``publish`` receives ``summarize``'s output.
* **A parallel tail.** ``notify`` and ``publish`` both depend only on
  ``summarize`` and therefore run side by side.
* **A schedule-ready shape.** Nothing in the workflow depends on a manual trigger,
  so it can be attached to a cron schedule unchanged.

Positions are included so the graph editor opens with a readable layout.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.workflow import Workflow
from app.services import workflow_service

DEMO_WORKFLOW_NAME = "Daily report pipeline"
DEMO_PUBLISH_NOTE = "Initial published version installed by the platform CLI"

DEMO_DEFINITION: dict[str, Any] = {
    "name": DEMO_WORKFLOW_NAME,
    "description": (
        "Fetches two sources in parallel, aggregates them, and publishes a report. "
        "The primary fetch fails on its first attempt on purpose so the retry path is visible."
    ),
    "default_max_parallel": 3,
    "tags": ["sample", "retries", "parallel"],
    "steps": [
        {
            "id": "fetch_primary",
            "type": "demo.fail_once",
            "name": "Fetch primary source",
            "description": "Fails on the first attempt, then succeeds. Retries use exponential backoff.",
            "input": {"value": "primary-source", "message": "Simulated upstream hiccup on the first attempt"},
            "depends_on": [],
            "retries": 2,
            "backoff_seconds": 3,
            "backoff_multiplier": 2.0,
            "priority": 15,
            "queue": "default",
            "timeout_seconds": 60,
            "position": {"x": 40, "y": 40},
        },
        {
            "id": "fetch_secondary",
            "type": "demo.echo",
            "name": "Fetch secondary source",
            "description": "Runs in parallel with the primary fetch.",
            "input": {"value": "secondary-source", "label": "secondary"},
            "depends_on": [],
            "retries": 0,
            "priority": 5,
            "queue": "default",
            "timeout_seconds": 60,
            "position": {"x": 40, "y": 220},
        },
        {
            "id": "aggregate",
            "type": "demo.add",
            "name": "Aggregate metrics",
            "description": "Waits for both fetches, then sums a list of numbers.",
            "input": {"values": [12, 30, 8]},
            "depends_on": ["fetch_primary", "fetch_secondary"],
            "retries": 1,
            "concurrency_key": "reporting-database",
            "concurrency_limit": 2,
            "timeout_seconds": 60,
            "position": {"x": 340, "y": 130},
        },
        {
            "id": "summarize",
            "type": "demo.summarize",
            "name": "Build summary",
            "description": "Receives the aggregate and both fetch outputs, and combines them into one document.",
            "input": {"title": "Daily report"},
            "depends_on": ["aggregate"],
            "retries": 1,
            "timeout_seconds": 120,
            "position": {"x": 640, "y": 130},
        },
        {
            "id": "publish",
            "type": "demo.publish_report",
            "name": "Publish report",
            "description": "Publishes the summary. Deduplicated by idempotency key so a redelivery is safe.",
            "input": {"channel": "console"},
            "depends_on": ["summarize"],
            "retries": 2,
            "backoff_seconds": 2,
            "backoff_multiplier": 2.0,
            "rate_limit_per_minute": 30,
            "rate_limit_key": "report-publishing",
            "timeout_seconds": 60,
            "position": {"x": 940, "y": 40},
        },
        {
            "id": "notify",
            "type": "demo.echo",
            "name": "Notify stakeholders",
            "description": "Runs in parallel with publishing, once the summary exists.",
            "input": {"value": "report-ready", "label": "notification"},
            "depends_on": ["summarize"],
            "retries": 0,
            "timeout_seconds": 60,
            "position": {"x": 940, "y": 220},
        },
    ],
}

# A compact variant used by tests: same shape, fewer steps, faster retries.
DEMO_SMOKE_DEFINITION: dict[str, Any] = {
    "name": "Smoke test",
    "description": "Two parallel steps feeding one dependent step.",
    "default_max_parallel": 2,
    "tags": ["sample"],
    "steps": [
        {"id": "left", "type": "demo.echo", "input": {"value": "left"}, "depends_on": [], "retries": 0, "timeout_seconds": 30, "position": {"x": 40, "y": 40}},
        {"id": "right", "type": "demo.add", "input": {"values": [1, 2, 3]}, "depends_on": [], "retries": 0, "timeout_seconds": 30, "position": {"x": 40, "y": 200}},
        {"id": "join", "type": "demo.summarize", "input": {"title": "Smoke"}, "depends_on": ["left", "right"], "retries": 1, "timeout_seconds": 30, "position": {"x": 340, "y": 120}},
    ],
}

DEMO_SCHEDULE = {"cron_expression": "0 7 * * *", "timezone": "UTC", "name": "Weekday morning report", "enabled": False}

# Additional published samples exercise the production engine from the UI and
# the worker protocol. External AI credentials are only needed for the PDF run.
DEMO_ADVANCED_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "Order routing with conditions",
        "description": "Routes large orders for review and smaller orders for automatic fulfilment.",
        "default_max_parallel": 3,
        "tags": ["sample", "branching"],
        "steps": [
            {"id": "route", "type": "condition", "when": "input.total >= 500", "if_true": "review", "if_false": "fulfil", "input": {}},
            {"id": "review", "type": "approval", "input": {"order_id": "{{input.order_id}}"}, "depends_on": ["route"], "approvers": []},
            {"id": "fulfil", "type": "demo.publish_report", "input": {"channel": "fulfilment"}, "depends_on": ["route"], "when": "input.total < 500"},
            {"id": "finish_reviewed", "type": "demo.publish_report", "input": {"channel": "fulfilment"}, "depends_on": ["review"], "when": "input.total >= 500"},
        ],
    },
    {
        "name": "PDF batch extraction",
        "description": "Extracts and classifies each PDF in a submitted batch with bounded parallelism.",
        "default_max_parallel": 4,
        "tags": ["sample", "pdf", "fan-out", "ai"],
        "steps": [
            {
                "id": "extract", "type": "document.extract_text", "input": {"pdf_base64": "{{item.pdf_base64}}"},
                "foreach": "{{input.pdfs}}", "max_concurrency": 2, "partial_failure": "continue",
            },
            {
                "id": "classify", "type": "ai.classify", "input": {"text": "{{item.text}}"},
                "depends_on": ["extract"], "foreach": "{{steps.extract.output}}", "max_concurrency": 2,
                "partial_failure": "continue",
            },
        ],
    },
    {
        "name": "Production deploy approval",
        "description": "Runs a deploy only after an authenticated human approval decision.",
        "default_max_parallel": 2,
        "tags": ["sample", "approval", "deployment"],
        "steps": [
            {"id": "plan", "type": "demo.echo", "input": {"value": "{{input.release}}"}},
            {"id": "approval", "type": "approval", "input": {"release": "{{input.release}}"}, "depends_on": ["plan"], "approvers": []},
            {"id": "deploy", "type": "demo.publish_report", "input": {"channel": "production"}, "depends_on": ["approval"]},
        ],
    },
    {
        "name": "Booking saga with rollback",
        "description": "Reserves inventory, charges a payment, and releases inventory if a later step fails.",
        "default_max_parallel": 1,
        "tags": ["sample", "saga", "compensation"],
        "steps": [
            {"id": "reserve", "type": "demo.echo", "input": {"value": "{{input.booking_id}}"}, "compensate": "release"},
            {"id": "charge", "type": "demo.fail", "input": {"message": "Replace with an idempotent payment connector"}, "depends_on": ["reserve"]},
            {"id": "release", "type": "demo.echo", "input": {"value": "{{steps.reserve.output.value}}"}, "depends_on": ["reserve"]},
        ],
        "on_failure": [
            {"id": "alert", "type": "demo.echo", "input": {"value": "{{input.booking_id}} rollback completed"}},
        ],
    },
    {
        "name": "Webhook order intake",
        "description": "Accepts a signed order webhook and records its mapped order fields.",
        "default_max_parallel": 2,
        "tags": ["sample", "webhook", "triggers"],
        "steps": [
            {"id": "accept_order", "type": "demo.echo", "name": "Accept order", "input": {"order_id": "{{input.order_id}}", "customer": "{{input.customer}}"}, "depends_on": [], "retries": 0, "timeout_seconds": 30},
        ],
    },
    {
        "name": "Queue priorities and dead letters",
        "description": (
            "Routes urgent work ahead of batch work, coordinates a shared resource, "
            "recovers from a transient failure and moves a permanent failure to the dead-letter queue."
        ),
        "default_max_parallel": 4,
        "tags": ["sample", "dispatch", "dlq"],
        "steps": [
            {"id": "urgent", "type": "demo.echo", "input": {"value": "urgent customer request"}, "queue": "priority", "priority": 90, "retries": 0},
            {"id": "batch", "type": "demo.echo", "input": {"value": "nightly batch"}, "queue": "batch", "priority": 5, "retries": 0},
            {
                "id": "shared", "type": "demo.echo", "input": {"value": "shared resource"},
                "queue": "batch", "concurrency_key": "demo-shared-resource", "concurrency_limit": 1, "retries": 0,
            },
            {"id": "transient", "type": "demo.fail_once", "input": {"value": "retryable failure"}, "depends_on": ["batch"], "retries": 3, "backoff_seconds": 1},
            {"id": "permanent", "type": "demo.add", "input": {"values": "not-a-list"}, "depends_on": ["batch"], "retries": 1},
            {"id": "after_transient", "type": "demo.echo", "input": {"value": "resumed after retry"}, "depends_on": ["transient"]},
        ],
    },
    {
        "name": "Invoice extraction with approval",
        "description": "Extracts invoice fields with ai.extract, pauses for human approval, then archives and notifies.",
        "default_max_parallel": 2,
        "tags": ["sample", "ai", "approval"],
        "steps": [
            {
                "id": "extract",
                "type": "ai.extract",
                "input": {
                    "text": "{{input.invoice_text}}",
                    "json_schema": {
                        "type": "object",
                        "properties": {
                            "invoice_number": {"type": "string"},
                            "vendor": {"type": "string"},
                            "total": {"type": "number"},
                        },
                        "required": ["invoice_number", "vendor", "total"],
                    },
                    "prompt_template": "Extract the invoice number, vendor name and total amount.",
                },
                "timeout_seconds": 300,
            },
            {
                "id": "review",
                "type": "approval",
                "input": {
                    "invoice_number": "{{steps.extract.output.extracted.invoice_number}}",
                    "vendor": "{{steps.extract.output.extracted.vendor}}",
                    "total": "{{steps.extract.output.extracted.total}}",
                },
                "depends_on": ["extract"],
                "approvers": [],
            },
            {
                "id": "archive",
                "type": "storage.put",
                "input": {
                    "key": "invoices/demo-invoice.json",
                    "content": "Invoice {{steps.extract.output.extracted.invoice_number}} from {{steps.extract.output.extracted.vendor}} total {{steps.extract.output.extracted.total}}",
                },
                "depends_on": ["extract", "review"],
            },
            {
                "id": "notify",
                "type": "slack.post",
                "input": {
                    "webhook_url": {"$secret": "SLACK_WEBHOOK"},
                    "text": "Invoice {{steps.extract.output.extracted.invoice_number}} approved and archived.",
                },
                "depends_on": ["extract", "archive"],
            },
        ],
    },
]


def install_demo_workflow(db: Session, owner_id: str, *, publish: bool = True) -> Workflow:
    """Create (or refresh) the sample workflow and publish it once."""
    existing = db.scalar(
        select(Workflow).where(Workflow.owner_id == owner_id, func.lower(Workflow.name) == DEMO_WORKFLOW_NAME.lower())
    )
    if existing is not None:
        workflow = existing
    else:
        workflow = Workflow(
            owner_id=owner_id,
            name=DEMO_DEFINITION["name"],
            description=DEMO_DEFINITION["description"],
            draft=DEMO_DEFINITION,
            default_max_parallel=DEMO_DEFINITION["default_max_parallel"],
        )
        db.add(workflow)
        db.flush()
    if publish and workflow.latest_version == 0:
        workflow_service.publish(db, workflow, note=DEMO_PUBLISH_NOTE, published_by="cli")
    return workflow


def install_demo_workflows(db: Session, owner_id: str) -> list[Workflow]:
    """Install the baseline and advanced examples, idempotently by owner/name."""
    installed = [install_demo_workflow(db, owner_id)]
    for definition in DEMO_ADVANCED_DEFINITIONS:
        workflow = db.scalar(
            select(Workflow).where(
                Workflow.owner_id == owner_id,
                func.lower(Workflow.name) == definition["name"].lower(),
            )
        )
        if workflow is None:
            workflow = Workflow(
                owner_id=owner_id,
                name=definition["name"],
                description=definition.get("description", ""),
                draft=definition,
                default_max_parallel=definition.get("default_max_parallel", 4),
            )
            db.add(workflow)
            db.flush()
        if workflow.latest_version == 0:
            workflow_service.publish(db, workflow, note="Production orchestration demo", published_by="cli")
        installed.append(workflow)
    return installed


def install_demo_webhook_trigger(db: Session, owner_id: str):
    """Create the seeded order webhook once for this owner."""
    from app.models.workflow import WorkflowTrigger
    from app.schemas.trigger import TriggerCreate
    from app.services import trigger_service
    import secrets

    workflow = db.scalar(
        select(Workflow).where(
            Workflow.owner_id == owner_id,
            func.lower(Workflow.name) == "webhook order intake",
        )
    )
    if workflow is None or workflow.latest_version < 1:
        raise ValueError("Install the published Webhook order intake workflow first")
    existing = db.scalar(
        select(WorkflowTrigger).where(
            WorkflowTrigger.owner_id == owner_id,
            WorkflowTrigger.workflow_id == workflow.id,
            WorkflowTrigger.kind == "webhook",
            WorkflowTrigger.name == "Demo order intake",
        )
    )
    if existing is not None:
        return existing
    trigger, _secret = trigger_service.create_trigger(
        db,
        workflow,
        owner_id,
        TriggerCreate(
            name="Demo order intake",
            kind="webhook",
            input_mapping={"order_id": "payload.data.id", "customer": "payload.data.customer"},
            signing_secret=secrets.token_urlsafe(48),
        ),
    )
    return trigger


def definition_for(name: str) -> dict[str, Any]:
    return DEMO_SMOKE_DEFINITION if name == "smoke" else DEMO_DEFINITION


def ensure_owner(db: Session, email: str) -> User:
    from app.core.security import hash_password

    user = db.scalar(select(User).where(func.lower(User.email) == email.lower()))
    if user is not None:
        return user
    user = User(email=email.lower(), display_name=email.split("@")[0], password_hash=hash_password(generate_demo_password()), is_admin=True)
    db.add(user)
    db.flush()
    return user


def generate_demo_password() -> str:
    import secrets

    return f"demo-{secrets.token_urlsafe(9)}"


__all__ = [
    "DEMO_DEFINITION",
    "DEMO_ADVANCED_DEFINITIONS",
    "DEMO_PUBLISH_NOTE",
    "DEMO_SCHEDULE",
    "DEMO_SMOKE_DEFINITION",
    "DEMO_WORKFLOW_NAME",
    "definition_for",
    "ensure_owner",
    "generate_demo_password",
    "install_demo_workflow",
    "install_demo_webhook_trigger",
    "install_demo_workflows",
]
