import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    project_memberships: Mapped[list["ProjectMember"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    owner: Mapped["User | None"] = relationship("User", foreign_keys="Project.owner_id")
    rooms: Mapped[list["Room"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    project: Mapped[Project] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="project_memberships")


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    floor_plan_coordinates: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    project: Mapped[Project] = relationship(back_populates="rooms")
    files: Mapped[list["FileAsset"]] = relationship(back_populates="room", cascade="all, delete-orphan")


class FileAsset(Base):
    __tablename__ = "file_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id"), nullable=False)
    media_type: Mapped[str] = mapped_column(String(30), nullable=False)
    capture_date: Mapped[date] = mapped_column(Date, nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    bucket_name: Mapped[str] = mapped_column(String(255), nullable=False)
    object_name: Mapped[str] = mapped_column(String(500), nullable=False)
    thumbnail_bucket_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    thumbnail_object_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ai_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_description_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    room: Mapped[Room] = relationship(back_populates="files")
    reports: Mapped[list["Report"]] = relationship(back_populates="file", cascade="all, delete-orphan")
    annotations: Mapped[list["Annotation"]] = relationship(back_populates="file", cascade="all, delete-orphan")
    comparison_drafts: Mapped[list["ComparisonDraft"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )
    viewer_report_drafts: Mapped[list["ViewerReportDraft"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


class ComparisonDraft(Base):
    __tablename__ = "comparison_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    file_id: Mapped[str] = mapped_column(ForeignKey("file_assets.id"), nullable=False)
    manual_observations: Mapped[str | None] = mapped_column(Text, nullable=True)
    flags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    state_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pdf_bucket_name: Mapped[str] = mapped_column(String(255), nullable=False)
    pdf_object_name: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    file: Mapped[FileAsset] = relationship(back_populates="comparison_drafts")


class ViewerReportDraft(Base):
    """Field-observation drafts from Static / Interactive / PCD viewers (not Compare)."""

    __tablename__ = "viewer_report_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    file_id: Mapped[str] = mapped_column(ForeignKey("file_assets.id"), nullable=False)
    viewer_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    manual_observations: Mapped[str | None] = mapped_column(Text, nullable=True)
    flags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    state_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pdf_bucket_name: Mapped[str] = mapped_column(String(255), nullable=False)
    pdf_object_name: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    file: Mapped[FileAsset] = relationship(back_populates="viewer_report_drafts")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    file_id: Mapped[str] = mapped_column(ForeignKey("file_assets.id"), nullable=False)
    ai_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_observations: Mapped[str | None] = mapped_column(Text, nullable=True)
    flags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    screenshots: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    pdf_bucket_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pdf_object_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    file: Mapped[FileAsset] = relationship(back_populates="reports")


class Annotation(Base):
    __tablename__ = "annotations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    file_id: Mapped[str] = mapped_column(ForeignKey("file_assets.id"), nullable=False)
    annotation_type: Mapped[str] = mapped_column(String(50), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    file: Mapped[FileAsset] = relationship(back_populates="annotations")
