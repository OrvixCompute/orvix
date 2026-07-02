"""Custom exception hierarchy and the FastAPI handlers that render them as JSON."""

import sentry_sdk
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.logger import logger


def _report_to_sentry(request: Request, exc: Exception) -> None:
    """Send an unexpected/server error to Sentry with request context.

    A no-op when Sentry is not initialized (empty SENTRY_DSN). Client errors
    (4xx OrvixException subclasses) are expected business logic and are never
    reported here.
    """
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("request_id", getattr(request.state, "request_id", None))
        scope.set_tag("path", request.url.path)
        sentry_sdk.capture_exception(exc)


class OrvixException(Exception):
    """Base class for all application errors.

    Carries an HTTP status code, a stable machine-readable error_code, a human
    message, and an optional details dict that is merged into the error body.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code: str = "internal_error"

    def __init__(
        self,
        message: str | None = None,
        *,
        error_code: str | None = None,
        status_code: int | None = None,
        details: dict | None = None,
    ) -> None:
        self.message = message or self.__class__.__doc__ or "An error occurred"
        if error_code is not None:
            self.error_code = error_code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


class NotFoundError(OrvixException):
    """The requested resource was not found."""

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "not_found"


class UnauthorizedError(OrvixException):
    """Authentication failed or was not provided."""

    status_code = status.HTTP_401_UNAUTHORIZED
    error_code = "unauthorized"


class InsufficientBalanceError(OrvixException):
    """The user does not have enough balance for this operation."""

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    error_code = "insufficient_balance"


class ValidationError(OrvixException):
    """The request was malformed or failed validation."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_code = "invalid_request"


class RateLimitError(OrvixException):
    """The caller has exceeded the allowed request rate."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_code = "rate_limit_exceeded"


def _error_body(request: Request, code: str, message: str, details: dict | None = None) -> dict:
    """Build the standard error envelope."""
    body: dict = {
        "error": {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", None),
        }
    }
    if details:
        body["error"].update(details)
    return body


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers that convert exceptions into the standard JSON envelope."""

    @app.exception_handler(OrvixException)
    async def _handle_orvix(request: Request, exc: OrvixException) -> JSONResponse:
        # 5xx errors are unexpected — log with traceback; 4xx are client errors.
        if exc.status_code >= 500:
            logger.opt(exception=exc).error("OrvixException: {}", exc.message)
            _report_to_sentry(request, exc)
        else:
            logger.warning("{} ({}): {}", exc.error_code, exc.status_code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(request, exc.error_code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning("Request validation failed: {}", exc.errors())
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_error_body(
                request,
                "invalid_request",
                "Request validation failed",
                # jsonable_encoder makes non-JSON-native ctx values (e.g. the
                # Decimal bound on a `gt=0` field) safe to serialize.
                {"details": jsonable_encoder(exc.errors())},
            ),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.opt(exception=exc).error("Unhandled exception: {}", exc)
        _report_to_sentry(request, exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body(request, "internal_error", "An internal error occurred"),
        )
