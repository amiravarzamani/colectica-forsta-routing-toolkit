from __future__ import annotations

from typing import Any

from flowise_questionnaire.models import (
    NormalizedQuestion,
    QuestionnaireGraph,
    QuestionnaireModule,
    RoutingEdge,
)
from flowise_questionnaire.services.coverage_intent_builder import (
    build_coverage_intents_for_module,
)

def build_agentflow_payload_for_module(module: QuestionnaireModule) -> dict[str, Any]:
    """
    Build a detailed payload for debugging/inspection.

    This payload is useful in the browser, but can be too large for local LLM calls
    because it includes option previews with category identifiers.
    """

    questions = list(
        NormalizedQuestion.objects.filter(module=module).order_by("sequence_index")
    )

    routing_edges = list(
        RoutingEdge.objects.filter(module=module).order_by("sequence_index")
    )

    graph = QuestionnaireGraph.objects.filter(module=module).first()

    conditional_edges = [
        _serialize_routing_edge(edge)
        for edge in routing_edges
        if edge.edge_type == RoutingEdge.EdgeType.CONDITIONAL
    ]

    loop_edges = [
        _serialize_routing_edge(edge)
        for edge in routing_edges
        if edge.edge_type == RoutingEdge.EdgeType.LOOP
    ]

    sequential_edges = [
        _serialize_routing_edge(edge)
        for edge in routing_edges
        if edge.edge_type == RoutingEdge.EdgeType.SEQUENTIAL
    ]

    external_variables = _extract_external_variables(
        questions=questions,
        routing_edges=conditional_edges,
    )

    return {
        "payload_version": "1.0",
        "payload_type": "full",
        "module": {
            "id": str(module.id),
            "name": module.name,
            "version": module.version,
            "processing_status": module.processing_status,
            "created_at": module.created_at.isoformat() if module.created_at else None,
            "updated_at": module.updated_at.isoformat() if module.updated_at else None,
        },
        "schema_summary": {
            "question_count": len(questions),
            "questions": [
                _serialize_question(question)
                for question in questions
            ],
        },
        "routing_summary": {
            "routing_edge_count": len(routing_edges),
            "conditional_edge_count": len(conditional_edges),
            "loop_edge_count": len(loop_edges),
            "sequential_edge_count": len(sequential_edges),
            "conditional_edges": conditional_edges,
            "loop_edges": loop_edges,
            "sequential_edges": sequential_edges,
            "external_variables": external_variables,
        },
        "graph_summary": {
            "graph_exists": graph is not None,
            "node_count": len(graph.nodes_json or []) if graph else 0,
            "edge_count": len(graph.edges_json or []) if graph else 0,
        },
        "agent_tasks": _agent_tasks(),
        "instructions": _agent_instructions(),
    }


def build_compact_agentflow_payload_for_module(module: QuestionnaireModule) -> dict[str, Any]:
    """
    Build a compact payload for Flowise runtime.

    This is intentionally smaller than build_agentflow_payload_for_module().
    It removes raw identifiers and limits options to code/label/missing/exclusive only.
    """

    questions = list(
        NormalizedQuestion.objects.filter(module=module).order_by("sequence_index")
    )

    routing_edges = list(
        RoutingEdge.objects.filter(module=module).order_by("sequence_index")
    )

    graph = QuestionnaireGraph.objects.filter(module=module).first()

    conditional_edges = [
        _serialize_compact_routing_edge(edge)
        for edge in routing_edges
        if edge.edge_type == RoutingEdge.EdgeType.CONDITIONAL
    ]

    loop_edges = [
        _serialize_compact_routing_edge(edge)
        for edge in routing_edges
        if edge.edge_type == RoutingEdge.EdgeType.LOOP
    ]

    sequential_edges = [
        _serialize_compact_routing_edge(edge)
        for edge in routing_edges
        if edge.edge_type == RoutingEdge.EdgeType.SEQUENTIAL
    ]

    external_variables = _extract_external_variables(
        questions=questions,
        routing_edges=conditional_edges,
    )

    coverage_intents = build_coverage_intents_for_module(module)
    coverage_summary = _build_coverage_intent_summary(
        coverage_intents=coverage_intents,
        conditional_edge_count=len(conditional_edges),
    )

    return {
        "payload_version": "1.0",
        "payload_type": "compact",
        "module": {
            "id": str(module.id),
            "name": module.name,
            "version": module.version,
            "processing_status": module.processing_status,
        },
        "schema_summary": {
            "question_count": len(questions),
            "questions": [
                _serialize_compact_question(question)
                for question in questions
            ],
        },
        "routing_summary": {
            "routing_edge_count": len(routing_edges),
            "conditional_edge_count": len(conditional_edges),
            "loop_edge_count": len(loop_edges),
            "sequential_edge_count": len(sequential_edges),
            "conditional_edges": conditional_edges,
            "loop_edges": loop_edges,
            "sequential_edges": sequential_edges,
            "external_variables": external_variables,
        },
        "coverage_intents": {
            "intent_count": len(coverage_intents),
            "covered_conditional_edges": coverage_summary["covered_conditional_edges"],
            "possibly_uncovered_conditional_edges": coverage_summary["possibly_uncovered_conditional_edges"],
            "intents": coverage_intents,
        },
        "graph_summary": {
            "graph_exists": graph is not None,
            "node_count": len(graph.nodes_json or []) if graph else 0,
            "edge_count": len(graph.edges_json or []) if graph else 0,
        },
        "agent_tasks": [
            "review_schema",
            "analyse_routing",
            "plan_route_coverage_tests",
            "suggest_synthetic_profiles",
        ],
        "instructions": {
            "role": "advisory_only",
            "rules": [
                "Use coverage_intents.intents as the primary test plan.",
                "Do not create route coverage tests from scratch.",
                "Use only question names from schema_summary.questions[*].name.",
                "Use only external variables from routing_summary.external_variables.",
                "Do not invent question names.",
                "Do not invent external variables.",
                "Do not modify routing logic.",
                "Do not generate deterministic simulator results.",
            ],
            "expected_output_keys": [
                "schema_review",
                "routing_review",
                "test_plan",
                "synthetic_profiles",
                "validation_self_check",
            ],
        },
    }


