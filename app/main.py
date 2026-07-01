from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import admin, ai, annotations, auth, feedback, files, media, projects, reports, robot_missions, robot_pairings, rooms, upload
from app.api.upload import cleanup_stale_uploads
from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.services.bootstrap import seed_defaults
from app.services.db_migrations import (
    ensure_comparison_drafts_state_json,
    ensure_file_assets_ai_description,
    ensure_file_assets_sha256_hash,
    ensure_project_floorplan_url,
    ensure_project_robot_map_json,
    ensure_project_members_table,
    ensure_projects_fields,
    ensure_projects_owner_name_unique,
    ensure_reports_label,
    ensure_robot_capture_points_table,
    ensure_rooms_fields,
    ensure_rooms_project_name_unique,
    ensure_rooms_slug_scoped_to_project,
    ensure_annotations_extensions,
    ensure_project_activity_table,
    ensure_search_trigram_indexes,
    ensure_users_email_fields,
    ensure_users_is_admin,
    ensure_users_is_robot,
    ensure_users_role_dropped,
)
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
    ensure_file_assets_ai_description(engine)
    ensure_users_is_admin(engine)
    ensure_users_role_dropped(engine)
    ensure_projects_fields(engine)
    ensure_project_members_table(engine)
    ensure_project_floorplan_url(engine)
    ensure_project_robot_map_json(engine)
    ensure_rooms_fields(engine)
    ensure_rooms_slug_scoped_to_project(engine)
    ensure_projects_owner_name_unique(engine)
    ensure_rooms_project_name_unique(engine)
    ensure_users_email_fields(engine)
    ensure_users_is_robot(engine)
    ensure_reports_label(engine)
    ensure_robot_capture_points_table(engine)
    ensure_annotations_extensions(engine)
    ensure_project_activity_table(engine)
    ensure_search_trigram_indexes(engine)
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
    # Disable automatic trailing-slash redirects. Without this, FastAPI generates
    # absolute 307 redirects (e.g. http://backend:3001/api/reports/) that the
    # browser cannot resolve when accessed through the Next.js rewrite proxy.
    redirect_slashes=False,
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
app.include_router(admin.router, prefix="/api/admin", tags=["Admin"])
app.include_router(projects.router, prefix="/api/projects", tags=["Projects"])
app.include_router(rooms.router, prefix="/api/rooms", tags=["Rooms"])
app.include_router(files.router, prefix="/api/files", tags=["Files"])
app.include_router(upload.router, prefix="/api/upload", tags=["Upload"])
app.include_router(robot_missions.router, prefix="/api", tags=["Robot Missions"])
app.include_router(robot_pairings.router, prefix="/api", tags=["Robot Pairing"])
app.include_router(ai.router, prefix="/api/ai", tags=["AI"])
app.include_router(reports.router, prefix="/api/reports", tags=["Reports"])
app.include_router(annotations.router, prefix="/api/annotations", tags=["Annotations"])
app.include_router(media.router, prefix="/api/media", tags=["Media"])
app.include_router(feedback.router, prefix="/api/feedback", tags=["Feedback"])
