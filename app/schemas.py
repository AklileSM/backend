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


class RoomMediaGroup(BaseModel):
    images: list[MediaFileResponse] = Field(default_factory=list)
    videos: list[MediaFileResponse] = Field(default_factory=list)
    pointclouds: list[MediaFileResponse] = Field(default_factory=list)


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


class ExplorerDatesSummaryResponse(BaseModel):
    dates: dict[str, DateMediaCounts]


class ProjectResponse(BaseModel):
    id: str
    name: str
    slug: str


class RoomResponse(BaseModel):
    id: str
    name: str
    slug: str
    project_id: str


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


class ReportCreateRequest(BaseModel):
    file_id: str
    ai_description: str | None = None
    manual_observations: str | None = None
    flags: list[str] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)


class ReportResponse(BaseModel):
    id: str
    file_id: str
    ai_description: str | None = None
    manual_observations: str | None = None
    flags: list[str] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    created_by: str | None = None
    pdf_url: str | None = None
    created_at: datetime


class AnnotationCreateRequest(BaseModel):
    file_id: str
    annotation_type: str
    data: dict[str, Any]


class AnnotationResponse(BaseModel):
    id: str
    file_id: str
    annotation_type: str
    data: dict[str, Any]
    created_at: datetime


class UserRegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    password: str = Field(min_length=8, max_length=128)
    email: str | None = Field(default=None, max_length=255)


class UserLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserPublic(BaseModel):
    id: str
    username: str
    email: str | None
    role: str

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic
