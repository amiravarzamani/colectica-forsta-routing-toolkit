from __future__ import annotations

import uuid as uuid_module
from typing import Any

from collections import Counter

from django.core.exceptions import ValidationError

from mcp.server.mcpserver.exceptions import ToolError

from flowise_questionnaire.models import (
    NormalizedQuestion,
    QuestionnaireModule,
    RoutingDiscrepancy,
    RoutingEdge,
    VersionRoutingDiscrepancy,
)
from flowise_questionnaire.services.agentflow_payload_builder import (
    _extract_external_variables,
    build_compact_agentflow_payload_for_module,
)
from flowise_questionnaire.services.routing_simulator import simulate_all_coverage_intents_for_module
from flowise_questionnaire.services.condition_evaluator import evaluate_condition
from flowise_questionnaire.services.forsta_condition_evaluator import evaluate_forsta_condition
from flowise_questionnaire.services.graph_enrichment import (
    build_mermaid_text,
    enrich_graph_dependencies,
    filter_graph_to_neighborhood,
)
from flowise_questionnaire.services.routing_diff_explainer import explain_discrepancy
from flowise_questionnaire.services.version_diff_explainer import explain_version_discrepancy

# Above this many questions, get_module_graph refuses to return the full
# enriched graph unscoped -- W18-scale modules (1803 questions, ~1428
# conditional edges) would otherwise return a multi-MB response. Tune after
# seeing real response sizes against production data.
LARGE_MODULE_QUESTION_THRESHOLD = 50


def _resolve_module(module_id_or_name: str) -> QuestionnaireModule:
    """
    Resolve a module by UUID (id) or, failing that, by exact case-insensitive name.
    """

    raw = str(module_id_or_name).strip()

    try:
        module_uuid = uuid_module.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        module_uuid = None

    if module_uuid is not None:
        module = QuestionnaireModule.objects.filter(id=module_uuid).first()
        if module is None:
            raise ToolError(f"No questionnaire module found with id '{raw}'.")
        return module

    candidates = list(QuestionnaireModule.objects.filter(name__iexact=raw))

    if not candidates:
        raise ToolError(f"No questionnaire module found matching name '{raw}'.")

    if len(candidates) > 1:
        ids = ", ".join(str(module.id) for module in candidates)
        raise ToolError(
            f"{len(candidates)} modules are named '{raw}'. "
            f"Use the module id instead: {ids}"
        )

    return candidates[0]


def _require_colectica(module: QuestionnaireModule) -> None:
    if module.source_format != QuestionnaireModule.SourceFormat.COLECTICA_JSON:
        raise ToolError(
            f"Module '{module.name}' ({module.id}) is a Forsta+ XML module "
            f"(source_format='{module.source_format}'). This MCP server currently "
            "supports Colectica JSON modules only."
        )


def _resolve_colectica_module(module_id_or_name: str) -> QuestionnaireModule:
    module = _resolve_module(module_id_or_name)
    _require_colectica(module)
    return module


def _serialize_question(question: NormalizedQuestion) -> dict[str, Any]:
    return {
        "name": question.name,
        "label": question.label,
        "text": question.text,
        "question_type": question.question_type,
        "options": question.options_json,
        "help_text": question.help_text,
        "interviewer_instruction": question.interviewer_instruction,
        "sequence_index": question.sequence_index,
    }


def _serialize_routing_edge(edge: RoutingEdge) -> dict[str, Any]:
    return {
        "source_question": edge.source_question,
        "target_question": edge.target_question,
        "condition_text": edge.condition_text,
        "edge_type": edge.edge_type,
        "sequence_index": edge.sequence_index,
    }


def list_modules() -> dict[str, Any]:
    """
    List every Colectica JSON QuestionnaireModule: id, name, status, question_count,
    edge_count.
    """

    modules = list(
        QuestionnaireModule.objects.filter(
            source_format=QuestionnaireModule.SourceFormat.COLECTICA_JSON,
        ).order_by("-created_at")
    )

    return {
        "count": len(modules),
        "modules": [
            {
                "id": str(module.id),
                "name": module.name,
                "version": module.version,
                "status": module.processing_status,
                "question_count": module.normalized_questions.count(),
                "edge_count": module.routing_edges.count(),
            }
            for module in modules
        ],
    }


