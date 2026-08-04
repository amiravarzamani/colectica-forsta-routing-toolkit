import json
from xml.etree import ElementTree as ET

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify

from flowise_questionnaire.forms import QuestionnaireModuleUploadForm
from flowise_questionnaire.models import (
    ModuleAIReview,
    QuestionnaireModule,
)
from flowise_questionnaire.services.agentflow_payload_builder import (
    build_agentflow_payload_for_module,
    build_compact_agentflow_payload_for_module,
)
from flowise_questionnaire.services.flowise_client import (
    FlowiseClientError,
    run_module_review_agentflow,
)
from flowise_questionnaire.services.graph_builder import build_graph_for_module
from flowise_questionnaire.services.graph_enrichment import enrich_graph_dependencies
from flowise_questionnaire.services.routing_extractor import extract_routing_for_module
from flowise_questionnaire.services.routing_simulator import (
    simulate_all_coverage_intents_for_module,
)
from flowise_questionnaire.services.schema_extractor import extract_schema_for_module


RAW_JSON_PREVIEW_LIMIT = 2_000_000


@login_required
def module_detail_view(request, module_id):
    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
        user=request.user,
    )

    questions = module.normalized_questions.all().order_by("sequence_index")
    routing_edges = module.routing_edges.all().order_by("sequence_index")
    graph = getattr(module, "graph", None)
    latest_ai_review = module.ai_reviews.first()

    raw_json_preview = ""
    raw_json_truncated = False

    if module.raw_json:
        try:
            raw_json_text = json.dumps(module.raw_json, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            raw_json_text = ""

        if len(raw_json_text) > RAW_JSON_PREVIEW_LIMIT:
            raw_json_preview = raw_json_text[:RAW_JSON_PREVIEW_LIMIT]
            raw_json_truncated = True
        else:
            raw_json_preview = raw_json_text

    return render(
        request,
        "flowise_questionnaire/module_detail.html",
        {
            "module": module,
            "questions": questions,
            "question_count": questions.count(),
            "routing_edges": routing_edges,
            "routing_edge_count": routing_edges.count(),
            "graph": graph,
            "latest_ai_review": latest_ai_review,
            "raw_json_preview": raw_json_preview,
            "raw_json_truncated": raw_json_truncated,
        },
    )


@login_required
def module_list_view(request):
    modules = (
        QuestionnaireModule.objects
        .filter(user=request.user)
        .prefetch_related("ai_reviews")
        .order_by("-created_at")
    )

    module_rows = []

    for module in modules:
        latest_ai_review = module.ai_reviews.first()

        module_rows.append(
            {
                "module": module,
                "latest_ai_review": latest_ai_review,
            }
        )

    colectica_modules = [
        module for module in modules
        if module.source_format == QuestionnaireModule.SourceFormat.COLECTICA_JSON
    ]
    forsta_modules = [
        module for module in modules
        if module.source_format == QuestionnaireModule.SourceFormat.FORSTA_XML
    ]

    return render(
        request,
        "flowise_questionnaire/module_list.html",
        {
            "module_rows": module_rows,
            "modules": modules,
            "colectica_modules": colectica_modules,
            "forsta_modules": forsta_modules,
        },
    )


@login_required
def module_upload_view(request):
    if request.method == "POST":
        form = QuestionnaireModuleUploadForm(request.POST, request.FILES)

        if form.is_valid():
            uploaded_file = form.cleaned_data["uploaded_file"]
            is_xml = uploaded_file.name.lower().endswith(".xml")

            if is_xml:
                try:
                    uploaded_file.seek(0)
                    raw_xml = uploaded_file.read().decode("utf-8", errors="replace")
                    uploaded_file.seek(0)
                    ET.fromstring(raw_xml)
                except ET.ParseError as exc:
                    form.add_error("uploaded_file", f"Invalid XML file: {exc}")
                    raw_xml = None
            else:
                try:
                    uploaded_file.seek(0)
                    raw_json = json.load(uploaded_file)
                    uploaded_file.seek(0)
                except json.JSONDecodeError as exc:
                    form.add_error("uploaded_file", f"Invalid JSON file: {exc}")
                    raw_json = None

            has_error = (is_xml and raw_xml is None) or (not is_xml and raw_json is None)

            if not has_error:
                module = form.save(commit=False)
                module.user = request.user

                if is_xml:
                    module.raw_xml = raw_xml
                    module.source_format = QuestionnaireModule.SourceFormat.FORSTA_XML
                    name_suffix = ".xml"
                else:
                    module.raw_json = raw_json
                    module.source_format = QuestionnaireModule.SourceFormat.COLECTICA_JSON
                    name_suffix = ".json"

                if not module.name:
                    module.name = slugify(uploaded_file.name[: -len(name_suffix)])

                module.processing_status = QuestionnaireModule.ProcessingStatus.UPLOADED
                module.save()

                messages.success(request, "Questionnaire file uploaded successfully.")
                return redirect("flowise_questionnaire:module_list")
    else:
        form = QuestionnaireModuleUploadForm()

    return render(
        request,
        "flowise_questionnaire/module_upload.html",
        {
            "form": form,
        },
    )


@login_required
def module_extract_schema_view(request, module_id):
    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
        user=request.user,
    )

    if request.method != "POST":
        messages.error(request, "Invalid request method.")
        return redirect("flowise_questionnaire:module_detail", module_id=module.id)

    try:
        result = extract_schema_for_module(module)
    except Exception as exc:
        module.processing_status = QuestionnaireModule.ProcessingStatus.FAILED
        module.error_message = str(exc)
        module.save(update_fields=["processing_status", "error_message", "updated_at"])

        messages.error(request, f"Schema extraction failed: {exc}")
    else:
        messages.success(
            request,
            f"Schema extracted successfully. Questions extracted: {result['question_count']}",
        )

    return redirect("flowise_questionnaire:module_detail", module_id=module.id)


