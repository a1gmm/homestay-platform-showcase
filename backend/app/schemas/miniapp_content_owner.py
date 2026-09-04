# generated, do not edit; source schema sha256: b5314382ef7305d910e7d3d2759ea9aba564792358461267650e9a25708f56d8

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.miniapp_content_common import ImageRef, MediaRef, VideoRef


OWNER_MANIFEST_SCHEMA: dict[str, Any] = json.loads(r'''{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$id": "https://contracts.example.invalid/miniapp-content/owner.v1.schema.json",
  "title": "OwnerManifest",
  "description": "Public owner-channel manifest for the Guanhaiju mini program.",
  "type": "object",
  "additionalProperties": false,
  "x-owner-rules": {
    "version": 1,
    "mediaAnnotation": "x-owner-media",
    "contentAddress": {
      "urlProperty": "url",
      "digestProperty": "sha256",
      "digestSource": "filenameStem",
      "code": "media.digest_mismatch",
      "message": "media URL digest must match sha256"
    },
    "derivativeIdentity": {
      "kind": "logicalImage",
      "setProperty": "derivativeSetId",
      "roleProperty": "role",
      "urlProperty": "url",
      "urlPrefix": "https://media.example.invalid/miniapp/media/image-sets/",
      "members": [
        {
          "property": "thumbnail",
          "role": "thumbnail"
        },
        {
          "property": "display",
          "role": "display"
        }
      ],
      "pairSetCode": "image_pair.derivative_set_mismatch",
      "pairSetMessage": "paired image derivatives must share derivativeSetId",
      "pathIdentityCode": "image_derivative.identity_mismatch",
      "pathIdentityMessage": "derivative URL set and role must match declared identity"
    },
    "mediaCounts": [
      {
        "kind": "logicalImage",
        "max": 20,
        "code": "manifest.max_images",
        "path": "/",
        "message": "must contain no more than {max} images"
      }
    ]
  },
  "required": [
    "schema",
    "version",
    "publishedAt",
    "hero",
    "cases"
  ],
  "properties": {
    "schema": {
      "const": "guanhaiju.owner.v1"
    },
    "version": {
      "type": "string",
      "minLength": 1,
      "pattern": ".*\\S.*"
    },
    "publishedAt": {
      "type": "string",
      "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?Z$"
    },
    "hero": {
      "$ref": "#/definitions/MediaRef"
    },
    "cases": {
      "type": "array",
      "minItems": 4,
      "items": {
        "$ref": "#/definitions/OwnerCaseContent"
      }
    },
    "video": {
      "$ref": "#/definitions/VideoRef"
    }
  },
  "definitions": {
    "MediaRef": {
      "type": "object",
      "additionalProperties": false,
      "x-owner-media": {
        "kind": "image"
      },
      "required": [
        "url",
        "alt",
        "width",
        "height",
        "mimeType",
        "sha256"
      ],
      "properties": {
        "url": {
          "type": "string",
          "pattern": "^https://media\\.example\\.invalid/miniapp/media/[0-9a-f]{64}\\.(?:jpg|jpeg|png|webp)$"
        },
        "alt": {
          "type": "string",
          "minLength": 1,
          "pattern": ".*\\S.*"
        },
        "width": {
          "type": "integer",
          "minimum": 1
        },
        "height": {
          "type": "integer",
          "minimum": 1
        },
        "mimeType": {
          "enum": [
            "image/jpeg",
            "image/png",
            "image/webp"
          ]
        },
        "sha256": {
          "type": "string",
          "pattern": "^[0-9a-f]{64}$"
        }
      }
    },
    "ThumbnailImageDerivativeRef": {
      "type": "object",
      "additionalProperties": false,
      "x-owner-media": {
        "kind": "imageDerivative"
      },
      "required": [
        "url",
        "derivativeSetId",
        "role",
        "width",
        "height",
        "bytes",
        "mimeType",
        "sha256"
      ],
      "properties": {
        "url": {
          "type": "string",
          "pattern": "^https://media\\.example\\.invalid/miniapp/media/image-sets/MDS-[0-9a-f]{20}/thumbnail/[0-9a-f]{64}\\.(?:jpg|jpeg|png|webp)$"
        },
        "derivativeSetId": {
          "type": "string",
          "pattern": "^MDS-[0-9a-f]{20}$"
        },
        "role": {
          "const": "thumbnail"
        },
        "width": {
          "type": "integer",
          "minimum": 1
        },
        "height": {
          "type": "integer",
          "minimum": 1
        },
        "bytes": {
          "type": "integer",
          "minimum": 1
        },
        "mimeType": {
          "enum": [
            "image/jpeg",
            "image/png",
            "image/webp"
          ]
        },
        "sha256": {
          "type": "string",
          "pattern": "^[0-9a-f]{64}$"
        }
      }
    },
    "DisplayImageDerivativeRef": {
      "type": "object",
      "additionalProperties": false,
      "x-owner-media": {
        "kind": "imageDerivative"
      },
      "required": [
        "url",
        "derivativeSetId",
        "role",
        "width",
        "height",
        "bytes",
        "mimeType",
        "sha256"
      ],
      "properties": {
        "url": {
          "type": "string",
          "pattern": "^https://media\\.example\\.invalid/miniapp/media/image-sets/MDS-[0-9a-f]{20}/display/[0-9a-f]{64}\\.(?:jpg|jpeg|png|webp)$"
        },
        "derivativeSetId": {
          "type": "string",
          "pattern": "^MDS-[0-9a-f]{20}$"
        },
        "role": {
          "const": "display"
        },
        "width": {
          "type": "integer",
          "minimum": 1
        },
        "height": {
          "type": "integer",
          "minimum": 1
        },
        "bytes": {
          "type": "integer",
          "minimum": 1
        },
        "mimeType": {
          "enum": [
            "image/jpeg",
            "image/png",
            "image/webp"
          ]
        },
        "sha256": {
          "type": "string",
          "pattern": "^[0-9a-f]{64}$"
        }
      }
    },
    "ImageRef": {
      "type": "object",
      "additionalProperties": false,
      "x-owner-media": {
        "kind": "logicalImage",
        "contentAddressed": false
      },
      "required": [
        "alt",
        "thumbnail",
        "display"
      ],
      "properties": {
        "alt": {
          "type": "string",
          "minLength": 1,
          "pattern": ".*\\S.*"
        },
        "thumbnail": {
          "$ref": "#/definitions/ThumbnailImageDerivativeRef"
        },
        "display": {
          "$ref": "#/definitions/DisplayImageDerivativeRef"
        }
      }
    },
    "VideoRef": {
      "type": "object",
      "additionalProperties": false,
      "x-owner-media": {
        "kind": "video"
      },
      "required": [
        "url",
        "poster",
        "alt",
        "width",
        "height",
        "mimeType",
        "sha256",
        "mutedByDefault"
      ],
      "properties": {
        "url": {
          "type": "string",
          "pattern": "^https://media\\.example\\.invalid/miniapp/media/[0-9a-f]{64}\\.mp4$"
        },
        "poster": {
          "$ref": "#/definitions/MediaRef"
        },
        "alt": {
          "type": "string",
          "minLength": 1,
          "pattern": ".*\\S.*"
        },
        "width": {
          "type": "integer",
          "minimum": 1
        },
        "height": {
          "type": "integer",
          "minimum": 1
        },
        "mimeType": {
          "const": "video/mp4"
        },
        "sha256": {
          "type": "string",
          "pattern": "^[0-9a-f]{64}$"
        },
        "mutedByDefault": {
          "const": true
        }
      }
    },
    "OwnerCaseContent": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "id",
        "title",
        "summary",
        "hero",
        "images"
      ],
      "properties": {
        "id": {
          "type": "string",
          "minLength": 1,
          "pattern": "^[a-z0-9][a-z0-9-]*$"
        },
        "title": {
          "type": "string",
          "minLength": 1,
          "maxLength": 80,
          "pattern": ".*\\S.*"
        },
        "summary": {
          "type": "string",
          "minLength": 1,
          "maxLength": 240,
          "pattern": ".*\\S.*"
        },
        "hero": {
          "$ref": "#/definitions/MediaRef"
        },
        "images": {
          "type": "array",
          "minItems": 1,
          "maxItems": 20,
          "items": {
            "$ref": "#/definitions/ImageRef"
          }
        }
      }
    }
  }
}''')


