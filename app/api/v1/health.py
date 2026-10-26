import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings, get_settings
from app.core.database import get_engine, ping_database
from app.core.errors import AppError
from app.schemas.health import HealthResponse, ReadinessResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """Liveness probe: the process is up. Does not touch external dependencies,
    so a database outage never causes the orchestrator to restart healthy app processes."""
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
    )


@router.get("/health/ready", response_model=ReadinessResponse)
def readiness(
    settings: Annotated[Settings, Depends(get_settings)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> ReadinessResponse:
    """Readiness probe: can this instance serve traffic right now? The database must be
    reachable and at the schema version of this code. The LLM is deliberately not checked:
    a generation outage only affects /ask (503 there), never search or uploads."""
    try:
        schema_current = ping_database(engine, settings.db_readiness_timeout_ms)
    except SQLAlchemyError as exc:
        # Log the class only: driver messages can include host, port and user names.
        logger.warning("Readiness check failed: database unavailable (%s)", type(exc).__name__)
        raise AppError(
            "service_unavailable",
            "Service is not ready",
            status.HTTP_503_SERVICE_UNAVAILABLE,
            details={"checks": {"database": "unavailable"}},
        ) from None
    if not schema_current:
        # Not migrated yet (or the code is older than the database): do not take traffic.
        logger.warning("Readiness check failed: database schema is not at the expected head")
        raise AppError(
            "service_unavailable",
            "Service is not ready",
            status.HTTP_503_SERVICE_UNAVAILABLE,
            details={"checks": {"database": "ok", "schema": "outdated"}},
        )
    return ReadinessResponse(status="ready", checks={"database": "ok", "schema": "ok"})