def _normalize_route_key_part(value):
    """
    Normalize one route-key part for stable lookup matching.
    """

    return " ".join(str(value or "").strip().lower().split())


def _make_route_coverage_key(source, target, condition_text):
    """
    Strict route key:
        source || target || condition

    This works when the graph edge source exactly matches the simulator source.
    """

    return "||".join(
        [
            _normalize_route_key_part(source),
            _normalize_route_key_part(target),
            _normalize_route_key_part(condition_text),
        ]
    )


def _make_target_condition_coverage_key(target, condition_text):
    """
    Fallback route key:
        target || condition

    This fixes multi-source routes where the simulator may report one route as:
        FICODE, FRVAL -> FITAX

    but the graph may render equivalent visual edges as:
        FICODE -> FITAX
        FRVAL -> FITAX
    """

    return "||".join(
        [
            _normalize_route_key_part(target),
            _normalize_route_key_part(condition_text),
        ]
    )




def _graph_template_payload(graph):
    """
    Return the graph data shape expected by module_graph.html.

    module.graph is a model instance in the current app, but the enrichment
    service works on a plain dictionary. Django templates can read dictionary
    keys with dot syntax, so returning a dict keeps the existing template
    contract: graph.nodes_json and graph.edges_json.
    """

    if isinstance(graph, dict):
        return {
            "nodes_json": graph.get("nodes_json") or [],
            "edges_json": graph.get("edges_json") or [],
        }

    return {
        "nodes_json": getattr(graph, "nodes_json", None) or [],
        "edges_json": getattr(graph, "edges_json", None) or [],
    }


def _build_routing_simulation_payload(module):
    """
    Build deterministic routing simulation payload for a module.

    This reuses the compact agentflow payload because coverage intents are generated there.
    """

    compact_payload = build_compact_agentflow_payload_for_module(module)
    coverage_section = compact_payload.get("coverage_intents") or {}
    coverage_intents = coverage_section.get("intents") or []

    return simulate_all_coverage_intents_for_module(
        module=module,
        coverage_intents=coverage_intents,
    )


def _prefer_coverage_record(existing, candidate):
    """
    Prefer a covered record over unknown/not-covered records when duplicate keys exist.
    """

    if not existing:
        return candidate

    if existing.get("coverage_status") == "covered":
        return existing

    if candidate.get("coverage_status") == "covered":
        return candidate

    return existing


def _build_route_coverage_lookup(simulation_results):
    """
    Build lookup from route identity to coverage metadata.

    The returned lookup has two indexes:

    by_full_route:
        source || target || condition

    by_target_condition:
        target || condition

    The second index fixes multi-source graph display where one simulator route may be
    rendered as multiple visual graph edges.
    """

    lookup = {
        "by_full_route": {},
        "by_target_condition": {},
    }

    for result in simulation_results:
        intent_id = result.get("intent_id")
        intent_label = result.get("intent_label")
        coverage_status = result.get("coverage_status") or "unknown"

        for target_edge in result.get("target_edges") or []:
            source = target_edge.get("source")
            target = target_edge.get("target")
            condition_text = target_edge.get("condition_text")

            record = {
                "coverage_status": coverage_status,
                "intent_id": intent_id,
                "intent_label": intent_label,
                "target_edge": target_edge,
            }

            full_key = _make_route_coverage_key(
                source,
                target,
                condition_text,
            )

            target_condition_key = _make_target_condition_coverage_key(
                target,
                condition_text,
            )

            if full_key:
                lookup["by_full_route"][full_key] = _prefer_coverage_record(
                    lookup["by_full_route"].get(full_key),
                    record,
                )

            if target_condition_key:
                lookup["by_target_condition"][target_condition_key] = _prefer_coverage_record(
                    lookup["by_target_condition"].get(target_condition_key),
                    record,
                )

    return lookup


