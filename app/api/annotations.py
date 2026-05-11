from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Annotation, FileAsset
from app.schemas import AnnotationCreateRequest, AnnotationResponse, AnnotationUpdateRequest

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


@router.post("", response_model=AnnotationResponse)
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


@router.patch("/{annotation_id}", response_model=AnnotationResponse)
def update_annotation(
    annotation_id: str,
    payload: AnnotationUpdateRequest,
    db: Session = Depends(get_db),
) -> AnnotationResponse:
    annotation = db.scalar(select(Annotation).where(Annotation.id == annotation_id))
    if annotation is None:
        raise HTTPException(status_code=404, detail="Annotation not found")

    if payload.annotation_type is not None and str(payload.annotation_type).strip():
        annotation.annotation_type = str(payload.annotation_type).strip()
    if payload.data is not None:
        annotation.data = payload.data

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


@router.delete("/{annotation_id}", status_code=204)
def delete_annotation(annotation_id: str, db: Session = Depends(get_db)) -> None:
    annotation = db.scalar(select(Annotation).where(Annotation.id == annotation_id))
    if annotation is None:
        raise HTTPException(status_code=404, detail="Annotation not found")
    db.delete(annotation)
    db.commit()
