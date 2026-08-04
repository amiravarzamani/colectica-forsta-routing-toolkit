from __future__ import annotations

import hashlib
import json
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache

from flowise_questionnaire.services.interview_simulator_contracts import (
    FlowiseQuestionRequest,
    FlowiseQuestionResponse,
    SimulatorQuestion,
    build_fallback_assistant_message,
)


class FlowiseInterviewWordingError(Exception):
    pass


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _flowise_base_url() -> str:
    base_url = _as_text(getattr(settings, "FLOWISE_BASE_URL", ""))

    if not base_url:
        raise FlowiseInterviewWordingError("FLOWISE_BASE_URL is not configured.")

    return base_url.rstrip("/")


def _flowise_wording_agentflow_id() -> str:
    agentflow_id = _as_text(
        getattr(settings, "FLOWISE_INTERVIEW_WORDING_AGENTFLOW_ID", "")
    )

    if not agentflow_id:
        raise FlowiseInterviewWordingError(
            "FLOWISE_INTERVIEW_WORDING_AGENTFLOW_ID is not configured."
        )

    return agentflow_id


def _flowise_timeout_seconds() -> int:
    return int(getattr(settings, "FLOWISE_TIMEOUT_SECONDS", 300))


def _flowise_wording_cache_enabled() -> bool:
    return bool(getattr(settings, "FLOWISE_INTERVIEW_WORDING_CACHE_ENABLED", True))


def _flowise_wording_cache_timeout_seconds() -> int:
    return int(
        getattr(
            settings,
            "FLOWISE_INTERVIEW_WORDING_CACHE_TIMEOUT_SECONDS",
            60 * 60 * 24,
        )
    )


def _cache_key_for_question(
    *,
    module_name: str,
    module_version: str,
    question: SimulatorQuestion,
) -> str:
    """
    Cache key is based on stable respondent-facing content.

    If question text/options/selection mode change, the hash changes.
    """
    payload = {
        "module_name": module_name,
        "module_version": module_version,
        "question_name": question.name,
        "question_text": question.text,
        "selection_mode": question.selection_mode,
        "help_text": question.help_text,
        "options": [
            {
                "letter": option.letter,
                "code": option.code,
                "label": option.label,
                "exclusive": option.exclusive,
            }
            for option in question.options
        ],
    }

    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")

    digest = hashlib.sha256(encoded).hexdigest()

    return f"flowise_interview_wording:{digest}"


def _extract_text_from_flowise_response(response_json: dict[str, Any]) -> str:
    """
    Support common Flowise response shapes.
    """
    for key in ("text", "answer", "output", "result"):
        value = response_json.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    if isinstance(response_json.get("json"), dict):
        nested = response_json["json"]

        for key in ("assistant_message", "message", "text"):
            value = nested.get(key)

            if isinstance(value, str) and value.strip():
                return value.strip()

    return ""


def _instruction_line_for_question(question: SimulatorQuestion) -> str:
    if question.selection_mode == "multiple":
        return "Choose all that apply. Answer using letters only, for example: A, C."

    return "Choose one option only. Answer using one letter only, for example: A."


def _message_contains_internal_variable(
    *,
    message: str,
    question: SimulatorQuestion,
) -> bool:
    question_name = _as_text(question.name)

    if not question_name:
        return False

    return question_name.lower() in message.lower()


def _message_contains_option_code(
    *,
    message: str,
    question: SimulatorQuestion,
) -> bool:
    """
    Guardrail against messages like:
        A. code 1 Yes
        option code: 3
    """
    lower_message = message.lower()

    for option in question.options:
        code = _as_text(option.code)

        if not code:
            continue

        suspicious_fragments = [
            f"code {code}".lower(),
            f"code: {code}".lower(),
            f"option code {code}".lower(),
            f"option code: {code}".lower(),
        ]

        if any(fragment in lower_message for fragment in suspicious_fragments):
            return True

    return False


def _message_has_required_option_letters(
    *,
    message: str,
    question: SimulatorQuestion,
) -> bool:
    """
    Flowise must preserve all A/B/C option markers.

    Accept:
        A. Yes
        A) Yes
    """
    upper_message = message.upper()

    for option in question.options:
        letter = _as_text(option.letter).upper()

        if not letter:
            continue

        if f"{letter}." not in upper_message and f"{letter})" not in upper_message:
            return False

    return True


def _message_has_question_text(
    *,
    message: str,
    question: SimulatorQuestion,
) -> bool:
    """
    Conservative check: require the beginning of the question text to appear.

    We avoid exact full-text matching because Flowise may normalize whitespace.
    """
    question_text = _as_text(question.text)

    if not question_text:
        return True

    question_start = question_text[:60].lower()
    message_lower = message.lower()

    return question_start in message_lower


def _message_has_instruction_line(
    *,
    message: str,
    question: SimulatorQuestion,
) -> bool:
    instruction = _instruction_line_for_question(question)
    return instruction.lower() in message.lower()


