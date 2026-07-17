import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import envelope
from app.api.router import api_router
from app.core.config import get_settings
from app.core.errors import (
    BusinessError,
    business_error_handler,
    validation_error_handler,
)
from app.db.session import SessionLocal

logger = logging.getLogger("salon_api")
settings = get_settings()

app = FastAPI(
    title="Salon Daily Sales API",
    version="1.0.0",
    docs_url=f"{settings.api_prefix}/docs",
    redoc_url=f"{settings.api_prefix}/redoc",
    openapi_url=f"{settings.api_prefix}/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token", "X-Work-Date", "Accept-Language"],
)
app.add_exception_handler(BusinessError, business_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]


@app.middleware("http")
async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
    request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    try:
        response = await call_next(request)
    except SQLAlchemyError:
        logger.exception("database_error", extra={"request_id": request.state.request_id})
        return JSONResponse(
            {
                "error": {
                    "code": "DATABASE_ERROR",
                    "message": "A database error occurred.",
                    "request_id": request.state.request_id,
                }
            },
            status_code=500,
        )
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.get(f"{settings.api_prefix}/health", tags=["Operations"])
def health(request: Request) -> dict[str, object]:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise BusinessError(503, "DATABASE_UNAVAILABLE", "The database is unavailable.") from exc
    return envelope(request, {"status": "ok", "database": "ready"})


app.include_router(api_router, prefix=settings.api_prefix)
