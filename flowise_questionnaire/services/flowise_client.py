from __future__ import annotations

import json
from typing import Any

import requests
from django.conf import settings

from flowise_questionnaire.models import QuestionnaireModule
from flowise_questionnaire.services.agentflow_payload_builder import (
    build_compact_agentflow_payload_for_module,
)


class FlowiseClientError(RuntimeError):
    pass


def run_module_review_agentflow(module: QuestionnaireModule) -> dict[str, Any]:
    """
    Send an ultra-compact questionnaire review payload to Flowise.

    Important:
    - Django builds deterministic schema/routing/coverage data.
    - Flowise only reviews it.
    - Django post-validates the Flowise review before returning it.
    """

    endpoint = _build_module_review_endpoint()

    compact_payload = build_compact_agentflow_payload_for_module(module)
    flowise_payload = _build_flowise_review_payload(compact_payload)

    request_body = {
        "form": {
            "payload": json.dumps(flowise_payload, ensure_ascii=False),
        },
        "streaming": False,
    }

    try:
        response = requests.post(
            endpoint,
            json=request_body,
            timeout=settings.FLOWISE_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise FlowiseClientError(f"Failed to call Flowise: {exc}") from exc

    if response.status_code >= 400:
        raise FlowiseClientError(
            f"Flowise returned HTTP {response.status_code}: {response.text[:1000]}"
        )

    try:
        response_json = response.json()
    except ValueError as exc:
        raise FlowiseClientError(
            f"Flowise returned non-JSON response: {response.text[:1000]}"
        ) from exc

    raw_text = _extract_response_text(response_json)

    try:
        parsed_review = json.loads(raw_text)
    except ValueError as exc:
        raise FlowiseClientError(
            f"Flowise response text was not valid JSON: {raw_text[:1000]}"
        ) from exc

    validation = _validate_flowise_review_against_payload(
        review=parsed_review,
        flowise_payload=flowise_payload,
    )

    return {
        "flowise_endpoint": endpoint,
        "request_payload": flowise_payload,
        "source_compact_payload": compact_payload,
        "raw_response": response_json,
        "review": parsed_review,
        "post_validation": validation,
    }


def _build_module_review_endpoint() -> str:
    base_url = getattr(settings, "FLOWISE_BASE_URL", "").strip()
    agentflow_id = getattr(settings, "FLOWISE_MODULE_REVIEW_AGENTFLOW_ID", "").strip()

    if not base_url:
        raise FlowiseClientError("FLOWISE_BASE_URL is not configured.")

    if not agentflow_id:
        raise FlowiseClientError("FLOWISE_MODULE_REVIEW_AGENTFLOW_ID is not configured.")

    return f"{base_url.rstrip('/')}/api/v1/prediction/{agentflow_id}"


def _build_flowise_review_payload(compact_payload: dict[str, Any]) -> dict[str, Any]:
    """
    Build a smaller Flowise-specific payload.

    The full compact payload is still useful for UI/debugging, but Flowise review
    does not need option previews, sequential edges, loop edges, graph details,
    or long labels. Reducing the payload mitigates timeout risk.
    """

    schema_summary = compact_payload.get("schema_summary") or {}
    routing_summary = compact_payload.get("routing_summary") or {}
    coverage_intents = compact_payload.get("coverage_intents") or {}

    questions = schema_summary.get("questions") or []
    conditional_edges = routing_summary.get("conditional_edges") or []
    intents = coverage_intents.get("intents") or []

    return {
        "payload_version": compact_payload.get("payload_version"),
        "payload_type": "flowise_review",
        "module": compact_payload.get("module") or {},
        "schema_summary": {
            "question_count": schema_summary.get("question_count", 0),
            "question_names": [
                question.get("name")
                for question in questions
                if isinstance(question, dict) and question.get("name")
            ],
            "question_types": {
                question.get("name"): question.get("question_type")
                for question in questions
                if isinstance(question, dict) and question.get("name")
            },
        },
        "routing_summary": {
            "conditional_edge_count": routing_summary.get("conditional_edge_count", 0),
            "external_variables": routing_summary.get("external_variables") or [],
            "conditional_edges": [
                {
                    "index": index,
                    "source_question": edge.get("source_question"),
                    "target_question": edge.get("target_question"),
                    "condition_text": edge.get("condition_text"),
                }
                for index, edge in enumerate(conditional_edges)
                if isinstance(edge, dict)
            ],
        },
        "coverage_intents": {
            "intent_count": coverage_intents.get("intent_count", 0),
            "covered_conditional_edges": coverage_intents.get(
                "covered_conditional_edges",
                [],
            ),
            "possibly_uncovered_conditional_edges": coverage_intents.get(
                "possibly_uncovered_conditional_edges",
                [],
            ),
            "intents": [
                {
                    "name": intent.get("name"),
                    "covers_edges": intent.get("covers_edges") or [],
                    "source_question": intent.get("source_question"),
                    "target_question": intent.get("target_question"),
                    "condition_text": intent.get("condition_text"),
                    "required_external_context": intent.get("required_external_context") or {},
                    "seed_answers": intent.get("seed_answers") or {},
                    "notes": intent.get("notes") or [],
                }
                for intent in intents
                if isinstance(intent, dict)
            ],
        },
        "instructions": {
            "role": "advisory_review_only",
            "rules": [
                "Review coverage_intents only.",
                "Do not create new test cases.",
                "Do not rewrite seed_answers.",
                "Copy coverage_intents.covered_conditional_edges exactly.",
                "Copy coverage_intents.possibly_uncovered_conditional_edges exactly.",
                "Do not produce expected_active_questions.",
                "Do not produce expected_skipped_questions.",
            ],
        },
    }


def _extract_response_text(response_json: dict[str, Any]) -> str:
    """
    Extract useful LLM output from Flowise response.
    """

    text = response_json.get("text")

    if isinstance(text, str) and text.strip():
        return text.strip()

    executed_data = response_json.get("agentFlowExecutedData") or []

    if isinstance(executed_data, list):
        for node in reversed(executed_data):
            data = node.get("data") or {}
            output = data.get("output") or {}
            content = output.get("content")

            if isinstance(content, str) and content.strip():
                return content.strip()

    raise FlowiseClientError(
        "Could not find Flowise response text in response JSON."
    )


def _validate_flowise_review_against_payload(
    review: dict[str, Any],
    flowise_payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Deterministically validate Flowise's advisory review.

    This prevents Django from trusting hallucinated or malformed advisory output.
    """

    errors: list[str] = []
    warnings: list[str] = []

    expected_question_names = (
        flowise_payload.get("schema_summary", {}).get("question_names") or []
    )
    expected_external_variables = (
        flowise_payload.get("routing_summary", {}).get("external_variables") or []
    )
    expected_conditional_edge_count = (
        flowise_payload.get("routing_summary", {}).get("conditional_edge_count")
    )
    expected_intent_count = (
        flowise_payload.get("coverage_intents", {}).get("intent_count")
    )
    expected_covered_edges = (
        flowise_payload.get("coverage_intents", {}).get("covered_conditional_edges") or []
    )
    expected_uncovered_edges = (
        flowise_payload.get("coverage_intents", {}).get("possibly_uncovered_conditional_edges") or []
    )

    schema_review = review.get("schema_review") or {}
    routing_review = review.get("routing_review") or {}
    coverage_review = review.get("coverage_intent_review") or {}

    actual_question_names = schema_review.get("real_question_names") or []
    actual_external_variables = routing_review.get("external_variables") or []
    actual_conditional_edge_count = routing_review.get("conditional_edge_count")
    actual_intent_count = coverage_review.get("intent_count")
    actual_covered_edges = coverage_review.get("covered_conditional_edges") or []
    actual_uncovered_edges = coverage_review.get("possibly_uncovered_conditional_edges") or []

    if actual_question_names != expected_question_names:
        errors.append(
            "schema_review.real_question_names does not exactly match Django question_names."
        )

    if actual_external_variables != expected_external_variables:
        errors.append(
            "routing_review.external_variables does not exactly match Django external_variables."
        )

    if actual_conditional_edge_count != expected_conditional_edge_count:
        errors.append(
            "routing_review.conditional_edge_count does not match Django conditional_edge_count."
        )

    if actual_intent_count != expected_intent_count:
        errors.append(
            "coverage_intent_review.intent_count does not match Django intent_count."
        )

    if actual_covered_edges != expected_covered_edges:
        errors.append(
            "coverage_intent_review.covered_conditional_edges does not exactly match Django covered_conditional_edges."
        )

    if actual_uncovered_edges != expected_uncovered_edges:
        errors.append(
            "coverage_intent_review.possibly_uncovered_conditional_edges does not exactly match Django possibly_uncovered_conditional_edges."
        )

    for forbidden_key in ("expected_active_questions", "expected_skipped_questions"):
        forbidden_paths = _find_key_paths(review, forbidden_key)

        if forbidden_paths:
            errors.append(
                f"Flowise review contains forbidden key {forbidden_key}: {forbidden_paths}"
            )

    suggested_improvements = review.get("suggested_improvements")

    if suggested_improvements is None:
        warnings.append("Flowise review omitted suggested_improvements.")

    if isinstance(suggested_improvements, list):
        for item in suggested_improvements:
            if not isinstance(item, dict):
                continue

            if not item.get("type") and not item.get("message") and not item.get("related_edge_indexes"):
                warnings.append("Flowise returned empty placeholder suggested_improvements item.")

    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "expected": {
            "question_names": expected_question_names,
            "external_variables": expected_external_variables,
            "conditional_edge_count": expected_conditional_edge_count,
            "intent_count": expected_intent_count,
            "covered_conditional_edges": expected_covered_edges,
            "possibly_uncovered_conditional_edges": expected_uncovered_edges,
        },
        "actual": {
            "question_names": actual_question_names,
            "external_variables": actual_external_variables,
            "conditional_edge_count": actual_conditional_edge_count,
            "intent_count": actual_intent_count,
            "covered_conditional_edges": actual_covered_edges,
            "possibly_uncovered_conditional_edges": actual_uncovered_edges,
        },
    }

def _find_key_paths(value: Any, target_key: str, prefix: str = "") -> list[str]:
    """
    Find exact dictionary-key matches recursively.

    This avoids false positives from keys like:
        contains_no_expected_active_questions

    We only want to reject an exact key:
        expected_active_questions
    """

    matches: list[str] = []

    if isinstance(value, dict):
        for key, child_value in value.items():
            current_path = f"{prefix}.{key}" if prefix else str(key)

            if key == target_key:
                matches.append(current_path)

            matches.extend(
                _find_key_paths(
                    value=child_value,
                    target_key=target_key,
                    prefix=current_path,
                )
            )

    elif isinstance(value, list):
        for index, item in enumerate(value):
            current_path = f"{prefix}[{index}]" if prefix else f"[{index}]"

            matches.extend(
                _find_key_paths(
                    value=item,
                    target_key=target_key,
                    prefix=current_path,
                )
            )

    return matches