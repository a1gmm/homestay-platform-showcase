# generated, do not edit; source schema sha256: aae2d9061da8b1710a70763771bb288d208cdad571bb768409b4078a7ee7d6eb
# generated, do not edit; source schema sha256: 5adf0756a555cfd77afc4bf9369fdf87d82084970bdeff1964b1c1b88cdf525d

import json
from typing import Annotated, Any, ClassVar, Literal

from jsonschema import Draft7Validator
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.miniapp_content_common import MediaRef


STAY_GUIDE_MANIFEST_SCHEMA: dict[str, Any] = json.loads(r'''{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$id": "https://contracts.example.invalid/miniapp-content/stay-guide.v1.schema.json",
  "title": "StayGuideManifest",
  "description": "Public stay-guide manifest for the Guanhaiju mini program.",
  "type": "object",
  "additionalProperties": false,
  "x-guide-rules": {
    "version": 1,
    "mediaAnnotation": "x-guide-media",
    "contentAddress": {
      "urlProperty": "url",
      "digestProperty": "sha256",
      "digestSource": "filenameStem",
      "code": "media.digest_mismatch",
      "message": "media URL digest must match sha256"
    },
    "uniqueItems": []
  },
  "required": ["schema", "version", "publishedAt", "title", "intro", "sections"],
  "properties": {
    "schema": { "const": "guanhaiju.stay_guide.v1" },
    "version": { "$ref": "#/definitions/Version" },
    "publishedAt": { "$ref": "#/definitions/PublishedAt" },
    "title": { "$ref": "#/definitions/Title" },
    "intro": { "$ref": "#/definitions/Intro" },
    "sections": { "$ref": "#/definitions/StayGuideSectionsManifest" }
  },
  "definitions": {
    "Version": { "type": "string", "minLength": 1, "pattern": ".*\\S.*" },
    "PublishedAt": { "type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?Z$" },
    "Title": { "type": "string", "minLength": 1, "maxLength": 80, "pattern": ".*\\S.*" },
    "Intro": { "type": "string", "minLength": 1, "maxLength": 240, "pattern": ".*\\S.*" },
    "Summary": { "type": "string", "minLength": 1, "maxLength": 240, "pattern": ".*\\S.*" },
    "Details": { "type": "string", "minLength": 1, "maxLength": 2000, "pattern": ".*\\S.*" },
    "DraftMediaRef": {
      "type": "object",
      "additionalProperties": false,
      "required": ["mediaId"],
      "properties": { "mediaId": { "type": "string", "minLength": 1, "maxLength": 24, "pattern": ".*\\S.*" } }
    },
    "StayGuideMediaRef": {
      "type": "object",
      "additionalProperties": false,
      "x-guide-media": { "kind": "publicImage" },
      "required": ["url", "alt", "width", "height", "mimeType", "sha256"],
      "properties": {
        "url": { "type": "string", "pattern": "^https://media\\.example\\.invalid/miniapp/media/[0-9a-f]{64}\\.(?:jpg|jpeg|png|webp)$" },
        "alt": { "type": "string", "minLength": 1, "pattern": ".*\\S.*" },
        "width": { "type": "integer", "minimum": 1 },
        "height": { "type": "integer", "minimum": 1 },
        "mimeType": { "enum": ["image/jpeg", "image/png", "image/webp"] },
        "sha256": { "type": "string", "pattern": "^[0-9a-f]{64}$" }
      }
    },
    "StayGuideSectionDraft": {
      "type": "object",
      "additionalProperties": false,
      "required": ["summary", "visible"],
      "properties": {
        "summary": { "$ref": "#/definitions/Summary" },
        "details": { "$ref": "#/definitions/Details" },
        "image": { "$ref": "#/definitions/DraftMediaRef" },
        "visible": { "type": "boolean" }
      }
    },
    "StayGuideSectionManifest": {
      "type": "object",
      "additionalProperties": false,
      "required": ["summary", "visible"],
      "properties": {
        "summary": { "$ref": "#/definitions/Summary" },
        "details": { "$ref": "#/definitions/Details" },
        "image": { "$ref": "#/definitions/StayGuideMediaRef" },
        "visible": { "type": "boolean" }
      }
    },
    "FaqQuestion": { "type": "string", "minLength": 1, "maxLength": 120, "pattern": ".*\\S.*" },
    "FaqAnswer": { "type": "string", "minLength": 1, "maxLength": 1000, "pattern": ".*\\S.*" },
    "StayGuideFaqItem": {
      "type": "object",
      "additionalProperties": false,
      "required": ["question", "answer"],
      "properties": {
        "question": { "$ref": "#/definitions/FaqQuestion" },
        "answer": { "$ref": "#/definitions/FaqAnswer" }
      }
    },
    "StayGuideFaqSectionDraft": {
      "type": "object",
      "additionalProperties": false,
      "required": ["summary", "visible", "items"],
      "properties": {
        "summary": { "$ref": "#/definitions/Summary" },
        "details": { "$ref": "#/definitions/Details" },
        "image": { "$ref": "#/definitions/DraftMediaRef" },
        "visible": { "type": "boolean" },
        "items": { "type": "array", "minItems": 1, "maxItems": 20, "items": { "$ref": "#/definitions/StayGuideFaqItem" } }
      }
    },
    "StayGuideFaqSectionManifest": {
      "type": "object",
      "additionalProperties": false,
      "required": ["summary", "visible", "items"],
      "properties": {
        "summary": { "$ref": "#/definitions/Summary" },
        "details": { "$ref": "#/definitions/Details" },
        "image": { "$ref": "#/definitions/StayGuideMediaRef" },
        "visible": { "type": "boolean" },
        "items": { "type": "array", "minItems": 1, "maxItems": 20, "items": { "$ref": "#/definitions/StayGuideFaqItem" } }
      }
    },
    "StayGuideSectionsDraft": {
      "type": "object",
      "additionalProperties": false,
      "required": ["arrivalDeparture", "parking", "wifiAndDevices", "houseRules", "checkOut", "support", "faq"],
      "properties": {
        "arrivalDeparture": { "$ref": "#/definitions/StayGuideSectionDraft" },
        "parking": { "$ref": "#/definitions/StayGuideSectionDraft" },
        "wifiAndDevices": { "$ref": "#/definitions/StayGuideSectionDraft" },
        "houseRules": { "$ref": "#/definitions/StayGuideSectionDraft" },
        "checkOut": { "$ref": "#/definitions/StayGuideSectionDraft" },
        "support": { "$ref": "#/definitions/StayGuideSectionDraft" },
        "faq": { "$ref": "#/definitions/StayGuideFaqSectionDraft" }
      }
    },
    "StayGuideSectionsManifest": {
      "type": "object",
      "additionalProperties": false,
      "required": ["arrivalDeparture", "parking", "wifiAndDevices", "houseRules", "checkOut", "support", "faq"],
      "properties": {
        "arrivalDeparture": { "$ref": "#/definitions/StayGuideSectionManifest" },
        "parking": { "$ref": "#/definitions/StayGuideSectionManifest" },
        "wifiAndDevices": { "$ref": "#/definitions/StayGuideSectionManifest" },
        "houseRules": { "$ref": "#/definitions/StayGuideSectionManifest" },
        "checkOut": { "$ref": "#/definitions/StayGuideSectionManifest" },
        "support": { "$ref": "#/definitions/StayGuideSectionManifest" },
        "faq": { "$ref": "#/definitions/StayGuideFaqSectionManifest" }
      }
    },
    "StayGuideDraft": {
      "type": "object",
      "additionalProperties": false,
      "required": ["title", "intro", "sections"],
      "properties": {
        "title": { "$ref": "#/definitions/Title" },
        "intro": { "$ref": "#/definitions/Intro" },
        "sections": { "$ref": "#/definitions/StayGuideSectionsDraft" }
      }
    }
  }
}
''')
TRAVEL_GUIDE_MANIFEST_SCHEMA: dict[str, Any] = json.loads(r'''{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$id": "https://contracts.example.invalid/miniapp-content/travel.v1.schema.json",
  "title": "TravelGuideManifest",
  "description": "Public travel-guide manifest for the Guanhaiju mini program.",
  "type": "object",
  "additionalProperties": false,
  "x-guide-rules": {
    "version": 1,
    "mediaAnnotation": "x-guide-media",
    "contentAddress": {
      "urlProperty": "url",
      "digestProperty": "sha256",
      "digestSource": "filenameStem",
      "code": "media.digest_mismatch",
      "message": "media URL digest must match sha256"
    },
    "uniqueItems": [
      {
        "path": "/recommendations",
        "property": "id",
        "code": "recommendation.id_duplicate",
        "message": "recommendation IDs must be unique"
      }
    ]
  },
  "required": ["schema", "version", "publishedAt", "title", "intro", "recommendations"],
  "properties": {
    "schema": { "const": "guanhaiju.travel.v1" },
    "version": { "$ref": "#/definitions/Version" },
    "publishedAt": { "$ref": "#/definitions/PublishedAt" },
    "title": { "$ref": "#/definitions/Title" },
    "intro": { "$ref": "#/definitions/Intro" },
    "recommendations": { "type": "array", "minItems": 1, "maxItems": 50, "items": { "$ref": "#/definitions/TravelRecommendationManifest" } }
  },
  "definitions": {
    "Version": { "type": "string", "minLength": 1, "pattern": ".*\\S.*" },
    "PublishedAt": { "type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?Z$" },
    "Title": { "type": "string", "minLength": 1, "maxLength": 80, "pattern": ".*\\S.*" },
    "Intro": { "type": "string", "minLength": 1, "maxLength": 240, "pattern": ".*\\S.*" },
    "DraftMediaRef": {
      "type": "object",
      "additionalProperties": false,
      "required": ["mediaId"],
      "properties": { "mediaId": { "type": "string", "minLength": 1, "maxLength": 24, "pattern": ".*\\S.*" } }
    },
    "TravelMediaRef": {
      "type": "object",
      "additionalProperties": false,
      "x-guide-media": { "kind": "publicImage" },
      "required": ["url", "alt", "width", "height", "mimeType", "sha256"],
      "properties": {
        "url": { "type": "string", "pattern": "^https://media\\.example\\.invalid/miniapp/media/[0-9a-f]{64}\\.(?:jpg|jpeg|png|webp)$" },
        "alt": { "type": "string", "minLength": 1, "pattern": ".*\\S.*" },
        "width": { "type": "integer", "minimum": 1 },
        "height": { "type": "integer", "minimum": 1 },
        "mimeType": { "enum": ["image/jpeg", "image/png", "image/webp"] },
        "sha256": { "type": "string", "pattern": "^[0-9a-f]{64}$" }
      }
    },
    "RecommendationId": { "type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[a-z0-9][a-z0-9-]*$" },
    "RecommendationName": { "type": "string", "minLength": 1, "maxLength": 80, "pattern": ".*\\S.*" },
    "Reason": { "type": "string", "minLength": 1, "maxLength": 240, "pattern": ".*\\S.*" },
    "ApproximateLocation": { "type": "string", "minLength": 1, "maxLength": 120, "pattern": ".*\\S.*" },
    "SuggestedDuration": { "type": "string", "minLength": 1, "maxLength": 80, "pattern": ".*\\S.*" },
    "Audience": { "type": "string", "minLength": 1, "maxLength": 40, "pattern": ".*\\S.*" },
    "TravelCategory": { "enum": ["must_see", "dining", "family", "transport_parking", "shopping", "rainy_day"] },
    "TravelRecommendationDraft": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "name", "category", "reason", "audiences", "visible"],
      "properties": {
        "id": { "$ref": "#/definitions/RecommendationId" },
        "name": { "$ref": "#/definitions/RecommendationName" },
        "category": { "$ref": "#/definitions/TravelCategory" },
        "reason": { "$ref": "#/definitions/Reason" },
        "approximateLocation": { "$ref": "#/definitions/ApproximateLocation" },
        "suggestedDuration": { "$ref": "#/definitions/SuggestedDuration" },
        "audiences": { "type": "array", "minItems": 1, "maxItems": 6, "items": { "$ref": "#/definitions/Audience" } },
        "image": { "$ref": "#/definitions/DraftMediaRef" },
        "visible": { "type": "boolean" }
      }
    },
    "TravelRecommendationManifest": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "name", "category", "reason", "audiences", "visible"],
      "properties": {
        "id": { "$ref": "#/definitions/RecommendationId" },
        "name": { "$ref": "#/definitions/RecommendationName" },
        "category": { "$ref": "#/definitions/TravelCategory" },
        "reason": { "$ref": "#/definitions/Reason" },
        "approximateLocation": { "$ref": "#/definitions/ApproximateLocation" },
        "suggestedDuration": { "$ref": "#/definitions/SuggestedDuration" },
        "audiences": { "type": "array", "minItems": 1, "maxItems": 6, "items": { "$ref": "#/definitions/Audience" } },
        "image": { "$ref": "#/definitions/TravelMediaRef" },
        "visible": { "type": "boolean" }
      }
    },
    "TravelGuideDraft": {
      "type": "object",
      "additionalProperties": false,
      "required": ["title", "intro", "recommendations"],
      "properties": {
        "title": { "$ref": "#/definitions/Title" },
        "intro": { "$ref": "#/definitions/Intro" },
        "recommendations": { "type": "array", "minItems": 1, "maxItems": 50, "items": { "$ref": "#/definitions/TravelRecommendationDraft" } }
      }
    }
  }
}
''')


