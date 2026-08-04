from __future__ import annotations

import re
from typing import Any

_VAR_METHOD_PATTERN = re.compile(
    r"^f\(\s*'([^']+)'\s*\)\.(any|none|toBoolean)\((?:\s*'([^']*)'\s*)?\)$"
)
_NEGATION_PATTERN = re.compile(r"^NOT\s*\((.*)\)$", re.IGNORECASE | re.DOTALL)


def evaluate_forsta_condition(
        expression_text: str,
        inputs: dict[str, Any],
) -> dict[str, Any]:
    """
    Evaluate a Forsta+ (Confirmit Horizons) routing condition expression.

    Supported patterns:
    - f('VAR').any('code')
    - f('VAR').none('code')
    - f('VAR').toBoolean()
    - && / ||
    - NOT (...) -- the wrapper ForstaXmlRoutingExtractor uses for FalseNodes
      branches (see forsta_xml_routing_validation_plan.md §6.2)

    Mirrors condition_evaluator.evaluate_condition()'s return shape and
    conservative status semantics ("unknown" for missing inputs, "unsupported"
    for anything the parser doesn't recognize) so both evaluators can be used
    interchangeably by future simulated-path comparison code (Step 8/9).
    """

    original_expression = expression_text or ""
    expression = original_expression.strip()

    result: dict[str, Any] = {
        "status": "unsupported",
        "missing_inputs": [],
        "used_inputs": {},
        "normalized_expression": expression,
        "warnings": [],
    }

    if not expression:
        result["warnings"].append("Empty condition.")
        return result

    try:
        status, missing_inputs, used_inputs, warnings = _evaluate_expression(expression, inputs)
    except Exception as exc:
        result["warnings"].append(f"Condition evaluation failed: {exc}")
        return result

    result["status"] = status
    result["missing_inputs"] = sorted(set(missing_inputs))
    result["used_inputs"] = used_inputs
    result["warnings"] = warnings

    return result


def _evaluate_expression(
        expression: str,
        inputs: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any], list[str]]:
    expression = expression.strip()

    negation_match = _NEGATION_PATTERN.match(expression)
    if negation_match:
        status, missing, used, warnings = _evaluate_expression(negation_match.group(1), inputs)
        negated_status = {"true": "false", "false": "true"}.get(status, status)
        return negated_status, missing, used, warnings

    expression = _strip_outer_parentheses(expression)

    or_parts = _split_top_level(expression, "||")
    if len(or_parts) > 1:
        return _combine(or_parts, inputs, any_true_wins=True)

    and_parts = _split_top_level(expression, "&&")
    if len(and_parts) > 1:
        return _combine(and_parts, inputs, any_true_wins=False)

    return _evaluate_atomic(expression, inputs)


def _combine(
        parts: list[str],
        inputs: dict[str, Any],
        any_true_wins: bool,
) -> tuple[str, list[str], dict[str, Any], list[str]]:
    statuses: list[str] = []
    missing_inputs: list[str] = []
    used_inputs: dict[str, Any] = {}
    warnings: list[str] = []

    for part in parts:
        status, part_missing, part_used, part_warnings = _evaluate_expression(part, inputs)
        statuses.append(status)
        missing_inputs.extend(part_missing)
        used_inputs.update(part_used)
        warnings.extend(part_warnings)

    if any_true_wins:
        # OR
        if "true" in statuses:
            return "true", [], used_inputs, warnings
        if all(status == "false" for status in statuses):
            return "false", [], used_inputs, warnings
    else:
        # AND
        if "false" in statuses:
            return "false", [], used_inputs, warnings
        if all(status == "true" for status in statuses):
            return "true", [], used_inputs, warnings

    if "unsupported" in statuses:
        return "unsupported", missing_inputs, used_inputs, warnings

    return "unknown", missing_inputs, used_inputs, warnings


def _evaluate_atomic(
        expression: str,
        inputs: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any], list[str]]:
    expression = _strip_outer_parentheses(expression.strip())

    match = _VAR_METHOD_PATTERN.match(expression)
    if not match:
        return "unsupported", [], {}, [f"Unsupported atomic condition: {expression}"]

    variable_name, method, argument = match.group(1), match.group(2), match.group(3)
    input_key = _find_input_key(inputs, variable_name)

    if not input_key:
        return "unknown", [variable_name], {}, []

    actual_value = inputs[input_key]

    if method == "toBoolean":
        normalized = str(actual_value).strip().lower()

        if normalized in {"true", "1"}:
            return "true", [], {input_key: actual_value}, []

        if normalized in {"false", "0", ""}:
            return "false", [], {input_key: actual_value}, []

        return "unsupported", [], {input_key: actual_value}, [
            f"Cannot coerce to boolean: {actual_value!r}"
        ]

    if argument is None:
        return "unsupported", [], {input_key: actual_value}, [
            f"Missing argument for {method}()"
        ]

    actual_strings = _normalize_values(actual_value)

    if method == "any":
        status = "true" if argument in actual_strings else "false"
        return status, [], {input_key: actual_value}, []

    if method == "none":
        status = "true" if argument not in actual_strings else "false"
        return status, [], {input_key: actual_value}, []

    return "unsupported", [], {input_key: actual_value}, [f"Unsupported method: {method}"]


def _find_input_key(inputs: dict[str, Any], variable_name: str) -> str | None:
    if variable_name in inputs:
        return variable_name

    target = variable_name.lower()

    for key in inputs:
        if key.lower() == target:
            return key

    return None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set)):
        return list(value)

    return [value]


def _normalize_values(value: Any) -> set[str]:
    return {str(item).strip() for item in _as_list(value)}


def _strip_outer_parentheses(expression: str) -> str:
    expression = expression.strip()

    while expression.startswith("(") and expression.endswith(")"):
        inner = expression[1:-1]

        if not _parentheses_balanced(inner):
            break

        expression = inner.strip()

    return expression


def _parentheses_balanced(expression: str) -> bool:
    depth = 0

    for char in expression:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1

        if depth < 0:
            return False

    return depth == 0


def _split_top_level(expression: str, operator: str) -> list[str]:
    """
    Split expression by top-level && or || only, ignoring nested parentheses
    and without splitting inside quoted string literals (Forsta+ expressions
    reference literal codes like f('A').any('1') where "1" must stay intact).
    """

    tokens: list[str] = []
    current: list[str] = []
    depth = 0
    in_quote = False
    index = 0

    while index < len(expression):
        char = expression[index]

        if char == "'" :
            in_quote = not in_quote
            current.append(char)
            index += 1
            continue

        if in_quote:
            current.append(char)
            index += 1
            continue

        if char == "(":
            depth += 1
            current.append(char)
            index += 1
            continue

        if char == ")":
            depth -= 1
            current.append(char)
            index += 1
            continue

        if depth == 0 and expression[index:index + len(operator)] == operator:
            token = "".join(current).strip()
            if token:
                tokens.append(token)
            current = []
            index += len(operator)
            continue

        current.append(char)
        index += 1

    final_token = "".join(current).strip()
    if final_token:
        tokens.append(final_token)

    return tokens
