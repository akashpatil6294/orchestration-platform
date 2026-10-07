from __future__ import annotations

import pytest

from app.core.dataflow import DataReferenceError, resolve_templates, validate_templates


def test_resolves_nested_workflow_and_step_values_without_losing_types():
    resolved = resolve_templates(
        {
            "prompt": "Summarize {{steps.extract.output.text}} for {{input.customer.name}}",
            "options": {"count": "{{input.options.count}}", "first": "{{steps.extract.output.pages.0}}"},
        },
        workflow_input={"customer": {"name": "Acme"}, "options": {"count": 3}},
        step_outputs={"extract": {"text": "report", "pages": ["page one"]}},
    )

    assert resolved == {
        "prompt": "Summarize report for Acme",
        "options": {"count": 3, "first": "page one"},
    }
    assert isinstance(resolved["options"]["count"], int)


@pytest.mark.parametrize(
    "template",
    [
        "{{python.__import__('os')}}",
        "{{ input.name",
        "{{steps.source.output.value}} }}",
    ],
)
def test_rejects_unsupported_or_incomplete_references(template):
    assert validate_templates(
        {"value": template}, step_id="consumer", dependencies={"source"}
    )


def test_requires_step_references_to_be_declared_dependencies():
    errors = validate_templates(
        {"value": "{{steps.source.output.value}}"},
        step_id="consumer",
        dependencies=set(),
    )

    assert errors
    with pytest.raises(DataReferenceError):
        resolve_templates(
            {"value": "{{steps.source.output.value}}"},
            workflow_input={},
            step_outputs={},
        )


def test_missing_runtime_path_is_an_explicit_resolution_error():
    with pytest.raises(DataReferenceError, match="field is unavailable"):
        resolve_templates(
            {"value": "{{steps.source.output.missing}}"},
            workflow_input={},
            step_outputs={"source": {"value": "present"}},
        )


def test_structured_values_cannot_be_embedded_in_text():
    with pytest.raises(DataReferenceError, match="must occupy the entire"):
        resolve_templates(
            {"value": "Result: {{input.object}}"},
            workflow_input={"object": {"key": "value"}},
            step_outputs={},
        )
