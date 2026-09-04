"""Typed authenticated preview manifests for structured guide drafts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from app.schemas.miniapp_content_guides import StayGuideDraft, TravelGuideDraft
from app.schemas.miniapp_content_guides_api import (
    GuidePreviewManifest,
    StayGuidePreviewManifest,
    TravelGuidePreviewManifest,
)
from app.services.miniapp_content.errors import ContentValidationError
from app.services.miniapp_content.publisher import StructuredGuideDraftSnapshot


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _preview_image(
    snapshot: StructuredGuideDraftSnapshot,
    media_id: str,
    *,
    alt: str,
    sign_url: Callable[[str, int], str],
) -> dict:
    try:
        ref = snapshot.media[media_id]
    except KeyError as exc:
        raise ContentValidationError(
            "guide preview references unavailable media"
        ) from exc
    return {
        "url": sign_url(ref.stored.object_key, 900),
        "alt": alt,
        "width": ref.width,
        "height": ref.height,
        "mimeType": ref.stored.mime_type,
        "sha256": ref.stored.sha256,
    }


def build_guide_preview_manifest(
    snapshot: StructuredGuideDraftSnapshot,
    *,
    sign_url: Callable[[str, int], str],
    created_at: datetime,
) -> GuidePreviewManifest:
    draft = snapshot.draft.model_dump(by_alias=True, exclude_none=True)
    common = {
        "version": f"preview-{snapshot.revision}",
        "publishedAt": _utc_iso(created_at),
        **draft,
    }
    if snapshot.channel == "stay_guide" and isinstance(
        snapshot.draft, StayGuideDraft
    ):
        for section in common["sections"].values():
            image = section.get("image")
            if image is not None:
                section["image"] = _preview_image(
                    snapshot,
                    image["mediaId"],
                    alt=section["summary"],
                    sign_url=sign_url,
                )
        return StayGuidePreviewManifest.model_validate(
            {"schema": "guanhaiju.stay_guide.preview.v1", **common}
        )
    if snapshot.channel == "travel" and isinstance(snapshot.draft, TravelGuideDraft):
        for recommendation in common["recommendations"]:
            image = recommendation.get("image")
            if image is not None:
                recommendation["image"] = _preview_image(
                    snapshot,
                    image["mediaId"],
                    alt=recommendation["name"],
                    sign_url=sign_url,
                )
        return TravelGuidePreviewManifest.model_validate(
            {"schema": "guanhaiju.travel.preview.v1", **common}
        )
    raise ContentValidationError("guide preview snapshot has the wrong channel")


__all__ = ["build_guide_preview_manifest"]