def _escape_owner_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _append_owner_pointer(path: str, value: str) -> str:
    token = _escape_owner_pointer_token(value)
    return f"/{token}" if path == "/" else f"{path}/{token}"


def _resolve_owner_schema_node(
    schema_root: dict[str, Any], schema_node: dict[str, Any]
) -> dict[str, Any]:
    reference = schema_node.get("$ref")
    if not reference:
        return schema_node
    if not reference.startswith("#/"):
        raise ValueError("owner rules support only local schema references")
    current: Any = schema_root
    for part in reference[2:].split("/"):
        current = current[part.replace("~1", "/").replace("~0", "~")]
    return current


def _collect_declared_owner_media(
    value: Any,
    schema_node: dict[str, Any],
    schema_root: dict[str, Any],
    path: str,
    annotation_name: str,
    media: list[dict[str, Any]],
) -> None:
    resolved = _resolve_owner_schema_node(schema_root, schema_node)
    annotation = resolved.get(annotation_name)
    if (
        isinstance(annotation, dict)
        and isinstance(annotation.get("kind"), str)
        and isinstance(value, dict)
    ):
        media.append(
            {
                "contentAddressed": annotation.get("contentAddressed", True),
                "kind": annotation["kind"],
                "path": path,
                "value": value,
            }
        )
    if isinstance(value, list) and resolved.get("items"):
        for index, item in enumerate(value):
            _collect_declared_owner_media(
                item,
                resolved["items"],
                schema_root,
                _append_owner_pointer(path, str(index)),
                annotation_name,
                media,
            )
        return
    if not isinstance(value, dict):
        return
    for property_name, property_schema in resolved.get("properties", {}).items():
        if property_name in value:
            _collect_declared_owner_media(
                value[property_name],
                property_schema,
                schema_root,
                _append_owner_pointer(path, property_name),
                annotation_name,
                media,
            )


