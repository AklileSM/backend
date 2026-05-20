import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.database import get_db
from app.models import User
from app.schemas import (
    PasswordResetConfirmSchema,
    PasswordResetRequestSchema,
    TokenResponse,
    UserLoginRequest,
    UserPublic,
    UserRegisterRequest,
)
from app.services.email import send_password_reset_email, send_verification_email

router = APIRouter()

_TOKEN_EXPIRY_VERIFICATION = timedelta(days=7)
_TOKEN_EXPIRY_RESET = timedelta(hours=1)


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def _to_public(user: User) -> UserPublic:
    return UserPublic(
        id=user.id,
        username=user.username,
        email=user.email,
        is_admin=user.is_admin,
        email_verified=user.email_verified,
    )


@router.post("/register", response_model=TokenResponse)
def register(payload: UserRegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user_count = db.scalar(select(func.count()).select_from(User)) or 0
    is_admin = user_count == 0

    email = payload.email.strip() or None
    verification_token: str | None = None
    verification_expires: datetime | None = None
    if email:
        verification_token = _generate_token()
        verification_expires = datetime.utcnow() + _TOKEN_EXPIRY_VERIFICATION

    user = User(
        username=payload.username.strip(),
        email=email,
        password_hash=hash_password(payload.password),
        is_admin=is_admin,
        email_verified=False,
        email_verification_token=verification_token,
        email_verification_token_expires_at=verification_expires,
    )
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Username or email already registered") from None

    if email and verification_token:
        send_verification_email(email, verification_token)

    token = create_access_token(subject=user.id, username=user.username, is_admin=user.is_admin)
    return TokenResponse(access_token=token, user=_to_public(user))


@router.post("/login", response_model=TokenResponse)
def login(payload: UserLoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.username == payload.username.strip()))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    token = create_access_token(subject=user.id, username=user.username, is_admin=user.is_admin)
    return TokenResponse(access_token=token, user=_to_public(user))


@router.get("/me", response_model=UserPublic)
def me(current: User = Depends(get_current_user)) -> UserPublic:
    return _to_public(current)


@router.post("/resend-verification", status_code=204)
def resend_verification(
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    if not current.email:
        raise HTTPException(status_code=400, detail="No email address on file")
    if current.email_verified:
        raise HTTPException(status_code=400, detail="Email is already verified")

    token = _generate_token()
    current.email_verification_token = token
    current.email_verification_token_expires_at = datetime.utcnow() + _TOKEN_EXPIRY_VERIFICATION
    db.add(current)
    db.commit()

    send_verification_email(current.email, token)


@router.post("/verify-email", status_code=204)
def verify_email(
    token: str = Query(...),
    db: Session = Depends(get_db),
) -> None:
    user = db.scalar(select(User).where(User.email_verification_token == token))
    if user is None:
        raise HTTPException(status_code=400, detail="This verification link is invalid or has expired.")
    if user.email_verification_token_expires_at and user.email_verification_token_expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="This verification link is invalid or has expired.")

    user.email_verified = True
    user.email_verification_token = None
    user.email_verification_token_expires_at = None
    db.add(user)
    db.commit()


@router.post("/request-password-reset", status_code=204)
def request_password_reset(
    payload: PasswordResetRequestSchema,
    db: Session = Depends(get_db),
) -> None:
    email = payload.email.strip().lower()
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if user is None or not user.is_active or not user.email_verified:
        # Always return 204 to prevent account enumeration, and to avoid
        # leaking whether an unverified address is associated with an account.
        # Password reset is gated on email_verified so that an attacker who
        # registered an account against someone else's address cannot take
        # it over once the real owner gains access to their mailbox.
        return

    token = _generate_token()
    user.password_reset_token = token
    user.password_reset_token_expires_at = datetime.utcnow() + _TOKEN_EXPIRY_RESET
    db.add(user)
    db.commit()

    send_password_reset_email(user.email, token)  # type: ignore[arg-type]


@router.get("/validate-reset-token", status_code=204)
def validate_reset_token(
    token: str = Query(...),
    db: Session = Depends(get_db),
) -> None:
    user = db.scalar(select(User).where(User.password_reset_token == token))
    if user is None:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired.")
    if user.password_reset_token_expires_at and user.password_reset_token_expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired.")


@router.post("/reset-password", status_code=204)
def reset_password(
    payload: PasswordResetConfirmSchema,
    db: Session = Depends(get_db),
) -> None:
    user = db.scalar(select(User).where(User.password_reset_token == payload.token))
    if user is None:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired.")
    if user.password_reset_token_expires_at and user.password_reset_token_expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired.")

    user.password_hash = hash_password(payload.new_password)
    user.password_reset_token = None
    user.password_reset_token_expires_at = None
    db.add(user)
    db.commit()
