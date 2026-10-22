from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models import User
from app.schemas import RobotAssistantMessageRequest, RobotAssistantMessageResponse
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
