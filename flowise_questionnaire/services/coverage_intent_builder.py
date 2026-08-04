from __future__ import annotations

import re
from typing import Any

from flowise_questionnaire.models import NormalizedQuestion, QuestionnaireModule, RoutingEdge


def build_coverage_intents_for_module(module: QuestionnaireModule) -> list[dict[str, Any]]:
    """
    Build deterministic route coverage input intents from conditional routing edges.

    Important:
    - This does not simulate the questionnaire.
    - This does not decide active/skipped questions.
    - It only creates seed inputs that can later be sent to the deterministic simulator.
    """

    questions = list(
        NormalizedQuestion.objects.filter(module=module).order_by("sequence_index")
    )

    question_names = {question.name for question in questions}

    conditional_edges = list(
        RoutingEdge.objects.filter(
            module=module,
            edge_type=RoutingEdge.EdgeType.CONDITIONAL,
        ).order_by("sequence_index")
    )

    intents: list[dict[str, Any]] = []

    for zero_based_index, edge in enumerate(conditional_edges):
        source_names = _split_source_names(edge.source_question)
        condition_text = edge.condition_text or ""

        required_external_context: dict[str, Any] = {}
        seed_answers: dict[str, Any] = {}

        for source_name in source_names:
            value = _infer_value_for_source(
                source_name=source_name,
                condition_text=condition_text,
            )

            if source_name in question_names:
                seed_answers[source_name] = value
            else:
                required_external_context[source_name] = value

        # Add obvious prerequisite answers for common dependent conditions.
        # This remains generic because it is derived from existing conditional edges.
        prerequisite_answers = _infer_prerequisite_answers(
            target_source_names=source_names,
            conditional_edges=conditional_edges,
            question_names=question_names,
        )

        for name, value in prerequisite_answers.items():
            seed_answers.setdefault(name, value)

        intents.append(
            {
                "name": f"Trigger {edge.target_question}",
                "purpose": f"Trigger conditional edge to {edge.target_question}.",
                "covers_edges": [zero_based_index],
                "source_question": edge.source_question,
                "target_question": edge.target_question,
                "condition_text": condition_text,
                "required_external_context": required_external_context,
                "seed_answers": seed_answers,
                "notes": _notes_for_condition(condition_text),
            }
        )

    intents.extend(
        _build_or_split_intents(
            conditional_edges=conditional_edges,
            question_names=question_names,
            existing_count=len(intents),
        )
    )

    # Do not generate a generic negative route intent here.
    # A safe negative-path generator needs condition inversion, because values
    # like "2" may trigger valid conditions in modules such as benefits_w18:
    # FRVAL > 0, FRJT = 2, FICODE IN 2,3,4,...
    return _deduplicate_intents(intents)[:10]


def _split_source_names(source_question: str) -> list[str]:
    return [
        item.strip()
        for item in (source_question or "").split(",")
        if item.strip()
    ]


def _infer_value_for_source(source_name: str, condition_text: str) -> Any:
    """
    Infer a simple satisfying value for one source variable/question.

    This is intentionally conservative. The deterministic simulator/validator
    will later determine whether the suggested value truly works.
    """

    condition = condition_text or ""

    escaped_source = re.escape(source_name)

    # source = 1
    equality_match = re.search(
        rf"\b{escaped_source}\b\s*=\s*([A-Za-z0-9_-]+)",
        condition,
        flags=re.IGNORECASE,
    )
    if equality_match:
        return str(equality_match.group(1))

    # source <> 4
    not_equal_match = re.search(
        rf"\b{escaped_source}\b\s*<>\s*([A-Za-z0-9_-]+)",
        condition,
        flags=re.IGNORECASE,
    )
    if not_equal_match:
        forbidden = str(not_equal_match.group(1))
        if forbidden != "1":
            return "1"
        return "2"

    # source > 1
    greater_than_match = re.search(
        rf"\b{escaped_source}\b\s*>\s*(-?\d+)",
        condition,
        flags=re.IGNORECASE,
    )
    if greater_than_match:
        threshold = int(greater_than_match.group(1))
        return threshold + 1

    # source >= 0
    greater_equal_match = re.search(
        rf"\b{escaped_source}\b\s*>=\s*(-?\d+)",
        condition,
        flags=re.IGNORECASE,
    )
    if greater_equal_match:
        return int(greater_equal_match.group(1))

    # source IN 1,2,3
    in_match = re.search(
        rf"\b{escaped_source}\b\s+IN\s+([0-9,\s-]+)",
        condition,
        flags=re.IGNORECASE,
    )
    if in_match:
        values = [
            item.strip()
            for item in in_match.group(1).split(",")
            if item.strip()
        ]
        if values:
            return str(values[0])

    # Special case: external arithmetic expression containing source.
    # Example: (HHGRID.HHSIZE - Number of absent hhold members) > 1
    if source_name in condition and ">" in condition:
        return "2"

    return "1"


