from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Annotation, FileAsset
from app.schemas import AnnotationCreateRequest, AnnotationResponse

router = APIRouter()


@router.get("/file/{file_id}", response_model=list[AnnotationResponse])
def list_annotations(file_id: str, db: Session = Depends(get_db)) -> list[AnnotationResponse]:
    annotations = db.scalars(select(Annotation).where(Annotation.file_id == file_id)).all()
    return [
        AnnotationResponse(
            id=item.id,
            file_id=item.file_id,
            annotation_type=item.annotation_type,
            data=item.data,
            created_at=item.created_at,
        )
        for item in annotations
    ]


@router.post("/", response_model=AnnotationResponse)
def create_annotation(payload: AnnotationCreateRequest, db: Session = Depends(get_db)) -> AnnotationResponse:
    file_asset = db.scalar(select(FileAsset).where(FileAsset.id == payload.file_id))
    if file_asset is None:
        raise HTTPException(status_code=404, detail="File not found")

    annotation = Annotation(
        file_id=payload.file_id,
        annotation_type=payload.annotation_type,
        data=payload.data,
    )
    db.add(annotation)
    db.commit()
    db.refresh(annotation)
    return AnnotationResponse(
        id=annotation.id,
        file_id=annotation.file_id,
        annotation_type=annotation.annotation_type,
        data=annotation.data,
        created_at=annotation.created_at,
    )
