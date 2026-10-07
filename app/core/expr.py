"""Safe boolean/arithmetic expressions over workflow JSON paths.

Workflow authors never supply Python. The grammar is a small recursive-descent
parser with a whitelist of operators and path access into ``input`` and
``steps.<id>.output`` only.
"""
from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from typing import Any

from app.core.dataflow import DataReferenceError, _resolve_reference

PATH_PATTERN = re.compile(
    r"^(input(?:\.[A-Za-z0-9_-]+)*|"
    r"steps\.[A-Za-z0-9_.-]+\.output(?:\.[A-Za-z0-9_-]+)*)$"
)

TOKEN_PATTERN = re.compile(
    r"""
    \s*
    (
        ==|!=|<=|>=|&&|\|\|
        |and\b|or\b|not\b
        |[+\-*/%<>=()[\],]
        |true\b|false\b|null\b
        |[A-Za-z_][A-Za-z0-9_.-]*
        |-?\d+\.\d+|-?\d+
        |'(?:\\'|[^'])*'
        |"(?:\\"|[^"])*"
    )
    """,
    re.VERBOSE,
)


class ExprError(ValueError):
    """Raised when an expression is syntactically or semantically invalid."""


_VALIDATION_VALUE = object()


def referenced_step_ids(expression: str) -> set[str]:
    ids: set[str] = set()
    for match in re.finditer(r"steps\.([A-Za-z0-9_.-]+)\.output", expression):
        ids.add(match.group(1))
    return ids


def validate_expression(expression: str, *, step_id: str, dependencies: set[str]) -> list[str]:
    if not isinstance(expression, str) or not expression.strip():
        return [f"Step '{step_id}' has an empty condition"]
    try:
        tokens = _tokenize(expression)
        parser = _Parser(tokens, validate_only=True)
        parser.parse()
        if parser.pos != len(tokens):
            return [f"Step '{step_id}' has an incomplete condition"]
    except ExprError as exc:
        return [f"Step '{step_id}' has an invalid condition: {exc}"]
    errors: list[str] = []
    for source_id in referenced_step_ids(expression):
        if source_id not in dependencies:
            errors.append(
                f"Step '{step_id}' condition references step '{source_id}' without declaring it in depends_on"
            )
    return errors


def evaluate(
    expression: str,
    *,
    workflow_input: Mapping[str, Any],
    step_outputs: Mapping[str, Any],
) -> Any:
    tokens = _tokenize(expression)
    parser = _Parser(tokens, workflow_input=workflow_input, step_outputs=step_outputs)
    value = parser.parse()
    if parser.pos != len(tokens):
        raise ExprError("Unexpected trailing tokens in expression")
    return value


def is_truthy(value: Any) -> bool:
    if value is None or value is False:
        return False
    if value == 0 or value == "":
        return False
    if value == [] or value == {}:
        return False
    return True


def _tokenize(expression: str) -> list[str]:
    tokens: list[str] = []
    cursor = 0
    length = len(expression)
    while cursor < length:
        match = TOKEN_PATTERN.match(expression, cursor)
        if match is None:
            raise ExprError(f"Unsupported token at position {cursor}")
        token = match.group(1)
        tokens.append(token)
        cursor = match.end()
    if cursor != length and expression[cursor:].strip():
        raise ExprError("Unsupported characters in expression")
    return tokens


