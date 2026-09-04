from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid import uuid4

from app.core.database import Base


CHANNEL_CHECK = "channel IN ('owner', 'stay_guide', 'travel')"
PUBLISH_RECOVERY_METADATA_VERSION = 1


class MiniappContentRelease(Base):
    __tablename__ = "miniapp_content_releases"
    __table_args__ = (
        CheckConstraint(CHANNEL_CHECK, name="ck_miniapp_content_releases_channel"),
        CheckConstraint(
            "status IN ('pending', 'promoting', 'published', 'failed', 'superseded', 'rolled_back')",
            name="ck_miniapp_content_releases_status",
        ),
        UniqueConstraint(
            "channel", "version", name="uq_miniapp_content_releases_channel_version"
        ),
    )

    release_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    manifest_url: Mapped[str] = mapped_column(String(500), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_payload: Mapped[dict | None] = mapped_column(JSONB)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pointer_swapped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    created_by: Mapped[str] = mapped_column(
        String(20), ForeignKey("users.user_id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @property
    def manifest_object_key(self) -> str:
        return f"miniapp/releases/{self.channel}/{self.version}/manifest.json"


class MiniappContentWorkspace(Base):
    __tablename__ = "miniapp_content_workspaces"
    __table_args__ = (
        CheckConstraint(CHANNEL_CHECK, name="ck_miniapp_content_workspaces_channel"),
        CheckConstraint("revision >= 0", name="ck_miniapp_content_workspaces_revision"),
    )

    channel: Mapped[str] = mapped_column(String(20), primary_key=True)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    is_dirty: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    current_release_id: Mapped[str | None] = mapped_column(
        String(24), ForeignKey("miniapp_content_releases.release_id")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MiniappStructuredDraft(Base):
    __tablename__ = "miniapp_structured_drafts"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('stay_guide', 'travel')",
            name="ck_miniapp_structured_drafts_channel",
        ),
    )

    channel: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("miniapp_content_workspaces.channel", ondelete="CASCADE"),
        primary_key=True,
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[str] = mapped_column(
        String(20), ForeignKey("users.user_id"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MiniappChannelConfig(Base):
    __tablename__ = "miniapp_channel_configs"
    __table_args__ = (
        CheckConstraint(CHANNEL_CHECK, name="ck_miniapp_channel_configs_channel"),
        UniqueConstraint(
            "auto_creation_token",
            name="uq_miniapp_channel_configs_auto_creation_token",
        ),
    )

    channel: Mapped[str] = mapped_column(String(20), primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    auto_creation_token: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MiniappMedia(Base):
    __tablename__ = "miniapp_media"
    __table_args__ = (
        CheckConstraint(
            "media_type IN ('image', 'video')", name="ck_miniapp_media_type"
        ),
        CheckConstraint("size_bytes >= 0", name="ck_miniapp_media_size_bytes"),
        CheckConstraint(
            "reference_status IN ('draft', 'published', 'unreferenced')",
            name="ck_miniapp_media_reference_status",
        ),
        CheckConstraint(
            "(derivative_set_id IS NULL AND derivative_role IS NULL AND source_sha256 IS NULL) OR "
            "(media_type = 'image' AND derivative_set_id IS NOT NULL "
            "AND derivative_role IS NOT NULL "
            "AND derivative_role IN ('thumbnail', 'display') "
            "AND source_sha256 IS NOT NULL AND length(source_sha256) = 64 "
            "AND size_bytes > 0)",
            name="ck_miniapp_media_derivative_provenance",
        ),
        CheckConstraint(
            "(public_approved_by IS NULL AND public_approved_at IS NULL) OR "
            "(public_approved_by IS NOT NULL AND public_approved_at IS NOT NULL)",
            name="ck_miniapp_media_public_approval_pair",
        ),
        UniqueConstraint(
            "derivative_set_id", "derivative_role",
            name="uq_miniapp_media_derivative_set_role",
        ),
        Index("ix_miniapp_media_source_sha256", "source_sha256"),
        Index(
            "ix_miniapp_media_private_cleanup_due",
            "pending_private_cleanup",
            "cleanup_next_attempt_at",
        ),
    )

    media_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    media_type: Mapped[str] = mapped_column(String(20), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    derivative_set_id: Mapped[str | None] = mapped_column(String(24))
    derivative_role: Mapped[str | None] = mapped_column(String(10))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    reference_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft"
    )
    last_referenced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    public_approved_by: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("users.user_id")
    )
    public_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A row is committed before its private object is written.  The flag is
    # cleared only after the object and its audit record are durably tracked.
    pending_private_cleanup: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    cleanup_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cleanup_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_claim_token: Mapped[str | None] = mapped_column(String(32))
    cleanup_error: Mapped[str | None] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MiniappImagePairUpload(Base):
    """Durable reservation for one server-generated image derivative set."""

    __tablename__ = "miniapp_image_pair_uploads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('reserved', 'cleanup_pending', 'cleaning', 'retired', 'finalized')",
            name="ck_miniapp_image_pair_uploads_status",
        ),
        CheckConstraint(
            "key_generation >= 1",
            name="ck_miniapp_image_pair_uploads_key_generation",
        ),
        CheckConstraint(
            "length(source_sha256) = 64 AND length(thumbnail_sha256) = 64 "
            "AND length(display_sha256) = 64",
            name="ck_miniapp_image_pair_uploads_digest_lengths",
        ),
        CheckConstraint(
            "cleanup_attempt_count >= 0",
            name="ck_miniapp_image_pair_uploads_cleanup_attempts",
        ),
        CheckConstraint(
            "thumbnail_media_id <> display_media_id",
            name="ck_miniapp_image_pair_uploads_media_ids_distinct",
        ),
        CheckConstraint(
            "thumbnail_key <> display_key",
            name="ck_miniapp_image_pair_uploads_keys_distinct",
        ),
        CheckConstraint(
            "thumbnail_sha256 <> display_sha256",
            name="ck_miniapp_image_pair_uploads_hashes_distinct",
        ),
        CheckConstraint(
            "thumbnail_size > 0 AND display_size > 0 "
            "AND thumbnail_width > 0 AND thumbnail_height > 0 "
            "AND display_width > 0 AND display_height > 0",
            name="ck_miniapp_image_pair_uploads_positive_media",
        ),
        Index(
            "ix_miniapp_image_pair_uploads_cleanup_due",
            "status",
            "cleanup_next_attempt_at",
        ),
    )

    derivative_set_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="reserved", server_default="reserved"
    )
    key_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    thumbnail_media_id: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    display_media_id: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    thumbnail_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    display_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    thumbnail_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    display_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    thumbnail_size: Mapped[int] = mapped_column(Integer, nullable=False)
    display_size: Mapped[int] = mapped_column(Integer, nullable=False)
    thumbnail_width: Mapped[int] = mapped_column(Integer, nullable=False)
    thumbnail_height: Mapped[int] = mapped_column(Integer, nullable=False)
    display_width: Mapped[int] = mapped_column(Integer, nullable=False)
    display_height: Mapped[int] = mapped_column(Integer, nullable=False)
    cleanup_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cleanup_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_claim_token: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    generations: Mapped[list["MiniappImagePairGeneration"]] = relationship(
        back_populates="upload",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MiniappImagePairGeneration(Base):
    """Exact-key cleanup tombstone for one image-pair upload generation."""

    __tablename__ = "miniapp_image_pair_generations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'cleanup_pending', 'cleaning', 'retired', 'finalized')",
            name="ck_miniapp_image_pair_generations_status",
        ),
        CheckConstraint(
            "key_generation >= 1",
            name="ck_miniapp_image_pair_generations_key_generation",
        ),
        CheckConstraint(
            "cleanup_attempt_count >= 0",
            name="ck_miniapp_image_pair_generations_cleanup_attempts",
        ),
        CheckConstraint(
            "thumbnail_key <> display_key",
            name="ck_miniapp_image_pair_generations_keys_distinct",
        ),
        Index(
            "ix_miniapp_image_pair_generations_cleanup_due",
            "status",
            "next_probe_at",
        ),
    )

    derivative_set_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("miniapp_image_pair_uploads.derivative_set_id", ondelete="CASCADE"),
        primary_key=True,
    )
    key_generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    thumbnail_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    display_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    cleanup_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_claim_token: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    upload: Mapped[MiniappImagePairUpload] = relationship(
        back_populates="generations"
    )


