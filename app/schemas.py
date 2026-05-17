from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class MediaFileResponse(BaseModel):
    id: str
    src: str
    type: str
    file_name: str
    full_src: str | None = None
    capture_date: date
    uploaded_by_user_id: str | None = None
    conversion_status: str | None = None
    conversion_error: str | None = None


class MyUploadItemResponse(BaseModel):
    """File asset uploaded by the current user (see metadata_json.uploaded_by_user_id)."""

    id: str
    room_slug: str
    room_name: str
    media_type: str
    file_name: str
    capture_date: date
    created_at: datetime
    src: str
    full_src: str | None = None
    conversion_status: str | None = None


class RoomMediaGroup(BaseModel):
    images: list[MediaFileResponse] = Field(default_factory=list)
    videos: list[MediaFileResponse] = Field(default_factory=list)
    pointclouds: list[MediaFileResponse] = Field(default_factory=list)
    pdfs: list[MediaFileResponse] = Field(default_factory=list)


class ExplorerByDateResponse(BaseModel):
    date: str
    rooms: dict[str, RoomMediaGroup]


class ExplorerByRoomResponse(BaseModel):
    room: str
    room_name: str
    dates: dict[str, RoomMediaGroup]


class DateMediaCounts(BaseModel):
    images: int
    videos: int
    pointclouds: int
    pdfs: int


class ExplorerDatesSummaryResponse(BaseModel):
    dates: dict[str, DateMediaCounts]


class ProjectResponse(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None = None
    location: str | None = None
    status: str = "active"
    owner_id: str | None = None
    floorplan_url: str | None = None
    created_at: datetime
    updated_at: datetime


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    description: str | None = None
    location: str | None = None


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    location: str | None = None
    status: str | None = None


class ProjectMemberResponse(BaseModel):
    user_id: str
    username: str
    email: str | None
    role: str
    joined_at: datetime


class ProjectMemberAddRequest(BaseModel):
    user_id: str
    role: str = Field(default="viewer", pattern=r"^(owner|editor|viewer)$")


class ProjectMemberUpdateRequest(BaseModel):
    role: str = Field(pattern=r"^(owner|editor|viewer)$")


class AdminUserResponse(BaseModel):
    id: str
    username: str
    email: str | None
    is_admin: bool
    is_active: bool
    created_at: datetime


class AdminUserUpdateRequest(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None
    email: str | None = None


class RoomResponse(BaseModel):
    id: str
    name: str
    slug: str
    project_id: str
    floor_plan_coordinates: dict | None = None
    sort_order: int = 0


class RoomCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    sort_order: int = 0


class RoomUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    floor_plan_coordinates: dict | None = None
    sort_order: int | None = None


class AnalyzeImageRequest(BaseModel):
    image_url: str
    file_id: str | None = None


class AnalyzeImageResponse(BaseModel):
    description: str
    cached: bool


class UploadResponse(BaseModel):
    id: str
    room: str
    media_type: str
    file_name: str
    capture_date: date


class BulkFileIdsRequest(BaseModel):
    ids: list[str]


class BulkActionResponse(BaseModel):
    """Result of a bulk operation across many file_assets.

    Numbers are: how many succeeded, how many were skipped because the user
    isn't allowed / the row was gone / it wasn't downloadable.
    """

    affected: int
    skipped: int


class PrecheckHashRequest(BaseModel):
    sha256_hash: str


class ProjectActivityEntry(BaseModel):
    """One row of the project activity feed."""

    id: str
    project_id: str
    user_id: str | None = None
    username: str
    action: str
    target_type: str | None = None
    target_id: str | None = None
    metadata: dict | None = None
    created_at: datetime


class PrecheckHashResponse(BaseModel):
    """Informational duplicate check; never raises 409, so the frontend can
    surface the existing-file info inline instead of waiting for the upload."""

    duplicate: bool
    room_name: str | None = None
    capture_date: date | None = None
    display_name: str | None = None


class ReportCreateRequest(BaseModel):
    file_id: str
    ai_description: str | None = None
    manual_observations: str | None = None
    flags: list[str] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)


class ReportResponse(BaseModel):
    id: str
    file_id: str
    label: str | None = None
    ai_description: str | None = None
    manual_observations: str | None = None
    flags: list[str] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    created_by: str | None = None
    pdf_url: str | None = None
    created_at: datetime


class ComparisonDraftResponse(BaseModel):
    id: str
    file_id: str
    """Human-readable name, e.g. left display name vs right display name."""
    label: str | None = None
    manual_observations: str | None = None
    flags: list[str] = Field(default_factory=list)
    pdf_url: str | None = None
    created_at: datetime


class ComparisonDraftDetailResponse(ComparisonDraftResponse):
    state_json: dict[str, Any] | None = None


class ComparisonDraftCreateRequest(BaseModel):
    file_id: str
    manual_observations: str | None = None
    flags: list[str] = Field(default_factory=list)
    state: dict[str, Any]


class ComparisonDraftUpdateRequest(BaseModel):
    file_id: str | None = None
    manual_observations: str | None = None
    flags: list[str] | None = None
    state: dict[str, Any] | None = None


class ViewerDraftResponse(BaseModel):
    id: str
    file_id: str
    viewer_kind: str
    label: str | None = None
    manual_observations: str | None = None
    flags: list[str] = Field(default_factory=list)
    created_at: datetime


class ViewerDraftDetailResponse(ViewerDraftResponse):
    state_json: dict[str, Any] | None = None


class ViewerDraftCreateRequest(BaseModel):
    file_id: str
    viewer_kind: str = Field(min_length=1, max_length=32)
    manual_observations: str | None = None
    flags: list[str] = Field(default_factory=list)
    state: dict[str, Any]


class ViewerDraftUpdateRequest(BaseModel):
    file_id: str | None = None
    viewer_kind: str | None = Field(default=None, max_length=32)
    manual_observations: str | None = None
    flags: list[str] | None = None
    state: dict[str, Any] | None = None


class AnnotationCreateRequest(BaseModel):
    file_id: str
    annotation_type: str
    data: dict[str, Any]
    # Optional category; drives the pin color in the UI. Free-form here so
    # the project can extend the taxonomy without a backend change.
    flag: str | None = None
    # Optional "see also" link to another annotation on the SAME file_id.
    linked_annotation_id: str | None = None


class AnnotationUpdateRequest(BaseModel):
    annotation_type: str | None = None
    data: dict[str, Any] | None = None
    flag: str | None = None
    linked_annotation_id: str | None = None
    # Sentinels that let the client clear a field explicitly. None on these
    # is "no change"; True/False on _set toggles set-to-null vs leave alone.
    clear_link: bool = False


class AnnotationResponse(BaseModel):
    id: str
    file_id: str
    annotation_type: str
    data: dict[str, Any]
    flag: str | None = None
    linked_annotation_id: str | None = None
    attachment_url: str | None = None
    created_at: datetime


class UserRegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    password: str = Field(min_length=6, max_length=128)
    email: str = Field(max_length=255)


class UserLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserPublic(BaseModel):
    id: str
    username: str
    email: str | None
    is_admin: bool
    email_verified: bool = False

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic


class PasswordResetRequestSchema(BaseModel):
    email: str


class PasswordResetConfirmSchema(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)
