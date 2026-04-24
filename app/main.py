from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import ai, annotations, auth, files, projects, reports, rooms, upload
from app.api.upload import cleanup_stale_uploads
from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.services.bootstrap import seed_defaults
from app.services.db_migrations import ensure_comparison_drafts_state_json, ensure_file_assets_sha256_hash
from app.services.pointcloud import init_converter_pool, reset_interrupted_conversions, shutdown_converter_pool
from app.services.storage import storage_service

settings = get_settings()


def _cors_origins() -> list[str]:
    bases = [
        settings.frontend_url,
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost",
        "http://127.0.0.1",
    ]
    extras = [x.strip() for x in settings.cors_extra_origins.split(",") if x.strip()]
    return list(dict.fromkeys([*bases, *extras]))


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_comparison_drafts_state_json(engine)
    ensure_file_assets_sha256_hash(engine)
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
        seed_defaults(db)
    storage_service.ensure_buckets()
    cleanup_stale_uploads()
    reset_interrupted_conversions()
    init_converter_pool(max_workers=2)
    yield
    shutdown_converter_pool()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
@app.get("/api/health")
def healthcheck() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.app_env,
        "storage": storage_service.healthcheck(),
    }


app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(projects.router, prefix="/api/projects", tags=["Projects"])
app.include_router(rooms.router, prefix="/api/rooms", tags=["Rooms"])
app.include_router(files.router, prefix="/api/files", tags=["Files"])
app.include_router(upload.router, prefix="/api/upload", tags=["Upload"])
app.include_router(ai.router, prefix="/api/ai", tags=["AI"])
app.include_router(reports.router, prefix="/api/reports", tags=["Reports"])
app.include_router(annotations.router, prefix="/api/annotations", tags=["Annotations"])
