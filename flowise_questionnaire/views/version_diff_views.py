from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from flowise_questionnaire.models import QuestionnaireModule, VersionRoutingDiscrepancy
from flowise_questionnaire.services.graph_enrichment import filter_graph_to_neighborhood
from flowise_questionnaire.services.version_diff_explainer import explain_version_discrepancy
from flowise_questionnaire.services.version_question_matcher import build_version_matches
from flowise_questionnaire.services.version_routing_comparator import compare_versions_for_modules

# Same rationale as routing_diff_views.GRAPH_NEIGHBORHOOD_DEPTH: real
# modules have hundreds of nodes, so the detail page shows only the routing
# immediately around the discrepancy's focus question.
GRAPH_NEIGHBORHOOD_DEPTH = 1


def _get_module_pair(request, base_module_id, compare_module_id):
    base_module = get_object_or_404(
        QuestionnaireModule,
        id=base_module_id,
        user=request.user,
        source_format=QuestionnaireModule.SourceFormat.COLECTICA_JSON,
    )
    compare_module = get_object_or_404(
        QuestionnaireModule,
        id=compare_module_id,
        user=request.user,
        source_format=QuestionnaireModule.SourceFormat.COLECTICA_JSON,
    )
    return base_module, compare_module


@login_required
def version_diff_run_view(request, base_module_id, compare_module_id):
    base_module, compare_module = _get_module_pair(request, base_module_id, compare_module_id)

    if request.method != "POST":
        messages.error(request, "Invalid request method.")
        return redirect(
            "flowise_questionnaire:version_diff_report",
            base_module_id=base_module.id,
            compare_module_id=compare_module.id,
        )

    if base_module.id == compare_module.id:
        messages.error(request, "Choose two different Colectica modules to compare.")
        return redirect(
            "flowise_questionnaire:version_diff_report",
            base_module_id=base_module.id,
            compare_module_id=compare_module.id,
        )

    match_summary = build_version_matches(base_module, compare_module)
    discrepancy_summary = compare_versions_for_modules(base_module, compare_module)

    messages.success(
        request,
        (
            f"Matched {match_summary['name_matched']} by name, "
            f"{match_summary['exact_text_matched']} by exact text, "
            f"{match_summary['fuzzy_matched']} by fuzzy text, "
            f"{match_summary['unmatched']} unmatched. "
            f"Found {discrepancy_summary['total']} routing discrepancies."
        ),
    )

    return redirect(
        "flowise_questionnaire:version_diff_report",
        base_module_id=base_module.id,
        compare_module_id=compare_module.id,
    )


@login_required
def version_diff_report_view(request, base_module_id, compare_module_id):
    base_module, compare_module = _get_module_pair(request, base_module_id, compare_module_id)

    discrepancies_for_pair = VersionRoutingDiscrepancy.objects.filter(
        question_match__base_question__module=base_module,
        question_match__compare_question__module=compare_module,
    ).select_related(
        "question_match",
        "question_match__base_question",
        "question_match__compare_question",
        "base_edge",
        "compare_edge",
    )
    discrepancy_count_for_pair = discrepancies_for_pair.count()

    selected_discrepancy_type = request.GET.get("discrepancy_type", "")
    discrepancies = discrepancies_for_pair
    if selected_discrepancy_type:
        discrepancies = discrepancies.filter(discrepancy_type=selected_discrepancy_type)

    discrepancies = list(discrepancies)
    for discrepancy in discrepancies:
        discrepancy.explanation = explain_version_discrepancy(discrepancy)

    # Same reasoning as routing_diff_report_view: scoped to *this*
    # compare module specifically, since matches are only wiped/recreated
    # per base module (see version_question_matcher.build_version_matches).
    total_questions = base_module.normalized_questions.count()
    matched_count = base_module.normalized_questions.filter(
        base_version_matches__compare_question__module=compare_module
    ).distinct().count()

    has_run = matched_count > 0 or discrepancy_count_for_pair > 0

    match_summary = {
        "total": total_questions,
        "matched": matched_count,
        "unmatched": total_questions - matched_count,
    }

    return render(
        request,
        "flowise_questionnaire/version_diff_report.html",
        {
            "base_module": base_module,
            "compare_module": compare_module,
            "discrepancies": discrepancies,
            "discrepancy_type_choices": VersionRoutingDiscrepancy.DiscrepancyType.choices,
            "selected_discrepancy_type": selected_discrepancy_type,
            "match_summary": match_summary,
            "has_run": has_run,
        },
    )


def _discrepancy_focus_question_names(discrepancy):
    """
    The (base_name, compare_name) pair this detail page's two graph panels
    should each center on: the matched question's own name on that side --
    always a single, resolvable graph node id.

    Deliberately NOT a RoutingEdge's raw source_question: a compound
    condition stores a comma-joined multi-variable source (e.g. "PERGRID,
    NAME, SNAME, PNAME, PSNAME, RESPEMAILCONF"), which never equals any
    single node id and silently produced an empty neighborhood graph for
    any discrepancy whose edge had more than one source variable.
    """

    match = discrepancy.question_match
    base_name = match.base_question.name
    compare_name = match.compare_question.name if match.compare_question_id else base_name
    return base_name, compare_name


def _discrepancy_highlight_edge(discrepancy):
    edge = discrepancy.base_edge or discrepancy.compare_edge
    if edge is None:
        return None
    return {"source": edge.source_question, "target": edge.target_question}


def _graph_json_payload(graph):
    if graph is None:
        return None

    return {
        "nodes_json": graph.nodes_json or [],
        "edges_json": graph.edges_json or [],
    }


def _graph_neighborhood_payload(graph_payload, focus_question_name, depth=GRAPH_NEIGHBORHOOD_DEPTH):
    return filter_graph_to_neighborhood(graph_payload, focus_question_name, depth=depth)


@login_required
def version_diff_discrepancy_detail_view(request, base_module_id, compare_module_id, discrepancy_id):
    base_module, compare_module = _get_module_pair(request, base_module_id, compare_module_id)

    discrepancy = get_object_or_404(
        VersionRoutingDiscrepancy.objects.select_related(
            "question_match",
            "question_match__base_question",
            "question_match__compare_question",
            "base_edge",
            "compare_edge",
        ),
        id=discrepancy_id,
        question_match__base_question__module=base_module,
        question_match__compare_question__module=compare_module,
    )

    explanation = explain_version_discrepancy(discrepancy)
    base_focus_question_name, compare_focus_question_name = _discrepancy_focus_question_names(discrepancy)
    highlight_edge = _discrepancy_highlight_edge(discrepancy)

    base_graph = _graph_neighborhood_payload(
        _graph_json_payload(getattr(base_module, "graph", None)),
        base_focus_question_name,
    )
    compare_graph = _graph_neighborhood_payload(
        _graph_json_payload(getattr(compare_module, "graph", None)),
        compare_focus_question_name,
    )

    return render(
        request,
        "flowise_questionnaire/version_diff_detail.html",
        {
            "base_module": base_module,
            "compare_module": compare_module,
            "discrepancy": discrepancy,
            "explanation": explanation,
            "focus_question_name": base_focus_question_name,
            "base_focus_question_name": base_focus_question_name,
            "compare_focus_question_name": compare_focus_question_name,
            "highlight_edge": highlight_edge,
            "base_graph": base_graph,
            "compare_graph": compare_graph,
        },
    )
