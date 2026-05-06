from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FileAsset
from app.schemas import AnalyzeImageRequest, AnalyzeImageResponse
from app.services.ai import analyze_image_url

router = APIRouter()


@router.post("/analyze", response_model=AnalyzeImageResponse)
async def analyze_image(
    payload: AnalyzeImageRequest,
    db: Session = Depends(get_db),
) -> AnalyzeImageResponse | JSONResponse:
    if payload.file_id:
        asset = db.scalar(select(FileAsset).where(FileAsset.id == payload.file_id))
        if asset is not None and asset.media_type == "image":
            status = asset.ai_description_status

            if status == "done" and asset.ai_description:
                return AnalyzeImageResponse(description=asset.ai_description, cached=True)

            if status == "generating":
                return JSONResponse(
                    status_code=202,
                    content={"status": "generating", "message": "Analysis in progress, please wait"},
                )

            # null or failed — run inference now and save the result

    try:
        result = await analyze_image_url(
            payload.image_url,
            file_id=payload.file_id,
            db=db,
        )

        if payload.file_id:
            asset = db.scalar(select(FileAsset).where(FileAsset.id == payload.file_id))
            if asset is not None:
                asset.ai_description = result["description"]
                asset.ai_description_status = "done"
                db.commit()

        return AnalyzeImageResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if payload.file_id:
            try:
                asset = db.scalar(select(FileAsset).where(FileAsset.id == payload.file_id))
                if asset is not None:
                    asset.ai_description_status = "failed"
                    db.commit()
            except Exception:
                pass
        raise HTTPException(status_code=502, detail=f"AI analysis failed: {exc}") from exc
