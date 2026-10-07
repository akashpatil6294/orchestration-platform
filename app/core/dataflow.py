"""Safe resolution of workflow data references.

Templates are deliberately limited to JSON paths; they are never evaluated as
Python, Jinja, or another expression language.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REFERENCE_PATTERN = re.compile(
    r"^\s*(?P<reference>input(?:\.[A-Za-z0-9_-]+)*|"
    r"steps\.(?P<step_id>[A-Za-z0-9_.-]+)\.output(?:\.[A-Za-z0-9_-]+)*|"
    r"secrets\.[A-Za-z0-9_.-]+)\s*$"
)
_SECRET_REFERENCE_PATTERN = re.compile(r"^\s*secrets\.[A-Za-z0-9_.-]+\s*$")


def _is_secret_reference(expression: str) -> bool:
    """True when ``expression`` is a ``secrets.NAME`` template reference.

    Secret values are interpolated later, at dispatch (task claim) time, by
    ``workflow_service.resolve_secrets`` — never during dataflow validation or
    template resolution, which must leave the ``{{secrets.NAME}}`` token
    untouched so the plaintext never enters the dataflow layer.
    """
    return bool(_SECRET_REFERENCE_PATTERN.fullmatch(expression))
REFERENCE_TOKEN_PATTERN = re.compile(r"\{\{(.*?)\}\}")
FOREACH_REFERENCE_PATTERN = re.compile(r"\{\{(item(?:\.[A-Za-z0-9_-]+)*|index)\}\}")


class DataReferenceError(ValueError):
    """Raised when a workflow data reference is invalid or unavailable."""


def validate_templates(value: Any, *, step_id: str, dependencies: set[str], allow_foreach: bool = False) -> list[str]:
    """Return definition errors for template expressions in a JSON value."""
    errors: list[str] = []
    for text in _strings(value):
        if "{{" not in text and "}}" not in text:
            continue
        matches = list(REFERENCE_TOKEN_PATTERN.finditer(text))
        if not matches:
            errors.append(f"Step '{step_id}' contains an incomplete data reference")
            continue
        residue = REFERENCE_TOKEN_PATTERN.sub("", text)
        if "{{" in residue or "}}" in residue:
            errors.append(f"Step '{step_id}' contains an incomplete data reference")
        for match in matches:
            expression = match.group(1)
            if allow_foreach and re.fullmatch(r"(?:item(?:\.[A-Za-z0-9_-]+)*|index)", expression):
                continue
            parsed = REFERENCE_PATTERN.fullmatch(expression)
            if parsed is None:
                errors.append(f"Step '{step_id}' contains an unsupported data reference")
                continue
            reference = parsed.group("reference")
            if reference.startswith("steps."):
                source_id = parsed.group("step_id")
                if source_id not in dependencies:
                    errors.append(
                        f"Step '{step_id}' references step '{source_id}' without declaring it in depends_on"
                    )
    return errors


def resolve_templates(
    value: Any,
    *,
    workflow_input: Mapping[str, Any],
    step_outputs: Mapping[str, Any],
) -> Any:
    """Resolve supported references recursively, preserving standalone value types."""
    if isinstance(value, dict):
        return {
            key: resolve_templates(item, workflow_input=workflow_input, step_outputs=step_outputs)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            resolve_templates(item, workflow_input=workflow_input, step_outputs=step_outputs)
            for item in value
        ]
    if not isinstance(value, str):
        return value
    if "{{" not in value and "}}" not in value:
        return value

    matches = list(REFERENCE_TOKEN_PATTERN.finditer(value))
    residue = REFERENCE_TOKEN_PATTERN.sub("", value)
    if not matches or "{{" in residue or "}}" in residue:
        raise DataReferenceError("Input contains an incomplete data reference")
    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        expression = matches[0].group(1)
        if _is_secret_reference(expression):
            # {{secrets.NAME}} is interpolated at dispatch time by
            # workflow_service.resolve_secrets; leave the token untouched.
            return value
        return _resolve_reference(
            expression, workflow_input=workflow_input, step_outputs=step_outputs
        )

    pieces: list[str] = []
    cursor = 0
    for match in matches:
        pieces.append(value[cursor:match.start()])
        expression = match.group(1)
        if _is_secret_reference(expression):
            # Preserve the secret token in place; ordinary input/step
            # references around it are still resolved.
            pieces.append(match.group(0))
            cursor = match.end()
            continue
        resolved = _resolve_reference(
            expression, workflow_input=workflow_input, step_outputs=step_outputs
        )
        if isinstance(resolved, (dict, list)):
            raise DataReferenceError("Object and array references must occupy the entire input value")
        if resolved is None:
            pieces.append("")
        elif isinstance(resolved, bool):
            pieces.append("true" if resolved else "false")
        else:
            pieces.append(str(resolved))
        cursor = match.end()
    pieces.append(value[cursor:])
    return "".join(pieces)


def resolve_foreach_templates(value: Any, *, item: Any, index: int) -> Any:
    """Resolve only the safe ``item``/``index`` variables used by fan-out.

    Ordinary ``input`` and ``steps`` references remain untouched for the
    standard resolver to handle when a worker claims each generated child.
    """
    if isinstance(value, dict):
        return {key: resolve_foreach_templates(child, item=item, index=index) for key, child in value.items()}
    if isinstance(value, list):
        return [resolve_foreach_templates(child, item=item, index=index) for child in value]
    if not isinstance(value, str) or "{{" not in value:
        return value

    matches = list(FOREACH_REFERENCE_PATTERN.finditer(value))
    if not matches:
        return value

    def resolve(path: str) -> Any:
        if path == "index":
            return index
        result = item
        for part in path.split(".")[1:]:
            if isinstance(result, Mapping) and part in result:
                result = result[part]
            elif isinstance(result, list) and part.isdecimal() and int(part) < len(result):
                result = result[int(part)]
            else:
                raise DataReferenceError("A foreach item field is unavailable")
        return result

    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        return resolve(matches[0].group(1))
    pieces: list[str] = []
    cursor = 0
    for match in matches:
        pieces.append(value[cursor:match.start()])
        resolved = resolve(match.group(1))
        if isinstance(resolved, (dict, list)):
            raise DataReferenceError("Object and array foreach references must occupy the entire input value")
        pieces.append("" if resolved is None else ("true" if resolved is True else "false" if resolved is False else str(resolved)))
        cursor = match.end()
    pieces.append(value[cursor:])
    return "".join(pieces)


def _resolve_reference(
    expression: str,
    *,
    workflow_input: Mapping[str, Any],
    step_outputs: Mapping[str, Any],
) -> Any:
    if _is_secret_reference(expression):
        raise DataReferenceError("Secret references are resolved at dispatch time, not here")

    parsed = REFERENCE_PATTERN.fullmatch(expression)
    if parsed is None:
        raise DataReferenceError("Input contains an unsupported data reference")

    reference = parsed.group("reference")
    if reference == "input" or reference.startswith("input."):
        value: Any = workflow_input
        path = reference.split(".")[1:]
    else:
        step_id = parsed.group("step_id")
        if step_id not in step_outputs:
            raise DataReferenceError("A referenced dependency output is unavailable")
        value = step_outputs[step_id]
        path = reference.split(".output", 1)[1].lstrip(".").split(".") if ".output." in reference else []

    for part in path:
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise DataReferenceError("A referenced input or dependency output field is unavailable")
    return value


def _strings(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value
