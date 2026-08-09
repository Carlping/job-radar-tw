from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .storage import APPLICATION_STAGES, Storage

STATIC_DIR = Path(__file__).with_name("web_static")


class ApplicationUpdate(BaseModel):
    stage: str = Field(description="One of the supported application pipeline stages")
    notes: str | None = None


def create_app(settings: Settings | None = None, storage: Storage | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Job Radar TW｜職缺雷達", version="0.1.0")
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def get_storage() -> Storage:
        if storage:
            return storage
        if not settings.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is required")
        return Storage(settings.database_url)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/dashboard")
    def dashboard(
        days: int = Query(30, ge=1, le=365),
        db: Storage = Depends(get_storage),
    ) -> dict:
        return db.dashboard_snapshot(days=days)

    @app.get("/api/jobs")
    def jobs(
        days: int = Query(30, ge=1, le=365),
        limit: int = Query(80, ge=1, le=200),
        stage: str | None = None,
        industry: str | None = None,
        db: Storage = Depends(get_storage),
    ) -> dict[str, list[dict]]:
        return {
            "jobs": db.list_dashboard_jobs(
                days=days,
                limit=limit,
                stage=stage,
                industry=industry,
            )
        }

    @app.patch("/api/jobs/{job_id}/application")
    def update_application(
        job_id: str,
        payload: ApplicationUpdate,
        db: Storage = Depends(get_storage),
    ) -> dict:
        if payload.stage not in APPLICATION_STAGES:
            raise HTTPException(
                status_code=422,
                detail=f"stage must be one of: {', '.join(sorted(APPLICATION_STAGES))}",
            )
        try:
            return {"application": db.set_application_stage(job_id, payload.stage, payload.notes)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    return app


app = create_app()
