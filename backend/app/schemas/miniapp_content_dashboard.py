from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MiniappContentDashboardChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Literal["owner", "stay_guide", "travel"]
    enabled: bool
    revision: int = Field(ge=0)
    draftDirty: bool
    publishedVersion: str | None


class MiniappContentDashboard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channels: list[MiniappContentDashboardChannel]
