from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models import User
from app.schemas import RobotAssistantMessageRequest, RobotAssistantMessageResponse, RobotAssistantSpeechRequest
from app.services.assistant_speech import AUDIO_TYPE, synthesize
from app.services.robot_assistant import handle_message

router = APIRouter()


@router.post("/robot/assistant/message", response_model=RobotAssistantMessageResponse)
def post_robot_assistant_message(
    payload: RobotAssistantMessageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RobotAssistantMessageResponse:
    """One turn of the text/voice assistant. Missions and schedules it creates go through the
    same endpoint functions, and therefore the same permission checks, as the robots page."""
    result = handle_message(
        db=db,
        user=current_user,
        project_slug=payload.project_slug,
        robot_id=payload.robot_id,
        message=payload.message,
        state=payload.state,
        timezone=payload.timezone,
        robot_connected=payload.robot_connected,
        voice=payload.voice,
        event=payload.event,
        history=payload.history,
        mission_id=payload.mission_id,
    )
    return RobotAssistantMessageResponse(
        reply=result.reply, state=result.state, action=result.action, draft=result.draft
    )


@router.post("/robot/assistant/speech")
def post_robot_assistant_speech(
    payload: RobotAssistantSpeechRequest,
    current_user: User = Depends(get_current_user),
) -> Response:
    """A reply as MP3 audio. 503 tells the browser to speak it with its own voice instead."""
    audio = synthesize(payload.text)
    if audio is None:
        raise HTTPException(status_code=503, detail="The assistant voice is not available.")
    return Response(content=audio, media_type=AUDIO_TYPE, headers={"Cache-Control": "no-store"})
