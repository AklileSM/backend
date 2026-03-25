from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import AnalyzeImageRequest, AnalyzeImageResponse
from app.services.ai import analyze_image_url

router = APIRouter()


@router.post("/analyze", response_model=AnalyzeImageResponse)
async def analyze_image(
    payload: AnalyzeImageRequest,
    db: Session = Depends(get_db),
) -> AnalyzeImageResponse:
    try:
        result = await analyze_image_url(
            payload.image_url,
            file_id=payload.file_id,
            db=db,
        )
        return AnalyzeImageResponse(**result)
    except ValueError as exc:
        msg = str(exc)
        status = 503 if "HYPERBOLIC_API_KEY" in msg else 400
        raise HTTPException(status_code=status, detail=msg) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI analysis failed: {exc}") from exc
