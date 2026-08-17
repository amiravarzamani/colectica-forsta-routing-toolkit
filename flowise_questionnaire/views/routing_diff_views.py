from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from flowise_questionnaire.models import QuestionnaireModule, RoutingDiscrepancy
from flowise_questionnaire.services.graph_enrichment import filter_graph_to_neighborhood
from flowise_questionnaire.services.question_matcher import build_question_matches
from flowise_questionnaire.services.routing_comparator import compare_routing_for_modules
from flowise_questionnaire.services.routing_diff_explainer import explain_discrepancy

# How many hops out from the focus question to include in the detail-page
# graphs. Kept small (immediate neighbors only) because real modules have
# hundreds of nodes -- the point of this view is "the routing around this
# one question", not the whole graph (that's what View Graph is for).
GRAPH_NEIGHBORHOOD_DEPTH = 1


def _get_module_pair(request, colectica_module_id, forsta_module_id):
    colectica_module = get_object_or_404(
        QuestionnaireModule,
        id=colectica_module_id,
        user=request.user,
        source_format=QuestionnaireModule.SourceFormat.COLECTICA_JSON,
    )
    forsta_module = get_object_or_404(
        QuestionnaireModule,
        id=forsta_module_id,
        user=request.user,
        source_format=QuestionnaireModule.SourceFormat.FORSTA_XML,
    )
    return colectica_module, forsta_module


@login_required
def routing_diff_run_view(request, colectica_module_id, forsta_module_id):
    colectica_module, forsta_module = _get_module_pair(request, colectica_module_id, forsta_module_id)

    if request.method != "POST":
        messages.error(request, "Invalid request method.")
        return redirect(
            "flowise_questionnaire:routing_diff_report",
            colectica_module_id=colectica_module.id,
            forsta_module_id=forsta_module.id,
        )

    match_summary = build_question_matches(colectica_module, forsta_module)
    discrepancy_summary = compare_routing_for_modules(colectica_module, forsta_module)

    messages.success(
        request,
        (
            f"Matched {match_summary['exact']} exact, {match_summary['fuzzy_matched']} fuzzy, "
            f"{match_summary['unmatched']} unmatched. "
            f"Found {discrepancy_summary['total']} routing discrepancies."
        ),
    )

    return redirect(
        "flowise_questionnaire:routing_diff_report",
        colectica_module_id=colectica_module.id,
        forsta_module_id=forsta_module.id,
    )


@login_required
def routing_diff_report_view(request, colectica_module_id, forsta_module_id):
    colectica_module, forsta_module = _get_module_pair(request, colectica_module_id, forsta_module_id)

    discrepancies_for_pair = RoutingDiscrepancy.objects.filter(
        question_match__colectica_question__module=colectica_module,
        question_match__forsta_question__module=forsta_module,
    ).select_related(
        "question_match",
        "question_match__colectica_question",
        "question_match__forsta_question",
        "colectica_edge",
        "forsta_edge",
    )
    discrepancy_count_for_pair = discrepancies_for_pair.count()

    selected_discrepancy_type = request.GET.get("discrepancy_type", "")
    discrepancies = discrepancies_for_pair
    if selected_discrepancy_type:
        discrepancies = discrepancies.filter(discrepancy_type=selected_discrepancy_type)

    discrepancies = list(discrepancies)
    for discrepancy in discrepancies:
        discrepancy.explanation = explain_discrepancy(discrepancy)

    # Scoped to *this* Forsta+ module specifically -- QuestionMatch rows are
    # only deleted/recreated per Colectica module (see
    # question_matcher.build_question_matches), so a Colectica module that
    # has been compared against more than one Forsta+ module over time can
    # still have matched-question rows pointing at a stale, different
    # Forsta+ module. Without this filter, "matched" would count those too.
    total_questions = colectica_module.normalized_questions.count()
    matched_count = colectica_module.normalized_questions.filter(
        colectica_matches__forsta_question__module=forsta_module
    ).distinct().count()

    # Whether this exact (colectica_module, forsta_module) pair has ever had
    # "Run matching & comparison" executed. Unmatched QuestionMatch rows
    # have forsta_question=None, so they can't be attributed to a specific
    # Forsta+ module directly -- but build_question_matches always deletes
    # and fully recreates the whole set for a Colectica module in one atomic
    # run, so as soon as we see *any* matched row (or discrepancy) pointing
    # at this Forsta+ module, the entire current set (matched + unmatched)
    # is guaranteed to belong to this pairing's most recent run.
    has_run = matched_count > 0 or discrepancy_count_for_pair > 0

    match_summary = {
        "total": total_questions,
        "matched": matched_count,
        "unmatched": total_questions - matched_count,
    }

    return render(
        request,
        "flowise_questionnaire/routing_diff_report.html",
        {
            "colectica_module": colectica_module,
            "forsta_module": forsta_module,
            "discrepancies": discrepancies,
            "discrepancy_type_choices": RoutingDiscrepancy.DiscrepancyType.choices,
            "selected_discrepancy_type": selected_discrepancy_type,
            "match_summary": match_summary,
            "has_run": has_run,
        },
    )


