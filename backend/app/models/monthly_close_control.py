"""Durable control-plane records for the monthly-close assistant.

These tables deliberately use ``cycle_id`` as their only business boundary.  The
current product has no Store/Tenant model, so adding a monthly-close-only
surrogate boundary here would create an authorization model the rest of the
application cannot enforce.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.monthly_close import MONTHLY_CLOSE_SOURCE_TYPES


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


CONVERSATION_AUDIENCE_SCOPES = ("finance", "operations", "assigned_staff")
CONVERSATION_STATUSES = ("active", "archived")
MESSAGE_ROLES = ("user", "assistant", "system", "tool_event")
RUN_TRIGGER_TYPES = ("user_message", "file_received", "schedule", "retry")
RUN_ACTOR_TYPES = ("user", "system")
RUN_STATUSES = (
    "queued",
    "running",
    "waiting_user",
    "waiting_approval",
    "succeeded",
    "degraded",
    "failed",
    "cancelled",
)
JOB_STATUSES = ("pending", "leased", "succeeded", "failed", "dead_letter")
PROPOSAL_STATUSES = (
    "draft",
    "pending_approval",
    "approved",
    "rejected",
    "stale",
    "superseded",
)
APPROVAL_DECISIONS = ("approved", "rejected")
ATTEMPT_STATUSES = (
    "pending",
    "executing",
    "succeeded_unverified",
    "verified",
    "failed_safe",
    "failed_confirmed",
    "unknown",
    "remediation_required",
)
VERIFICATION_STATUSES = ("pending", "running", "passed", "failed", "inconclusive")
ISSUE_STATUSES = (
    "open",
    "assigned",
    "waiting_information",
    "proposed_fix",
    "rework",
    "resolved",
    "cancelled",
    "reopened",
)
REMEDIATION_STATUSES = ("open", "investigating", "compensating", "resolved", "rework")
OUTBOX_STATUSES = ("pending", "leased", "delivered", "failed", "dead_letter")
ANALYSIS_STATUSES = ("queued", "processing", "needs_review", "completed", "failed_safe")


class MonthlyCloseConversation(Base):
    __tablename__ = "monthly_close_conversations"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "cycle_id", name="uq_monthly_close_conversation_id_cycle"
        ),
        CheckConstraint(
            f"audience_scope IN ({_sql_values(CONVERSATION_AUDIENCE_SCOPES)})",
            name="ck_monthly_close_conversation_audience_scope",
        ),
        CheckConstraint(
            f"status IN ({_sql_values(CONVERSATION_STATUSES)})",
            name="ck_monthly_close_conversation_status",
        ),
        Index("ix_monthly_close_conversation_cycle_status", "cycle_id", "status"),
    )

    conversation_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"),
        nullable=False,
    )
    audience_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_by: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MonthlyCloseRun(Base):
    __tablename__ = "monthly_close_agent_runs"
    __table_args__ = (
        UniqueConstraint("run_id", "cycle_id", name="uq_monthly_close_run_id_cycle"),
        ForeignKeyConstraint(
            ["conversation_id", "cycle_id"],
            [
                "monthly_close_conversations.conversation_id",
                "monthly_close_conversations.cycle_id",
            ],
            name="fk_monthly_close_run_conversation_cycle",
        ),
        ForeignKeyConstraint(
            ["parent_run_id", "cycle_id"],
            ["monthly_close_agent_runs.run_id", "monthly_close_agent_runs.cycle_id"],
            name="fk_monthly_close_run_parent_cycle",
        ),
        CheckConstraint(
            f"trigger_type IN ({_sql_values(RUN_TRIGGER_TYPES)})",
            name="ck_monthly_close_run_trigger_type",
        ),
        CheckConstraint(
            f"actor_type IN ({_sql_values(RUN_ACTOR_TYPES)})",
            name="ck_monthly_close_run_actor_type",
        ),
        CheckConstraint(
            f"status IN ({_sql_values(RUN_STATUSES)})",
            name="ck_monthly_close_run_status",
        ),
        Index("ix_monthly_close_run_cycle_status", "cycle_id", "status"),
    )

    run_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[str | None] = mapped_column(String(24))
    parent_run_id: Mapped[str | None] = mapped_column(String(24))
    trigger_type: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(20))
    permission_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", server_default="queued")
    model_provider: Mapped[str | None] = mapped_column(String(80))
    model_name: Mapped[str | None] = mapped_column(String(160))
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    tool_manifest_version: Mapped[str] = mapped_column(String(80), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    output_payload_redacted: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail_redacted: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class MonthlyCloseMessage(Base):
    __tablename__ = "monthly_close_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "cycle_id"],
            [
                "monthly_close_conversations.conversation_id",
                "monthly_close_conversations.cycle_id",
            ],
            name="fk_monthly_close_message_conversation_cycle",
        ),
        ForeignKeyConstraint(
            ["run_id", "cycle_id"],
            ["monthly_close_agent_runs.run_id", "monthly_close_agent_runs.cycle_id"],
            name="fk_monthly_close_message_run_cycle",
        ),
        CheckConstraint(
            f"role IN ({_sql_values(MESSAGE_ROLES)})", name="ck_monthly_close_message_role"
        ),
        CheckConstraint(
            f"visibility_scope IN ({_sql_values(CONVERSATION_AUDIENCE_SCOPES)})",
            name="ck_monthly_close_message_visibility_scope",
        ),
        Index("ix_monthly_close_message_cycle_created", "cycle_id", "created_at"),
    )

    message_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(24), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(24), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(24))
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content_redacted: Mapped[str] = mapped_column(Text, nullable=False)
    content_encrypted: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    visibility_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MonthlyCloseEvent(Base):
    __tablename__ = "monthly_close_agent_events"
    __table_args__ = (
        UniqueConstraint("cycle_id", "sequence", name="uq_monthly_close_event_cycle_sequence"),
        UniqueConstraint("cycle_id", "dedupe_key", name="uq_monthly_close_event_cycle_dedupe"),
        ForeignKeyConstraint(
            ["conversation_id", "cycle_id"],
            [
                "monthly_close_conversations.conversation_id",
                "monthly_close_conversations.cycle_id",
            ],
            name="fk_monthly_close_event_conversation_cycle",
        ),
        ForeignKeyConstraint(
            ["run_id", "cycle_id"],
            ["monthly_close_agent_runs.run_id", "monthly_close_agent_runs.cycle_id"],
            name="fk_monthly_close_event_run_cycle",
        ),
        Index("ix_monthly_close_event_cycle_sequence", "cycle_id", "sequence"),
    )

    event_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[str | None] = mapped_column(String(24))
    run_id: Mapped[str | None] = mapped_column(String(24))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    conversation_sequence: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_redacted: Mapped[dict] = mapped_column(JSONB, nullable=False)
    visibility_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    persisted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MonthlyCloseProcessingJob(Base):
    __tablename__ = "monthly_close_processing_jobs"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "cycle_id", name="uq_monthly_close_job_id_cycle"
        ),
        UniqueConstraint("cycle_id", "dedupe_key", name="uq_monthly_close_job_cycle_dedupe"),
        CheckConstraint(f"status IN ({_sql_values(JOB_STATUSES)})", name="ck_monthly_close_job_status"),
        Index("ix_monthly_close_job_cycle_status", "cycle_id", "status"),
        Index("ix_monthly_close_job_available_at", "available_at"),
    )

    job_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(24), ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"), nullable=False)
    job_type: Mapped[str] = mapped_column(String(80), nullable=False)
    job_version: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(80), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    lease_owner: Mapped[str | None] = mapped_column(String(80))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    actor_type: Mapped[str | None] = mapped_column(String(16))
    actor_id: Mapped[str | None] = mapped_column(String(20))
    permission_policy_version: Mapped[str | None] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class MonthlyCloseDocumentAnalysis(Base):
    """Append-only result and evidence for one document classification generation."""

    __tablename__ = "monthly_close_document_analyses"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "cycle_id"],
            [
                "monthly_close_documents.document_id",
                "monthly_close_documents.cycle_id",
            ],
            name="fk_monthly_close_analysis_document_cycle",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["job_id", "cycle_id"],
            [
                "monthly_close_processing_jobs.job_id",
                "monthly_close_processing_jobs.cycle_id",
            ],
            name="fk_monthly_close_analysis_job_cycle",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "document_id",
            "generation",
            name="uq_monthly_close_analysis_document_generation",
        ),
        UniqueConstraint(
            "cycle_id",
            "dedupe_key",
            name="uq_monthly_close_analysis_cycle_dedupe",
        ),
        UniqueConstraint(
            "job_id", name="uq_monthly_close_document_analyses_job_id"
        ),
        CheckConstraint(
            "generation >= 1",
            name="ck_monthly_close_analysis_generation",
        ),
        CheckConstraint(
            "classification_generation >= 1",
            name="ck_monthly_close_analysis_classification_generation",
        ),
        CheckConstraint(
            f"status IN ({_sql_values(ANALYSIS_STATUSES)})",
            name="ck_monthly_close_analysis_status",
        ),
        CheckConstraint(
            f"source_type IN ({_sql_values(MONTHLY_CLOSE_SOURCE_TYPES)})",
            name="ck_monthly_close_analysis_source_type",
        ),
        Index(
            "ix_monthly_close_analysis_document_generation",
            "document_id",
            "generation",
        ),
    )

    analysis_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
    )
    job_id: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    classification_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(80), nullable=False)
    result_schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    mapping_version: Mapped[str] = mapped_column(String(80), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="queued", server_default="queued"
    )
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))
    manual_action: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MonthlyCloseProposal(Base):
    __tablename__ = "monthly_close_action_proposals"
    __table_args__ = (
        UniqueConstraint("proposal_id", "cycle_id", name="uq_monthly_close_proposal_id_cycle"),
        UniqueConstraint(
            "proposal_id",
            "proposal_binding_hash",
            name="uq_monthly_close_proposal_id_binding",
        ),
        ForeignKeyConstraint(
            ["supersedes_proposal_id", "cycle_id"],
            [
                "monthly_close_action_proposals.proposal_id",
                "monthly_close_action_proposals.cycle_id",
            ],
            name="fk_monthly_close_proposal_supersedes_cycle",
        ),
        ForeignKeyConstraint(
            ["created_by_run_id", "cycle_id"],
            ["monthly_close_agent_runs.run_id", "monthly_close_agent_runs.cycle_id"],
            name="fk_monthly_close_proposal_created_run_cycle",
        ),
        CheckConstraint(f"status IN ({_sql_values(PROPOSAL_STATUSES)})", name="ck_monthly_close_proposal_status"),
        Index("ix_monthly_close_proposal_cycle_status", "cycle_id", "status"),
    )

    proposal_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(24), ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"), nullable=False)
    proposal_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", server_default="draft")
    proposal_version: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_proposal_id: Mapped[str | None] = mapped_column(String(24))
    schema_version: Mapped[str] = mapped_column(String(80), nullable=False, default="v1", server_default="v1")
    canonical_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_versions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    active_input_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_versions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(80), nullable=False)
    calculation_version: Mapped[str] = mapped_column(String(80), nullable=False)
    configuration_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_policy_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    impact_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    impact_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    validation_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_run_id: Mapped[str | None] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MonthlyCloseApproval(Base):
    __tablename__ = "monthly_close_action_approvals"
    __table_args__ = (
        UniqueConstraint("proposal_id", "decided_by", name="uq_monthly_close_approval_proposal_decider"),
        UniqueConstraint("proposal_id", "request_id", name="uq_monthly_close_approval_proposal_request"),
        ForeignKeyConstraint(
            ["proposal_id", "proposal_binding_hash"],
            [
                "monthly_close_action_proposals.proposal_id",
                "monthly_close_action_proposals.proposal_binding_hash",
            ],
            name="fk_monthly_close_approval_proposal_binding",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"decision IN ({_sql_values(APPROVAL_DECISIONS)})", name="ck_monthly_close_approval_decision"),
        Index("ix_monthly_close_approval_proposal", "proposal_id"),
    )

    approval_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(24), ForeignKey("monthly_close_action_proposals.proposal_id", ondelete="CASCADE"), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(20), nullable=False)
    approval_role: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(String(80), nullable=False)
    maker_is_checker: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MonthlyCloseApprovalEvaluation(Base):
    __tablename__ = "monthly_close_approval_evaluations"
    __table_args__ = (
        UniqueConstraint("evaluation_id", "proposal_id", name="uq_monthly_close_evaluation_id_proposal"),
        UniqueConstraint(
            "evaluation_id",
            "proposal_id",
            "approval_set_hash",
            name="uq_monthly_close_evaluation_id_proposal_hash",
        ),
        UniqueConstraint(
            "evaluation_id",
            "proposal_id",
            "approval_set_hash",
            "proposal_binding_hash",
            name="uq_monthly_close_evaluation_id_proposal_approval_binding",
        ),
        ForeignKeyConstraint(
            ["proposal_id", "proposal_binding_hash"],
            [
                "monthly_close_action_proposals.proposal_id",
                "monthly_close_action_proposals.proposal_binding_hash",
            ],
            name="fk_monthly_close_evaluation_proposal_binding",
            ondelete="CASCADE",
        ),
        Index("ix_monthly_close_evaluation_proposal", "proposal_id"),
    )

    evaluation_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(24), ForeignKey("monthly_close_action_proposals.proposal_id", ondelete="CASCADE"), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    approval_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    approval_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    satisfied: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MonthlyCloseExecutionAttempt(Base):
    __tablename__ = "monthly_close_execution_attempts"
    __table_args__ = (
        UniqueConstraint("attempt_id", "cycle_id", name="uq_monthly_close_attempt_id_cycle"),
        UniqueConstraint("proposal_id", "request_id", name="uq_monthly_close_attempt_proposal_request"),
        UniqueConstraint("cycle_id", "idempotency_key", name="uq_monthly_close_attempt_cycle_idempotency"),
        ForeignKeyConstraint(
            ["proposal_id", "cycle_id"],
            ["monthly_close_action_proposals.proposal_id", "monthly_close_action_proposals.cycle_id"],
            name="fk_monthly_close_attempt_proposal_cycle",
        ),
        ForeignKeyConstraint(
            [
                "approval_evaluation_id",
                "proposal_id",
                "approval_set_hash",
                "proposal_binding_hash",
            ],
            [
                "monthly_close_approval_evaluations.evaluation_id",
                "monthly_close_approval_evaluations.proposal_id",
                "monthly_close_approval_evaluations.approval_set_hash",
                "monthly_close_approval_evaluations.proposal_binding_hash",
            ],
            name="fk_monthly_close_attempt_evaluation_proposal",
        ),
        ForeignKeyConstraint(
            ["proposal_id", "proposal_binding_hash"],
            [
                "monthly_close_action_proposals.proposal_id",
                "monthly_close_action_proposals.proposal_binding_hash",
            ],
            name="fk_monthly_close_attempt_proposal_binding",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"status IN ({_sql_values(ATTEMPT_STATUSES)})", name="ck_monthly_close_attempt_status"),
        Index(
            "uq_monthly_close_attempt_one_executing",
            "proposal_id",
            unique=True,
            postgresql_where=text("status = 'executing'"),
            sqlite_where=text("status = 'executing'"),
        ),
        Index("ix_monthly_close_attempt_proposal_status", "proposal_id", "status"),
    )

    attempt_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(24), ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"), nullable=False)
    proposal_id: Mapped[str] = mapped_column(String(24), nullable=False)
    approval_evaluation_id: Mapped[str] = mapped_column(String(24), nullable=False)
    approval_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(20))
    command_results: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    audit_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    resolved_from_unknown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MonthlyCloseVerification(Base):
    __tablename__ = "monthly_close_verifications"
    __table_args__ = (
        UniqueConstraint("verification_id", "cycle_id", name="uq_monthly_close_verification_id_cycle"),
        UniqueConstraint(
            "verification_id",
            "attempt_id",
            "cycle_id",
            name="uq_monthly_close_verification_id_attempt_cycle",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "cycle_id"],
            ["monthly_close_execution_attempts.attempt_id", "monthly_close_execution_attempts.cycle_id"],
            name="fk_monthly_close_verification_attempt_cycle",
        ),
        CheckConstraint(f"status IN ({_sql_values(VERIFICATION_STATUSES)})", name="ck_monthly_close_verification_status"),
        Index("ix_monthly_close_verification_attempt_status", "attempt_id", "status"),
    )

    verification_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(24), nullable=False)
    verification_version: Mapped[str] = mapped_column(String(80), nullable=False)
    checks: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    verified_subject_versions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MonthlyCloseIssueInstance(Base):
    __tablename__ = "monthly_close_issue_instances"
    __table_args__ = (
        UniqueConstraint("cycle_id", "adapter_type", "source_subject_id", "issue_key", name="uq_monthly_close_issue_cycle_subject_key"),
        UniqueConstraint("issue_id", "cycle_id", name="uq_monthly_close_issue_id_cycle"),
        ForeignKeyConstraint(
            ["resolution_proposal_id", "cycle_id"],
            ["monthly_close_action_proposals.proposal_id", "monthly_close_action_proposals.cycle_id"],
            name="fk_monthly_close_issue_resolution_proposal_cycle",
        ),
        ForeignKeyConstraint(
            ["verification_id", "cycle_id"],
            ["monthly_close_verifications.verification_id", "monthly_close_verifications.cycle_id"],
            name="fk_monthly_close_issue_verification_cycle",
        ),
        CheckConstraint(f"status IN ({_sql_values(ISSUE_STATUSES)})", name="ck_monthly_close_issue_status"),
        Index("ix_monthly_close_issue_cycle_status", "cycle_id", "status"),
    )

    issue_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(24), ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_subject_id: Mapped[str] = mapped_column(String(80), nullable=False)
    issue_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open", server_default="open")
    assigned_to: Mapped[str | None] = mapped_column(String(20))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manual_note: Mapped[str | None] = mapped_column(Text)
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolution_proposal_id: Mapped[str | None] = mapped_column(String(24))
    verification_id: Mapped[str | None] = mapped_column(String(24))
    last_seen_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class MonthlyCloseOtaSettlementConsumption(Base):
    """Immutable proof that one later-statement row settled one OTA appeal."""

    __tablename__ = "monthly_close_ota_settlement_consumptions"
    __table_args__ = (
        UniqueConstraint(
            "row_identity_hash", name="uq_mc_ota_consumption_row_identity"
        ),
        UniqueConstraint(
            "later_cycle_id",
            "later_document_id",
            "later_batch_id",
            "platform_namespace",
            "source_row_index",
            name="uq_mc_ota_consumption_source_row",
        ),
        UniqueConstraint("original_issue_id", name="uq_mc_ota_consumption_issue"),
        Index("ix_mc_ota_consumption_later_cycle", "later_cycle_id"),
        Index("ix_mc_ota_consumption_original_cycle", "original_cycle_id"),
    )

    consumption_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    row_identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    settlement_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    later_cycle_id: Mapped[str] = mapped_column(
        String(24), ForeignKey("monthly_close_cycles.cycle_id"), nullable=False
    )
    later_document_id: Mapped[str] = mapped_column(
        String(24), ForeignKey("monthly_close_documents.document_id"), nullable=False
    )
    later_document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    later_batch_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("recon_batches.batch_id"), nullable=False
    )
    later_batch_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_namespace: Mapped[str] = mapped_column(String(80), nullable=False)
    source_row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source_row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    economic_facts: Mapped[dict] = mapped_column(JSONB, nullable=False)
    original_cycle_id: Mapped[str] = mapped_column(
        String(24), ForeignKey("monthly_close_cycles.cycle_id"), nullable=False
    )
    original_issue_id: Mapped[str] = mapped_column(
        String(24), ForeignKey("monthly_close_issue_instances.issue_id"), nullable=False
    )
    original_batch_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("recon_batches.batch_id"), nullable=False
    )
    original_diff_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("recon_diffs.diff_id"), nullable=False
    )
    proposal_id: Mapped[str] = mapped_column(
        String(24), ForeignKey("monthly_close_action_proposals.proposal_id"), nullable=False
    )
    attempt_id: Mapped[str] = mapped_column(
        String(24), ForeignKey("monthly_close_execution_attempts.attempt_id"), nullable=False
    )
    verification_id: Mapped[str] = mapped_column(
        String(24), ForeignKey("monthly_close_verifications.verification_id"), nullable=False
    )
    request_id: Mapped[str] = mapped_column(String(80), nullable=False)
    created_by: Mapped[str] = mapped_column(
        String(20), ForeignKey("users.user_id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MonthlyCloseRemediation(Base):
    __tablename__ = "monthly_close_remediations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "cycle_id"],
            ["monthly_close_execution_attempts.attempt_id", "monthly_close_execution_attempts.cycle_id"],
            name="fk_monthly_close_remediation_attempt_cycle",
        ),
        ForeignKeyConstraint(
            ["issue_id", "cycle_id"],
            ["monthly_close_issue_instances.issue_id", "monthly_close_issue_instances.cycle_id"],
            name="fk_monthly_close_remediation_issue_cycle",
        ),
        ForeignKeyConstraint(
            ["verification_id", "attempt_id", "cycle_id"],
            [
                "monthly_close_verifications.verification_id",
                "monthly_close_verifications.attempt_id",
                "monthly_close_verifications.cycle_id",
            ],
            name="fk_monthly_close_remediation_verification_cycle",
        ),
        ForeignKeyConstraint(
            ["resolution_proposal_id", "cycle_id"],
            ["monthly_close_action_proposals.proposal_id", "monthly_close_action_proposals.cycle_id"],
            name="fk_monthly_close_remediation_proposal_cycle",
        ),
        CheckConstraint(f"status IN ({_sql_values(REMEDIATION_STATUSES)})", name="ck_monthly_close_remediation_status"),
        CheckConstraint("status != 'resolved' OR verification_id IS NOT NULL", name="ck_monthly_close_remediation_resolved_verification"),
        Index("ix_monthly_close_remediation_attempt_status", "attempt_id", "status"),
    )

    remediation_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(24), nullable=False)
    issue_id: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open", server_default="open")
    observed_state: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    resolution_proposal_id: Mapped[str | None] = mapped_column(String(24))
    verification_id: Mapped[str | None] = mapped_column(String(24))
    assigned_to: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MonthlyCloseOutbox(Base):
    __tablename__ = "monthly_close_outbox"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_monthly_close_outbox_dedupe"),
        CheckConstraint(f"status IN ({_sql_values(OUTBOX_STATUSES)})", name="ck_monthly_close_outbox_status"),
        Index("ix_monthly_close_outbox_available_at", "available_at"),
    )

    outbox_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(24), ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"), nullable=False)
    topic: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    lease_owner: Mapped[str | None] = mapped_column(String(80))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