def get_module_summary(module_id_or_name: str) -> dict[str, Any]:
    """
    Full detail on one Colectica module: status, counts, timestamps.

    Raises a clear ToolError (not an empty result) if the module exists but is
    a Forsta+ XML module.
    """

    module = _resolve_colectica_module(module_id_or_name)

    routing_edges = module.routing_edges.all()

    return {
        "id": str(module.id),
        "name": module.name,
        "version": module.version,
        "status": module.processing_status,
        "error_message": module.error_message,
        "question_count": module.normalized_questions.count(),
        "routing_edge_count": routing_edges.count(),
        "conditional_edge_count": routing_edges.filter(
            edge_type=RoutingEdge.EdgeType.CONDITIONAL
        ).count(),
        "loop_edge_count": routing_edges.filter(
            edge_type=RoutingEdge.EdgeType.LOOP
        ).count(),
        "sequential_edge_count": routing_edges.filter(
            edge_type=RoutingEdge.EdgeType.SEQUENTIAL
        ).count(),
        "has_graph": hasattr(module, "graph"),
        "created_at": module.created_at.isoformat() if module.created_at else None,
        "updated_at": module.updated_at.isoformat() if module.updated_at else None,
    }


def list_questions(module_id_or_name: str) -> dict[str, Any]:
    """
    All NormalizedQuestion rows for a Colectica module (name, text, type, options).
    """

    module = _resolve_colectica_module(module_id_or_name)

    questions = module.normalized_questions.order_by("sequence_index")

    return {
        "module_id": str(module.id),
        "module_name": module.name,
        "count": questions.count(),
        "questions": [_serialize_question(question) for question in questions],
    }


def get_question(module_id_or_name: str, question_name: str) -> dict[str, Any]:
    """
    Single question lookup, case-insensitive on name.
    """

    module = _resolve_colectica_module(module_id_or_name)

    question = module.normalized_questions.filter(name__iexact=question_name).first()

    if question is None:
        raise ToolError(
            f"Question '{question_name}' not found in module '{module.name}' ({module.id})."
        )

    return {
        "module_id": str(module.id),
        "module_name": module.name,
        **_serialize_question(question),
    }


def get_routing_edges(module_id_or_name: str) -> dict[str, Any]:
    """
    All RoutingEdge rows for a Colectica module (source, target, condition, type).
    """

    module = _resolve_colectica_module(module_id_or_name)

    edges = module.routing_edges.order_by("sequence_index")

    return {
        "module_id": str(module.id),
        "module_name": module.name,
        "count": edges.count(),
        "edges": [_serialize_routing_edge(edge) for edge in edges],
    }


def trace_variable(variable_name: str, module_id_or_name: str | None = None) -> dict[str, Any]:
    """
    Find every RoutingEdge whose condition references this variable, across one
    Colectica module or all Colectica modules if module_id_or_name is omitted.

    Uses the same external-variable rule as
    agentflow_payload_builder._extract_external_variables: a referenced variable
    is "external" if it is not the name of any NormalizedQuestion in that module
    and does not end in "Loop" (loop containers aren't external variables).
    """

    variable_lower = variable_name.strip().lower()

    if not variable_lower:
        raise ToolError("variable_name must not be empty.")

    if module_id_or_name:
        modules = [_resolve_colectica_module(module_id_or_name)]
    else:
        modules = list(
            QuestionnaireModule.objects.filter(
                source_format=QuestionnaireModule.SourceFormat.COLECTICA_JSON,
            )
        )

    matches: list[dict[str, Any]] = []

    for module in modules:
        questions = list(module.normalized_questions.all())
        conditional_edges = list(
            module.routing_edges.filter(edge_type=RoutingEdge.EdgeType.CONDITIONAL)
        )
        external_variables = set(
            _extract_external_variables(
                questions=questions,
                routing_edges=conditional_edges,
            )
        )

        for edge in module.routing_edges.order_by("sequence_index"):
            source_tokens = [
                token.strip()
                for token in (edge.source_question or "").split(",")
                if token.strip()
            ]
            matched_token = next(
                (token for token in source_tokens if token.lower() == variable_lower),
                None,
            )
            text_hit = variable_lower in (edge.condition_text or "").lower()

            if matched_token is None and not text_hit:
                continue

            matches.append(
                {
                    "module_id": str(module.id),
                    "module_name": module.name,
                    **_serialize_routing_edge(edge),
                    "is_external_variable": (
                        matched_token in external_variables
                        if matched_token is not None
                        else None
                    ),
                }
            )

    return {
        "variable_name": variable_name,
        "match_count": len(matches),
        "matches": matches,
    }


