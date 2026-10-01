"""add monthly-close assistant control-plane records

Revision ID: monthlyclose0831control
Revises: cleaning0830cutoff
Create Date: 2026-08-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "monthlyclose0831control"
down_revision: Union[str, None] = "cleaning0830cutoff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


JSONB = postgresql.JSONB(astext_type=sa.Text())


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def upgrade() -> None:
    op.add_column(
        "monthly_close_cycles",
        sa.Column("write_control_owner", sa.String(length=16), nullable=False, server_default="legacy"),
    )
    op.add_column(
        "monthly_close_cycles",
        sa.Column("control_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_monthly_close_cycles_write_control_owner",
        "monthly_close_cycles",
        "write_control_owner IN ('legacy', 'assistant')",
    )
    op.create_check_constraint(
        "ck_monthly_close_cycles_control_version",
        "monthly_close_cycles",
        "control_version >= 1",
    )

    op.create_table(
        "monthly_close_conversations",
        sa.Column("conversation_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("audience_scope", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(length=20), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("audience_scope IN ('finance', 'operations', 'assigned_staff')", name="ck_monthly_close_conversation_audience_scope"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_monthly_close_conversation_status"),
        sa.ForeignKeyConstraint(["cycle_id"], ["monthly_close_cycles.cycle_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("conversation_id"),
        sa.UniqueConstraint("conversation_id", "cycle_id", name="uq_monthly_close_conversation_id_cycle"),
    )
    op.create_index("ix_monthly_close_conversation_cycle_status", "monthly_close_conversations", ["cycle_id", "status"])

    op.create_table(
        "monthly_close_agent_runs",
        sa.Column("run_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("conversation_id", sa.String(length=24), nullable=True),
        sa.Column("parent_run_id", sa.String(length=24), nullable=True),
        sa.Column("trigger_type", sa.String(length=24), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=20), nullable=True),
        sa.Column("permission_snapshot", JSONB, nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
        sa.Column("model_provider", sa.String(length=80), nullable=True),
        sa.Column("model_name", sa.String(length=160), nullable=True),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("tool_manifest_version", sa.String(length=80), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_detail_redacted", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("trigger_type IN ('user_message', 'file_received', 'schedule', 'retry')", name="ck_monthly_close_run_trigger_type"),
        sa.CheckConstraint("actor_type IN ('user', 'system')", name="ck_monthly_close_run_actor_type"),
        sa.CheckConstraint("status IN ('queued', 'running', 'waiting_user', 'waiting_approval', 'succeeded', 'degraded', 'failed', 'cancelled')", name="ck_monthly_close_run_status"),
        sa.ForeignKeyConstraint(["cycle_id"], ["monthly_close_cycles.cycle_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id", "cycle_id"], ["monthly_close_conversations.conversation_id", "monthly_close_conversations.cycle_id"], name="fk_monthly_close_run_conversation_cycle"),
        sa.ForeignKeyConstraint(["parent_run_id", "cycle_id"], ["monthly_close_agent_runs.run_id", "monthly_close_agent_runs.cycle_id"], name="fk_monthly_close_run_parent_cycle"),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("run_id", "cycle_id", name="uq_monthly_close_run_id_cycle"),
    )
    op.create_index("ix_monthly_close_run_cycle_status", "monthly_close_agent_runs", ["cycle_id", "status"])

    op.create_table(
        "monthly_close_messages",
        sa.Column("message_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("conversation_id", sa.String(length=24), nullable=False),
        sa.Column("run_id", sa.String(length=24), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content_redacted", sa.Text(), nullable=False),
        sa.Column("content_encrypted", sa.Text(), nullable=True),
        sa.Column("attachments", JSONB, nullable=False, server_default="[]"),
        sa.Column("visibility_scope", sa.String(length=24), nullable=False),
        sa.Column("created_by", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("role IN ('user', 'assistant', 'system', 'tool_event')", name="ck_monthly_close_message_role"),
        sa.CheckConstraint("visibility_scope IN ('finance', 'operations', 'assigned_staff')", name="ck_monthly_close_message_visibility_scope"),
        sa.ForeignKeyConstraint(["conversation_id", "cycle_id"], ["monthly_close_conversations.conversation_id", "monthly_close_conversations.cycle_id"], name="fk_monthly_close_message_conversation_cycle"),
        sa.ForeignKeyConstraint(["run_id", "cycle_id"], ["monthly_close_agent_runs.run_id", "monthly_close_agent_runs.cycle_id"], name="fk_monthly_close_message_run_cycle"),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index("ix_monthly_close_message_cycle_created", "monthly_close_messages", ["cycle_id", "created_at"])

    op.create_table(
        "monthly_close_agent_events",
        sa.Column("event_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("conversation_id", sa.String(length=24), nullable=True),
        sa.Column("run_id", sa.String(length=24), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("conversation_sequence", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("schema_version", sa.String(length=40), nullable=False),
        sa.Column("payload_redacted", JSONB, nullable=False),
        sa.Column("visibility_scope", sa.String(length=24), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("persisted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["cycle_id"], ["monthly_close_cycles.cycle_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id", "cycle_id"], ["monthly_close_conversations.conversation_id", "monthly_close_conversations.cycle_id"], name="fk_monthly_close_event_conversation_cycle"),
        sa.ForeignKeyConstraint(["run_id", "cycle_id"], ["monthly_close_agent_runs.run_id", "monthly_close_agent_runs.cycle_id"], name="fk_monthly_close_event_run_cycle"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("cycle_id", "sequence", name="uq_monthly_close_event_cycle_sequence"),
        sa.UniqueConstraint("cycle_id", "dedupe_key", name="uq_monthly_close_event_cycle_dedupe"),
    )
    op.create_index("ix_monthly_close_event_cycle_sequence", "monthly_close_agent_events", ["cycle_id", "sequence"])

    op.create_table(
        "monthly_close_processing_jobs",
        sa.Column("job_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("job_type", sa.String(length=80), nullable=False),
        sa.Column("job_version", sa.String(length=80), nullable=False),
        sa.Column("subject_type", sa.String(length=80), nullable=False),
        sa.Column("subject_id", sa.String(length=80), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("lease_owner", sa.String(length=80), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("actor_type", sa.String(length=16), nullable=True),
        sa.Column("actor_id", sa.String(length=20), nullable=True),
        sa.Column("permission_policy_version", sa.String(length=80), nullable=True),
        sa.Column("payload", JSONB, nullable=False, server_default="{}"),
        *_timestamps(),
        sa.CheckConstraint("status IN ('pending', 'leased', 'succeeded', 'failed', 'dead_letter')", name="ck_monthly_close_job_status"),
        sa.ForeignKeyConstraint(["cycle_id"], ["monthly_close_cycles.cycle_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("cycle_id", "dedupe_key", name="uq_monthly_close_job_cycle_dedupe"),
    )
    op.create_index("ix_monthly_close_job_cycle_status", "monthly_close_processing_jobs", ["cycle_id", "status"])
    op.create_index("ix_monthly_close_job_available_at", "monthly_close_processing_jobs", ["available_at"])

    op.create_table(
        "monthly_close_action_proposals",
        sa.Column("proposal_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("proposal_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
        sa.Column("proposal_version", sa.Integer(), nullable=False),
        sa.Column("supersedes_proposal_id", sa.String(length=24), nullable=True),
        sa.Column("schema_version", sa.String(length=80), nullable=False, server_default="v1"),
        sa.Column("canonical_payload", JSONB, nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_refs", JSONB, nullable=False, server_default="[]"),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("subject_versions", JSONB, nullable=False),
        sa.Column("active_input_set_hash", sa.String(length=64), nullable=False),
        sa.Column("mapping_versions", JSONB, nullable=False),
        sa.Column("ruleset_version", sa.String(length=80), nullable=False),
        sa.Column("calculation_version", sa.String(length=80), nullable=False),
        sa.Column("configuration_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("approval_policy_snapshot", JSONB, nullable=False),
        sa.Column("impact_snapshot", JSONB, nullable=False),
        sa.Column("impact_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("validation_snapshot", JSONB, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_run_id", sa.String(length=24), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('draft', 'pending_approval', 'approved', 'rejected', 'stale', 'superseded')", name="ck_monthly_close_proposal_status"),
        sa.ForeignKeyConstraint(["cycle_id"], ["monthly_close_cycles.cycle_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supersedes_proposal_id", "cycle_id"], ["monthly_close_action_proposals.proposal_id", "monthly_close_action_proposals.cycle_id"], name="fk_monthly_close_proposal_supersedes_cycle"),
        sa.ForeignKeyConstraint(["created_by_run_id", "cycle_id"], ["monthly_close_agent_runs.run_id", "monthly_close_agent_runs.cycle_id"], name="fk_monthly_close_proposal_created_run_cycle"),
        sa.PrimaryKeyConstraint("proposal_id"),
        sa.UniqueConstraint("proposal_id", "cycle_id", name="uq_monthly_close_proposal_id_cycle"),
    )
    op.create_index("ix_monthly_close_proposal_cycle_status", "monthly_close_action_proposals", ["cycle_id", "status"])

    op.create_table(
        "monthly_close_action_approvals",
        sa.Column("approval_id", sa.String(length=24), nullable=False),
        sa.Column("proposal_id", sa.String(length=24), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decided_by", sa.String(length=20), nullable=False),
        sa.Column("approval_role", sa.String(length=80), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("required_approvals", sa.Integer(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("maker_is_checker", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("decision IN ('approved', 'rejected')", name="ck_monthly_close_approval_decision"),
        sa.ForeignKeyConstraint(["proposal_id"], ["monthly_close_action_proposals.proposal_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("approval_id"),
        sa.UniqueConstraint("proposal_id", "decided_by", name="uq_monthly_close_approval_proposal_decider"),
        sa.UniqueConstraint("proposal_id", "request_id", name="uq_monthly_close_approval_proposal_request"),
    )
    op.create_index("ix_monthly_close_approval_proposal", "monthly_close_action_approvals", ["proposal_id"])

    op.create_table(
        "monthly_close_approval_evaluations",
        sa.Column("evaluation_id", sa.String(length=24), nullable=False),
        sa.Column("proposal_id", sa.String(length=24), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("approval_ids", JSONB, nullable=False),
        sa.Column("approval_set_hash", sa.String(length=64), nullable=False),
        sa.Column("satisfied", sa.Boolean(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["proposal_id"], ["monthly_close_action_proposals.proposal_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("evaluation_id"),
        sa.UniqueConstraint("evaluation_id", "proposal_id", name="uq_monthly_close_evaluation_id_proposal"),
        sa.UniqueConstraint("evaluation_id", "proposal_id", "approval_set_hash", name="uq_monthly_close_evaluation_id_proposal_hash"),
    )
    op.create_index("ix_monthly_close_evaluation_proposal", "monthly_close_approval_evaluations", ["proposal_id"])

    op.create_table(
        "monthly_close_execution_attempts",
        sa.Column("attempt_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("proposal_id", sa.String(length=24), nullable=False),
        sa.Column("approval_evaluation_id", sa.String(length=24), nullable=False),
        sa.Column("approval_set_hash", sa.String(length=64), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=20), nullable=True),
        sa.Column("command_results", JSONB, nullable=False, server_default="[]"),
        sa.Column("audit_refs", JSONB, nullable=False, server_default="[]"),
        sa.Column("resolved_from_unknown_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_evidence_refs", JSONB, nullable=False, server_default="[]"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('queued', 'executing', 'succeeded', 'failed_safe', 'failed_unsafe', 'unknown', 'succeeded_confirmed', 'failed_confirmed', 'manual_remediation_required')", name="ck_monthly_close_attempt_status"),
        sa.ForeignKeyConstraint(["cycle_id"], ["monthly_close_cycles.cycle_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["proposal_id", "cycle_id"], ["monthly_close_action_proposals.proposal_id", "monthly_close_action_proposals.cycle_id"], name="fk_monthly_close_attempt_proposal_cycle"),
        sa.ForeignKeyConstraint(["approval_evaluation_id", "proposal_id", "approval_set_hash"], ["monthly_close_approval_evaluations.evaluation_id", "monthly_close_approval_evaluations.proposal_id", "monthly_close_approval_evaluations.approval_set_hash"], name="fk_monthly_close_attempt_evaluation_proposal"),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint("attempt_id", "cycle_id", name="uq_monthly_close_attempt_id_cycle"),
        sa.UniqueConstraint("proposal_id", "request_id", name="uq_monthly_close_attempt_proposal_request"),
        sa.UniqueConstraint("cycle_id", "idempotency_key", name="uq_monthly_close_attempt_cycle_idempotency"),
    )
    op.create_index("uq_monthly_close_attempt_one_executing", "monthly_close_execution_attempts", ["proposal_id"], unique=True, postgresql_where=sa.text("status = 'executing'"))
    op.create_index("ix_monthly_close_attempt_proposal_status", "monthly_close_execution_attempts", ["proposal_id", "status"])

    op.create_table(
        "monthly_close_verifications",
        sa.Column("verification_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("attempt_id", sa.String(length=24), nullable=False),
        sa.Column("verification_version", sa.String(length=80), nullable=False),
        sa.Column("checks", JSONB, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("verified_subject_versions", JSONB, nullable=False),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('pending', 'running', 'passed', 'failed', 'inconclusive')", name="ck_monthly_close_verification_status"),
        sa.ForeignKeyConstraint(["attempt_id", "cycle_id"], ["monthly_close_execution_attempts.attempt_id", "monthly_close_execution_attempts.cycle_id"], name="fk_monthly_close_verification_attempt_cycle"),
        sa.PrimaryKeyConstraint("verification_id"),
        sa.UniqueConstraint("verification_id", "cycle_id", name="uq_monthly_close_verification_id_cycle"),
        sa.UniqueConstraint("verification_id", "attempt_id", "cycle_id", name="uq_monthly_close_verification_id_attempt_cycle"),
    )
    op.create_index("ix_monthly_close_verification_attempt_status", "monthly_close_verifications", ["attempt_id", "status"])

    op.create_table(
        "monthly_close_issue_instances",
        sa.Column("issue_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("adapter_type", sa.String(length=80), nullable=False),
        sa.Column("source_subject_id", sa.String(length=80), nullable=False),
        sa.Column("issue_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="open"),
        sa.Column("assigned_to", sa.String(length=20), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manual_note", sa.Text(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolution_proposal_id", sa.String(length=24), nullable=True),
        sa.Column("verification_id", sa.String(length=24), nullable=True),
        sa.Column("last_seen_evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("status IN ('open', 'assigned', 'waiting_information', 'proposed_fix', 'rework', 'resolved', 'cancelled', 'reopened')", name="ck_monthly_close_issue_status"),
        sa.ForeignKeyConstraint(["cycle_id"], ["monthly_close_cycles.cycle_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolution_proposal_id", "cycle_id"], ["monthly_close_action_proposals.proposal_id", "monthly_close_action_proposals.cycle_id"], name="fk_monthly_close_issue_resolution_proposal_cycle"),
        sa.ForeignKeyConstraint(["verification_id", "cycle_id"], ["monthly_close_verifications.verification_id", "monthly_close_verifications.cycle_id"], name="fk_monthly_close_issue_verification_cycle"),
        sa.PrimaryKeyConstraint("issue_id"),
        sa.UniqueConstraint("cycle_id", "adapter_type", "source_subject_id", "issue_key", name="uq_monthly_close_issue_cycle_subject_key"),
        sa.UniqueConstraint("issue_id", "cycle_id", name="uq_monthly_close_issue_id_cycle"),
    )
    op.create_index("ix_monthly_close_issue_cycle_status", "monthly_close_issue_instances", ["cycle_id", "status"])

    op.create_table(
        "monthly_close_remediations",
        sa.Column("remediation_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("attempt_id", sa.String(length=24), nullable=False),
        sa.Column("issue_id", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="open"),
        sa.Column("observed_state", sa.Text(), nullable=False),
        sa.Column("evidence_refs", JSONB, nullable=False, server_default="[]"),
        sa.Column("resolution_proposal_id", sa.String(length=24), nullable=True),
        sa.Column("verification_id", sa.String(length=24), nullable=True),
        sa.Column("assigned_to", sa.String(length=20), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'investigating', 'compensating', 'resolved', 'rework')", name="ck_monthly_close_remediation_status"),
        sa.CheckConstraint("status != 'resolved' OR verification_id IS NOT NULL", name="ck_monthly_close_remediation_resolved_verification"),
        sa.ForeignKeyConstraint(["attempt_id", "cycle_id"], ["monthly_close_execution_attempts.attempt_id", "monthly_close_execution_attempts.cycle_id"], name="fk_monthly_close_remediation_attempt_cycle"),
        sa.ForeignKeyConstraint(["issue_id", "cycle_id"], ["monthly_close_issue_instances.issue_id", "monthly_close_issue_instances.cycle_id"], name="fk_monthly_close_remediation_issue_cycle"),
        sa.ForeignKeyConstraint(["verification_id", "attempt_id", "cycle_id"], ["monthly_close_verifications.verification_id", "monthly_close_verifications.attempt_id", "monthly_close_verifications.cycle_id"], name="fk_monthly_close_remediation_verification_cycle"),
        sa.ForeignKeyConstraint(["resolution_proposal_id", "cycle_id"], ["monthly_close_action_proposals.proposal_id", "monthly_close_action_proposals.cycle_id"], name="fk_monthly_close_remediation_proposal_cycle"),
        sa.PrimaryKeyConstraint("remediation_id"),
    )
    op.create_index("ix_monthly_close_remediation_attempt_status", "monthly_close_remediations", ["attempt_id", "status"])

    op.create_table(
        "monthly_close_outbox",
        sa.Column("outbox_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("topic", sa.String(length=120), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("lease_owner", sa.String(length=80), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("status IN ('pending', 'leased', 'delivered', 'failed', 'dead_letter')", name="ck_monthly_close_outbox_status"),
        sa.ForeignKeyConstraint(["cycle_id"], ["monthly_close_cycles.cycle_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("outbox_id"),
        sa.UniqueConstraint("dedupe_key", name="uq_monthly_close_outbox_dedupe"),
    )
    op.create_index("ix_monthly_close_outbox_available_at", "monthly_close_outbox", ["available_at"])


def downgrade() -> None:
    op.drop_index("ix_monthly_close_outbox_available_at", table_name="monthly_close_outbox")
    op.drop_table("monthly_close_outbox")
    op.drop_index("ix_monthly_close_remediation_attempt_status", table_name="monthly_close_remediations")
    op.drop_table("monthly_close_remediations")
    op.drop_index("ix_monthly_close_issue_cycle_status", table_name="monthly_close_issue_instances")
    op.drop_table("monthly_close_issue_instances")
    op.drop_index("ix_monthly_close_verification_attempt_status", table_name="monthly_close_verifications")
    op.drop_table("monthly_close_verifications")
    op.drop_index("ix_monthly_close_attempt_proposal_status", table_name="monthly_close_execution_attempts")
    op.drop_index("uq_monthly_close_attempt_one_executing", table_name="monthly_close_execution_attempts")
    op.drop_table("monthly_close_execution_attempts")
    op.drop_index("ix_monthly_close_evaluation_proposal", table_name="monthly_close_approval_evaluations")
    op.drop_table("monthly_close_approval_evaluations")
    op.drop_index("ix_monthly_close_approval_proposal", table_name="monthly_close_action_approvals")
    op.drop_table("monthly_close_action_approvals")
    op.drop_index("ix_monthly_close_proposal_cycle_status", table_name="monthly_close_action_proposals")
    op.drop_table("monthly_close_action_proposals")
    op.drop_index("ix_monthly_close_job_available_at", table_name="monthly_close_processing_jobs")
    op.drop_index("ix_monthly_close_job_cycle_status", table_name="monthly_close_processing_jobs")
    op.drop_table("monthly_close_processing_jobs")
    op.drop_index("ix_monthly_close_event_cycle_sequence", table_name="monthly_close_agent_events")
    op.drop_table("monthly_close_agent_events")
    op.drop_index("ix_monthly_close_message_cycle_created", table_name="monthly_close_messages")
    op.drop_table("monthly_close_messages")
    op.drop_index("ix_monthly_close_run_cycle_status", table_name="monthly_close_agent_runs")
    op.drop_table("monthly_close_agent_runs")
    op.drop_index("ix_monthly_close_conversation_cycle_status", table_name="monthly_close_conversations")
    op.drop_table("monthly_close_conversations")
    op.drop_constraint("ck_monthly_close_cycles_control_version", "monthly_close_cycles", type_="check")
    op.drop_constraint("ck_monthly_close_cycles_write_control_owner", "monthly_close_cycles", type_="check")
    op.drop_column("monthly_close_cycles", "control_version")
    op.drop_column("monthly_close_cycles", "write_control_owner")
