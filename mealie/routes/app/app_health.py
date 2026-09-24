from fastapi import APIRouter, Response
from sqlalchemy.exc import SQLAlchemyError

from mealie.db.db_setup import engine
from mealie.db.health import DatabaseProbe

router = APIRouter(prefix="/health")

# Read a table, rather than only SELECT 1, to exercise SQLite storage as well as
# database connectivity. An empty users table is still a successful query.
database_probe = DatabaseProbe(engine, "SELECT 1 FROM users LIMIT 1")
READINESS_TIMEOUT_SECONDS = 3.0


@router.get("/live", response_class=Response)
async def live() -> Response:
    """Check that the application can respond, without checking dependencies."""
    return Response(status_code=200, headers={"Cache-Control": "no-store"})


@router.get("/ready", response_class=Response, responses={503: {"description": "Database unavailable"}})
async def ready() -> Response:
    """Check database access with a bounded wait and no public error details."""
    try:
        await database_probe.check(READINESS_TIMEOUT_SECONDS)
    except SQLAlchemyError, TimeoutError:
        return Response(status_code=503, headers={"Cache-Control": "no-store"})
    return Response(status_code=200, headers={"Cache-Control": "no-store"})