def _serialize_question(question: NormalizedQuestion) -> dict[str, Any]:
    options = question.options_json or []

    return {
        "sequence_index": question.sequence_index,
        "name": question.name,
        "label": question.label,
        "question_type": question.question_type,
        "option_count": len(options),
        "options_preview": options[:20],
    }


def _serialize_compact_question(question: NormalizedQuestion) -> dict[str, Any]:
    options = question.options_json or []

    compact_options = [
        {
            "code": option.get("code"),
            "label": option.get("label"),
            "is_missing": option.get("is_missing", False),
            "exclusive": option.get("exclusive", False),
        }
        for option in options[:8]
        if isinstance(option, dict)
    ]

    return {
        "sequence_index": question.sequence_index,
        "name": question.name,
        "label": question.label,
        "question_type": question.question_type,
        "option_count": len(options),
        "options_preview": compact_options,
    }


def _serialize_routing_edge(edge: RoutingEdge) -> dict[str, Any]:
    return {
        "sequence_index": edge.sequence_index,
        "source_question": edge.source_question,
        "target_question": edge.target_question,
        "edge_type": edge.edge_type,
        "condition_text": edge.condition_text,
    }


def _serialize_compact_routing_edge(edge: RoutingEdge) -> dict[str, Any]:
    return {
        "source_question": edge.source_question,
        "target_question": edge.target_question,
        "edge_type": edge.edge_type,
        "condition_text": edge.condition_text,
    }


def _extract_external_variables(
    questions: list[NormalizedQuestion],
    routing_edges: list[Any],
) -> list[str]:
    """
    Extract external variables only from conditional routing edges.

    Loop container nodes such as MISSOURCELoop, NFHLoop, and FRVALLoop
    are not external variables and must not be sent to Flowise as external
    dependencies.
    """

    question_names = {
        question.name
        for question in questions
    }

    external_variables: list[str] = []

    for edge in routing_edges:
        if isinstance(edge, dict):
            source_question = edge.get("source_question") or ""
            edge_type = edge.get("edge_type") or ""
        else:
            source_question = edge.source_question or ""
            edge_type = edge.edge_type or ""

        if edge_type != RoutingEdge.EdgeType.CONDITIONAL:
            continue

        source_names = [
            item.strip()
            for item in source_question.split(",")
            if item.strip()
        ]

        for source_name in source_names:
            if source_name in question_names:
                continue

            if source_name.endswith("Loop"):
                continue

            if source_name not in external_variables:
                external_variables.append(source_name)

    return external_variables


def _agent_tasks() -> list[dict[str, str]]:
    return [
        {
            "name": "review_schema",
            "description": "Review extracted questionnaire schema for missing labels, unusual question types, duplicated question names, or suspicious option structures.",
        },
        {
            "name": "analyse_routing",
            "description": "Analyse conditional, loop, and sequential routing edges and identify potential routing risks or unreachable branches.",
        },
        {
            "name": "plan_route_coverage_tests",
            "description": "Suggest test scenarios that cover conditional branches, loops, external variables, skipped questions, and edge cases.",
        },
        {
            "name": "suggest_synthetic_profiles",
            "description": "Propose synthetic respondent profiles that can trigger important questionnaire routes.",
        },
    ]


def _agent_instructions() -> dict[str, Any]:
    return {
        "flowise_role": "Advisory multi-agent analysis only.",
        "do_not_do": [
            "Do not treat LLM output as routing authority.",
            "Do not modify routing logic.",
            "Do not invent question names that are not present in schema_summary.questions.",
            "Do not generate final simulator results. Django deterministic simulator will do that later.",
        ],
        "expected_output": {
            "schema_review": "List schema observations and warnings.",
            "routing_review": "List route risks, complex branches, external dependencies, and possible unreachable paths.",
            "test_plan": "List recommended route coverage tests.",
            "synthetic_profiles": "List synthetic profile specifications with intended route targets.",
        },
    }

def _build_coverage_intent_summary(
    coverage_intents: list[dict[str, Any]],
    conditional_edge_count: int,
) -> dict[str, list[int]]:
    covered_edges: set[int] = set()

    for intent in coverage_intents:
        covers_edges = intent.get("covers_edges") or []

        for edge_index in covers_edges:
            if isinstance(edge_index, int):
                covered_edges.add(edge_index)
                continue

            if isinstance(edge_index, str) and edge_index.isdigit():
                covered_edges.add(int(edge_index))

    valid_covered_edges = sorted(
        edge_index
        for edge_index in covered_edges
        if 0 <= edge_index < conditional_edge_count
    )

    all_edges = set(range(conditional_edge_count))

    possibly_uncovered_edges = sorted(
        all_edges - set(valid_covered_edges)
    )

    return {
        "covered_conditional_edges": valid_covered_edges,
        "possibly_uncovered_conditional_edges": possibly_uncovered_edges,
    }