def _append_instruction_if_missing(
    *,
    message: str,
    question: SimulatorQuestion,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    instruction = _instruction_line_for_question(question)

    if _message_has_instruction_line(message=message, question=question):
        return message.strip(), warnings

    warnings.append("Flowise response omitted instruction line; Django appended it.")

    return f"{message.strip()}\n\n{instruction}".strip(), warnings


def _repair_flowise_message(
    *,
    question: SimulatorQuestion,
    assistant_message: str,
) -> tuple[str, list[str]]:
    """
    Repair safe minor omissions from Flowise output.

    Repair allowed:
    - missing final instruction line

    Repair not allowed:
    - missing question text
    - missing option letters
    - internal variable leakage
    - option code leakage

    For non-repairable failures, return deterministic fallback.
    """
    warnings: list[str] = []
    message = _as_text(assistant_message)

    if not message:
        warnings.append("Flowise response was empty; fallback used.")
        return build_fallback_assistant_message(question), warnings

    if _message_contains_internal_variable(message=message, question=question):
        warnings.append("Flowise response exposed internal question variable; fallback used.")
        return build_fallback_assistant_message(question), warnings

    if _message_contains_option_code(message=message, question=question):
        warnings.append("Flowise response exposed option code; fallback used.")
        return build_fallback_assistant_message(question), warnings

    if not _message_has_question_text(message=message, question=question):
        warnings.append("Flowise response omitted question text; fallback used.")
        return build_fallback_assistant_message(question), warnings

    if not _message_has_required_option_letters(message=message, question=question):
        warnings.append("Flowise response omitted one or more option letters; fallback used.")
        return build_fallback_assistant_message(question), warnings

    repaired_message, repair_warnings = _append_instruction_if_missing(
        message=message,
        question=question,
    )
    warnings.extend(repair_warnings)

    return repaired_message, warnings


def _build_flowise_prompt(request_payload: dict[str, Any]) -> str:
    return (
        "You are formatting one questionnaire interview question.\n\n"
        "STRICT RULES:\n"
        "1. Ask only the provided real question text.\n"
        "2. Do not expose internal variable names.\n"
        "3. Do not invent, remove, reorder, or rename options.\n"
        "4. Preserve the exact A/B/C option letters.\n"
        "5. Do not validate answers.\n"
        "6. Do not choose the next question.\n"
        "7. Do not mention routing.\n"
        "8. Do not mention option codes.\n"
        "9. Return only the respondent-facing assistant message.\n\n"
        "Required output shape:\n\n"
        "<question text>\n\n"
        "A. <option label>\n"
        "B. <option label>\n\n"
        "<instruction line>\n\n"
        "If the question is single-answer, use this exact instruction line:\n"
        "Choose one option only. Answer using one letter only, for example: A.\n\n"
        "If the question is multiple-answer, use this exact instruction line:\n"
        "Choose all that apply. Answer using letters only, for example: A, C.\n\n"
        "Payload:\n"
        f"{json.dumps(request_payload, ensure_ascii=False, indent=2)}"
    )


def ask_flowise_to_format_question(
    *,
    module_name: str,
    module_version: str,
    question: SimulatorQuestion,
    previous_answers: dict[str, Any] | None = None,
    routing_context: dict[str, Any] | None = None,
) -> FlowiseQuestionResponse:
    request = FlowiseQuestionRequest(
        module_name=module_name,
        module_version=module_version,
        question=question,
        previous_answers=previous_answers or {},
        routing_context=routing_context or {},
    )

    request_payload = request.to_dict()
    prompt = _build_flowise_prompt(request_payload)

    url = f"{_flowise_base_url()}/api/v1/prediction/{_flowise_wording_agentflow_id()}"

    try:
        http_response = requests.post(
            url,
            json={"question": prompt},
            timeout=_flowise_timeout_seconds(),
        )
    except requests.RequestException as exc:
        raise FlowiseInterviewWordingError(str(exc)) from exc

    if http_response.status_code >= 400:
        raise FlowiseInterviewWordingError(
            f"Flowise returned HTTP {http_response.status_code}: "
            f"{http_response.text[:500]}"
        )

    try:
        response_json = http_response.json()
    except ValueError as exc:
        raise FlowiseInterviewWordingError(
            "Flowise returned a non-JSON response."
        ) from exc

    raw_message = _extract_text_from_flowise_response(response_json)

    if not raw_message:
        raise FlowiseInterviewWordingError(
            "Flowise response did not contain assistant message text."
        )

    assistant_message, repair_warnings = _repair_flowise_message(
        question=question,
        assistant_message=raw_message,
    )

    return FlowiseQuestionResponse(
        assistant_message=assistant_message,
        raw_response=response_json,
        warnings=repair_warnings,
    )


def build_interview_assistant_message(
    *,
    module_name: str,
    module_version: str,
    question: SimulatorQuestion,
    previous_answers: dict[str, Any] | None = None,
    routing_context: dict[str, Any] | None = None,
) -> FlowiseQuestionResponse:
    if not getattr(settings, "FLOWISE_INTERVIEW_WORDING_ENABLED", False):
        return FlowiseQuestionResponse(
            assistant_message=build_fallback_assistant_message(question),
            raw_response={},
            warnings=["Flowise interview wording is disabled."],
        )

    cache_key = _cache_key_for_question(
        module_name=module_name,
        module_version=module_version,
        question=question,
    )

    if _flowise_wording_cache_enabled():
        cached_response = cache.get(cache_key)

        if cached_response:
            return FlowiseQuestionResponse(
                assistant_message=cached_response["assistant_message"],
                raw_response=cached_response.get("raw_response") or {},
                warnings=[
                    "Flowise interview wording cache hit.",
                    *(cached_response.get("warnings") or []),
                ],
            )

    try:
        response = ask_flowise_to_format_question(
            module_name=module_name,
            module_version=module_version,
            question=question,
            previous_answers=previous_answers,
            routing_context=routing_context,
        )

        if _flowise_wording_cache_enabled():
            cache.set(
                cache_key,
                {
                    "assistant_message": response.assistant_message,
                    "raw_response": response.raw_response,
                    "warnings": response.warnings,
                },
                timeout=_flowise_wording_cache_timeout_seconds(),
            )

        return response

    except FlowiseInterviewWordingError as exc:
        return FlowiseQuestionResponse(
            assistant_message=build_fallback_assistant_message(question),
            raw_response={},
            warnings=[f"Flowise interview wording fallback used: {exc}"],
        )
