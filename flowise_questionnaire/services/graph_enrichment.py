from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


NON_QUESTION_NODE_TYPES = {
    "loop",
    "external_variable",
    "unknown_target",
}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _split_source_parts(source: Any) -> list[str]:
    source_text = _as_text(source)

    if not source_text:
        return []

    return [
        item.strip()
        for item in source_text.split(",")
        if item and item.strip()
    ]


def _unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []

    for value in values:
        text = _as_text(value)

        if not text:
            continue

        key = text.lower()

        if key in seen:
            continue

        seen.add(key)
        result.append(text)

    return result


def _condition_text_from_edge(edge: dict[str, Any]) -> str:
    return (
        _as_text(edge.get("condition_text"))
        or _as_text(edge.get("condition"))
        or _as_text(edge.get("label"))
    )


def _is_question_node(node: dict[str, Any]) -> bool:
    return _as_text(node.get("type")) not in NON_QUESTION_NODE_TYPES


def _get_sequence_index(node: dict[str, Any]) -> int | None:
    data = node.get("data") or {}

    value = (
        data.get("sequence_index")
        or data.get("sequence")
        or node.get("sequence_index")
        or node.get("sequence")
    )

    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _route_summary(edge: dict[str, Any]) -> dict[str, Any]:
    source = _as_text(edge.get("source"))
    target = _as_text(edge.get("target"))
    source_parts = _split_source_parts(source)

    return {
        "id": edge.get("id"),
        "source": source,
        "source_parts": source_parts,
        "target": target,
        "type": edge.get("type") or "unknown",
        "label": edge.get("label") or "",
        "condition_text": _condition_text_from_edge(edge),
    }


def _find_first_module_question_id(nodes: list[dict[str, Any]]) -> str | None:
    question_nodes = [
        node
        for node in nodes
        if isinstance(node, dict) and _is_question_node(node) and _as_text(node.get("id"))
    ]

    if not question_nodes:
        return None

    with_sequence = []

    for index, node in enumerate(question_nodes):
        sequence_index = _get_sequence_index(node)

        if sequence_index is not None:
            with_sequence.append((sequence_index, index, node))

    if with_sequence:
        with_sequence.sort(key=lambda item: (item[0], item[1]))
        return _as_text(with_sequence[0][2].get("id"))

    return _as_text(question_nodes[0].get("id"))


def _is_external_prerequisite(
    prerequisite_id: str,
    node_by_id: dict[str, dict[str, Any]],
) -> bool:
    prerequisite_node = node_by_id.get(prerequisite_id)

    if prerequisite_node is None:
        return True

    if not _is_question_node(prerequisite_node):
        return True

    return False