class MiniappVideoUpload(Base):
    """Durable quarantine-to-draft video finalization state."""

    __tablename__ = "miniapp_video_uploads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploading', 'finalizing', 'finalized', 'failed')",
            name="ck_miniapp_video_uploads_status",
        ),
        CheckConstraint("declared_size > 0", name="ck_miniapp_video_uploads_size"),
        CheckConstraint(
            "(width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)",
            name="ck_miniapp_video_uploads_dimensions",
        ),
        Index(
            "ix_miniapp_video_uploads_cleanup_due",
            "pending_quarantine_cleanup",
            "cleanup_next_attempt_at",
        ),
    )

    media_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    quarantine_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    draft_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    declared_size: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploading")
    mime_type: Mapped[str | None] = mapped_column(String(100))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(240))
    pending_draft_cleanup: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Set in the same commit as MiniappMedia/audit.  Cleanup is deliberately a
    # retryable post-commit operation so a crash cannot lose the only source.
    pending_quarantine_cleanup: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    cleanup_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cleanup_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_claim_token: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OwnerCase(Base):
    __tablename__ = "owner_cases"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_owner_cases_revision"),
        CheckConstraint(
            "NOT is_visible OR privacy_confirmed_by IS NOT NULL",
            name="ck_owner_cases_visible_privacy_confirmed",
        ),
    )

    case_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    is_visible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    primary_media_id: Mapped[str | None] = mapped_column(
        String(24), ForeignKey("miniapp_media.media_id")
    )
    # JSONB stores feature-local logical image records with thumbnail/display IDs.
    detail_media_ids: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'")
    )
    privacy_confirmed_by: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("users.user_id")
    )
    privacy_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OwnerDraftVideo(Base):
    """The owner draft's single finalized video plus its required poster."""

    __tablename__ = "owner_draft_videos"
    __table_args__ = (
        CheckConstraint("channel = 'owner'", name="ck_owner_draft_videos_channel"),
        CheckConstraint("revision >= 1", name="ck_owner_draft_videos_revision"),
        CheckConstraint(
            "video_media_id <> poster_media_id",
            name="ck_owner_draft_videos_distinct_media",
        ),
    )

    channel: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("miniapp_content_workspaces.channel", ondelete="CASCADE"),
        primary_key=True,
    )
    video_media_id: Mapped[str] = mapped_column(
        String(24), ForeignKey("miniapp_media.media_id"), nullable=False
    )
    poster_media_id: Mapped[str] = mapped_column(
        String(24), ForeignKey("miniapp_media.media_id"), nullable=False
    )
    alt: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MiniappPublishJob(Base):
    __tablename__ = "miniapp_publish_jobs"
    __table_args__ = (
        CheckConstraint(CHANNEL_CHECK, name="ck_miniapp_publish_jobs_channel"),
        CheckConstraint(
            "operation IN ('publish', 'rollback')",
            name="ck_miniapp_publish_jobs_operation",
        ),
        CheckConstraint(
            "(operation = 'publish' AND expected_revision IS NOT NULL) OR "
            "(operation = 'rollback' AND expected_revision IS NULL "
            "AND target_version IS NOT NULL)",
            name="ck_miniapp_publish_jobs_operation_input",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'rolled_back')",
            name="ck_miniapp_publish_jobs_status",
        ),
        CheckConstraint(
            "progress_percent BETWEEN 0 AND 100",
            name="ck_miniapp_publish_jobs_progress_percent",
        ),
        CheckConstraint(
            "attempt_generation >= 1",
            name="ck_miniapp_publish_jobs_attempt_generation",
        ),
        CheckConstraint(
            "transient_publish_retry_count >= 0",
            name="ck_miniapp_publish_jobs_transient_retry_count",
        ),
        CheckConstraint(
            "recovery_count >= 0",
            name="ck_miniapp_publish_jobs_recovery_count",
        ),
        CheckConstraint(
            "result_release_previous_status IS NULL OR "
            "result_release_previous_status IN ('published', 'superseded', 'rolled_back')",
            name="ck_miniapp_publish_jobs_result_previous_status",
        ),
        CheckConstraint(
            "recovery_metadata_version IS NULL OR recovery_metadata_version = 1",
            name="ck_miniapp_publish_jobs_recovery_metadata_version",
        ),
        CheckConstraint(
            "status NOT IN ('queued', 'running') OR "
            "recovery_metadata_version IS NOT NULL",
            name="ck_miniapp_publish_jobs_active_recovery_metadata",
        ),
        CheckConstraint(
            "recovery_metadata_version IS NULL OR "
            "((auto_enabled_channel = false "
            "AND auto_enabled_config_token IS NULL "
            "AND auto_enabled_config_updated_at IS NULL) OR "
            "(auto_enabled_channel = true "
            "AND auto_enabled_config_token IS NOT NULL "
            "AND auto_enabled_config_updated_at IS NOT NULL))",
            name="ck_miniapp_publish_jobs_auto_config_evidence",
        ),
        UniqueConstraint(
            "idempotency_scope",
            "idempotency_key",
            name="uq_miniapp_publish_jobs_idempotency",
        ),
        Index(
            "uq_miniapp_publish_jobs_active_channel",
            "channel",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    job_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="queued", server_default="queued"
    )
    expected_revision: Mapped[int | None] = mapped_column(Integer)
    target_version: Mapped[str | None] = mapped_column(String(40))
    result_release_id: Mapped[str | None] = mapped_column(
        String(24), ForeignKey("miniapp_content_releases.release_id")
    )
    previous_release_id: Mapped[str | None] = mapped_column(
        String(24), ForeignKey("miniapp_content_releases.release_id")
    )
    result_release_previous_status: Mapped[str | None] = mapped_column(String(20))
    auto_enabled_channel: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    recovery_metadata_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=PUBLISH_RECOVERY_METADATA_VERSION
    )
    auto_enabled_config_token: Mapped[str | None] = mapped_column(String(32))
    auto_enabled_config_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    actor_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("users.user_id"), nullable=False
    )
    idempotency_scope: Mapped[str] = mapped_column(
        String(80), nullable=False, default="miniapp_content", server_default="miniapp_content"
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    attempt_token: Mapped[str] = mapped_column(
        String(32), nullable=False, default=lambda: uuid4().hex, server_default="00000000000000000000000000000000"
    )
    transient_publish_retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    recovery_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    manifest_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress_stage: Mapped[str | None] = mapped_column(String(40))
    progress_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_code: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MiniappPublishOutbox(Base):
    __tablename__ = "miniapp_publish_outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'claiming', 'delivered')",
            name="ck_miniapp_publish_outbox_status",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_miniapp_publish_outbox_attempt_count"
        ),
        CheckConstraint(
            "attempt_generation >= 1",
            name="ck_miniapp_publish_outbox_attempt_generation",
        ),
        UniqueConstraint(
            "job_id", "attempt_generation", name="uq_miniapp_publish_outbox_job_generation"
        ),
        Index(
            "ix_miniapp_publish_outbox_claim",
            "status",
            "available_at",
            "outbox_id",
        ),
    )

    outbox_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(24), ForeignKey("miniapp_publish_jobs.job_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    attempt_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    attempt_token: Mapped[str] = mapped_column(
        String(32), nullable=False, default=lambda: uuid4().hex, server_default="00000000000000000000000000000000"
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now(), server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
