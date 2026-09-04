"""Typed, PII-free responses for the BYPMS operations workbench."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


CycleStatus = Literal["running", "succeeded", "partial", "failed", "skipped"]
CycleTrigger = Literal["scheduled", "admin_retry", "unknown"]
StepName = Literal[
    "pull",
    "price_reconcile",
    "name_reconcile",
    "create",
    "date_reconcile",
    "room_reconcile",
    "assign",
    "status_reconcile",
    "subsidized_scan",
    "cancel",
    "room_status_reconcile",
    "unknown",
]
StepStatus = Literal["running", "succeeded", "failed", "skipped"]
StepSummaryStatus = Literal["success", "failure", "skipped"]
StepErrorCode = Literal[
    "authentication_failed",
    "cancelled",
    "configuration_invalid",
    "conflict",
    "database_unavailable",
    "internal_error",
    "incomplete_response",
    "network_error",
    "not_found",
    "timeout",
    "unknown",
    "upstream_unavailable",
    "upstream_http_error",
    "upstream_response_invalid",
    "validation_failed",
]
ConflictStatus = Literal["open", "ignored", "resolved"]
RetryActor = Literal["admin", "operations", "ops", "pms_admin", "system"]
RetryStatus = Literal["queued", "running", "succeeded", "failed", "skipped"]


class BypmsSyncStepSummary(BaseModel):
    name: StepName
    status: StepSummaryStatus
    counts: dict[str, int] = Field(default_factory=dict)
    error_code: StepErrorCode | None = None
    duration_ms: int = Field(ge=0)


class BypmsSyncStep(BaseModel):
    step_id: int
    name: StepName
    status: StepStatus
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int = Field(ge=0)
    summary: BypmsSyncStepSummary | None = None


class BypmsSyncCycle(BaseModel):
    cycle_id: str = Field(max_length=24)
    trigger: CycleTrigger
    status: CycleStatus
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int = Field(ge=0)
    write_modes: dict[str, bool] = Field(default_factory=dict)


class BypmsSyncCycleWithSteps(BypmsSyncCycle):
    steps: list[BypmsSyncStep] = Field(default_factory=list)


class BypmsSyncOverview(BaseModel):
    available: bool
    message: str | None = None
    stale_after_seconds: int = Field(gt=0)
    clock_skew_detected: bool
    latest_cycle: BypmsSyncCycle | None = None
    last_successful_or_partial_at: datetime | None = None
    last_successful_or_partial_age_seconds: int | None = Field(default=None, ge=0)
    pending_retry_count: int = Field(default=0, ge=0)
    open_conflicts_by_field: dict[str, int] = Field(default_factory=dict)
    staging_watermark: datetime | None = None
    staging_lag_seconds: int | None = Field(default=None, ge=0)
    write_modes: dict[str, bool] = Field(default_factory=dict)
    retry_available: bool = False


class BypmsSyncCyclesResponse(BaseModel):
    available: bool
    message: str | None = None
    items: list[BypmsSyncCycleWithSteps] = Field(default_factory=list)


class BypmsSyncConflict(BaseModel):
    conflict_id: str = Field(max_length=40)
    source_order_id: str = Field(max_length=20)
    channel: str = Field(max_length=32)
    check_in_date: date
    check_out_date: date
    field: str = Field(max_length=50)
    local_value: Any
    upstream_value: Any
    status: ConflictStatus
    first_seen_at: datetime
    last_seen_at: datetime


class BypmsSyncConflictsResponse(BaseModel):
    items: list[BypmsSyncConflict] = Field(default_factory=list)
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class BypmsSyncRetryResponse(BaseModel):
    request_id: str = Field(max_length=24)
    requested_by: RetryActor
    status: RetryStatus
    requested_at: datetime
