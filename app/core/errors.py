from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class BusinessError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        field_errors: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.field_errors = field_errors or []
        self.extra = extra or {}


def error_payload(request: Request, error: BusinessError) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": error.code,
        "message": error.message,
        "request_id": getattr(request.state, "request_id", None),
    }
    if error.field_errors:
        body["field_errors"] = error.field_errors
    body.update(error.extra)
    return {"error": body}


async def business_error_handler(request: Request, error: BusinessError) -> JSONResponse:
    return JSONResponse(error_payload(request, error), status_code=error.status_code)


async def validation_error_handler(request: Request, error: RequestValidationError) -> JSONResponse:
    field_errors = []
    for item in error.errors():
        location = [str(part) for part in item.get("loc", []) if part not in {"body", "query"}]
        field_errors.append(
            {
                "field": ".".join(location) or "request",
                "code": str(item.get("type", "INVALID_VALUE")).upper(),
                "message": item.get("msg", "Invalid value"),
            }
        )
    business_error = BusinessError(
        422,
        "VALIDATION_ERROR",
        "Check the submitted fields.",
        field_errors=field_errors,
    )
    return JSONResponse(error_payload(request, business_error), status_code=422)
