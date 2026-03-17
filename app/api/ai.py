from fastapi import APIRouter, HTTPException

from app.schemas import AnalyzeImageRequest, AnalyzeImageResponse
from app.services.ai import analyze_image_url

router = APIRouter()


@router.post("/analyze", response_model=AnalyzeImageResponse)
async def analyze_image(payload: AnalyzeImageRequest) -> AnalyzeImageResponse:
    try:
        result = await analyze_image_url(payload.image_url)
        return AnalyzeImageResponse(**result)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI analysis failed: {exc}") from exc
