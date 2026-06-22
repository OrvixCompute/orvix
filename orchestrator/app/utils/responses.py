"""Helpers for building consistent JSON responses."""

from typing import Any

from fastapi.responses import JSONResponse


def success(data: Any, status_code: int = 200) -> JSONResponse:
    """Wrap a payload in a standard success envelope."""
    return JSONResponse(status_code=status_code, content={"data": data})


def error(code: str, message: str, status_code: int = 400, **extra: Any) -> JSONResponse:
    """Build a standard error envelope (mirrors exceptions.register_exception_handlers)."""
    body = {"error": {"code": code, "message": message, **extra}}
    return JSONResponse(status_code=status_code, content=body)