@login_required
def module_graph_view(request, module_id):
    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
        user=request.user,
    )

    graph = getattr(module, "graph", None)

    if graph is None:
        raise Http404("Graph has not been generated for this module yet.")

    graph = enrich_graph_dependencies(_graph_template_payload(graph))

    routing_simulation_results = []
    route_coverage_lookup = {}
    routing_simulation_error = None

    try:
        routing_simulation_results = _build_routing_simulation_payload(module)
        route_coverage_lookup = _build_route_coverage_lookup(
            routing_simulation_results
        )
    except Exception as exc:
        # Do not break the graph page if simulation fails.
        # The graph can still render without coverage overlay.
        routing_simulation_error = str(exc)

    return render(
        request,
        "flowise_questionnaire/module_graph.html",
        {
            "module": module,
            "graph": graph,
            "routing_simulation_results": routing_simulation_results,
            "route_coverage_lookup": route_coverage_lookup,
            "routing_simulation_error": routing_simulation_error,
        },
    )


@login_required
def module_extract_routing_view(request, module_id):
    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
        user=request.user,
    )

    if request.method != "POST":
        messages.error(request, "Invalid request method.")
        return redirect("flowise_questionnaire:module_detail", module_id=module.id)

    if not module.normalized_questions.exists():
        messages.error(request, "Extract schema first before extracting routing.")
        return redirect("flowise_questionnaire:module_detail", module_id=module.id)

    try:
        result = extract_routing_for_module(module)
    except Exception as exc:
        messages.error(request, f"Routing extraction failed: {exc}")
    else:
        messages.success(
            request,
            (
                "Routing extracted successfully. "
                f"Total edges: {result['routing_edge_count']}. "
                f"Conditional: {result.get('conditional_edge_count', 0)}. "
                f"Loop: {result.get('loop_edge_count', 0)}. "
                f"Sequential: {result.get('sequential_edge_count', 0)}."
            ),
        )

    return redirect("flowise_questionnaire:module_detail", module_id=module.id)


@login_required
def module_build_graph_view(request, module_id):
    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
        user=request.user,
    )

    if request.method != "POST":
        messages.error(request, "Invalid request method.")
        return redirect("flowise_questionnaire:module_detail", module_id=module.id)

    if not module.routing_edges.exists():
        messages.error(request, "Extract routing first before building the graph.")
        return redirect("flowise_questionnaire:module_detail", module_id=module.id)

    try:
        result = build_graph_for_module(module)
    except Exception as exc:
        messages.error(request, f"Graph build failed: {exc}")
    else:
        messages.success(
            request,
            (
                "Graph built successfully. "
                f"Nodes: {result['node_count']}. "
                f"Edges: {result['edge_count']}."
            ),
        )

    return redirect("flowise_questionnaire:module_detail", module_id=module.id)


@login_required
def module_build_all_view(request, module_id):
    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
        user=request.user,
    )

    if request.method != "POST":
        messages.error(request, "Invalid request method.")
        return redirect("flowise_questionnaire:module_detail", module_id=module.id)

    try:
        schema_result = extract_schema_for_module(module)
    except Exception as exc:
        module.processing_status = QuestionnaireModule.ProcessingStatus.FAILED
        module.error_message = str(exc)
        module.save(update_fields=["processing_status", "error_message", "updated_at"])

        messages.error(request, f"Schema extraction failed: {exc}")
        return redirect("flowise_questionnaire:module_detail", module_id=module.id)

    try:
        routing_result = extract_routing_for_module(module)
    except Exception as exc:
        messages.error(
            request,
            (
                f"Schema extracted (questions: {schema_result['question_count']}), "
                f"but routing extraction failed: {exc}"
            ),
        )
        return redirect("flowise_questionnaire:module_detail", module_id=module.id)

    try:
        graph_result = build_graph_for_module(module)
    except Exception as exc:
        messages.error(
            request,
            (
                f"Schema and routing extracted (edges: {routing_result['routing_edge_count']}), "
                f"but graph build failed: {exc}"
            ),
        )
        return redirect("flowise_questionnaire:module_detail", module_id=module.id)

    messages.success(
        request,
        (
            "Schema, routing, and graph built successfully. "
            f"Questions: {schema_result['question_count']}. "
            f"Routing edges: {routing_result['routing_edge_count']}. "
            f"Graph nodes: {graph_result['node_count']}, edges: {graph_result['edge_count']}."
        ),
    )

    return redirect("flowise_questionnaire:module_detail", module_id=module.id)


