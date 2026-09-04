"""Shared read and configuration endpoints for mini-program content."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core.deps import DBSession, require_role
from app.models.miniapp_content import (
    MiniappChannelConfig,
    MiniappContentRelease,
    MiniappContentWorkspace,
)
from app.services.miniapp_content.errors import ContentValidationError
from app.schemas.miniapp_content_dashboard import MiniappContentDashboard


router = APIRouter(prefix="/miniapp-content", tags=["miniapp-content"])

PUBLIC_CHANNEL_ORDER = ("owner", "stay_guide", "travel")
SUPPORTED_PUBLIC_CHANNELS = frozenset(PUBLIC_CHANNEL_ORDER)
PUBLIC_CAPABILITY_PROTOCOL = "miniapp-content.v2"


class ChannelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


@router.get("/dashboard", response_model=MiniappContentDashboard)
async def dashboard(
    db: DBSession,
    _current_user: dict = Depends(require_role("admin", "operator")),
) -> MiniappContentDashboard:
    configs = {
        row.channel: row
        for row in (await db.scalars(select(MiniappChannelConfig))).all()
    }
    workspaces = {
        row.channel: row
        for row in (await db.scalars(select(MiniappContentWorkspace))).all()
    }
    active_release_ids = {
        workspace.current_release_id
        for workspace in workspaces.values()
        if workspace.current_release_id is not None
    }
    active_releases = {
        row.release_id: row
        for row in (
            await db.scalars(
                select(MiniappContentRelease).where(
                    MiniappContentRelease.release_id.in_(active_release_ids),
                    MiniappContentRelease.status == "published",
                )
            )
        ).all()
    } if active_release_ids else {}
    return MiniappContentDashboard(
        channels=[
            {
                "channel": channel,
                "enabled": bool(configs.get(channel) and configs[channel].enabled),
                "revision": workspaces[channel].revision if channel in workspaces else 0,
                "draftDirty": workspaces[channel].is_dirty if channel in workspaces else False,
                "publishedVersion": (
                    active_releases[workspaces[channel].current_release_id].version
                    if channel in workspaces
                    and workspaces[channel].current_release_id in active_releases
                    else None
                ),
            }
            for channel in PUBLIC_CHANNEL_ORDER
        ]
    )


@router.patch("/channels/{channel}")
async def update_channel(
    channel: str,
    body: ChannelPatch,
    db: DBSession,
    _current_user: dict = Depends(require_role("admin")),
) -> dict:
    if channel not in SUPPORTED_PUBLIC_CHANNELS:
        raise ContentValidationError("content channel is not supported")
    async with db.begin_nested():
        workspace = await db.scalar(
            select(MiniappContentWorkspace)
            .where(MiniappContentWorkspace.channel == channel)
            .with_for_update()
        )
        config = await db.scalar(
            select(MiniappChannelConfig)
            .where(MiniappChannelConfig.channel == channel)
            .with_for_update()
        )
        if body.enabled:
            if workspace is None or workspace.current_release_id is None:
                raise ContentValidationError(
                    "content channel has no active published release"
                )
            release = await db.scalar(
                select(MiniappContentRelease)
                .where(
                    MiniappContentRelease.release_id
                    == workspace.current_release_id
                )
                .with_for_update()
            )
            if (
                release is None
                or release.channel != channel
                or release.status != "published"
            ):
                raise ContentValidationError(
                    "content channel has no active published release"
                )
        if config is None:
            config = MiniappChannelConfig(channel=channel, enabled=body.enabled)
            db.add(config)
        else:
            config.enabled = body.enabled
            config.auto_creation_token = None
    await db.commit()
    return {"channel": config.channel, "enabled": config.enabled}


@router.get("/public/service-home")
async def public_service_home(db: DBSession) -> dict:
    channels: list[dict[str, str]] = []
    for channel in PUBLIC_CHANNEL_ORDER:
        config = await db.get(MiniappChannelConfig, channel)
        if config is None or not config.enabled:
            continue
        workspace = await db.get(MiniappContentWorkspace, channel)
        release = (
            await db.get(MiniappContentRelease, workspace.current_release_id)
            if workspace is not None and workspace.current_release_id is not None
            else None
        )
        if (
            release is not None
            and release.channel == channel
            and release.status == "published"
        ):
            channels.append({"channel": channel, "version": release.version})
    return {"channels": channels}


@router.get("/public/capabilities")
async def public_capabilities() -> dict:
    """Static compatibility proof with no deployment state or secret material."""

    return {
        "protocolVersion": PUBLIC_CAPABILITY_PROTOCOL,
        "channels": list(PUBLIC_CHANNEL_ORDER),
    }
