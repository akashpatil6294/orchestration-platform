"""Storage for large, redacted JSON step outputs.

Local disk is the default for single-host development. S3-compatible object
storage is opt-in and imported lazily so local installs need no cloud SDK.
Artifact metadata is always stored in the application database and every read
is scoped through the owning run before this module is called.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import NotFound, PayloadTooLarge
from app.core.security import redact
from app.models.base import new_id, utcnow
from app.models.run import RunArtifact, StepRun, WorkflowRun


def store_if_large(db: Session, run: WorkflowRun, step: StepRun, output: Any) -> Any:
    """Return inline JSON or an authenticated artifact reference."""
    # Preserve large text fields here; normal API/log redaction truncates long
    # inline strings, while artifact storage must retain the full safe payload.
    safe_output = redact(output, truncate_strings=False)
    try:
        payload = json.dumps(safe_output, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PayloadTooLarge("Task output must be JSON-serialisable") from exc
    if len(payload) > settings.max_task_output_bytes:
        raise PayloadTooLarge(
            f"Task output is {len(payload)} bytes; the limit is {settings.max_task_output_bytes} bytes",
            details={"bytes": len(payload), "limit": settings.max_task_output_bytes},
        )
    if len(payload) <= settings.max_inline_output_bytes:
        return safe_output

    artifact_id = new_id()
    backend = settings.artifact_storage
    storage_key = _write_payload(backend, artifact_id, payload)
    db.add(
        RunArtifact(
            id=artifact_id,
            run_id=run.id,
            step_run_id=step.id,
            storage_backend=backend,
            storage_key=storage_key,
            content_type="application/json",
            size_bytes=len(payload),
            created_at=utcnow(),
        )
    )
    db.flush()
    return {
        "artifact_id": artifact_id,
        "artifact_uri": f"/api/v1/runs/{run.id}/artifacts/{artifact_id}",
        "content_type": "application/json",
        "size_bytes": len(payload),
    }


def read_artifact(artifact: RunArtifact) -> bytes:
    if artifact.storage_backend == "local":
        root = Path(settings.artifact_dir).resolve()
        path = Path(artifact.storage_key).resolve()
        if root not in path.parents:
            raise NotFound("Artifact not found", code="artifact_not_found")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise NotFound("Artifact data is unavailable", code="artifact_not_found") from exc
    if artifact.storage_backend == "s3":
        client = _s3_client()
        try:
            result = client.get_object(Bucket=settings.artifact_s3_bucket, Key=artifact.storage_key)
            return result["Body"].read()
        except Exception as exc:
            raise NotFound("Artifact data is unavailable", code="artifact_not_found") from exc
    raise NotFound("Artifact backend is unavailable", code="artifact_not_found")


def get_run_artifact(db: Session, run_id: str, artifact_id: str) -> RunArtifact:
    row = db.scalar(select(RunArtifact).where(RunArtifact.id == artifact_id, RunArtifact.run_id == run_id))
    if row is None:
        raise NotFound("Artifact not found", code="artifact_not_found")
    return row


def _write_payload(backend: str, artifact_id: str, payload: bytes) -> str:
    if backend == "local":
        root = Path(settings.artifact_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = (root / f"{artifact_id}.json").resolve()
        if root not in path.parents:
            raise ValueError("Artifact path escaped its configured directory")
        with path.open("xb") as output_file:
            output_file.write(payload)
        return str(path)
    if backend == "s3":
        key = f"{artifact_id[:2]}/{artifact_id}.json"
        _s3_client().put_object(
            Bucket=settings.artifact_s3_bucket,
            Key=key,
            Body=payload,
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        return key
    raise ValueError(f"Unsupported artifact storage backend: {backend}")


def _s3_client():
    import boto3

    kwargs: dict[str, Any] = {
        "service_name": "s3",
        "endpoint_url": settings.artifact_s3_endpoint_url or None,
        "region_name": settings.artifact_s3_region,
    }
    if settings.artifact_s3_access_key_id and settings.artifact_s3_secret_access_key:
        kwargs["aws_access_key_id"] = settings.artifact_s3_access_key_id
        kwargs["aws_secret_access_key"] = settings.artifact_s3_secret_access_key
    return boto3.client(**kwargs)


__all__ = ["get_run_artifact", "read_artifact", "store_if_large"]
