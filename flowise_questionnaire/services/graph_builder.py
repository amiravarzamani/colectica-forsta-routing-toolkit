from __future__ import annotations

from typing import Any

from django.db import transaction

from flowise_questionnaire.models import (
    NormalizedQuestion,
    QuestionnaireGraph,
    QuestionnaireModule,
    RoutingEdge,
)
from flowise_questionnaire.services.graph_enrichment import build_mermaid_text


def build_graph_for_module(module: QuestionnaireModule) -> dict[str, Any]:
    """
    Builds graph-ready data from extracted questions and routing edges.

    First version:
    - question nodes from NormalizedQuestion
    - loop nodes from RoutingEdge where edge_type='loop'
    - conditional and loop edges from RoutingEdge
    - Mermaid text for quick visual preview
    """

    questions = list(
        NormalizedQuestion.objects.filter(module=module).order_by("sequence_index")
    )
    routing_edges = list(
        RoutingEdge.objects.filter(module=module).order_by("sequence_index")
    )

    question_names = {question.name for question in questions}

    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()

    for question in questions:
        node = {
            "id": question.name,
            "type": "question",
            "data": {
                "label": question.name,
                "title": question.label,
                "question_type": question.question_type,
                "option_count": len(question.options_json or []),
            },
        }
        nodes.append(node)
        node_ids.add(question.name)

    for edge in routing_edges:
        source_names = _split_source_names(edge.source_question)

        for source_name in source_names:
            if not source_name:
                continue

            if source_name not in node_ids:
                node_type = "loop" if edge.edge_type == RoutingEdge.EdgeType.LOOP else "external_variable"

                nodes.append(
                    {
                        "id": source_name,
                        "type": node_type,
                        "data": {
                            "label": source_name,
                            "title": source_name,
                            "question_type": node_type,
                            "option_count": 0,
                        },
                    }
                )
                node_ids.add(source_name)

        if edge.target_question and edge.target_question not in node_ids:
            nodes.append(
                {
                    "id": edge.target_question,
                    "type": "unknown_target",
                    "data": {
                        "label": edge.target_question,
                        "title": edge.target_question,
                        "question_type": "unknown_target",
                        "option_count": 0,
                    },
                }
            )
            node_ids.add(edge.target_question)

    graph_edges: list[dict[str, Any]] = []

    for edge in routing_edges:
        source_names = _split_source_names(edge.source_question)

        if not source_names:
            source_names = ["UNKNOWN_SOURCE"]

        for source_name in source_names:
            graph_edges.append(
                {
                    "id": f"{source_name}__{edge.target_question}__{edge.sequence_index}",
                    "source": source_name,
                    "target": edge.target_question,
                    "type": edge.edge_type,
                    "label": edge.condition_text or edge.edge_type,
                    "condition": edge.condition_text,
                    "sequence_index": edge.sequence_index,
                }
            )

    mermaid_text = build_mermaid_text(nodes=nodes, edges=graph_edges)

    with transaction.atomic():
        graph, _created = QuestionnaireGraph.objects.update_or_create(
            module=module,
            defaults={
                "nodes_json": nodes,
                "edges_json": graph_edges,
                "mermaid_text": mermaid_text,
            },
        )

    return {
        "module_id": str(module.id),
        "graph_id": str(graph.id),
        "node_count": len(nodes),
        "edge_count": len(graph_edges),
    }


def _split_source_names(source_question: str) -> list[str]:
    """
    Handles multi-source conditions like:
    'FICODE, FRVAL'
    """
    if not source_question:
        return []

    return [
        item.strip()
        for item in source_question.split(",")
        if item.strip()
    ]