def _discrepancy_focus_question_names(discrepancy):
    """
    The (colectica_name, forsta_name) pair this detail page's two graph
    panels should each center on: the matched question's own name on that
    side -- always a single, resolvable graph node id.

    Deliberately NOT a RoutingEdge's raw source_question: a compound
    condition stores a comma-joined multi-variable source (e.g. "PERGRID,
    NAME, SNAME, PNAME, PSNAME, RESPEMAILCONF"), which never equals any
    single node id and silently produced an empty neighborhood graph for
    any discrepancy whose edge had more than one source variable.
    """

    match = discrepancy.question_match
    colectica_name = match.colectica_question.name
    forsta_name = match.forsta_question.name if match.forsta_question_id else colectica_name
    return colectica_name, forsta_name


def _discrepancy_highlight_edge(discrepancy):
    edge = discrepancy.colectica_edge or discrepancy.forsta_edge
    if edge is None:
        return None
    return {"source": edge.source_question, "target": edge.target_question}


def _graph_json_payload(graph):
    """
    Same dict shape module_views._graph_template_payload builds for View
    Graph (module_graph.html): {nodes_json, edges_json}. Returns None when
    the module's graph hasn't been built yet, so the template can show a
    friendly message per side instead of erroring.
    """

    if graph is None:
        return None

    return {
        "nodes_json": graph.nodes_json or [],
        "edges_json": graph.edges_json or [],
    }


def _graph_neighborhood_payload(graph_payload, focus_question_name, depth=GRAPH_NEIGHBORHOOD_DEPTH):
    return filter_graph_to_neighborhood(graph_payload, focus_question_name, depth=depth)


@login_required
def routing_diff_discrepancy_detail_view(request, colectica_module_id, forsta_module_id, discrepancy_id):
    colectica_module, forsta_module = _get_module_pair(request, colectica_module_id, forsta_module_id)

    discrepancy = get_object_or_404(
        RoutingDiscrepancy.objects.select_related(
            "question_match",
            "question_match__colectica_question",
            "question_match__forsta_question",
            "colectica_edge",
            "forsta_edge",
        ),
        id=discrepancy_id,
        question_match__colectica_question__module=colectica_module,
        question_match__forsta_question__module=forsta_module,
    )

    explanation = explain_discrepancy(discrepancy)
    colectica_focus_question_name, forsta_focus_question_name = _discrepancy_focus_question_names(discrepancy)
    highlight_edge = _discrepancy_highlight_edge(discrepancy)

    colectica_graph = _graph_neighborhood_payload(
        _graph_json_payload(getattr(colectica_module, "graph", None)),
        colectica_focus_question_name,
    )
    forsta_graph = _graph_neighborhood_payload(
        _graph_json_payload(getattr(forsta_module, "graph", None)),
        forsta_focus_question_name,
    )

    return render(
        request,
        "flowise_questionnaire/routing_diff_detail.html",
        {
            "colectica_module": colectica_module,
            "forsta_module": forsta_module,
            "discrepancy": discrepancy,
            "explanation": explanation,
            "focus_question_name": colectica_focus_question_name,
            "colectica_focus_question_name": colectica_focus_question_name,
            "forsta_focus_question_name": forsta_focus_question_name,
            "highlight_edge": highlight_edge,
            "colectica_graph": colectica_graph,
            "forsta_graph": forsta_graph,
        },
    )