def _escape_guide_pointer_token(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _append_guide_pointer(path: str, value: object) -> str:
    token = _escape_guide_pointer_token(value)
    return f"/{token}" if path == "/" else f"{path}/{token}"


def _resolve_guide_schema_node(schema_root: dict[str, Any], schema_node: dict[str, Any]) -> dict[str, Any]:
    reference = schema_node.get("$ref")
    if not isinstance(reference, str):
        return schema_node
    if not reference.startswith("#/"):
        raise ValueError("guide rules support only local schema references")
    current: Any = schema_root
    for part in reference[2:].split("/"):
        current = current[part.replace("~1", "/").replace("~0", "~")]
    return current


def _collect_declared_guide_media(
    value: Any,
    schema_node: dict[str, Any],
    schema_root: dict[str, Any],
    path: str,
    annotation_name: str,
    media: list[dict[str, Any]],
) -> None:
    resolved = _resolve_guide_schema_node(schema_root, schema_node)
    if isinstance(value, dict) and isinstance(resolved.get(annotation_name), dict):
        media.append({"path": path, "value": value})
    if isinstance(value, list):
        items = resolved.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _collect_declared_guide_media(
                    item, items, schema_root, _append_guide_pointer(path, index), annotation_name, media
                )
        return
    if not isinstance(value, dict):
        return
    for property_name, property_schema in resolved.get("properties", {}).items():
        if property_name in value and isinstance(property_schema, dict):
            _collect_declared_guide_media(
                value[property_name], property_schema, schema_root,
                _append_guide_pointer(path, property_name), annotation_name, media,
            )


def _guide_pointer_value(value: Any, pointer: str) -> Any:
    if pointer == "/":
        return value
    current = value
    for token in pointer[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        current = current[int(key)] if isinstance(current, list) else current.get(key) if isinstance(current, dict) else None
    return current


def _guide_rule_issues(payload: Any, schema_root: dict[str, Any]) -> list[dict[str, str]]:
    rules = schema_root.get("x-guide-rules")
    if not isinstance(rules, dict):
        return []
    annotation_name = rules.get("mediaAnnotation")
    address_rule = rules.get("contentAddress")
    if not isinstance(annotation_name, str) or not isinstance(address_rule, dict):
        return []
    media: list[dict[str, Any]] = []
    _collect_declared_guide_media(payload, schema_root, schema_root, "/", annotation_name, media)
    issues: list[dict[str, str]] = []
    for entry in media:
        value = entry["value"]
        url = value.get(address_rule["urlProperty"])
        digest = value.get(address_rule["digestProperty"])
        if not isinstance(url, str) or not isinstance(digest, str):
            continue
        filename = url.rsplit("/", 1)[-1]
        stem, separator, _extension = filename.rpartition(".")
        if not separator or stem != digest:
            issues.append({
                "code": address_rule["code"],
                "path": _append_guide_pointer(entry["path"], address_rule["urlProperty"]),
            })
    for unique_rule in rules.get("uniqueItems", []):
        if not isinstance(unique_rule, dict):
            continue
        collection = _guide_pointer_value(payload, unique_rule.get("path", ""))
        if not isinstance(collection, list):
            continue
        seen: set[str] = set()
        for index, entry in enumerate(collection):
            value = entry.get(unique_rule.get("property")) if isinstance(entry, dict) else None
            if not isinstance(value, str):
                continue
            if value in seen:
                issues.append({
                    "code": unique_rule["code"],
                    "path": _append_guide_pointer(
                        _append_guide_pointer(unique_rule["path"], index), unique_rule["property"]
                    ),
                })
            seen.add(value)
    unique = {(issue["code"], issue["path"]): issue for issue in issues}
    return [unique[key] for key in sorted(unique, key=lambda item: (item[1], item[0]))]


def _guide_schema_issues(payload: Any, schema_root: dict[str, Any], definition: str | None) -> list[dict[str, str]]:
    validation_schema = schema_root if definition is None else {
        "$schema": schema_root["$schema"],
        "definitions": schema_root["definitions"],
        "$ref": f"#/definitions/{definition}",
    }
    issues = []
    for error in Draft7Validator(validation_schema).iter_errors(payload):
        path = "/" + "/".join(_escape_guide_pointer_token(part) for part in error.absolute_path)
        normalized_path = path if path != "" else "/"
        if error.validator == "required" and isinstance(error.instance, dict):
            required = error.validator_value if isinstance(error.validator_value, list) else []
            missing = [name for name in required if name not in error.instance]
            for name in missing:
                issues.append({"code": "required", "path": _append_guide_pointer(normalized_path, name)})
            if missing:
                continue
        if error.validator == "additionalProperties" and isinstance(error.instance, dict):
            properties = error.schema.get("properties", {}) if isinstance(error.schema, dict) else {}
            extra = [name for name in error.instance if name not in properties]
            for name in extra:
                issues.append({"code": "additionalProperties", "path": _append_guide_pointer(normalized_path, name)})
            if extra:
                continue
        issues.append({"code": str(error.validator), "path": normalized_path})
    return issues


def _validate_guide_contract(payload: Any, schema_root: dict[str, Any], definition: str | None) -> None:
    issues = _guide_schema_issues(payload, schema_root, definition)
    issues.extend(_guide_rule_issues(payload, schema_root))
    unique = {(issue["code"], issue["path"]): issue for issue in issues}
    if unique:
        ordered = [unique[key] for key in sorted(unique, key=lambda item: (item[1], item[0]))]
        raise ValueError("; ".join(f"{issue['path']} {issue['code']}" for issue in ordered))


class GuideContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    _contract_schema: ClassVar[dict[str, Any] | None] = None
    _contract_definition: ClassVar[str | None] = None

    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any):
        if cls._contract_schema is not None:
            _validate_guide_contract(obj, cls._contract_schema, cls._contract_definition)
        kwargs["strict"] = True
        return super().model_validate(obj, *args, **kwargs)

    def __init__(self, **data: Any) -> None:
        if self._contract_schema is not None:
            _validate_guide_contract(data, self._contract_schema, self._contract_definition)
        super().__init__(**data)

Version = Annotated[str, Field(min_length=1, pattern=".*\\S.*")]
PublishedAt = Annotated[str, Field(pattern="^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?Z$")]
Title = Annotated[str, Field(min_length=1, max_length=80, pattern=".*\\S.*")]
Intro = Annotated[str, Field(min_length=1, max_length=240, pattern=".*\\S.*")]
Summary = Annotated[str, Field(min_length=1, max_length=240, pattern=".*\\S.*")]
Details = Annotated[str, Field(min_length=1, max_length=2000, pattern=".*\\S.*")]
FaqQuestion = Annotated[str, Field(min_length=1, max_length=120, pattern=".*\\S.*")]
FaqAnswer = Annotated[str, Field(min_length=1, max_length=1000, pattern=".*\\S.*")]
RecommendationId = Annotated[str, Field(min_length=1, max_length=64, pattern="^[a-z0-9][a-z0-9-]*$")]
RecommendationName = Annotated[str, Field(min_length=1, max_length=80, pattern=".*\\S.*")]
Reason = Annotated[str, Field(min_length=1, max_length=240, pattern=".*\\S.*")]
ApproximateLocation = Annotated[str, Field(min_length=1, max_length=120, pattern=".*\\S.*")]
SuggestedDuration = Annotated[str, Field(min_length=1, max_length=80, pattern=".*\\S.*")]
Audience = Annotated[str, Field(min_length=1, max_length=40, pattern=".*\\S.*")]
TravelCategory = Literal["must_see", "dining", "family", "transport_parking", "shopping", "rainy_day"]

class DraftMediaRef(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    mediaId: str = Field(..., min_length=1, max_length=24, pattern=".*\\S.*")

class StayGuideSectionDraft(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    summary: Summary = Field(...)
    details: Details | None = Field(None)
    image: DraftMediaRef | None = Field(None)
    visible: bool = Field(...)

class StayGuideSectionManifest(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    summary: Summary = Field(...)
    details: Details | None = Field(None)
    image: MediaRef | None = Field(None)
    visible: bool = Field(...)

class StayGuideFaqItem(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    question: FaqQuestion = Field(...)
    answer: FaqAnswer = Field(...)

class StayGuideFaqSectionDraft(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    summary: Summary = Field(...)
    details: Details | None = Field(None)
    image: DraftMediaRef | None = Field(None)
    visible: bool = Field(...)
    items: list[StayGuideFaqItem] = Field(..., min_length=1, max_length=20)

class StayGuideFaqSectionManifest(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    summary: Summary = Field(...)
    details: Details | None = Field(None)
    image: MediaRef | None = Field(None)
    visible: bool = Field(...)
    items: list[StayGuideFaqItem] = Field(..., min_length=1, max_length=20)

class StayGuideSectionsDraft(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    arrivalDeparture: StayGuideSectionDraft = Field(...)
    parking: StayGuideSectionDraft = Field(...)
    wifiAndDevices: StayGuideSectionDraft = Field(...)
    houseRules: StayGuideSectionDraft = Field(...)
    checkOut: StayGuideSectionDraft = Field(...)
    support: StayGuideSectionDraft = Field(...)
    faq: StayGuideFaqSectionDraft = Field(...)

class StayGuideSectionsManifest(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    arrivalDeparture: StayGuideSectionManifest = Field(...)
    parking: StayGuideSectionManifest = Field(...)
    wifiAndDevices: StayGuideSectionManifest = Field(...)
    houseRules: StayGuideSectionManifest = Field(...)
    checkOut: StayGuideSectionManifest = Field(...)
    support: StayGuideSectionManifest = Field(...)
    faq: StayGuideFaqSectionManifest = Field(...)

class StayGuideDraft(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    _contract_schema: ClassVar[dict[str, Any]] = STAY_GUIDE_MANIFEST_SCHEMA
    _contract_definition: ClassVar[str | None] = "StayGuideDraft"

    title: Title = Field(...)
    intro: Intro = Field(...)
    sections: StayGuideSectionsDraft = Field(...)

class TravelRecommendationDraft(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    id: RecommendationId = Field(...)
    name: RecommendationName = Field(...)
    category: TravelCategory = Field(...)
    reason: Reason = Field(...)
    approximateLocation: ApproximateLocation | None = Field(None)
    suggestedDuration: SuggestedDuration | None = Field(None)
    audiences: list[Audience] = Field(..., min_length=1, max_length=6)
    image: DraftMediaRef | None = Field(None)
    visible: bool = Field(...)

class TravelRecommendationManifest(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    id: RecommendationId = Field(...)
    name: RecommendationName = Field(...)
    category: TravelCategory = Field(...)
    reason: Reason = Field(...)
    approximateLocation: ApproximateLocation | None = Field(None)
    suggestedDuration: SuggestedDuration | None = Field(None)
    audiences: list[Audience] = Field(..., min_length=1, max_length=6)
    image: MediaRef | None = Field(None)
    visible: bool = Field(...)

class TravelGuideDraft(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    _contract_schema: ClassVar[dict[str, Any]] = TRAVEL_GUIDE_MANIFEST_SCHEMA
    _contract_definition: ClassVar[str | None] = "TravelGuideDraft"

    title: Title = Field(...)
    intro: Intro = Field(...)
    recommendations: list[TravelRecommendationDraft] = Field(..., min_length=1, max_length=50)

class StayGuideManifest(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    _contract_schema: ClassVar[dict[str, Any]] = STAY_GUIDE_MANIFEST_SCHEMA
    _contract_definition: ClassVar[str | None] = None

    schema_: Literal["guanhaiju.stay_guide.v1"] = Field(..., alias="schema")
    version: Version = Field(...)
    publishedAt: PublishedAt = Field(...)
    title: Title = Field(...)
    intro: Intro = Field(...)
    sections: StayGuideSectionsManifest = Field(...)

class TravelGuideManifest(GuideContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, strict=True)

    _contract_schema: ClassVar[dict[str, Any]] = TRAVEL_GUIDE_MANIFEST_SCHEMA
    _contract_definition: ClassVar[str | None] = None

    schema_: Literal["guanhaiju.travel.v1"] = Field(..., alias="schema")
    version: Version = Field(...)
    publishedAt: PublishedAt = Field(...)
    title: Title = Field(...)
    intro: Intro = Field(...)
    recommendations: list[TravelRecommendationManifest] = Field(..., min_length=1, max_length=50)
