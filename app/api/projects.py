from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Project
from app.schemas import ProjectResponse

router = APIRouter()


@router.get("/", response_model=list[ProjectResponse])
def list_projects(db: Session = Depends(get_db)) -> list[ProjectResponse]:
    projects = db.scalars(select(Project).order_by(Project.name.asc())).all()
    return [ProjectResponse(id=p.id, name=p.name, slug=p.slug) for p in projects]