@login_required
def module_agentflow_payload_view(request, module_id):
    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
        user=request.user,
    )

    if not module.normalized_questions.exists():
        return JsonResponse(
            {
                "error": "Schema has not been extracted yet.",
                "next_step": "Click Extract schema first.",
            },
            status=400,
        )

    if not module.routing_edges.exists():
        return JsonResponse(
            {
                "error": "Routing has not been extracted yet.",
                "next_step": "Click Extract routing first.",
            },
            status=400,
        )

    payload = build_agentflow_payload_for_module(module)

    return JsonResponse(
        payload,
        json_dumps_params={
            "indent": 2,
        },
    )


@login_required
def module_agentflow_compact_payload_view(request, module_id):
    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
        user=request.user,
    )

    if not module.normalized_questions.exists():
        return JsonResponse(
            {
                "error": "Schema has not been extracted yet.",
                "next_step": "Click Extract schema first.",
            },
            status=400,
        )

    if not module.routing_edges.exists():
        return JsonResponse(
            {
                "error": "Routing has not been extracted yet.",
                "next_step": "Click Extract routing first.",
            },
            status=400,
        )

    payload = build_compact_agentflow_payload_for_module(module)

    return JsonResponse(
        payload,
        json_dumps_params={
            "indent": 2,
        },
    )


@login_required
def module_run_flowise_review_view(request, module_id):
    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
        user=request.user,
    )

    if request.method != "POST":
        return JsonResponse(
            {
                "error": "Method not allowed. Use POST.",
            },
            status=405,
        )

    if not module.normalized_questions.exists():
        return JsonResponse(
            {
                "error": "Schema has not been extracted yet.",
                "next_step": "Click Extract schema first.",
            },
            status=400,
        )

    if not module.routing_edges.exists():
        return JsonResponse(
            {
                "error": "Routing has not been extracted yet.",
                "next_step": "Click Extract routing first.",
            },
            status=400,
        )

    try:
        result = run_module_review_agentflow(module)
    except FlowiseClientError as exc:
        saved_review = ModuleAIReview.objects.create(
            module=module,
            status=ModuleAIReview.Status.ERROR,
            error_message=str(exc),
        )

        return JsonResponse(
            {
                "error": "AI routing review failed.",
                "detail": str(exc),
                "saved_review_id": saved_review.id,
            },
            status=502,
        )

    post_validation = result["post_validation"]

    if post_validation.get("passed"):
        status = ModuleAIReview.Status.PASSED
    else:
        status = ModuleAIReview.Status.FAILED

    saved_review = ModuleAIReview.objects.create(
        module=module,
        status=status,
        review_json=result["review"],
        post_validation_json=post_validation,
        request_payload_json=result["request_payload"],
        raw_response_json=result["raw_response"],
    )

    return JsonResponse(
        {
            "module": {
                "id": str(module.id),
                "name": module.name,
                "version": module.version,
            },
            "saved_review": {
                "id": saved_review.id,
                "status": saved_review.status,
                "created_at": saved_review.created_at.isoformat(),
            },
            "review": result["review"],
            "post_validation": post_validation,
        },
        json_dumps_params={
            "indent": 2,
        },
    )


@login_required
def module_ai_review_history_view(request, module_id):
    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
        user=request.user,
    )

    ai_reviews = module.ai_reviews.all()

    return render(
        request,
        "flowise_questionnaire/module_ai_review_history.html",
        {
            "module": module,
            "ai_reviews": ai_reviews,
        },
    )


@login_required
def module_ai_review_detail_view(request, module_id, review_id):
    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
        user=request.user,
    )

    ai_review = get_object_or_404(
        ModuleAIReview,
        id=review_id,
        module=module,
    )

    return render(
        request,
        "flowise_questionnaire/module_ai_review_detail.html",
        {
            "module": module,
            "ai_review": ai_review,
        },
    )