def _infer_prerequisite_answers(
    target_source_names: list[str],
    conditional_edges: list[RoutingEdge],
    question_names: set[str],
) -> dict[str, Any]:
    """
    If a source question is itself conditionally activated, include the
    obvious answer needed to activate it.

    Example:
    NAIDXHH is used by condition NAIDXHH > 1.
    NAIDXHH is activated by AIDXHH = 1.
    Therefore include AIDXHH = 1.
    """

    prerequisites: dict[str, Any] = {}

    for source_name in target_source_names:
        if source_name not in question_names:
            continue

        for edge in conditional_edges:
            if edge.target_question != source_name:
                continue

            upstream_sources = _split_source_names(edge.source_question)

            for upstream_source in upstream_sources:
                if upstream_source not in question_names:
                    continue

                prerequisites[upstream_source] = _infer_value_for_source(
                    source_name=upstream_source,
                    condition_text=edge.condition_text or "",
                )

    return prerequisites


def _build_or_split_intents(
    conditional_edges: list[RoutingEdge],
    question_names: set[str],
    existing_count: int,
) -> list[dict[str, Any]]:
    """
    Create separate advisory intents for OR conditions when possible.
    """

    intents: list[dict[str, Any]] = []

    for zero_based_index, edge in enumerate(conditional_edges):
        condition_text = edge.condition_text or ""

        if " OR " not in condition_text.upper():
            continue

        source_names = _split_source_names(edge.source_question)

        for source_name in source_names:
            if source_name not in question_names:
                continue

            seed_answers = {
                source_name: _infer_value_for_source(
                    source_name=source_name,
                    condition_text=condition_text,
                )
            }

            intents.append(
                {
                    "name": f"Trigger {edge.target_question} via {source_name}",
                    "purpose": f"Trigger OR-condition route to {edge.target_question} using {source_name}.",
                    "covers_edges": [zero_based_index],
                    "source_question": edge.source_question,
                    "target_question": edge.target_question,
                    "condition_text": condition_text,
                    "required_external_context": {},
                    "seed_answers": seed_answers,
                    "notes": [
                        "OR-condition split intent. Django simulator should verify the actual active path."
                    ],
                }
            )

    return intents


def _build_negative_intent(
    conditional_edges: list[RoutingEdge],
    question_names: set[str],
) -> dict[str, Any] | None:
    """
    Build one generic negative path intent using no/false answers where obvious.

    Currently not used by build_coverage_intents_for_module because a generic
    negative intent is unsafe across modules. A safe version needs condition
    inversion logic.
    """

    seed_answers: dict[str, Any] = {}
    required_external_context: dict[str, Any] = {}

    for edge in conditional_edges:
        source_names = _split_source_names(edge.source_question)

        for source_name in source_names:
            if source_name in question_names:
                # For survey coded yes/no questions, "2" is commonly No.
                seed_answers.setdefault(source_name, "2")
            else:
                required_external_context.setdefault(source_name, "1")

    if not seed_answers and not required_external_context:
        return None

    return {
        "name": "Negative route intent",
        "purpose": "Provide no/low values intended not to trigger main conditional routes.",
        "covers_edges": [],
        "source_question": "",
        "target_question": "",
        "condition_text": "",
        "required_external_context": required_external_context,
        "seed_answers": seed_answers,
        "notes": [
            "Negative intent only. It should not be counted as covering conditional edges.",
            "Django deterministic simulator must determine actual skipped/active questions.",
        ],
    }


def _notes_for_condition(condition_text: str) -> list[str]:
    notes: list[str] = []

    if "<>" in condition_text:
        notes.append("Contains not-equal condition. Suggested value is an assumed satisfying value.")

    if " OR " in condition_text.upper():
        notes.append("Contains OR condition. Separate OR-side intents may also be generated.")

    if "Number of absent hhold members" in condition_text:
        notes.append(
            "Condition contains derived household expression. Suggested HHGRID.HHSIZE value assumes absent household members is 0."
        )

    return notes


def _deduplicate_intents(intents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []

    for intent in intents:
        key = (
            str(intent.get("covers_edges", [])),
            str(sorted((intent.get("required_external_context") or {}).items())),
            str(sorted((intent.get("seed_answers") or {}).items())),
        )

        if key in seen:
            continue

        seen.add(key)
        deduped.append(intent)

    return deduped