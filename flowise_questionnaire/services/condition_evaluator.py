from __future__ import annotations

import re
from typing import Any


def evaluate_condition(
    condition_text: str,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """
    Evaluate a simple extracted questionnaire routing condition.

    Supported patterns:
    - VAR = VALUE
    - VAR <> VALUE
    - VAR > VALUE
    - VAR >= VALUE
    - VAR IN 1,2,3
    - VAR = 1|2|3
    - AND / &
    - OR
    - basic derived household expression:
      (HHGRID.HHSIZE - Number of absent hhold members) > 1

    Conservative behavior:
    - unknown if required input is missing
    - unsupported if expression cannot be safely parsed
    """

    original_condition = condition_text or ""
    expression = _strip_condition_wrapper(original_condition)

    result = {
        "status": "unsupported",
        "missing_inputs": [],
        "used_inputs": {},
        "normalized_expression": expression,
        "warnings": [],
    }

    if not expression:
        result["status"] = "unsupported"
        result["warnings"].append("Empty condition.")
        return result

    try:
        status, missing_inputs, used_inputs, warnings = _evaluate_expression(
            expression=expression,
            inputs=inputs,
        )
    except Exception as exc:
        result["status"] = "unsupported"
        result["warnings"].append(f"Condition evaluation failed: {exc}")
        return result

    result["status"] = status
    result["missing_inputs"] = sorted(set(missing_inputs))
    result["used_inputs"] = used_inputs
    result["warnings"] = warnings

    return result


def strip_condition_wrapper(condition_text: str) -> str:
    """
    Public entry point for display code (e.g. the routing diff GUI) that wants
    the bare comparison expression without the `if [...]` wrapper, using the
    exact same stripping rules the evaluator itself relies on.
    """

    return _strip_condition_wrapper(condition_text or "")


def _strip_condition_wrapper(condition_text: str) -> str:
    expression = condition_text.strip()

    if expression.lower().startswith("if "):
        expression = expression[3:].strip()

    # Remove one outer bracket pair if the whole expression is wrapped.
    if expression.startswith("[") and expression.endswith("]"):
        expression = expression[1:-1].strip()

    return expression


def _evaluate_expression(
    expression: str,
    inputs: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any], list[str]]:
    expression = expression.strip()

    # Normalize symbolic AND. Conditions can use either " & " or "&".
    expression = re.sub(r"\s*&\s*", " AND ", expression)

    # Remove harmless wrapping parentheses repeatedly.
    expression = _strip_outer_parentheses(expression)

    or_parts = _split_top_level(expression, "OR")
    if len(or_parts) > 1:
        statuses = []
        missing_inputs: list[str] = []
        used_inputs: dict[str, Any] = {}
        warnings: list[str] = []

        for part in or_parts:
            status, part_missing, part_used, part_warnings = _evaluate_expression(
                expression=part,
                inputs=inputs,
            )
            statuses.append(status)
            missing_inputs.extend(part_missing)
            used_inputs.update(part_used)
            warnings.extend(part_warnings)

        if "true" in statuses:
            return "true", [], used_inputs, warnings

        if all(status == "false" for status in statuses):
            return "false", [], used_inputs, warnings

        if "unsupported" in statuses:
            return "unsupported", missing_inputs, used_inputs, warnings

        return "unknown", missing_inputs, used_inputs, warnings

    and_parts = _split_top_level(expression, "AND")
    if len(and_parts) > 1:
        statuses = []
        missing_inputs: list[str] = []
        used_inputs: dict[str, Any] = {}
        warnings: list[str] = []

        for part in and_parts:
            status, part_missing, part_used, part_warnings = _evaluate_expression(
                expression=part,
                inputs=inputs,
            )
            statuses.append(status)
            missing_inputs.extend(part_missing)
            used_inputs.update(part_used)
            warnings.extend(part_warnings)

        if "false" in statuses:
            return "false", [], used_inputs, warnings

        if all(status == "true" for status in statuses):
            return "true", [], used_inputs, warnings

        if "unsupported" in statuses:
            return "unsupported", missing_inputs, used_inputs, warnings

        return "unknown", missing_inputs, used_inputs, warnings

    return _evaluate_atomic_condition(expression, inputs)


