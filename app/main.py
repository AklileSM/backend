from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import ai, annotations, files, projects, reports, rooms, upload
from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.services.bootstrap import seed_defaults
from app.services.storage import storage_service

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
        seed_defaults(db)
    storage_service.ensure_buckets()
    yield


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
    allow_origins=[settings.frontend_url, "http://localhost:5173", "http://127.0.0.1:5173"],
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


app.include_router(projects.router, prefix="/api/projects", tags=["Projects"])
app.include_router(rooms.router, prefix="/api/rooms", tags=["Rooms"])
app.include_router(files.router, prefix="/api/files", tags=["Files"])
app.include_router(upload.router, prefix="/api/upload", tags=["Upload"])
app.include_router(ai.router, prefix="/api/ai", tags=["AI"])
app.include_router(reports.router, prefix="/api/reports", tags=["Reports"])
app.include_router(annotations.router, prefix="/api/annotations", tags=["Annotations"])
