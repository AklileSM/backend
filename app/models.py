import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    """Application user account.

    The first account registered via POST /api/auth/register is automatically
    granted is_admin=True; all subsequent registrations get is_admin=False.

    is_active=False disables login without deleting the account, the user's
    uploads and reports remain intact. Set via the admin panel.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Service accounts for autonomous agents (e.g. the Go2W robot). Robots have
    # no email, so the email_verified upload gate is relaxed for them; see
    # app/api/deps.py::require_user_can_upload and api/upload/robot.py.
    is_robot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verification_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    email_verification_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    password_reset_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_reset_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    project_memberships: Mapped[list["ProjectMember"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    robot_presence: Mapped["RobotPresence | None"] = relationship(
        back_populates="robot_user", cascade="all, delete-orphan"
    )
    assigned_robot_missions: Mapped[list["RobotMission"]] = relationship(
        back_populates="robot_user",
        foreign_keys="RobotMission.robot_user_id",
    )
    requested_robot_missions: Mapped[list["RobotMission"]] = relationship(
        back_populates="requested_by_user",
        foreign_keys="RobotMission.requested_by_user_id",
    )


class Project(Base):
    """A construction project that groups rooms and files.

    slug is the stable URL identifier (e.g. "a6-stern"). It must be globally
    unique and is used in frontend routes.

    owner_id uses SET NULL on delete so the project survives if the owning user
    is removed. Global admins have implicit access regardless of membership.

    status is typically "active" or "archived"; the frontend filters on this.
    floorplan_url points to a presigned MinIO URL for the floor plan image used
    as the backdrop in the room-picker UI.
    """

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_projects_owner_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    floorplan_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    owner: Mapped["User | None"] = relationship("User", foreign_keys="Project.owner_id")
    rooms: Mapped[list["Room"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    robot_missions: Mapped[list["RobotMission"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectMember(Base):
    """Many-to-many link between users and projects with a role.

    role must be one of: "owner" | "editor" | "viewer"
      owner , can manage project settings, members, and upload files
      editor, can upload files, create annotations and reports
      viewer, read-only access to all project content

    Global admins bypass this table entirely and have implicit owner-level
    access to every project.
    """

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
    """A named location within a project (e.g. "Ground Floor", "Room 3").

    slug is scoped per project, two different projects may have a room with
    the same slug. The unique constraint is (project_id, slug).

    floor_plan_coordinates is a JSON object that records where this room's
    marker sits on the project's floor plan image, used by the room-picker UI.
    Shape: {"x": float, "y": float} as fractions of the image dimensions.

    sort_order controls the display order in the sidebar and explorer.
    Lower values appear first; default is 0 (insertion order within a tie).
    """

    __tablename__ = "rooms"
    __table_args__ = (
        UniqueConstraint("project_id", "slug", name="uq_rooms_project_slug"),
        UniqueConstraint("project_id", "name", name="uq_rooms_project_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    floor_plan_coordinates: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    project: Mapped[Project] = relationship(back_populates="rooms")
    files: Mapped[list["FileAsset"]] = relationship(back_populates="room", cascade="all, delete-orphan")


class FileAsset(Base):
    """An uploaded file stored in MinIO with its metadata in the DB.

    media_type is one of: "image" | "video" | "pointcloud" | "pdf"

    display_name is the canonical filename in the format <room-slug>-<YYYYMMDD>-<NNN>.<ext>
    (e.g. "room3-20260329-001.jpg"). original_name preserves what the user uploaded.

    object_name is the MinIO key within bucket_name, structured as:
      <room_id>/<capture_date>/<display_name>

    sha256_hash enables system-wide duplicate detection: the same file cannot
    be uploaded twice even to a different room or project (409 on conflict).

    metadata_json carries upload provenance and type-specific state:
      - All types: {"uploaded_by_user_id": str, "uploaded_by_username": str}
      - Point clouds additionally: {
            "conversion_status": "uploading" | "pending" | "processing" | "ready" | "failed",
            "conversion_error": str,          # present only on failure
            "potree_base_object": str,         # MinIO key prefix for Potree files
            "original_removed_after_conversion": bool
        }

    ai_description_status tracks async AI analysis: "generating" → null (done) or
    stored in ai_description. Only populated for images.

    thumbnail_bucket_name / thumbnail_object_name are set for images only; the
    thumbnail is a 400×300 JPEG auto-generated at upload time.
    """

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
    """In-progress report from the Compare viewer (two images side-by-side).

    state_json holds the viewer's serialised state at save time:
      {
        "leftFileId": str,
        "rightFileId": str,
        "screenshots": [{"dataUrl": str, "label": str}, ...],
        "cameraA": {...},   # optional Three.js camera state
        "cameraB": {...}
      }

    flags is a list of string tags chosen by the reporter (e.g. ["crack", "water"]).
    pdf_bucket_name / pdf_object_name point to the generated PDF in MinIO.
    created_by stores the uploader's username (denormalised for display).
    """

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
    """In-progress report from a single-file viewer (not the Compare viewer).

    viewer_kind identifies which viewer produced this draft:
      "static"     , standard image viewer
      "panorama"   , 360° panorama (Three.js sphere)
      "pointcloud" , Potree point cloud viewer
      "pdf"        , PDF.js document viewer

    state_json holds viewer-specific serialised state (camera position,
    screenshots, active page number, etc.). Schema varies by viewer_kind;
    see frontend-next/VIEWERS.md for the full shapes.

    flags and pdf_bucket_name / pdf_object_name behave the same as in
    ComparisonDraft. created_by stores the uploader's username.
    """

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
    """A published (finalised) field-observation report attached to a file.

    Unlike drafts (ComparisonDraft, ViewerReportDraft), a Report is considered
    immutable once created. The frontend uses the presence of a Report to show
    the "Published" badge on a file card.

    screenshots is a list of base64-encoded JPEG data-URLs captured by the
    viewer at publish time (embedded directly in the PDF).

    pdf_bucket_name / pdf_object_name are nullable: they are populated only
    after the PDF generation background task completes. A null pdf_object_name
    means the report is saved but the PDF is not ready yet.

    ai_description is copied from FileAsset.ai_description at publish time so
    the report PDF captures the analysis that existed when the report was filed.
    """

    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    file_id: Mapped[str] = mapped_column(ForeignKey("file_assets.id"), nullable=False)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    """A spatial annotation attached to a file (pin, polygon, measurement, etc.).

    annotation_type identifies the shape kind; data holds its coordinates and
    label. Both are defined by the frontend viewer, the backend stores them
    opaquely without validating the schema.

    Common annotation_type values:
      "pin"      , {"x": float, "y": float, "label": str}
      "polygon"  , {"points": [[x, y], ...], "label": str}
      "distance" , {"start": [x, y], "end": [x, y], "metres": float}

    Annotations cascade-delete when their parent FileAsset is deleted.
    """

    __tablename__ = "annotations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    file_id: Mapped[str] = mapped_column(ForeignKey("file_assets.id"), nullable=False)
    annotation_type: Mapped[str] = mapped_column(String(50), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Optional category (e.g. "safety" / "quality" / "delayed"). The UI maps
    # this to a pin color and a small chip in the details panel.
    flag: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Optional same-file pointer to another annotation ("see #4"). ON DELETE
    # SET NULL so removing the referenced annotation doesn't dangle.
    linked_annotation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("annotations.id", ondelete="SET NULL"), nullable=True
    )
    # Optional image attachment (a zoom-in shot etc.) stored in MinIO under
    # the dedicated annotation_attachments bucket.
    attachment_bucket_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attachment_object_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    file: Mapped[FileAsset] = relationship(back_populates="annotations")


class ProjectActivity(Base):
    """Append-only audit feed for a project.

    Each row records *who did what when* against a project. The actor's
    username is denormalised so the log keeps making sense after the user
    is deleted (user_id is nullable + ON DELETE SET NULL for the same
    reason).

    action is a dotted string (e.g. "upload.image", "annotation.create",
    "report.publish", "member.add", "member.remove"), kept as plain text
    rather than an Enum so new actions can be added without a schema
    migration.

    metadata_json carries the small bag of human-readable fields the feed
    needs to render without joining (file_name, room_name, annotation
    preview, etc.).

    The cascade on project_id is the load-bearing one: deleting a project
    sweeps its activity. target_id is plain text, different tables have
    different id shapes (file_assets.id is uuid, project_members has a
    composite PK).
    """

    __tablename__ = "project_activity"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class RobotPresence(Base):
    """Most recent heartbeat/state reported by a robot agent."""

    __tablename__ = "robot_presence"

    robot_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    robot_username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    current_mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    robot_user: Mapped[User] = relationship(back_populates="robot_presence")


class RobotMission(Base):
    """A mission assigned to a robot for autonomous capture collection."""

    __tablename__ = "robot_missions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    robot_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    robot_username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    capture_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="panorama")
    capture_date: Mapped[date] = mapped_column(Date, nullable=False)
    waypoints_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    room_slug_map_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    retry_policy_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    robot_meta_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    robot_user: Mapped[User] = relationship(
        back_populates="assigned_robot_missions",
        foreign_keys=[robot_user_id],
    )
    requested_by_user: Mapped["User | None"] = relationship(
        back_populates="requested_robot_missions",
        foreign_keys=[requested_by_user_id],
    )
    project: Mapped[Project] = relationship(back_populates="robot_missions")
    steps: Mapped[list["RobotMissionStep"]] = relationship(
        back_populates="mission", cascade="all, delete-orphan"
    )


class RobotMissionStep(Base):
    """One waypoint-level step inside a robot mission."""

    __tablename__ = "robot_mission_steps"
    __table_args__ = (
        UniqueConstraint("mission_id", "sequence_index", name="uq_robot_mission_steps_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    mission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("robot_missions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    waypoint_name: Mapped[str] = mapped_column(String(255), nullable=False)
    room_slug: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    navigation_goal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    navigation_result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uploaded_file_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("file_assets.id", ondelete="SET NULL"), nullable=True
    )
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    mission: Mapped[RobotMission] = relationship(back_populates="steps")
    uploaded_file: Mapped["FileAsset | None"] = relationship()
