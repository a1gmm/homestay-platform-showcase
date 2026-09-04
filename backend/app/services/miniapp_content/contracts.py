"""Validation for generated public mini-program content contracts."""

from collections.abc import Iterable
from typing import Any

from jsonschema import Draft7Validator

from app.schemas.miniapp_content_owner import (
    OWNER_MANIFEST_SCHEMA,
    evaluate_owner_manifest_rules,
    format_owner_manifest_rule_issue,
)


_OWNER_VALIDATOR = Draft7Validator(OWNER_MANIFEST_SCHEMA)


class OwnerManifestValidationError(ValueError):
    """Backward-compatible validation error with stable machine-readable issues."""

    def __init__(self, errors: list[str], issues: list[dict[str, str]]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors
        self.issues = issues


def _escape_pointer_token(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _append_pointer(path: str, value: object) -> str:
    token = _escape_pointer_token(value)
    return f"/{token}" if path == "/" else f"{path}/{token}"


def _error_path(error: Any) -> str:
    parts = [_escape_pointer_token(part) for part in error.absolute_path]
    return "/" + "/".join(parts) if parts else "/"


def _schema_issues(errors: Iterable[Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for error in errors:
        path = _error_path(error)
        if error.validator == "required" and isinstance(error.instance, dict):
            for property_name in error.validator_value:
                if property_name not in error.instance:
                    issues.append(
                        {
                            "code": "required",
                            "path": _append_pointer(path, property_name),
                        }
                    )
        elif error.validator == "additionalProperties" and isinstance(
            error.instance, dict
        ):
            allowed = set(error.schema.get("properties", {}))
            for property_name in sorted(set(error.instance) - allowed):
                issues.append(
                    {
                        "code": "additionalProperties",
                        "path": _append_pointer(path, property_name),
                    }
                )
        else:
            issues.append({"code": str(error.validator), "path": path})
    unique = {(issue["code"], issue["path"]): issue for issue in issues}
    return [unique[key] for key in sorted(unique, key=lambda item: (item[1], item[0]))]


def validate_owner_manifest(payload: dict) -> dict:
    """Return a valid owner manifest, or raise a compatible structured error."""

    issues = _schema_issues(_OWNER_VALIDATOR.iter_errors(payload))
    if issues:
        errors = [f"{issue['path']} {issue['code']}" for issue in issues]
        raise OwnerManifestValidationError(errors, issues)

    issues = evaluate_owner_manifest_rules(payload)
    if issues:
        errors = [format_owner_manifest_rule_issue(issue) for issue in issues]
        raise OwnerManifestValidationError(errors, issues)
    return payload
