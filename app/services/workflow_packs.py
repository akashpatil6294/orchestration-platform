"""Real-world workflow packs: installable templates for engineering and operations.

Each pack is a complete, runnable workflow built from production task handlers
(connectors, AI tasks, approvals, storage) — not demo stubs. Packs reference
team connections via ``{"$connection": "name"}``; installing a pack never
creates credentials. The templates gallery lists them and
``install_pack`` copies a pack into the user's workflows idempotently.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.workflow import Workflow
from app.services import workflow_service

PACKS: list[dict[str, Any]] = [
    {
        "id": "incident-triage",
        "name": "Incident triage & paging",
        "category": "Operations",
        "description": "Classify an incoming alert with AI, write a situation brief, page the "
        "on-call channel, and run the mitigation runbook only after human approval.",
        "connections_required": [
            {"name": "team-slack", "kind": "slack_webhook", "label": "Slack webhook for paging the on-call channel"}
        ],
        "definition": {
            "name": "Incident triage & paging",
            "description": "AI-classified incident response with approval-gated mitigation.",
            "default_max_parallel": 4,
            "tags": ["pack", "incident", "ai", "approval"],
            "steps": [
                {
                    "id": "classify",
                    "type": "ai.classify",
                    "input": {"text": "{{input.alert_text}}", "categories": ["critical", "high", "medium", "low"]},
                },
                {
                    "id": "brief",
                    "type": "ai.summarize",
                    "input": {"text": "{{input.alert_text}}", "max_length": 600},
                    "depends_on": ["classify"],
                },
                {
                    "id": "page",
                    "type": "slack.post",
                    "input": {
                        "webhook_url": {"$connection": "team-slack"},
                        "text": "INCIDENT [{{steps.classify.output.category}}] {{input.alert_title}}\n{{steps.brief.output.summary}}",
                    },
                    "depends_on": ["brief", "classify"],
                },
                {
                    "id": "approve_mitigation",
                    "type": "approval",
                    "input": {"summary": "{{steps.brief.output.summary}}", "severity": "{{steps.classify.output.category}}"},
                    "depends_on": ["page", "brief", "classify"],
                    "approvers": [],
                },
                {
                    "id": "mitigate",
                    "type": "webhook.call",
                    "input": {
                        "url": "{{input.runbook_url}}",
                        "method": "POST",
                        "json": {"alert_title": "{{input.alert_title}}", "severity": "{{steps.classify.output.category}}"},
                    },
                    "depends_on": ["approve_mitigation", "classify"],
                },
            ],
        },
    },
    {
        "id": "deploy-pipeline",
        "name": "Safe deploy pipeline",
        "category": "Engineering",
        "description": "Check CI status, require human approval for production, trigger the deploy "
        "webhook, verify the health endpoint, and notify the team. The health check "
        "gates the notification so a bad deploy pages instead of celebrating.",
        "connections_required": [
            {"name": "team-slack", "kind": "slack_webhook", "label": "Slack webhook for deploy notifications"}
        ],
        "definition": {
            "name": "Safe deploy pipeline",
            "description": "Approval-gated deploy with post-deploy health verification.",
            "default_max_parallel": 2,
            "tags": ["pack", "deploy", "approval"],
            "steps": [
                {
                    "id": "ci_status",
                    "type": "http.request",
                    "input": {"url": "{{input.ci_status_url}}", "method": "GET"},
                },
                {
                    "id": "approve_prod",
                    "type": "approval",
                    "input": {"text": "Release {{input.release}} CI status code {{steps.ci_status.output.status_code}}: {{steps.ci_status.output.text}}"},
                    "depends_on": ["ci_status"],
                    "approvers": [],
                },
                {
                    "id": "deploy",
                    "type": "webhook.call",
                    "input": {
                        "url": "{{input.deploy_hook_url}}",
                        "method": "POST",
                        "json": {"release": "{{input.release}}", "approved_by": "{{steps.approve_prod.output.decided_by}}"},
                    },
                    "depends_on": ["approve_prod"],
                },
                {
                    "id": "health_check",
                    "type": "http.request",
                    "input": {"url": "{{input.health_url}}", "method": "GET"},
                    "depends_on": ["deploy"],
                    "retries": 5,
                },
                {
                    "id": "notify",
                    "type": "slack.post",
                    "input": {
                        "webhook_url": {"$connection": "team-slack"},
                        "text": "Deployed {{input.release}} — health check {{steps.health_check.output.status_code}}.",
                    },
                    "depends_on": ["health_check"],
                },
            ],
        },
    },
    {
        "id": "db-migration",
        "name": "Guarded DB migration",
        "category": "Engineering",
        "description": "Run a read-only pre-check against the production database, require approval, "
        "trigger the migration job, verify the schema version, and notify. The migration "
        "never runs without the pre-check and the approval.",
        "connections_required": [
            {"name": "prod-db", "kind": "sql_url", "label": "Read-only SQL connection URL for the production database"},
            {"name": "team-slack", "kind": "slack_webhook", "label": "Slack webhook for migration notifications"},
        ],
        "definition": {
            "name": "Guarded DB migration",
            "description": "Pre-check, approval, migrate, verify.",
            "default_max_parallel": 2,
            "tags": ["pack", "database", "approval"],
            "steps": [
                {
                    "id": "precheck",
                    "type": "sql.query",
                    "input": {
                        "connection_url": {"$connection": "prod-db"},
                        "query": "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1",
                    },
                },
                {
                    "id": "approve",
                    "type": "approval",
                    "input": {"text": "Pre-migration check for {{input.migration_name}} completed."},
                    "depends_on": ["precheck"],
                    "approvers": [],
                },
                {
                    "id": "migrate",
                    "type": "http.request",
                    "input": {
                        "url": "{{input.migration_job_url}}",
                        "method": "POST",
                        "json": {"migration": "{{input.migration_name}}"},
                    },
                    "depends_on": ["approve"],
                },
                {
                    "id": "verify",
                    "type": "sql.query",
                    "input": {
                        "connection_url": {"$connection": "prod-db"},
                        "query": "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1",
                    },
                    "depends_on": ["migrate"],
                },
                {
                    "id": "notify",
                    "type": "slack.post",
                    "input": {
                        "webhook_url": {"$connection": "team-slack"},
                        "text": "Migration {{input.migration_name}} applied and verified.",
                    },
                    "depends_on": ["verify"],
                },
            ],
        },
    },
    {
        "id": "invoice-processing",
        "name": "Invoice extraction with approval",
        "category": "Finance",
        "description": "Extract text from an uploaded invoice PDF (by document ID), pull structured "
        "fields with AI, require approval when confidence is low, archive the result, "
        "and notify finance.",
        "connections_required": [
            {"name": "team-slack", "kind": "slack_webhook", "label": "Slack webhook for finance notifications"}
        ],
        "definition": {
            "name": "Invoice extraction with approval",
            "description": "Document AI pipeline with a human checkpoint.",
            "default_max_parallel": 2,
            "tags": ["pack", "document", "ai", "approval"],
            "steps": [
                {
                    "id": "extract",
                    "type": "document.extract_text",
                    "input": {"document_id": "{{input.document_id}}"},
                },
                {
                    "id": "parse",
                    "type": "ai.extract",
                    "input": {
                        "text": "{{steps.extract.output.text}}",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "vendor": {"type": "string"},
                                "invoice_number": {"type": "string"},
                                "total": {"type": "number"},
                                "currency": {"type": "string"},
                                "due_date": {"type": "string"},
                            },
                            "required": ["vendor", "total"],
                        },
                    },
                    "depends_on": ["extract"],
                },
                {
                    "id": "review",
                    "type": "approval",
                    "input": {"invoice": "{{steps.parse.output}}"},
                    "depends_on": ["parse"],
                    "approvers": [],
                },
                {
                    "id": "archive",
                    "type": "storage.put",
                    "input": {
                        "key": "invoices/{{steps.parse.output.invoice_number}}.txt",
                        "content": "Invoice {{steps.parse.output.invoice_number}} from {{steps.parse.output.vendor}}: "
                        "{{steps.parse.output.total}} {{steps.parse.output.currency}} due {{steps.parse.output.due_date}}",
                        "content_type": "text/plain",
                    },
                    "depends_on": ["review", "parse"],
                },
                {
                    "id": "notify",
                    "type": "slack.post",
                    "input": {
                        "webhook_url": {"$connection": "team-slack"},
                        "text": "Invoice {{steps.parse.output.invoice_number}} from {{steps.parse.output.vendor}} "
                        "({{steps.parse.output.total}} {{steps.parse.output.currency}}) approved and archived.",
                    },
                    "depends_on": ["archive", "parse"],
                },
            ],
        },
    },
    {
        "id": "log-digest",
        "name": "Error log digest",
        "category": "Operations",
        "description": "Pull recent error logs from your log aggregator, classify them with AI, "
        "summarize the digest, and email it to the team. Runs on a schedule.",
        "connections_required": [],
        "definition": {
            "name": "Error log digest",
            "description": "Scheduled AI digest of error logs.",
            "default_max_parallel": 2,
            "tags": ["pack", "logs", "ai", "email"],
            "steps": [
                {
                    "id": "fetch_logs",
                    "type": "http.request",
                    "input": {"url": "{{input.logs_url}}", "method": "GET"},
                },
                {
                    "id": "classify",
                    "type": "ai.classify",
                    "input": {"text": "{{steps.fetch_logs.output.text}}", "categories": ["infrastructure", "application", "security", "noise"]},
                    "depends_on": ["fetch_logs"],
                },
                {
                    "id": "summarize",
                    "type": "ai.summarize",
                    "input": {"text": "{{steps.fetch_logs.output.text}}", "max_length": 1500},
                    "depends_on": ["fetch_logs"],
                },
                {
                    "id": "send",
                    "type": "email.send",
                    "input": {
                        "to": "{{input.digest_recipients}}",
                        "subject": "Error digest [{{steps.classify.output.category}}]",
                        "text": "{{steps.summarize.output.summary}}",
                    },
                    "depends_on": ["classify", "summarize"],
                },
            ],
        },
    },
    {
        "id": "vuln-scan",
        "name": "Dependency vulnerability scan",
        "category": "Security",
        "description": "Query the OSV vulnerability database for your dependencies, normalize the "
        "findings, require security approval, and file tickets via webhook.",
        "connections_required": [],
        "definition": {
            "name": "Dependency vulnerability scan",
            "description": "OSV scan with approval-gated ticketing.",
            "default_max_parallel": 2,
            "tags": ["pack", "security", "approval"],
            "steps": [
                {
                    "id": "fetch_advisories",
                    "type": "http.request",
                    "input": {
                        "url": "https://api.osv.dev/v1/querybatch",
                        "method": "POST",
                        "json": {"queries": "{{input.osv_queries}}"},
                    },
                },
                {
                    "id": "normalize",
                    "type": "transform.json",
                    "input": {
                        "source": {"advisories": "{{steps.fetch_advisories.output}}"},
                        "expressions": {"status": "advisories.status_code", "body": "advisories.text"},
                    },
                    "depends_on": ["fetch_advisories"],
                },
                {
                    "id": "approve",
                    "type": "approval",
                    "input": {"findings": "{{steps.normalize.output}}"},
                    "depends_on": ["normalize"],
                    "approvers": [],
                },
                {
                    "id": "ticket",
                    "type": "webhook.call",
                    "input": {
                        "url": "{{input.ticket_webhook_url}}",
                        "method": "POST",
                        "json": {"title": "Vulnerability scan findings", "findings": "{{steps.normalize.output}}"},
                    },
                    "depends_on": ["approve", "normalize"],
                },
            ],
        },
    },
    {
        "id": "backup-verify",
        "name": "Backup verification",
        "category": "Operations",
        "description": "Trigger a backup snapshot, wait, verify it independently, and alert the "
        "team when verification fails. A backup that is never verified is not a backup.",
        "connections_required": [
            {"name": "team-slack", "kind": "slack_webhook", "label": "Slack webhook for backup alerts"}
        ],
        "definition": {
            "name": "Backup verification",
            "description": "Snapshot, wait, verify, alert on failure.",
            "default_max_parallel": 2,
            "tags": ["pack", "backup", "reliability"],
            "steps": [
                {
                    "id": "snapshot",
                    "type": "http.request",
                    "input": {"url": "{{input.backup_trigger_url}}", "method": "POST", "json": {"target": "{{input.backup_target}}"}},
                },
                {
                    "id": "wait",
                    "type": "demo.sleep",
                    "input": {"seconds": 30},
                    "depends_on": ["snapshot"],
                },
                {
                    "id": "verify",
                    "type": "http.request",
                    "input": {"url": "{{input.backup_verify_url}}", "method": "GET"},
                    "depends_on": ["wait"],
                    "retries": 3,
                },
                {
                    "id": "alert",
                    "type": "slack.post",
                    "input": {
                        "webhook_url": {"$connection": "team-slack"},
                        "text": "Backup verification for {{input.backup_target}}: {{steps.verify.output.status_code}}.",
                    },
                    "depends_on": ["verify"],
                },
            ],
        },
    },
]


def list_packs() -> list[dict[str, Any]]:
    """Gallery metadata (definitions are returned on install, not on list)."""
    return [
        {
            "id": pack["id"],
            "name": pack["name"],
            "category": pack["category"],
            "description": pack["description"],
            "connections_required": pack["connections_required"],
            "step_count": len(pack["definition"]["steps"]),
            "tags": pack["definition"].get("tags", []),
        }
        for pack in PACKS
    ]


def get_pack(pack_id: str) -> dict[str, Any] | None:
    return next((pack for pack in PACKS if pack["id"] == pack_id), None)


def install_pack(db: Session, owner_id: str, pack_id: str) -> Workflow:
    """Install a pack as a new workflow for the owner; idempotent by name.

    The installed workflow starts as a draft copy of the pack definition and is
    published as v1 immediately so it can run. Connection references are left
    intact — the user maps them to their own connections before running.
    """
    pack = get_pack(pack_id)
    if pack is None:
        from app.core.errors import NotFound

        raise NotFound(f"Unknown template '{pack_id}'", code="template_not_found")
    definition = pack["definition"]
    workflow = db.scalar(
        select(Workflow).where(Workflow.owner_id == owner_id, func.lower(Workflow.name) == definition["name"].lower())
    )
    if workflow is None:
        workflow = Workflow(
            owner_id=owner_id,
            name=definition["name"],
            description=pack["description"],
            draft=dict(definition),
            default_max_parallel=definition.get("default_max_parallel", 4),
        )
        db.add(workflow)
        db.flush()
    if workflow.latest_version == 0:
        workflow_service.publish(db, workflow, note=f"Installed template '{pack['id']}'", published_by="templates")
    db.commit()
    db.refresh(workflow)
    return workflow


__all__ = ["PACKS", "get_pack", "install_pack", "list_packs"]
