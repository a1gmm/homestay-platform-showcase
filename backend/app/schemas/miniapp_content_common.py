# generated, do not edit; source schema sha256: b5314382ef7305d910e7d3d2759ea9aba564792358461267650e9a25708f56d8

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MediaRef(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    url: str = Field(..., pattern="^https://media\\.example\\.invalid/miniapp/media/[0-9a-f]{64}\\.(?:jpg|jpeg|png|webp)$")
    alt: str = Field(..., min_length=1, pattern=".*\\S.*")
    width: int = Field(..., ge=1)
    height: int = Field(..., ge=1)
    mimeType: Literal["image/jpeg", "image/png", "image/webp"] = Field(...)
    sha256: str = Field(..., pattern="^[0-9a-f]{64}$")

class ThumbnailImageDerivativeRef(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    url: str = Field(..., pattern="^https://media\\.example\\.invalid/miniapp/media/image-sets/MDS-[0-9a-f]{20}/thumbnail/[0-9a-f]{64}\\.(?:jpg|jpeg|png|webp)$")
    derivativeSetId: str = Field(..., pattern="^MDS-[0-9a-f]{20}$")
    role: Literal["thumbnail"] = Field(...)
    width: int = Field(..., ge=1)
    height: int = Field(..., ge=1)
    bytes: int = Field(..., ge=1)
    mimeType: Literal["image/jpeg", "image/png", "image/webp"] = Field(...)
    sha256: str = Field(..., pattern="^[0-9a-f]{64}$")

class DisplayImageDerivativeRef(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    url: str = Field(..., pattern="^https://media\\.example\\.invalid/miniapp/media/image-sets/MDS-[0-9a-f]{20}/display/[0-9a-f]{64}\\.(?:jpg|jpeg|png|webp)$")
    derivativeSetId: str = Field(..., pattern="^MDS-[0-9a-f]{20}$")
    role: Literal["display"] = Field(...)
    width: int = Field(..., ge=1)
    height: int = Field(..., ge=1)
    bytes: int = Field(..., ge=1)
    mimeType: Literal["image/jpeg", "image/png", "image/webp"] = Field(...)
    sha256: str = Field(..., pattern="^[0-9a-f]{64}$")

class ImageRef(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    alt: str = Field(..., min_length=1, pattern=".*\\S.*")
    thumbnail: ThumbnailImageDerivativeRef = Field(...)
    display: DisplayImageDerivativeRef = Field(...)

class VideoRef(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    url: str = Field(..., pattern="^https://media\\.example\\.invalid/miniapp/media/[0-9a-f]{64}\\.mp4$")
    poster: MediaRef = Field(...)
    alt: str = Field(..., min_length=1, pattern=".*\\S.*")
    width: int = Field(..., ge=1)
    height: int = Field(..., ge=1)
    mimeType: Literal["video/mp4"] = Field(...)
    sha256: str = Field(..., pattern="^[0-9a-f]{64}$")
    mutedByDefault: Literal[True] = Field(...)