def get_module_graph(
    module_id_or_name: str,
    question_name: str | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    """
    Enriched routing graph for a module -- Colectica OR Forsta+, both are
    supported. Returns the same incoming_routes/outgoing_routes/
    prerequisite_questions/start_semantics per node, and graph_start_summary
    overall, that the "View Graph" GUI page shows. Never builds the graph
    itself (that's a write path) -- raises ToolError if none has been built
    yet for this module.

    Large modules cannot be returned in full: pass question_name to scope
    the response to that question's neighborhood (depth hops out, default
    1, max 3) -- this is the primary way to explore a large module. Omit
    question_name only on modules with 50 or fewer questions ("mode":
    "full" in the response); on larger modules, omitting it returns "mode":
    "summary" (counts + graph_start_summary only, no per-node detail)
    rather than risk a huge response -- pass question_name in that case.

    "mermaid" is Mermaid flowchart syntax (```mermaid fenced code renders
    as an actual diagram in most MCP clients, including Claude) for
    whatever is actually being returned -- the full graph in "full" mode,
    the scoped subgraph in "neighborhood" mode. Always None in "summary"
    mode, for the same size reason "graph" is None there.
    """

    module = _resolve_module(module_id_or_name)

    graph = getattr(module, "graph", None)
    if graph is None:
        raise ToolError(
            f"No graph has been built yet for module '{module.name}' ({module.id})."
        )

    raw_graph = {
        "nodes_json": graph.nodes_json or [],
        "edges_json": graph.edges_json or [],
    }
    total_node_count = len(raw_graph["nodes_json"])
    total_edge_count = len(raw_graph["edges_json"])

    base_response = {
        "module_id": str(module.id),
        "module_name": module.name,
        "source_format": module.source_format,
        "scoped_to_question": question_name,
        "total_node_count": total_node_count,
        "total_edge_count": total_edge_count,
    }

    if question_name:
        clamped_depth = max(1, min(depth, 3))
        scoped = filter_graph_to_neighborhood(raw_graph, question_name, depth=clamped_depth)
        scoped_ids = {
            str(node.get("id") or "").strip().casefold()
            for node in scoped["nodes_json"]
        }
        if question_name.strip().casefold() not in scoped_ids:
            raise ToolError(
                f"Question '{question_name}' not found in module '{module.name}' "
                f"({module.id})'s graph."
            )

        enriched = enrich_graph_dependencies(scoped)
        return {
            **base_response,
            "mode": "neighborhood",
            "graph_start_summary": enriched.get("graph_start_summary"),
            "graph": enriched,
            "mermaid": build_mermaid_text(scoped["nodes_json"], scoped["edges_json"]),
        }

    enriched = enrich_graph_dependencies(raw_graph)

    if total_node_count > LARGE_MODULE_QUESTION_THRESHOLD:
        return {
            **base_response,
            "mode": "summary",
            "graph_start_summary": enriched.get("graph_start_summary"),
            "graph": None,
            "mermaid": None,
            "hint": (
                f"This module has {total_node_count} questions -- too many to "
                "return in full. Pass question_name to get a scoped "
                "neighborhood around one question."
            ),
        }

    return {
        **base_response,
        "mode": "full",
        "graph_start_summary": enriched.get("graph_start_summary"),
        "graph": enriched,
        "mermaid": build_mermaid_text(raw_graph["nodes_json"], raw_graph["edges_json"]),
    }


def evaluate_edge_condition(
    module_id_or_name: str,
    condition_text: str,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """
    Evaluate a hypothetical condition expression against hypothetical
    answers -- Colectica OR Forsta+, both supported (the grammar picked
    automatically from the module's source_format). condition_text does not
    need to belong to any real RoutingEdge in this module; this lets a
    client ask "what if" questions interactively (e.g. would this fire if
    AIDHH=1), not just replay edges that already exist.

    Pure/side-effect-free. Returns status ("true"/"false"/"unknown"/
    "unsupported"), missing_inputs, used_inputs, normalized_expression, and
    warnings -- unchanged from the underlying evaluator.
    """

    module = _resolve_module(module_id_or_name)

    if module.source_format == QuestionnaireModule.SourceFormat.FORSTA_XML:
        result = evaluate_forsta_condition(condition_text, inputs)
    else:
        result = evaluate_condition(condition_text, inputs)

    return {
        "module_id": str(module.id),
        "module_name": module.name,
        "source_format": module.source_format,
        "condition_text": condition_text,
        **result,
    }


def _require_forsta(module: QuestionnaireModule) -> None:
    if module.source_format != QuestionnaireModule.SourceFormat.FORSTA_XML:
        raise ToolError(
            f"Module '{module.name}' ({module.id}) is a Colectica JSON module "
            f"(source_format='{module.source_format}'). Expected a Forsta+ XML "
            "module here."
        )


def _resolve_forsta_module(module_id_or_name: str) -> QuestionnaireModule:
    module = _resolve_module(module_id_or_name)
    _require_forsta(module)
    return module


def _serialize_discrepancy(discrepancy: RoutingDiscrepancy) -> dict[str, Any]:
    explanation = explain_discrepancy(discrepancy)
    match = discrepancy.question_match

    return {
        "id": str(discrepancy.id),
        "discrepancy_type": discrepancy.discrepancy_type,
        "severity": discrepancy.severity,
        "colectica_question": match.colectica_question.name,
        "forsta_question": match.forsta_question.name if match.forsta_question_id else None,
        "summary": explanation.summary,
        "meaning": explanation.meaning,
        "edge": (
            {
                "origin_system": explanation.edge.origin_system,
                "source_question": explanation.edge.source_question,
                "target_question": explanation.edge.target_question,
                "condition_text": explanation.edge.condition_text,
            }
            if explanation.edge is not None
            else None
        ),
    }


def _has_routing_diff_run(
    colectica_module: QuestionnaireModule,
    forsta_module: QuestionnaireModule,
) -> tuple[bool, dict[str, int]]:
    """
    Replicates routing_diff_views.routing_diff_report_view's has_run/
    match_summary logic exactly. QuestionMatch rows are only deleted/
    recreated per Colectica module (see question_matcher.build_question_matches),
    not per (colectica, forsta) pair, so a Colectica module compared against
    more than one Forsta+ module over time can carry matched rows pointing
    at a stale, different Forsta+ module -- scoping "matched" to *this
    specific* forsta_module is what makes this correct.
    """

    total_questions = colectica_module.normalized_questions.count()
    matched_count = colectica_module.normalized_questions.filter(
        colectica_matches__forsta_question__module=forsta_module
    ).distinct().count()

    discrepancy_count = RoutingDiscrepancy.objects.filter(
        question_match__colectica_question__module=colectica_module,
        question_match__forsta_question__module=forsta_module,
    ).count()

    has_run = matched_count > 0 or discrepancy_count > 0

    match_summary = {
        "total": total_questions,
        "matched": matched_count,
        "unmatched": total_questions - matched_count,
    }

    return has_run, match_summary


def _module_graph_neighborhood(
    module: QuestionnaireModule,
    focus_question_name: str,
) -> dict[str, Any] | None:
    graph = getattr(module, "graph", None)
    if graph is None:
        return None

    raw_graph = {
        "nodes_json": graph.nodes_json or [],
        "edges_json": graph.edges_json or [],
    }
    return filter_graph_to_neighborhood(raw_graph, focus_question_name, depth=1)


def get_routing_diff_report(
    colectica_module_id_or_name: str,
    forsta_module_id_or_name: str,
    discrepancy_type: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """
    Structural routing discrepancies between a matched Colectica/Forsta+
    module pair, exactly matching the routing-diff report GUI page
    (/questionnaires/routing-diff/<colectica_id>/<forsta_id>/). Does NOT run
    matching/comparison itself (build_question_matches/
    compare_routing_for_modules are expensive for large modules) -- if
    matching has never been run for this exact pair, has_run is False and
    discrepancies is empty rather than raising.

    Each discrepancy is a *lead for manual review*, not a confirmed bug --
    see each row's "meaning" field: this is a structural comparison only,
    it never evaluates what a condition means.

    discrepancy_type filters to one DiscrepancyType value. limit caps how
    many discrepancies are returned (default 200) -- discrepancy_count
    always reports the true total regardless of limit, since large modules
    can have hundreds of rows with full explainer text each.
    """

    colectica_module = _resolve_colectica_module(colectica_module_id_or_name)
    forsta_module = _resolve_forsta_module(forsta_module_id_or_name)

    has_run, match_summary = _has_routing_diff_run(colectica_module, forsta_module)

    discrepancies_qs = RoutingDiscrepancy.objects.filter(
        question_match__colectica_question__module=colectica_module,
        question_match__forsta_question__module=forsta_module,
    ).select_related(
        "question_match",
        "question_match__colectica_question",
        "question_match__forsta_question",
        "colectica_edge",
        "forsta_edge",
    )

    if discrepancy_type:
        valid_types = {choice for choice, _ in RoutingDiscrepancy.DiscrepancyType.choices}
        if discrepancy_type not in valid_types:
            raise ToolError(
                f"Unknown discrepancy_type '{discrepancy_type}'. Valid values: "
                f"{', '.join(sorted(valid_types))}."
            )
        discrepancies_qs = discrepancies_qs.filter(discrepancy_type=discrepancy_type)

    discrepancy_count = discrepancies_qs.count()
    discrepancies = list(discrepancies_qs[:limit])

    return {
        "colectica_module_id": str(colectica_module.id),
        "colectica_module_name": colectica_module.name,
        "forsta_module_id": str(forsta_module.id),
        "forsta_module_name": forsta_module.name,
        "has_run": has_run,
        "match_summary": match_summary,
        "discrepancy_count": discrepancy_count,
        "discrepancies": [_serialize_discrepancy(d) for d in discrepancies],
    }


def get_routing_discrepancy_detail(
    colectica_module_id_or_name: str,
    forsta_module_id_or_name: str,
    discrepancy_id: str,
) -> dict[str, Any]:
    """
    One routing discrepancy in full detail, exactly matching the routing-diff
    discrepancy detail GUI page
    (/questionnaires/routing-diff/<colectica_id>/<forsta_id>/discrepancy/<discrepancy_id>/)
    -- including BOTH systems' routing graph neighborhoods around the focus
    question (colectica_graph and forsta_graph), not just Colectica's side.
    Either graph is None if that module has no graph built yet, rather than
    erroring the whole call.
    """

    colectica_module = _resolve_colectica_module(colectica_module_id_or_name)
    forsta_module = _resolve_forsta_module(forsta_module_id_or_name)

    try:
        discrepancy = RoutingDiscrepancy.objects.select_related(
            "question_match",
            "question_match__colectica_question",
            "question_match__forsta_question",
            "colectica_edge",
            "forsta_edge",
        ).get(
            id=discrepancy_id,
            question_match__colectica_question__module=colectica_module,
            question_match__forsta_question__module=forsta_module,
        )
    except (RoutingDiscrepancy.DoesNotExist, ValueError, ValidationError):
        raise ToolError(
            f"No routing discrepancy '{discrepancy_id}' found for modules "
            f"'{colectica_module.name}' and '{forsta_module.name}'."
        )

    explanation = explain_discrepancy(discrepancy)

    # Per-side focus name, not a RoutingEdge's raw source_question: a
    # compound condition stores a comma-joined multi-variable source (e.g.
    # "PERGRID, NAME, SNAME, ..."), which never equals any single graph node
    # id and silently produced an empty neighborhood for any discrepancy on
    # a multi-source edge -- same fix as routing_diff_views' GUI detail page
    # (see the qrvss skill's "Multi-source conditional edges" note). Kept in
    # sync with that fix so this tool mirrors the GUI page exactly.
    match = discrepancy.question_match
    colectica_focus_question_name = match.colectica_question.name
    forsta_focus_question_name = (
        match.forsta_question.name if match.forsta_question_id else colectica_focus_question_name
    )

    highlight_source_edge = discrepancy.colectica_edge or discrepancy.forsta_edge
    highlight_edge = (
        {
            "source": highlight_source_edge.source_question,
            "target": highlight_source_edge.target_question,
        }
        if highlight_source_edge is not None
        else None
    )

    return {
        "colectica_module_id": str(colectica_module.id),
        "forsta_module_id": str(forsta_module.id),
        "discrepancy_id": str(discrepancy.id),
        "discrepancy_type": discrepancy.discrepancy_type,
        "severity": discrepancy.severity,
        "summary": explanation.summary,
        "meaning": explanation.meaning,
        "edge": (
            {
                "origin_system": explanation.edge.origin_system,
                "source_question": explanation.edge.source_question,
                "target_question": explanation.edge.target_question,
                "condition_text": explanation.edge.condition_text,
            }
            if explanation.edge is not None
            else None
        ),
        "focus_question_name": colectica_focus_question_name,
        "colectica_focus_question_name": colectica_focus_question_name,
        "forsta_focus_question_name": forsta_focus_question_name,
        "highlight_edge": highlight_edge,
        "colectica_graph": _module_graph_neighborhood(colectica_module, colectica_focus_question_name),
        "forsta_graph": _module_graph_neighborhood(forsta_module, forsta_focus_question_name),
    }


def get_routing_simulation(module_id_or_name: str) -> dict[str, Any]:
    """
    Deterministic coverage-intent simulation for a Colectica module: for
    each of up to 10 auto-generated coverage intents, which conditional
    edges fired given seed answers, and whether each intent's target
    edge(s) were covered. Exactly replicates what the routing-simulation
    GUI JSON endpoint already computes -- not persisted, recomputed on
    every call, same cost profile as that existing endpoint. Bounded
    regardless of module size (coverage_intent_builder caps at 10 intents).
    """

    module = _resolve_colectica_module(module_id_or_name)

    compact_payload = build_compact_agentflow_payload_for_module(module)
    coverage_intents = (compact_payload.get("coverage_intents") or {}).get("intents") or []

    results = simulate_all_coverage_intents_for_module(
        module=module,
        coverage_intents=coverage_intents,
    )

    status_counts = Counter(
        str(result.get("coverage_status") or "unknown") for result in results
    )

    return {
        "module_id": str(module.id),
        "module_name": module.name,
        "coverage_intent_count": len(results),
        "status_counts": dict(status_counts),
        "results": results,
    }


def _serialize_version_discrepancy(discrepancy: VersionRoutingDiscrepancy) -> dict[str, Any]:
    explanation = explain_version_discrepancy(discrepancy)
    match = discrepancy.question_match

    return {
        "id": str(discrepancy.id),
        "discrepancy_type": discrepancy.discrepancy_type,
        "severity": discrepancy.severity,
        "base_question": match.base_question.name,
        "compare_question": match.compare_question.name if match.compare_question_id else None,
        "summary": explanation.summary,
        "meaning": explanation.meaning,
        "edge": (
            {
                "origin_module": explanation.edge.origin_module,
                "source_question": explanation.edge.source_question,
                "target_question": explanation.edge.target_question,
                "condition_text": explanation.edge.condition_text,
            }
            if explanation.edge is not None
            else None
        ),
    }


def _has_version_diff_run(
    base_module: QuestionnaireModule,
    compare_module: QuestionnaireModule,
) -> tuple[bool, dict[str, int]]:
    """
    Replicates version_diff_views.version_diff_report_view's has_run/
    match_summary logic exactly -- same rationale as _has_routing_diff_run.
    """

    total_questions = base_module.normalized_questions.count()
    matched_count = base_module.normalized_questions.filter(
        base_version_matches__compare_question__module=compare_module
    ).distinct().count()

    discrepancy_count = VersionRoutingDiscrepancy.objects.filter(
        question_match__base_question__module=base_module,
        question_match__compare_question__module=compare_module,
    ).count()

    has_run = matched_count > 0 or discrepancy_count > 0

    match_summary = {
        "total": total_questions,
        "matched": matched_count,
        "unmatched": total_questions - matched_count,
    }

    return has_run, match_summary


def get_version_diff_report(
    base_module_id_or_name: str,
    compare_module_id_or_name: str,
    discrepancy_type: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """
    Structural (plus condition-text) routing discrepancies between two
    Colectica-format modules -- e.g. two waves/versions of the same
    questionnaire -- exactly matching the version-diff report GUI page
    (/questionnaires/version-diff/<base_id>/<compare_id>/). Does NOT run
    matching/comparison itself -- if it's never been run for this exact
    pair, has_run is False and discrepancies is empty rather than raising.

    Both modules must be Colectica JSON; a Forsta+ module raises ToolError.
    This is a separate pipeline from get_routing_diff_report (Colectica vs
    Forsta+) -- different matcher priority (name-first) and comparator
    (adds a CONDITION_MISMATCH type, meaningful here specifically because
    both sides share the same Colectica grammar).

    discrepancy_type filters to one VersionRoutingDiscrepancy.DiscrepancyType
    value (missing_in_compare / missing_in_base / condition_mismatch). limit
    caps how many discrepancies are returned (default 200) --
    discrepancy_count always reports the true total regardless of limit.
    """

    base_module = _resolve_colectica_module(base_module_id_or_name)
    compare_module = _resolve_colectica_module(compare_module_id_or_name)

    has_run, match_summary = _has_version_diff_run(base_module, compare_module)

    discrepancies_qs = VersionRoutingDiscrepancy.objects.filter(
        question_match__base_question__module=base_module,
        question_match__compare_question__module=compare_module,
    ).select_related(
        "question_match",
        "question_match__base_question",
        "question_match__compare_question",
        "base_edge",
        "compare_edge",
    )

    if discrepancy_type:
        valid_types = {choice for choice, _ in VersionRoutingDiscrepancy.DiscrepancyType.choices}
        if discrepancy_type not in valid_types:
            raise ToolError(
                f"Unknown discrepancy_type '{discrepancy_type}'. Valid values: "
                f"{', '.join(sorted(valid_types))}."
            )
        discrepancies_qs = discrepancies_qs.filter(discrepancy_type=discrepancy_type)

    discrepancy_count = discrepancies_qs.count()
    discrepancies = list(discrepancies_qs[:limit])

    return {
        "base_module_id": str(base_module.id),
        "base_module_name": base_module.name,
        "compare_module_id": str(compare_module.id),
        "compare_module_name": compare_module.name,
        "has_run": has_run,
        "match_summary": match_summary,
        "discrepancy_count": discrepancy_count,
        "discrepancies": [_serialize_version_discrepancy(d) for d in discrepancies],
    }


def get_version_diff_discrepancy_detail(
    base_module_id_or_name: str,
    compare_module_id_or_name: str,
    discrepancy_id: str,
) -> dict[str, Any]:
    """
    One version-diff discrepancy in full detail, exactly matching the
    version-diff discrepancy detail GUI page
    (/questionnaires/version-diff/<base_id>/<compare_id>/discrepancy/<discrepancy_id>/)
    -- including BOTH modules' routing graph neighborhoods around the focus
    question (base_graph and compare_graph), not just the base side. Either
    graph is None if that module has no graph built yet, rather than
    erroring the whole call.
    """

    base_module = _resolve_colectica_module(base_module_id_or_name)
    compare_module = _resolve_colectica_module(compare_module_id_or_name)

    try:
        discrepancy = VersionRoutingDiscrepancy.objects.select_related(
            "question_match",
            "question_match__base_question",
            "question_match__compare_question",
            "base_edge",
            "compare_edge",
        ).get(
            id=discrepancy_id,
            question_match__base_question__module=base_module,
            question_match__compare_question__module=compare_module,
        )
    except (VersionRoutingDiscrepancy.DoesNotExist, ValueError, ValidationError):
        raise ToolError(
            f"No version-diff discrepancy '{discrepancy_id}' found for modules "
            f"'{base_module.name}' and '{compare_module.name}'."
        )

    explanation = explain_version_discrepancy(discrepancy)

    match = discrepancy.question_match
    base_focus_question_name = match.base_question.name
    compare_focus_question_name = (
        match.compare_question.name if match.compare_question_id else base_focus_question_name
    )

    highlight_source_edge = discrepancy.base_edge or discrepancy.compare_edge
    highlight_edge = (
        {
            "source": highlight_source_edge.source_question,
            "target": highlight_source_edge.target_question,
        }
        if highlight_source_edge is not None
        else None
    )

    return {
        "base_module_id": str(base_module.id),
        "compare_module_id": str(compare_module.id),
        "discrepancy_id": str(discrepancy.id),
        "discrepancy_type": discrepancy.discrepancy_type,
        "severity": discrepancy.severity,
        "summary": explanation.summary,
        "meaning": explanation.meaning,
        "edge": (
            {
                "origin_module": explanation.edge.origin_module,
                "source_question": explanation.edge.source_question,
                "target_question": explanation.edge.target_question,
                "condition_text": explanation.edge.condition_text,
            }
            if explanation.edge is not None
            else None
        ),
        "focus_question_name": base_focus_question_name,
        "base_focus_question_name": base_focus_question_name,
        "compare_focus_question_name": compare_focus_question_name,
        "highlight_edge": highlight_edge,
        "base_graph": _module_graph_neighborhood(base_module, base_focus_question_name),
        "compare_graph": _module_graph_neighborhood(compare_module, compare_focus_question_name),
    }