def _evaluate_atomic_condition(
    expression: str,
    inputs: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any], list[str]]:
    expression = _strip_outer_parentheses(expression.strip())
    warnings: list[str] = []

    # Special case:
    # (HHGRID.HHSIZE - Number of absent hhold members) > 1
    household_match = re.search(
        r"HHGRID\.HHSIZE\s*-\s*Number\s+of\s+absent\s+hhold\s+members\)\s*>\s*(-?\d+)",
        expression,
        flags=re.IGNORECASE,
    )
    if household_match:
        source_name = _find_input_key(inputs, "HHGRID.HHSIZE")
        if not source_name:
            return "unknown", ["HHGRID.HHSIZE"], {}, warnings

        threshold = int(household_match.group(1))
        value = _to_number(inputs[source_name])

        if value is None:
            return "unsupported", [], {source_name: inputs[source_name]}, [
                f"HHGRID.HHSIZE value is not numeric: {inputs[source_name]}"
            ]

        # We do not currently have absent household members as a separate input.
        # Conservative assumption: absent count = 0. This matches the coverage-intent note.
        return (
            "true" if value > threshold else "false",
            [],
            {source_name: inputs[source_name]},
            ["Assumed Number of absent hhold members = 0."],
        )

    # VAR IN 1,2,3
    in_match = re.fullmatch(
        r"([A-Za-z0-9_.]+)\s+IN\s+([A-Za-z0-9_,\s-]+)",
        expression,
        flags=re.IGNORECASE,
    )
    if in_match:
        variable_name = in_match.group(1)
        allowed_values = [
            item.strip()
            for item in in_match.group(2).split(",")
            if item.strip()
        ]

        return _compare_membership(
            variable_name=variable_name,
            allowed_values=allowed_values,
            inputs=inputs,
            warnings=warnings,
        )

    # VAR = 1|2|3
    pipe_match = re.fullmatch(
        r"([A-Za-z0-9_.]+)\s*=\s*([A-Za-z0-9_|-]+)",
        expression,
        flags=re.IGNORECASE,
    )
    if pipe_match and "|" in pipe_match.group(2):
        variable_name = pipe_match.group(1)
        allowed_values = [
            item.strip()
            for item in pipe_match.group(2).split("|")
            if item.strip()
        ]

        return _compare_membership(
            variable_name=variable_name,
            allowed_values=allowed_values,
            inputs=inputs,
            warnings=warnings,
        )

    comparison_match = re.fullmatch(
        r"([A-Za-z0-9_.]+)\s*(=|<>|>=|>)\s*([A-Za-z0-9_.-]+)",
        expression,
        flags=re.IGNORECASE,
    )
    if comparison_match:
        variable_name = comparison_match.group(1)
        operator = comparison_match.group(2)
        expected_value = comparison_match.group(3)

        return _compare_value(
            variable_name=variable_name,
            operator=operator,
            expected_value=expected_value,
            inputs=inputs,
            warnings=warnings,
        )

    return "unsupported", [], {}, [f"Unsupported atomic condition: {expression}"]


def _compare_membership(
    variable_name: str,
    allowed_values: list[str],
    inputs: dict[str, Any],
    warnings: list[str],
) -> tuple[str, list[str], dict[str, Any], list[str]]:
    input_key = _find_input_key(inputs, variable_name)

    if not input_key:
        return "unknown", [variable_name], {}, warnings

    actual_value = inputs[input_key]
    actual_strings = _normalize_values(actual_value)

    allowed_strings = {
        _normalize_scalar(value)
        for value in allowed_values
    }

    # Scalar: normal membership.
    # List/multi-choice: true when any selected code appears in the allowed set.
    return (
        "true" if bool(actual_strings & allowed_strings) else "false",
        [],
        {input_key: actual_value},
        warnings,
    )


def _compare_value(
    variable_name: str,
    operator: str,
    expected_value: str,
    inputs: dict[str, Any],
    warnings: list[str],
) -> tuple[str, list[str], dict[str, Any], list[str]]:
    input_key = _find_input_key(inputs, variable_name)

    if not input_key:
        return "unknown", [variable_name], {}, warnings

    actual_value = inputs[input_key]

    if operator in (">", ">="):
        actual_numbers = [
            number
            for number in (_to_number(value) for value in _as_list(actual_value))
            if number is not None
        ]
        expected_number = _to_number(expected_value)

        if not actual_numbers or expected_number is None:
            return "unsupported", [], {input_key: actual_value}, [
                f"Numeric comparison failed for {variable_name}: {actual_value} {operator} {expected_value}"
            ]

        # For list values, true if any selected value satisfies the numeric comparison.
        if operator == ">":
            status = "true" if any(number > expected_number for number in actual_numbers) else "false"
        else:
            status = "true" if any(number >= expected_number for number in actual_numbers) else "false"

        return status, [], {input_key: actual_value}, warnings

    actual_strings = _normalize_values(actual_value)
    expected_string = _normalize_scalar(expected_value)

    if operator == "=":
        # Scalar: exact equality.
        # List/multi-choice: expected code is selected.
        return (
            "true" if expected_string in actual_strings else "false",
            [],
            {input_key: actual_value},
            warnings,
        )

    if operator == "<>":
        # Scalar: not equal.
        # List/multi-choice: expected code is not selected.
        return (
            "true" if expected_string not in actual_strings else "false",
            [],
            {input_key: actual_value},
            warnings,
        )

    return "unsupported", [], {input_key: actual_value}, [
        f"Unsupported operator: {operator}"
    ]


def _find_input_key(inputs: dict[str, Any], variable_name: str) -> str | None:
    """
    Match variable names case-insensitively.

    This handles extracted conditions like:
        AidHH = 1
    while Django question names are:
        AIDHH
    """

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
    return {
        _normalize_scalar(item)
        for item in _as_list(value)
    }


def _normalize_scalar(value: Any) -> str:
    return str(value).strip()


def _to_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strip_outer_parentheses(expression: str) -> str:
    expression = expression.strip()

    while expression.startswith("(") and expression.endswith(")"):
        inner = expression[1:-1].strip()

        if not _parentheses_balanced(inner):
            break

        expression = inner

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
    Split expression by top-level AND/OR only, ignoring nested parentheses.
    """

    tokens: list[str] = []
    current: list[str] = []
    depth = 0

    parts = re.split(r"(\s+)", expression)
    index = 0

    while index < len(parts):
        part = parts[index]
        upper_part = part.upper()

        depth += part.count("(")
        depth -= part.count(")")

        if depth == 0 and upper_part == operator:
            token = "".join(current).strip()
            if token:
                tokens.append(token)
            current = []
        else:
            current.append(part)

        index += 1

    final_token = "".join(current).strip()

    if final_token:
        tokens.append(final_token)

    return tokens
