"""Bounded JSON helpers shared by connectors and AI tasks.

Two small, dependency-free utilities:

* ``jsonpath_get`` — a deliberately small JSONPath subset (``$``, ``.key``,
  ``[n]``, ``[*]``) used by ``transform.json`` and result extraction. No
  recursive descent, no filters, no evaluation: a path is data, never code.
* ``validate_schema`` — a JSON-Schema subset validator covering the keywords
  connectors and AI extraction actually use (type, required, properties,
  additionalProperties, items, enum, const, numeric/string bounds, pattern,
  min/maxItems). Unknown keywords are rejected so a schema cannot pretend to
  enforce something it does not.
"""
from __future__ import annotations

import re
from typing import Any

MISSING = object()

PATH_TOKEN_PATTERN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\['([^']+)'\]|\[\*\]|\[(\d+)\]")
PATH_PREFIX_PATTERN = re.compile(r"^\$")

SUPPORTED_SCHEMA_KEYWORDS = {
    "$schema",
    "title",
    "description",
    "default",
    "examples",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "format",
    "anyOf",
    "oneOf",
    "nullable",
}

TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}


class SchemaError(ValueError):
    """Raised when a JSON Schema itself is not supported or is malformed."""


class JsonPathError(ValueError):
    """Raised when a JSONPath expression is unsupported."""


def parse_path(path: str) -> list[tuple[str, Any]]:
    """Translate a supported JSONPath into a list of access steps."""
    if not isinstance(path, str) or not path:
        raise JsonPathError("JSONPath must be a non-empty string")
    text = path.strip()
    if not PATH_PREFIX_PATTERN.match(text):
        raise JsonPathError("JSONPath must start with '$'")
    text = text[1:]
    steps: list[tuple[str, Any]] = []
    position = 0
    while position < len(text):
        match = PATH_TOKEN_PATTERN.match(text, position)
        if match is None:
            raise JsonPathError(f"Unsupported JSONPath syntax at '{text[position:]}'")
        if match.group(1) is not None:
            steps.append(("key", match.group(1)))
        elif match.group(2) is not None:
            steps.append(("key", match.group(2)))
        elif match.group(3) is not None:
            steps.append(("index", int(match.group(3))))
        else:
            steps.append(("wildcard", None))
        position = match.end()
    return steps


def jsonpath_get(value: Any, path: str, *, default: Any = MISSING) -> Any:
    """Resolve a supported JSONPath. Raises ``JsonPathError`` when missing."""
    return _resolve_path(value, parse_path(path), 0, path, default)


def _resolve_path(current: Any, steps: list[tuple[str, Any]], index: int, path: str, default: Any) -> Any:
    if index >= len(steps):
        return current
    kind, argument = steps[index]
    if kind == "key":
        if not isinstance(current, dict) or argument not in current:
            return _path_missing(path, default)
        return _resolve_path(current[argument], steps, index + 1, path, default)
    if kind == "index":
        if not isinstance(current, list) or not -len(current) <= argument < len(current):
            return _path_missing(path, default)
        return _resolve_path(current[argument], steps, index + 1, path, default)
    if not isinstance(current, list):
        return _path_missing(path, default)
    return [_resolve_path(item, steps, index + 1, path, default) for item in current]


def _path_missing(path: str, default: Any) -> Any:
    if default is not MISSING:
        return default
    raise JsonPathError(f"Path '{path}' does not exist")


def _type_name(value: Any) -> str:
    return "null" if value is None else type(value).__name__


def validate_schema(schema: Any, *, where: str = "schema") -> None:
    """Validate the schema document itself. Raises ``SchemaError``."""
    if not isinstance(schema, dict):
        raise SchemaError(f"{where} must be an object")
    unknown = set(schema) - SUPPORTED_SCHEMA_KEYWORDS
    if unknown:
        raise SchemaError(f"{where} uses unsupported keyword(s): {', '.join(sorted(unknown))}")
    declared = schema.get("type")
    if declared is not None:
        names = [declared] if isinstance(declared, str) else declared
        if not isinstance(names, list) or any(name not in TYPE_CHECKS for name in names):
            raise SchemaError(f"{where}.type must be one of {', '.join(sorted(TYPE_CHECKS))}")
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise SchemaError(f"{where}.properties must be an object")
        for name, child in properties.items():
            validate_schema(child, where=f"{where}.properties.{name}")
    if "items" in schema and schema["items"] is not None:
        validate_schema(schema["items"], where=f"{where}.items")
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            variants = schema[keyword]
            if not isinstance(variants, list) or not variants:
                raise SchemaError(f"{where}.{keyword} must be a non-empty array")
            for index, child in enumerate(variants):
                validate_schema(child, where=f"{where}.{keyword}[{index}]")
    if "pattern" in schema:
        try:
            re.compile(str(schema["pattern"]))
        except re.error as exc:
            raise SchemaError(f"{where}.pattern is not a valid regular expression") from exc


def validate_instance(instance: Any, schema: dict[str, Any], *, path: str = "$") -> list[str]:
    """Return human-readable validation errors (empty list means valid)."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: schema is not an object"]

    if "type" in schema and schema["type"] is not None:
        names = [schema["type"]] if isinstance(schema["type"], str) else list(schema["type"])
        allowed = tuple(t for name in names for t in TYPE_CHECKS.get(name, ()))
        if allowed and not isinstance(instance, allowed):
            errors.append(f"{path}: expected {'|'.join(names)}, got {_type_name(instance)}")
            return errors
        if "integer" in names and isinstance(instance, bool):
            errors.append(f"{path}: expected integer, got boolean")
            return errors
        if "number" in names and "integer" not in names and isinstance(instance, bool):
            errors.append(f"{path}: expected number, got boolean")
            return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: value is not one of the allowed options")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: value does not match the required constant")
    if instance is None:
        if schema.get("nullable") is False:
            errors.append(f"{path}: null is not allowed")
        return errors

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: value is below the minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: value is above the maximum")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: value must be greater than exclusiveMinimum")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: value must be less than exclusiveMaximum")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: string is shorter than minLength")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: string is longer than maxLength")
        if "pattern" in schema and not re.search(str(schema["pattern"]), instance):
            errors.append(f"{path}: string does not match the pattern")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: array has fewer than minItems entries")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: array has more than maxItems entries")
        if schema.get("uniqueItems"):
            seen = [repr(item) for item in instance]
            if len(seen) != len(set(seen)):
                errors.append(f"{path}: array entries must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                errors.extend(validate_instance(item, item_schema, path=f"{path}[{index}]"))
    if isinstance(instance, dict):
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        for name in required:
            if name not in instance:
                errors.append(f"{path}: missing required property '{name}'")
        for name, value in instance.items():
            if name in properties:
                errors.extend(validate_instance(value, properties[name], path=f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property '{name}'")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(validate_instance(value, schema["additionalProperties"], path=f"{path}.{name}"))

    for keyword in ("anyOf", "oneOf"):
        variants = schema.get(keyword)
        if not isinstance(variants, list) or not variants:
            continue
        matched = [index for index, variant in enumerate(variants) if not validate_instance(instance, variant, path=path)]
        if keyword == "anyOf" and not matched:
            errors.append(f"{path}: value does not match any allowed shape")
        if keyword == "oneOf" and len(matched) != 1:
            errors.append(f"{path}: value must match exactly one allowed shape")
    return errors


__all__ = [
    "JsonPathError",
    "MISSING",
    "SchemaError",
    "jsonpath_get",
    "parse_path",
    "validate_instance",
    "validate_schema",
]
