"""Deterministic numeric question generation.

For NUMERICAL families the backend computes the answer. A model may be asked to
reword the stem, but the number it would produce is never trusted and never
stored — which is why these variants are recorded as
``GenerationStatus.DETERMINISTIC``.

Parameters are drawn from a seeded PRNG so a family's variants are
reproducible: the same seed and config always yield the same questions, which
makes a disputed paper explainable after the fact.
"""

from __future__ import annotations

import ast
import math
import operator
import random
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.ai.dedupe import content_hash
from app.core.errors import AppError

MAX_PARAMETER_ATTEMPTS = 200


class NumericTemplateError(AppError):
    """The teacher's template or parameter config cannot produce questions."""

    code = "INVALID_NUMERIC_TEMPLATE"


# --- Safe expression evaluation -------------------------------------------
# A whitelisted AST walk. No eval(), no name lookup beyond the declared
# parameters, no attribute access, no calls except the functions below. A
# teacher-authored expression is still input, and input is not executed.

_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_ALLOWED_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
}

# Guards against a template like 9**9**9 exhausting memory.
_MAX_EXPONENT = 64


def evaluate_expression(expression: str, variables: dict[str, float]) -> float:
    """Evaluate an arithmetic expression over the declared variables."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise NumericTemplateError(f"Expression is not valid arithmetic: {exc.msg}") from exc

    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise NumericTemplateError("Only numeric constants are allowed.")
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise NumericTemplateError(f"Unknown parameter '{node.id}' in expression.")
            return variables[node.id]
        if isinstance(node, ast.BinOp):
            handler = _BINARY_OPS.get(type(node.op))
            if handler is None:
                raise NumericTemplateError(f"Operator {type(node.op).__name__} is not allowed.")
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
                raise NumericTemplateError("Exponent is too large.")
            if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
                raise NumericTemplateError("Expression divides by zero for these parameters.")
            return handler(left, right)
        if isinstance(node, ast.UnaryOp):
            handler = _UNARY_OPS.get(type(node.op))
            if handler is None:
                raise NumericTemplateError("Unary operator is not allowed.")
            return handler(visit(node.operand))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
                raise NumericTemplateError("Only whitelisted maths functions may be called.")
            if node.keywords:
                raise NumericTemplateError("Keyword arguments are not allowed.")
            return _ALLOWED_FUNCTIONS[node.func.id](*[visit(arg) for arg in node.args])
        raise NumericTemplateError(f"{type(node).__name__} is not allowed in an expression.")

    try:
        result = visit(tree)
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        raise NumericTemplateError(f"Expression could not be evaluated: {exc}") from exc

    if not isinstance(result, (int, float)) or isinstance(result, bool):
        raise NumericTemplateError("Expression did not produce a number.")
    if not math.isfinite(result):
        raise NumericTemplateError("Expression produced a non-finite value.")
    return float(result)


# --- Parameter drawing -----------------------------------------------------


@dataclass(frozen=True)
class NumericVariant:
    """A generated numeric question with its backend-computed answer."""

    question_text: str
    value: float
    tolerance: float
    unit: str | None
    parameters: dict[str, float]
    content_hash: str

    def numeric_answer_json(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "tolerance": self.tolerance,
            "unit": self.unit,
            # Kept so a disputed mark can be recomputed from the same inputs.
            "parameters": self.parameters,
        }


def _draw_parameter(rng: random.Random, name: str, spec: dict[str, Any]) -> float:
    kind = spec.get("type", "int")
    minimum, maximum = spec.get("min"), spec.get("max")
    if minimum is None or maximum is None:
        raise NumericTemplateError(f"Parameter '{name}' needs both 'min' and 'max'.")
    if minimum > maximum:
        raise NumericTemplateError(f"Parameter '{name}' has min greater than max.")

    if kind == "int":
        step = int(spec.get("step", 1) or 1)
        low, high = int(minimum), int(maximum)
        if step > 1:
            choices = list(range(low, high + 1, step))
            if not choices:
                raise NumericTemplateError(f"Parameter '{name}' has an empty range.")
            return float(rng.choice(choices))
        return float(rng.randint(low, high))
    if kind == "float":
        decimals = int(spec.get("decimals", 2))
        return round(rng.uniform(float(minimum), float(maximum)), decimals)
    raise NumericTemplateError(f"Parameter '{name}' has unsupported type '{kind}'.")


def _satisfies(values: dict[str, float], name: str, spec: dict[str, Any]) -> bool:
    """Check a cross-parameter rule such as 'correct <= total'."""
    depends_on = spec.get("depends_on")
    if not depends_on:
        return True
    if depends_on not in values:
        raise NumericTemplateError(
            f"Parameter '{name}' depends on '{depends_on}', which is not declared."
        )
    rule = spec.get("rule", "lte")
    other = values[depends_on]
    checks = {
        "lt": values[name] < other,
        "lte": values[name] <= other,
        "gt": values[name] > other,
        "gte": values[name] >= other,
        "ne": values[name] != other,
    }
    if rule not in checks:
        raise NumericTemplateError(f"Parameter '{name}' has unsupported rule '{rule}'.")
    return checks[rule]


def generate_numeric_variants(
    config: dict[str, Any],
    *,
    count: int,
    seed: int,
) -> list[NumericVariant]:
    """Generate ``count`` distinct numeric variants from a template.

    Draws are seeded, so the same config and seed reproduce the same questions.
    Duplicate parameter draws are skipped rather than stored twice, which means
    fewer than ``count`` variants is a possible and acceptable outcome for a
    narrow parameter range.
    """
    template = config.get("template")
    expression = config.get("expression")
    if not template or not isinstance(template, str):
        raise NumericTemplateError("A numeric family needs a 'template' string.")
    if not expression or not isinstance(expression, str):
        raise NumericTemplateError("A numeric family needs an 'expression'.")

    parameters = config.get("parameters") or {}
    if not isinstance(parameters, dict) or not parameters:
        raise NumericTemplateError("A numeric family needs at least one parameter.")

    tolerance = float(config.get("tolerance", 0.01))
    unit = config.get("unit")
    decimals = int(config.get("decimals", 2))

    rng = random.Random(seed)
    produced: list[NumericVariant] = []
    seen_hashes: set[str] = set()
    attempts = 0

    while len(produced) < count and attempts < MAX_PARAMETER_ATTEMPTS:
        attempts += 1
        values: dict[str, float] = {}
        # Independent parameters first, so a dependent one can be checked
        # against a value that already exists.
        ordered = sorted(parameters.items(), key=lambda item: bool(item[1].get("depends_on")))
        satisfied = True
        for name, spec in ordered:
            if not isinstance(spec, dict):
                raise NumericTemplateError(f"Parameter '{name}' must be an object.")
            values[name] = _draw_parameter(rng, name, spec)
            if not _satisfies(values, name, spec):
                satisfied = False
                break
        if not satisfied:
            continue

        raw_value = evaluate_expression(expression, values)
        value = float(
            Decimal(str(raw_value)).quantize(
                Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP
            )
        )

        try:
            question_text = template.format(**{k: _render(v) for k, v in values.items()})
        except KeyError as exc:
            raise NumericTemplateError(
                f"Template references {exc} which is not a declared parameter."
            ) from exc
        except (IndexError, ValueError) as exc:
            raise NumericTemplateError(f"Template is not a valid format string: {exc}") from exc

        digest = content_hash(question_text, None)
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)

        produced.append(
            NumericVariant(
                question_text=question_text,
                value=value,
                tolerance=tolerance,
                unit=unit if isinstance(unit, str) else None,
                parameters=dict(values),
                content_hash=digest,
            )
        )

    if not produced:
        raise NumericTemplateError(
            "No valid parameter combination was found. Check the ranges and any "
            "depends_on rules."
        )
    return produced


def _render(value: float) -> str:
    """Render a drawn parameter for substitution into the stem."""
    return str(int(value)) if float(value).is_integer() else str(value)


def placeholders_in(template: str) -> list[str]:
    """Names referenced by a format-string template."""
    import string

    return [name for _, name, _, _ in string.Formatter().parse(template) if name]


def reword_preserves_placeholders(reworded: str, required: list[str]) -> bool:
    """Whether a model's rewording kept every parameter placeholder.

    A rewording that drops one would produce a question with a literal
    ``{total}`` in it, or worse, a question missing the quantity it asks about.
    """
    found = set(placeholders_in(reworded))
    return set(required) <= found


__all__ = [
    "NumericTemplateError",
    "NumericVariant",
    "evaluate_expression",
    "generate_numeric_variants",
    "placeholders_in",
    "reword_preserves_placeholders",
]