def _build_prerequisite_questions(
    prerequisite_ids: list[str],
    node_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    prerequisite_questions = []

    for prerequisite_id in prerequisite_ids:
        prerequisite_node = node_by_id.get(prerequisite_id)
        exists_in_graph = prerequisite_node is not None
        is_external = _is_external_prerequisite(prerequisite_id, node_by_id)

        if prerequisite_node:
            data = prerequisite_node.get("data") or {}
            label = (
                data.get("label")
                or data.get("title")
                or prerequisite_id
            )
            node_type = prerequisite_node.get("type") or "question"
        else:
            label = prerequisite_id
            node_type = "external_variable"

        prerequisite_questions.append(
            {
                "id": prerequisite_id,
                "exists_in_graph": exists_in_graph,
                "is_external": is_external,
                "type": node_type,
                "label": label,
            }
        )

    return prerequisite_questions


def _classify_start_semantics(
    *,
    node_id: str,
    first_module_question_id: str | None,
    incoming_routes: list[dict[str, Any]],
    prerequisite_questions: list[dict[str, Any]],
) -> dict[str, Any]:
    is_first_module_question = node_id == first_module_question_id

    has_incoming_routes = bool(incoming_routes)
    has_external_prerequisites = any(
        item.get("is_external") for item in prerequisite_questions
    )
    has_internal_prerequisites = any(
        not item.get("is_external") for item in prerequisite_questions
    )
    has_conditional_incoming_routes = any(
        route.get("type") == "conditional" for route in incoming_routes
    )

    is_true_unconditional_start = (
        is_first_module_question
        and not has_incoming_routes
        and not has_external_prerequisites
        and not has_internal_prerequisites
    )

    is_entry_with_external_prerequisites = (
        is_first_module_question
        and has_external_prerequisites
    )

    if is_true_unconditional_start:
        start_kind = "true_unconditional_start"
        display_label = "True unconditional start"
        explanation = (
            "This is the first question inside the module and no incoming "
            "or external prerequisite routes were detected."
        )
    elif is_entry_with_external_prerequisites:
        start_kind = "first_question_with_external_prerequisites"
        display_label = "First question with external prerequisites"
        explanation = (
            "This is the first question inside the module, but it depends on "
            "one or more external prerequisite variables or routes."
        )
    elif is_first_module_question:
        start_kind = "first_question_with_internal_prerequisites"
        display_label = "First question with prerequisites"
        explanation = (
            "This is the first question inside the module, but incoming "
            "prerequisite route(s) were detected."
        )
    else:
        start_kind = "not_start"
        display_label = "Not a module start"
        explanation = "This is not the first question detected inside this module."

    return {
        "is_first_module_question": is_first_module_question,
        "is_true_unconditional_start": is_true_unconditional_start,
        "is_entry_with_external_prerequisites": is_entry_with_external_prerequisites,
        "has_incoming_routes": has_incoming_routes,
        "has_external_prerequisites": has_external_prerequisites,
        "has_internal_prerequisites": has_internal_prerequisites,
        "has_conditional_incoming_routes": has_conditional_incoming_routes,
        "start_kind": start_kind,
        "display_label": display_label,
        "explanation": explanation,
    }


def _safe_mermaid_id(value: str) -> str:
    """Mermaid node IDs should be simple and stable."""

    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", (value or "").strip())
    if not cleaned:
        return "UNKNOWN"
    if cleaned[0].isdigit():
        cleaned = f"N_{cleaned}"
    return cleaned


def _short_mermaid_label(value: str, max_length: int = 80) -> str:
    value = (value or "").strip()
    if len(value) <= max_length:
        return value
    return f"{value[: max_length - 3]}..."


def build_mermaid_text(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    """
    Renders a {nodes, edges} pair (the same shape graph_builder.build_graph_for_module
    produces, whole-module or a filter_graph_to_neighborhood-scoped subset) as
    Mermaid flowchart syntax -- different node shapes per type (loop=rhombus,
    external_variable=circle, else=rectangle), edge labels from condition
    text truncated to 90 chars. Moved here (from graph_builder, where it
    started as a private module-persistence helper) so it can be reused
    on-demand against an arbitrary subgraph, e.g. mcp_server's
    get_module_graph tool, without requiring a persisted QuestionnaireGraph.
    """

    lines: list[str] = ["flowchart TD"]

    for node in nodes:
        node_id = _safe_mermaid_id(node["id"])
        label = (node.get("data") or {}).get("label") or node["id"]
        node_type = node.get("type")

        if node_type == "loop":
            lines.append(f'    {node_id}{{"{label}"}}')
        elif node_type == "external_variable":
            lines.append(f'    {node_id}(("{label}"))')
        else:
            lines.append(f'    {node_id}["{label}"]')

    for edge in edges:
        source_id = _safe_mermaid_id(edge["source"])
        target_id = _safe_mermaid_id(edge["target"])

        label = edge.get("condition") or edge.get("type") or ""
        label = _short_mermaid_label(label.replace('"', "'"), 90)

        if label:
            lines.append(f'    {source_id} -->|"{label}"| {target_id}')
        else:
            lines.append(f"    {source_id} --> {target_id}")

    return "\n".join(lines)


def filter_graph_to_neighborhood(
    graph: dict[str, Any] | None,
    focus_node_id: str,
    depth: int = 1,
) -> dict[str, Any] | None:
    """
    Filter a full {nodes_json, edges_json} graph payload down to the focus
    node plus its edges out to `depth` hops, in either direction. Real
    modules have hundreds/thousands of nodes, so returning the whole graph
    for "the routing around this one question" would defeat the point --
    this keeps the same node/edge JSON shape the rest of the app uses (View
    Graph, the routing-diff detail page, mcp_server's get_module_graph
    tool), just scoped down first.

    Moved here (from flowise_questionnaire.views.routing_diff_views, where
    it started as a private view helper) so it can be reused by both a view
    and mcp_server without mcp_server importing from views.
    """

    if graph is None:
        return None

    nodes = graph.get("nodes_json") or []
    edges = graph.get("edges_json") or []

    focus_norm = (focus_node_id or "").strip().casefold()
    if not focus_norm:
        return {"nodes_json": nodes, "edges_json": edges}

    included_ids = {focus_norm}
    frontier = {focus_norm}

    matched_edges = []

    for _ in range(max(depth, 0)):
        next_frontier = set()

        for edge in edges:
            source_norms = {name.casefold() for name in _split_source_parts(edge.get("source"))}
            target_norm = _as_text(edge.get("target")).casefold()

            if not (source_norms & frontier or target_norm in frontier):
                continue

            matched_edges.append(edge)
            included_ids.update(source_norms)
            included_ids.add(target_norm)
            next_frontier.update(source_norms)
            next_frontier.add(target_norm)

        frontier |= next_frontier

    seen_edge_keys = set()
    deduped_edges = []
    for edge in matched_edges:
        key = edge.get("id") or (edge.get("source"), edge.get("target"), edge.get("type"))
        if key in seen_edge_keys:
            continue
        seen_edge_keys.add(key)
        deduped_edges.append(edge)

    filtered_nodes = [
        node for node in nodes
        if _as_text(node.get("id")).casefold() in included_ids
    ]

    return {"nodes_json": filtered_nodes, "edges_json": deduped_edges}


def enrich_graph_dependencies(graph: dict[str, Any]) -> dict[str, Any]:
    """
    Enrich graph nodes with explicit dependency and semantic start metadata.

    Expected graph shape:
        {
            "nodes_json": [...],
            "edges_json": [...]
        }

    Added to each node:
        incoming_routes
        outgoing_routes
        prerequisite_questions
        dependency_summary
        start_semantics

    Added to graph:
        graph_start_summary
    """
    enriched_graph = deepcopy(graph)

    nodes = enriched_graph.get("nodes_json") or []
    edges = enriched_graph.get("edges_json") or []

    if not isinstance(nodes, list) or not isinstance(edges, list):
        return enriched_graph

    node_by_id: dict[str, dict[str, Any]] = {
        _as_text(node.get("id")): node
        for node in nodes
        if isinstance(node, dict) and _as_text(node.get("id"))
    }

    incoming_by_target: dict[str, list[dict[str, Any]]] = {}
    outgoing_by_source: dict[str, list[dict[str, Any]]] = {}

    for edge in edges:
        if not isinstance(edge, dict):
            continue

        target = _as_text(edge.get("target"))
        source_parts = _split_source_parts(edge.get("source"))
        route = _route_summary(edge)

        if target:
            incoming_by_target.setdefault(target, []).append(route)

        for source_part in source_parts:
            outgoing_by_source.setdefault(source_part, []).append(route)

    first_module_question_id = _find_first_module_question_id(nodes)

    true_start_question_ids: list[str] = []
    external_entry_prerequisite_nodes: list[dict[str, Any]] = []

    for node_id, node in node_by_id.items():
        if not _is_question_node(node):
            continue

        incoming_routes = incoming_by_target.get(node_id, [])
        outgoing_routes = outgoing_by_source.get(node_id, [])

        prerequisite_ids = _unique_preserve_order(
            [
                source_part
                for route in incoming_routes
                for source_part in route.get("source_parts", [])
            ]
        )

        prerequisite_questions = _build_prerequisite_questions(
            prerequisite_ids,
            node_by_id,
        )

        start_semantics = _classify_start_semantics(
            node_id=node_id,
            first_module_question_id=first_module_question_id,
            incoming_routes=incoming_routes,
            prerequisite_questions=prerequisite_questions,
        )

        node["incoming_routes"] = incoming_routes
        node["outgoing_routes"] = outgoing_routes
        node["prerequisite_questions"] = prerequisite_questions
        node["start_semantics"] = start_semantics

        node.setdefault("dependency_summary", {})
        node["dependency_summary"].update(
            {
                "incoming_route_count": len(incoming_routes),
                "outgoing_route_count": len(outgoing_routes),
                "prerequisite_question_count": len(prerequisite_questions),
                "has_external_prerequisites": start_semantics[
                    "has_external_prerequisites"
                ],
                "has_conditional_incoming_routes": start_semantics[
                    "has_conditional_incoming_routes"
                ],
            }
        )

        if start_semantics["is_true_unconditional_start"]:
            true_start_question_ids.append(node_id)

        if start_semantics["is_entry_with_external_prerequisites"]:
            external_entry_prerequisite_nodes.append(
                {
                    "question_id": node_id,
                    "prerequisites": prerequisite_questions,
                    "incoming_routes": incoming_routes,
                }
            )

    enriched_graph["nodes_json"] = nodes
    enriched_graph["edges_json"] = edges
    enriched_graph["graph_start_summary"] = {
        "first_module_question_id": first_module_question_id,
        "true_start_question_ids": true_start_question_ids,
        "external_entry_prerequisites": external_entry_prerequisite_nodes,
        "has_true_unconditional_start": bool(true_start_question_ids),
        "has_external_entry_prerequisites": bool(external_entry_prerequisite_nodes),
    }

    return enriched_graph