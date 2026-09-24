from fastapi import APIRouter

from . import app_about, app_health

router = APIRouter(prefix="/app")

router.include_router(app_about.router, tags=["App: About"])
router.include_router(app_health.router, tags=["App: Health"])
