"""Domain errors and their HTTP mapping.

Services raise these; the API layer converts them into consistent JSON error
bodies. Nothing here leaks internal paths, SQL or stack traces to a client.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, request_id_var
from app.core.security import redact

logger = get_logger("app.errors")


class DomainError(Exception):
    """Base class for expected, user-facing failures."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, details: Any = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.details = details
        if status_code:
            self.status_code = status_code

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details is not None:
            body["error"]["details"] = redact(self.details)
        request_id = request_id_var.get()
        if request_id:
            body["error"]["request_id"] = request_id
        return body


class NotFound(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class Conflict(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class Invalid(DomainError):
    status_code = 422
    code = "invalid_request"


class Unauthorized(DomainError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class Forbidden(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class RateLimited(DomainError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class PayloadTooLarge(DomainError):
    status_code = 413
    code = "payload_too_large"


class ServiceUnavailable(DomainError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_request: Request, exc: DomainError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.exception("Domain error", extra={"code": exc.code})
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())[1:]) or "body",
                "message": error.get("msg", "Invalid value"),
                "code": error.get("type", "invalid"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": "The request could not be validated", "details": issues}},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": code, "message": message}})

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover - defensive
        logger.exception("Unhandled error")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": {"code": "internal_error", "message": "An unexpected error occurred. The incident has been logged."}},
        )


__all__ = [
    "Conflict",
    "DomainError",
    "Forbidden",
    "Invalid",
    "NotFound",
    "PayloadTooLarge",
    "RateLimited",
    "ServiceUnavailable",
    "Unauthorized",
    "register_exception_handlers",
]