def _unique_sorted_owner_issues(
    issues: list[dict[str, str]],
) -> list[dict[str, str]]:
    unique = {(issue["code"], issue["path"]): issue for issue in issues}
    return [unique[key] for key in sorted(unique, key=lambda item: (item[1], item[0]))]


def evaluate_owner_manifest_rules(
    payload: Any, schema_root: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    schema_root = OWNER_MANIFEST_SCHEMA if schema_root is None else schema_root
    rules = schema_root.get("x-owner-rules")
    if not isinstance(rules, dict) or rules.get("version") != 1:
        return []
    if not isinstance(payload, dict):
        return []
    media: list[dict[str, Any]] = []
    _collect_declared_owner_media(
        payload,
        schema_root,
        schema_root,
        "/",
        rules["mediaAnnotation"],
        media,
    )
    issues: list[dict[str, str]] = []
    address_rule = rules["contentAddress"]
    for entry in media:
        if not entry["contentAddressed"]:
            continue
        url = entry["value"].get(address_rule["urlProperty"])
        digest = entry["value"].get(address_rule["digestProperty"])
        if not isinstance(url, str) or not isinstance(digest, str):
            continue
        filename = url.rsplit("/", 1)[-1]
        filename_digest, separator, _extension = filename.rpartition(".")
        if not separator or filename_digest != digest:
            issues.append(
                {
                    "code": address_rule["code"],
                    "path": _append_owner_pointer(
                        entry["path"], address_rule["urlProperty"]
                    ),
                }
            )
    identity_rule = rules["derivativeIdentity"]
    for entry in media:
        if entry["kind"] != identity_rule["kind"]:
            continue
        pair_set: str | None = None
        for member_rule in identity_rule["members"]:
            member = entry["value"].get(member_rule["property"])
            if not isinstance(member, dict):
                continue
            declared_set = member.get(identity_rule["setProperty"])
            declared_role = member.get(identity_rule["roleProperty"])
            url = member.get(identity_rule["urlProperty"])
            member_path = _append_owner_pointer(
                entry["path"], member_rule["property"]
            )
            if isinstance(declared_set, str):
                if pair_set is None:
                    pair_set = declared_set
                elif declared_set != pair_set:
                    issues.append(
                        {
                            "code": identity_rule["pairSetCode"],
                            "path": _append_owner_pointer(
                                member_path, identity_rule["setProperty"]
                            ),
                        }
                    )
            if (
                isinstance(declared_set, str)
                and isinstance(declared_role, str)
                and isinstance(url, str)
            ):
                prefix = identity_rule["urlPrefix"]
                relative = url[len(prefix):].split("/") if url.startswith(prefix) else []
                if (
                    len(relative) < 2
                    or relative[0] != declared_set
                    or relative[1] != declared_role
                    or declared_role != member_rule["role"]
                ):
                    issues.append(
                        {
                            "code": identity_rule["pathIdentityCode"],
                            "path": _append_owner_pointer(
                                member_path, identity_rule["urlProperty"]
                            ),
                        }
                    )
    for count_rule in rules["mediaCounts"]:
        count = sum(1 for entry in media if entry["kind"] == count_rule["kind"])
        if count > count_rule["max"]:
            issues.append({"code": count_rule["code"], "path": count_rule["path"]})
    return _unique_sorted_owner_issues(issues)


def format_owner_manifest_rule_issue(
    issue: dict[str, str], schema_root: dict[str, Any] | None = None
) -> str:
    schema_root = OWNER_MANIFEST_SCHEMA if schema_root is None else schema_root
    rules = schema_root["x-owner-rules"]
    address_rule = rules["contentAddress"]
    if issue["code"] == address_rule["code"]:
        return f"{issue['path']} {address_rule['message']}"
    identity_rule = rules["derivativeIdentity"]
    if issue["code"] == identity_rule["pairSetCode"]:
        return f"{issue['path']} {identity_rule['pairSetMessage']}"
    if issue["code"] == identity_rule["pathIdentityCode"]:
        return f"{issue['path']} {identity_rule['pathIdentityMessage']}"
    for count_rule in rules["mediaCounts"]:
        if issue["code"] == count_rule["code"]:
            message = count_rule["message"].replace("{max}", str(count_rule["max"]))
            return f"{issue['path']} {message}"
    return f"{issue['path']} {issue['code']}"



class OwnerCaseContent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(..., min_length=1, pattern="^[a-z0-9][a-z0-9-]*$")
    title: str = Field(..., min_length=1, max_length=80, pattern=".*\\S.*")
    summary: str = Field(..., min_length=1, max_length=240, pattern=".*\\S.*")
    hero: MediaRef = Field(...)
    images: list[ImageRef] = Field(..., min_length=1, max_length=20)

class OwnerManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["guanhaiju.owner.v1"] = Field(..., alias="schema")
    version: str = Field(..., min_length=1, pattern=".*\\S.*")
    publishedAt: str = Field(..., pattern="^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?Z$")
    hero: MediaRef = Field(...)
    cases: list[OwnerCaseContent] = Field(..., min_length=4)
    video: VideoRef | None = None

class OwnerCaseDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str | None = Field(None, min_length=1, pattern="^[a-z0-9][a-z0-9-]*$")
    title: str | None = Field(None, min_length=1, max_length=80, pattern=".*\\S.*")
    summary: str | None = Field(None, min_length=1, max_length=240, pattern=".*\\S.*")
    hero: MediaRef | None = Field(None)
    images: list[ImageRef] = Field(default_factory=list, max_length=20)

class OwnerManifestDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["guanhaiju.owner.v1"] | None = Field(None, alias="schema")
    version: str | None = Field(None, min_length=1, pattern=".*\\S.*")
    publishedAt: str | None = Field(None, pattern="^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?Z$")
    hero: MediaRef | None = Field(None)
    cases: list[OwnerCaseDraft] = Field(default_factory=list)
    video: VideoRef | None = Field(None)
