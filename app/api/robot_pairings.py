from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.config import get_settings
from app.database import get_db
from app.models import Project, RobotPairingToken, User
from app.schemas import (
    RobotPairingClaimResponse,
    RobotPairingTokenClaimRequest,
    RobotPairingTokenCreateRequest,
    RobotPairingTokenResponse,
)

router = APIRouter()
settings = get_settings()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _resolve_robot_user(robot_id: str, db: Session) -> User:
    robot = db.scalar(
        select(User).where(
            User.is_robot == True,  # noqa: E712
            or_(User.id == robot_id, User.username == robot_id),
        )
    )
    if robot is None:
        raise HTTPException(status_code=404, detail="Robot not found")
    return robot


def _to_response(token: RobotPairingToken) -> RobotPairingTokenResponse:
    return RobotPairingTokenResponse(
        id=token.id,
        robot_id=token.robot_username,
        token=token.token,
        default_project_slug=token.default_project_slug,
        note=token.note,
        expires_at=token.expires_at,
        claimed_at=token.claimed_at,
        claimed_hostname=token.claimed_hostname,
        revoked_at=token.revoked_at,
        created_at=token.created_at,
    )


@router.get("/robot-pairings", response_model=list[RobotPairingTokenResponse])
def list_robot_pairings(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[RobotPairingTokenResponse]:
    rows = db.scalars(
        select(RobotPairingToken).order_by(RobotPairingToken.created_at.desc())
    ).all()
    return [_to_response(row) for row in rows]


@router.post("/robot-pairings", response_model=RobotPairingTokenResponse, status_code=201)
def create_robot_pairing(
    payload: RobotPairingTokenCreateRequest,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RobotPairingTokenResponse:
    robot = _resolve_robot_user(payload.robot_id, db)
    if payload.default_project_slug:
        project = db.scalar(select(Project).where(Project.slug == payload.default_project_slug))
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

    pairing = RobotPairingToken(
        token=secrets.token_urlsafe(24),
        robot_user_id=robot.id,
        robot_username=robot.username,
        robot_password_plaintext=payload.robot_password,
        default_project_slug=payload.default_project_slug,
        note=payload.note,
        created_by_user_id=current_user.id,
        created_by_username=current_user.username,
        expires_at=_utc_now() + timedelta(hours=payload.expires_in_hours),
    )
    db.add(pairing)
    db.commit()
    db.refresh(pairing)
    return _to_response(pairing)


@router.post("/robot-pairings/{pairing_id}/revoke", response_model=RobotPairingTokenResponse)
def revoke_robot_pairing(
    pairing_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RobotPairingTokenResponse:
    pairing = db.scalar(select(RobotPairingToken).where(RobotPairingToken.id == pairing_id))
    if pairing is None:
        raise HTTPException(status_code=404, detail="Pairing token not found")
    if pairing.revoked_at is None:
        pairing.revoked_at = _utc_now()
        db.commit()
        db.refresh(pairing)
    return _to_response(pairing)


@router.post("/robot-pairings/claim", response_model=RobotPairingClaimResponse)
def claim_robot_pairing(
    payload: RobotPairingTokenClaimRequest,
    db: Session = Depends(get_db),
) -> RobotPairingClaimResponse:
    pairing = db.scalar(select(RobotPairingToken).where(RobotPairingToken.token == payload.token))
    if pairing is None:
        raise HTTPException(status_code=404, detail="Pairing token not found")
    if pairing.revoked_at is not None:
        raise HTTPException(status_code=410, detail="Pairing token has been revoked")
    if pairing.expires_at and pairing.expires_at < _utc_now():
        raise HTTPException(status_code=410, detail="Pairing token has expired")
    if pairing.claimed_at is not None:
        raise HTTPException(status_code=409, detail="Pairing token has already been claimed")

    pairing.claimed_at = _utc_now()
    pairing.claimed_hostname = payload.hostname
    db.commit()

    return RobotPairingClaimResponse(
        robot_id=pairing.robot_username,
        base_url=settings.frontend_url.rstrip("/"),
        username=pairing.robot_username,
        password=pairing.robot_password_plaintext,
        default_project_slug=pairing.default_project_slug,
    )
