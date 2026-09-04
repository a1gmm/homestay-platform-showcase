from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.schemas.miniapp_content_guides import (
    ApproximateLocation,
    Audience,
    Details,
    FaqAnswer,
    FaqQuestion,
    Intro,
    PublishedAt,
    Reason,
    RecommendationId,
    RecommendationName,
    StayGuideDraft,
    SuggestedDuration,
    Summary,
    Title,
    TravelCategory,
    TravelGuideDraft,
    Version,
)


GuideChannel = Literal["stay_guide", "travel"]
GuideDraft = StayGuideDraft | TravelGuideDraft


class GuidePreviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)


class GuidePreviewMediaRef(GuidePreviewModel):
    """Private signed media reference for authenticated draft previews only."""

    url: str = Field(pattern=r"^https://")
    alt: str = Field(min_length=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    mime_type: Literal["image/jpeg", "image/png", "image/webp"] = Field(
        alias="mimeType"
    )
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StayGuideSectionPreview(GuidePreviewModel):
    summary: Summary
    details: Details | None = None
    image: GuidePreviewMediaRef | None = None
    visible: bool


class StayGuideFaqItemPreview(GuidePreviewModel):
    question: FaqQuestion
    answer: FaqAnswer


class StayGuideFaqSectionPreview(StayGuideSectionPreview):
    items: list[StayGuideFaqItemPreview] = Field(min_length=1, max_length=20)


class StayGuideSectionsPreview(GuidePreviewModel):
    arrival_departure: StayGuideSectionPreview = Field(alias="arrivalDeparture")
    parking: StayGuideSectionPreview
    wifi_and_devices: StayGuideSectionPreview = Field(alias="wifiAndDevices")
    house_rules: StayGuideSectionPreview = Field(alias="houseRules")
    check_out: StayGuideSectionPreview = Field(alias="checkOut")
    support: StayGuideSectionPreview
    faq: StayGuideFaqSectionPreview


class StayGuidePreviewManifest(GuidePreviewModel):
    schema_: Literal["guanhaiju.stay_guide.preview.v1"] = Field(alias="schema")
    version: Version
    published_at: PublishedAt = Field(alias="publishedAt")
    title: Title
    intro: Intro
    sections: StayGuideSectionsPreview


class TravelRecommendationPreview(GuidePreviewModel):
    id: RecommendationId
    name: RecommendationName
    category: TravelCategory
    reason: Reason
    approximate_location: ApproximateLocation | None = Field(
        default=None, alias="approximateLocation"
    )
    suggested_duration: SuggestedDuration | None = Field(
        default=None, alias="suggestedDuration"
    )
    audiences: list[Audience] = Field(min_length=1, max_length=6)
    image: GuidePreviewMediaRef | None = None
    visible: bool


class TravelGuidePreviewManifest(GuidePreviewModel):
    schema_: Literal["guanhaiju.travel.preview.v1"] = Field(alias="schema")
    version: Version
    published_at: PublishedAt = Field(alias="publishedAt")
    title: Title
    intro: Intro
    recommendations: list[TravelRecommendationPreview] = Field(
        min_length=1, max_length=50
    )


GuidePreviewManifest = StayGuidePreviewManifest | TravelGuidePreviewManifest


class StructuredGuidePreview(GuidePreviewModel):
    channel: GuideChannel
    revision: int = Field(ge=0)
    manifest: GuidePreviewManifest


class StructuredDraftSaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)

    expected_revision: int = Field(alias="expectedRevision", ge=0)
    draft: GuideDraft


class StructuredDraftRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)

    channel: GuideChannel
    revision: int = Field(ge=0)
    draft: GuideDraft
    draft_dirty: bool = Field(alias="draftDirty")
    saved_at: datetime | None = Field(alias="savedAt")
    saved_by: str | None = Field(alias="savedBy")

    @field_serializer("draft")
    def serialize_draft(self, draft: GuideDraft) -> dict:
        return draft.model_dump(by_alias=True, exclude_none=True)


__all__ = [
    "GuideChannel",
    "GuideDraft",
    "GuidePreviewManifest",
    "GuidePreviewMediaRef",
    "StayGuidePreviewManifest",
    "StructuredGuidePreview",
    "StructuredDraftRevision",
    "StructuredDraftSaveInput",
    "TravelGuidePreviewManifest",
]