class _Parser:
    def __init__(
        self,
        tokens: list[str],
        *,
        workflow_input: Mapping[str, Any] | None = None,
        step_outputs: Mapping[str, Any] | None = None,
        validate_only: bool = False,
    ) -> None:
        self.tokens = tokens
        self.pos = 0
        self.workflow_input = workflow_input if workflow_input is not None else {}
        self.step_outputs = step_outputs if step_outputs is not None else {}
        self.validate_only = validate_only

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if token is None:
            raise ExprError("Unexpected end of expression")
        if expected is not None and token != expected:
            raise ExprError(f"Expected '{expected}'")
        self.pos += 1
        return token

    def parse(self) -> Any:
        return self._or()

    def _or(self) -> Any:
        value = self._and()
        while self.peek() in {"or", "||"}:
            self.take()
            right = self._and()
            value = _VALIDATION_VALUE if self.validate_only else is_truthy(value) or is_truthy(right)
        return value

    def _and(self) -> Any:
        value = self._not()
        while self.peek() in {"and", "&&"}:
            self.take()
            right = self._not()
            value = _VALIDATION_VALUE if self.validate_only else is_truthy(value) and is_truthy(right)
        return value

    def _not(self) -> Any:
        if self.peek() == "not":
            self.take()
            value = self._not()
            return _VALIDATION_VALUE if self.validate_only else not is_truthy(value)
        return self._compare()

    def _compare(self) -> Any:
        value = self._add()
        while self.peek() in {"==", "!=", "<", ">", "<=", ">="}:
            op = self.take()
            right = self._add()
            if self.validate_only:
                value = _VALIDATION_VALUE
            elif op == "==":
                value = value == right
            elif op == "!=":
                value = value != right
            elif op == "<":
                value = _ordered(value, right, lambda a, b: a < b)
            elif op == ">":
                value = _ordered(value, right, lambda a, b: a > b)
            elif op == "<=":
                value = _ordered(value, right, lambda a, b: a <= b)
            else:
                value = _ordered(value, right, lambda a, b: a >= b)
        return value

    def _add(self) -> Any:
        value = self._mul()
        while self.peek() in {"+", "-"}:
            op = self.take()
            right = self._mul()
            if self.validate_only:
                value = _VALIDATION_VALUE
            elif op == "+":
                if isinstance(value, str) or isinstance(right, str):
                    value = str(value) + str(right)
                else:
                    value = _numeric(value) + _numeric(right)
            else:
                value = _numeric(value) - _numeric(right)
        return value

    def _mul(self) -> Any:
        value = self._unary()
        while self.peek() in {"*", "/", "%"}:
            op = self.take()
            right = self._unary()
            if self.validate_only:
                value = _VALIDATION_VALUE
            elif op == "*":
                value = _numeric(value) * _numeric(right)
            elif op == "/":
                denom = _numeric(right)
                if denom == 0:
                    raise ExprError("Division by zero")
                value = _numeric(value) / denom
            else:
                denom = _numeric(right)
                if denom == 0:
                    raise ExprError("Modulo by zero")
                value = _numeric(value) % denom
        return value

    def _unary(self) -> Any:
        if self.peek() == "-":
            self.take()
            value = self._unary()
            return _VALIDATION_VALUE if self.validate_only else -_numeric(value)
        if self.peek() == "+":
            self.take()
            value = self._unary()
            return _VALIDATION_VALUE if self.validate_only else _numeric(value)
        return self._primary()

    def _primary(self) -> Any:
        token = self.peek()
        if token is None:
            raise ExprError("Unexpected end of expression")
        if token == "(":
            self.take()
            value = self.parse()
            self.take(")")
            return value
        if token in {"true", "false", "null"}:
            self.take()
            return _VALIDATION_VALUE if self.validate_only else {"true": True, "false": False, "null": None}[token]
        if token[0] in {"'", '"'}:
            self.take()
            return _VALIDATION_VALUE if self.validate_only else ast.literal_eval(token)
        if re.fullmatch(r"-?\d+", token):
            self.take()
            return _VALIDATION_VALUE if self.validate_only else int(token)
        if re.fullmatch(r"-?\d+\.\d+", token):
            self.take()
            return _VALIDATION_VALUE if self.validate_only else float(token)
        if token in {"input", "steps"} or token.startswith("input.") or token.startswith("steps."):
            path = self._consume_path()
            return _VALIDATION_VALUE if self.validate_only else self._resolve_path(path)
        raise ExprError(f"Unsupported token '{token}'")

    def _consume_path(self) -> str:
        parts = [self.take()]
        while self.peek() == ".":
            self.take()
            nxt = self.peek()
            if nxt is None or not re.fullmatch(r"[A-Za-z0-9_-]+", nxt):
                raise ExprError("Invalid path after '.'")
            parts.append(self.take())
        path = ".".join(parts)
        if PATH_PATTERN.fullmatch(path) is None:
            raise ExprError("Expressions may only read input.* or steps.<id>.output.*")
        return path

    def _resolve_path(self, path: str) -> Any:
        try:
            return _resolve_reference(
                path, workflow_input=self.workflow_input, step_outputs=self.step_outputs
            )
        except DataReferenceError as exc:
            raise ExprError(str(exc)) from exc


def _numeric(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExprError("Arithmetic is only allowed on numbers")
    return float(value)


def _ordered(left: Any, right: Any, op) -> bool:
    try:
        return bool(op(left, right))
    except TypeError as exc:
        raise ExprError("Values cannot be compared") from exc


__all__ = [
    "ExprError",
    "evaluate",
    "is_truthy",
    "referenced_step_ids",
    "validate_expression",
]
