from __future__ import annotations

import re
from typing import Any

from flowise_questionnaire.services.interview_simulator_contracts import (
    AnswerValidationResult,
    SimulatorOption,
    SimulatorQuestion,
)


class AnswerValidationError(Exception):
    """
    Raised only for programmer/configuration errors.

    Normal respondent input errors are returned as AnswerValidationResult.
    """


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalise_letter(value: Any) -> str:
    return _as_text(value).upper()


def _split_answer_letters(user_input: str) -> list[str]:
    """
    Parse respondent input into option letters.

    Accepted examples:
        A
        a
        A,C
        A, C
        A C
        A;C
        A/C

    Rejected later if any parsed token is not a valid option letter.
    """
    text = _as_text(user_input).upper()

    if not text:
        return []

    # Split on common separators and whitespace.
    parts = re.split(r"[\s,;/|]+", text)

    return [
        part.strip()
        for part in parts
        if part and part.strip()
    ]


def _unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []

    for value in values:
        key = value.upper()

        if key in seen:
            continue

        seen.add(key)
        result.append(value.upper())

    return result


def _option_by_letter(question: SimulatorQuestion) -> dict[str, SimulatorOption]:
    mapping: dict[str, SimulatorOption] = {}

    for option in question.options:
        letter = _normalise_letter(option.letter)

        if not letter:
            raise AnswerValidationError(
                f"Question {question.name} has an option without a letter."
            )

        if letter in mapping:
            raise AnswerValidationError(
                f"Question {question.name} has duplicate option letter {letter}."
            )

        mapping[letter] = option

    return mapping


def _valid_letters_text(question: SimulatorQuestion) -> str:
    letters = [_normalise_letter(option.letter) for option in question.options]
    return ", ".join(letters)


def _normalise_number_text(value: str) -> str:
    value = value.strip()

    if re.fullmatch(r"[-+]?\d+", value):
        return str(int(value))

    if re.fullmatch(r"[-+]?(?:\d+\.\d+|\d+\.|\.\d+)", value):
        return str(float(value))

    return value


def _validate_number_answer(
    question: SimulatorQuestion,
    user_input: str,
) -> AnswerValidationResult:
    value = _as_text(user_input)

    if not value:
        return AnswerValidationResult(
            accepted=False,
            user_answer_letters=[],
            coded_answer=None,
            errors=["Please enter a number."],
        )

    if not re.fullmatch(r"[-+]?(?:\d+|\d+\.\d+|\d+\.|\.\d+)", value):
        return AnswerValidationResult(
            accepted=False,
            user_answer_letters=[],
            coded_answer=None,
            errors=["Invalid number. Please enter a numeric value, for example 1 or 12.50."],
        )

    return AnswerValidationResult(
        accepted=True,
        user_answer_letters=[],
        coded_answer={question.name: _normalise_number_text(value)},
        errors=[],
        warnings=[],
    )


def _validate_text_answer(
    question: SimulatorQuestion,
    user_input: str,
) -> AnswerValidationResult:
    value = _as_text(user_input)

    if not value:
        return AnswerValidationResult(
            accepted=False,
            user_answer_letters=[],
            coded_answer=None,
            errors=["Please type an answer."],
        )

    return AnswerValidationResult(
        accepted=True,
        user_answer_letters=[],
        coded_answer={question.name: value},
        errors=[],
        warnings=[],
    )


def validate_answer_letters(
    question: SimulatorQuestion,
    user_input: str,
) -> AnswerValidationResult:
    """
    Validate respondent input and convert to real questionnaire values.

    Option questions:
        A      -> {"AIDHH": "1"}
        A, C   -> {"BENBASE": ["1", "3"]}

    Raw-entry questions:
        1      -> {"FISEQ": "1"}
        12.50  -> {"FRVAL": "12.5"}
    """
    if question.selection_mode == "number":
        return _validate_number_answer(question, user_input)

    if question.selection_mode == "text":
        return _validate_text_answer(question, user_input)

    option_lookup = _option_by_letter(question)
    parsed_letters = _unique_preserve_order(_split_answer_letters(user_input))

    if not parsed_letters:
        return AnswerValidationResult(
            accepted=False,
            user_answer_letters=[],
            coded_answer=None,
            errors=[
                (
                    "Please answer using option letter(s) only. "
                    f"Valid option letters are: {_valid_letters_text(question)}."
                )
            ],
        )

    invalid_letters = [
        letter
        for letter in parsed_letters
        if letter not in option_lookup
    ]

    if invalid_letters:
        return AnswerValidationResult(
            accepted=False,
            user_answer_letters=parsed_letters,
            coded_answer=None,
            errors=[
                (
                    f"Invalid option letter(s): {', '.join(invalid_letters)}. "
                    f"Valid option letters are: {_valid_letters_text(question)}."
                )
            ],
        )

    if question.selection_mode == "single" and len(parsed_letters) > 1:
        return AnswerValidationResult(
            accepted=False,
            user_answer_letters=parsed_letters,
            coded_answer=None,
            errors=[
                (
                    "This question accepts one option only. "
                    f"Please choose one: {_valid_letters_text(question)}."
                )
            ],
        )

    selected_options = [
        option_lookup[letter]
        for letter in parsed_letters
    ]

    exclusive_selected = [
        option
        for option in selected_options
        if option.exclusive
    ]

    if exclusive_selected and len(selected_options) > 1:
        exclusive_letters = ", ".join(
            _normalise_letter(option.letter)
            for option in exclusive_selected
        )

        return AnswerValidationResult(
            accepted=False,
            user_answer_letters=parsed_letters,
            coded_answer=None,
            errors=[
                (
                    f"Option {exclusive_letters} cannot be selected with other options. "
                    "Please choose it alone, or choose the other applicable options."
                )
            ],
        )

    selected_codes = [
        option.code
        for option in selected_options
    ]

    if question.selection_mode == "multiple":
        coded_value: Any = selected_codes
    else:
        coded_value = selected_codes[0]

    return AnswerValidationResult(
        accepted=True,
        user_answer_letters=parsed_letters,
        coded_answer={
            question.name: coded_value,
        },
        errors=[],
        warnings=[],
    )


def require_valid_answer_letters(
    question: SimulatorQuestion,
    user_input: str,
) -> AnswerValidationResult:
    """
    Convenience wrapper for service code that wants exception behaviour.
    """
    result = validate_answer_letters(question, user_input)

    if not result.accepted:
        raise AnswerValidationError("; ".join(result.errors))

    return result
