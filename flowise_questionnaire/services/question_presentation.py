from __future__ import annotations

import string
from typing import Any

from flowise_questionnaire.models import NormalizedQuestion, QuestionnaireModule
from flowise_questionnaire.services.interview_simulator_contracts import (
    SelectionMode,
    SimulatorOption,
    SimulatorQuestion,
    build_fallback_assistant_message,
)


MULTIPLE_CHOICE_TYPES = {
    "multi_choice",
    "multiple_choice",
    "multi_select",
    "multiple_select",
    "checkbox",
    "checkboxes",
}

SINGLE_CHOICE_TYPES = {
    "single_choice",
    "single_select",
    "radio",
    "select_one",
}

NUMBER_TYPES = {
    "number",
    "numeric",
    "integer",
    "decimal",
    "float",
}

TEXT_TYPES = {
    "text",
    "string",
    "open",
    "open_text",
    "free_text",
    "textarea",
    "date",
    "datetime",
}


class QuestionPresentationError(Exception):
    """
    Raised when a question cannot be converted into a simulator prompt.
    """


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalise_question_type(question_type: Any) -> str:
    return _as_text(question_type).lower()


def _selection_mode_from_question(question: NormalizedQuestion) -> SelectionMode:
    question_type = _normalise_question_type(question.question_type)

    if question_type in MULTIPLE_CHOICE_TYPES:
        return "multiple"

    if question_type in SINGLE_CHOICE_TYPES:
        return "single"

    if question_type in NUMBER_TYPES:
        return "number"

    if question_type in TEXT_TYPES:
        return "text"

    # Conservative default.
    # Unknown closed-ended question types should not allow multiple answers
    # unless we explicitly know they are multi-select.
    return "single"


def _is_raw_entry_question(question: NormalizedQuestion) -> bool:
    return _selection_mode_from_question(question) in {"number", "text"}


def _letter_for_index(index: int) -> str:
    """
    Convert zero-based index to spreadsheet-style letters.

    Examples:
        0  -> A
        25 -> Z
        26 -> AA
        27 -> AB
    """
    if index < 0:
        raise ValueError("Option index cannot be negative.")

    alphabet = string.ascii_uppercase
    base = len(alphabet)

    result = ""
    number = index + 1

    while number:
        number, remainder = divmod(number - 1, base)
        result = alphabet[remainder] + result

    return result


def _is_missing_option(option: dict[str, Any]) -> bool:
    return bool(option.get("is_missing"))


def _clean_question_text(question: NormalizedQuestion) -> str:
    """
    Return respondent-facing question text.

    Prefer the real question text. Fall back to label/name only if necessary.
    This is important for numeric/internal-looking questions such as FISEQ,
    where `text` may be empty but `label` is usable for simulator testing.
    """
    text = _as_text(question.text)

    if text:
        return text

    label = _as_text(question.label)

    if label:
        return label

    name = _as_text(question.name)

    if name:
        return name

    raise QuestionPresentationError(
        f"Question {question.pk} has no text, label, or name."
    )


def _clean_option_label(option: dict[str, Any]) -> str:
    label = _as_text(option.get("label"))

    if label:
        return label

    code = _as_text(option.get("code"))

    if code:
        return code

    return "Unlabelled option"


def _build_simulator_options(
    question: NormalizedQuestion,
    *,
    include_missing_options: bool = False,
) -> list[SimulatorOption]:
    raw_options = question.options_json or []

    if not isinstance(raw_options, list):
        raise QuestionPresentationError(
            f"Question {question.name} has invalid options_json. Expected a list."
        )

    visible_options: list[dict[str, Any]] = []

    for option in raw_options:
        if not isinstance(option, dict):
            continue

        if _is_missing_option(option) and not include_missing_options:
            continue

        visible_options.append(option)

    if not visible_options:
        if _is_raw_entry_question(question):
            return []

        raise QuestionPresentationError(
            f"Question {question.name} has no respondent-facing options."
        )

    simulator_options: list[SimulatorOption] = []

    for index, option in enumerate(visible_options):
        code = option.get("code")

        if code is None or code == "":
            raise QuestionPresentationError(
                f"Question {question.name} has an option without a code."
            )

        simulator_options.append(
            SimulatorOption(
                letter=_letter_for_index(index),
                code=code,
                label=_clean_option_label(option),
                exclusive=bool(option.get("exclusive")),
            )
        )

    return simulator_options


def build_simulator_question(
    question: NormalizedQuestion,
    *,
    include_missing_options: bool = False,
) -> SimulatorQuestion:
    """
    Convert a NormalizedQuestion into respondent-facing simulator format.

    The returned object still includes `name` for backend/debug/graph use,
    but respondent-facing UI should display only text/options.
    """
    selection_mode = _selection_mode_from_question(question)

    return SimulatorQuestion(
        name=question.name,
        text=_clean_question_text(question),
        selection_mode=selection_mode,
        options=_build_simulator_options(
            question,
            include_missing_options=include_missing_options,
        ),
        help_text=_as_text(question.help_text),
    )


def build_simulator_question_for_module(
    module: QuestionnaireModule,
    question_name: str,
    *,
    include_missing_options: bool = False,
) -> SimulatorQuestion:
    """
    Load a question by name from a module and convert it into simulator format.
    """
    question_name = _as_text(question_name)

    if not question_name:
        raise QuestionPresentationError("question_name is required.")

    question = (
        module.normalized_questions
        .filter(name__iexact=question_name)
        .order_by("sequence_index")
        .first()
    )

    if question is None:
        raise QuestionPresentationError(
            f"Question {question_name} was not found in module {module.name}."
        )

    return build_simulator_question(
        question,
        include_missing_options=include_missing_options,
    )


def build_first_question_for_module(
    module: QuestionnaireModule,
    *,
    include_missing_options: bool = False,
) -> SimulatorQuestion:
    """
    Build the first question by extracted module sequence.

    Later, Step 29 will use semantic start detection/routing to choose the
    correct eligible start. For now this is a presentation helper only.
    """
    question = module.normalized_questions.order_by("sequence_index").first()

    if question is None:
        raise QuestionPresentationError(
            f"Module {module.name} has no normalized questions."
        )

    return build_simulator_question(
        question,
        include_missing_options=include_missing_options,
    )


def build_question_display_payload(
    question: NormalizedQuestion,
    *,
    include_missing_options: bool = False,
) -> dict[str, Any]:
    """
    Convenience helper for API/debugging.

    Returns both the structured simulator question and deterministic fallback
    assistant message.
    """
    simulator_question = build_simulator_question(
        question,
        include_missing_options=include_missing_options,
    )

    return {
        "question": simulator_question.to_dict(),
        "assistant_message": build_fallback_assistant_message(simulator_question),
        "letter_to_code": {
            option.letter: option.code
            for option in simulator_question.options
        },
    }
