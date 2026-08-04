from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List
from uuid import UUID

from django.shortcuts import get_object_or_404, render
from rest_framework import status
from rest_framework.decorators import api_view, renderer_classes
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from flowise_questionnaire.models import QuestionnaireModule
from flowise_questionnaire.services.agentflow_payload_builder import (
    build_compact_agentflow_payload_for_module,
)
from flowise_questionnaire.services.routing_simulator import (
    simulate_all_coverage_intents_for_module,
)


def _build_status_counts(results: List[Dict[str, Any]]) -> Counter:
    return Counter(
        str(result.get("coverage_status") or "unknown")
        for result in results
    )


def _build_response_payload(
    *,
    module: QuestionnaireModule,
    results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    status_counts = _build_status_counts(results)

    return {
        "module": {
            "id": str(module.id),
            "name": module.name,
            "version": module.version,
        },
        "coverage_intent_count": len(results),
        "covered_count": status_counts.get("covered", 0),
        "not_covered_count": status_counts.get("not_covered", 0),
        "blocked_missing_inputs_count": status_counts.get("blocked_missing_inputs", 0),
        "target_edge_not_found_count": status_counts.get("target_edge_not_found", 0),
        "no_target_edges_count": status_counts.get("no_target_edges", 0),
        "unknown_count": status_counts.get("unknown", 0),
        "status_counts": dict(status_counts),
        "results": results,
    }


def _run_routing_simulation(module: QuestionnaireModule) -> Dict[str, Any]:
    compact_payload = build_compact_agentflow_payload_for_module(module)
    coverage_section = compact_payload.get("coverage_intents") or {}
    coverage_intents = coverage_section.get("intents") or []

    results = simulate_all_coverage_intents_for_module(
        module=module,
        coverage_intents=coverage_intents,
    )

    return _build_response_payload(
        module=module,
        results=results,
    )


def module_routing_simulation_view(request, module_id: UUID):
    """
    Render deterministic routing simulation as an HTML GUI page.
    """

    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
    )

    try:
        simulation = _run_routing_simulation(module)

        return render(
            request,
            "flowise_questionnaire/module_routing_simulation.html",
            {
                "module": module,
                "simulation": simulation,
                "results": simulation.get("results", []),
            },
        )

    except Exception as exc:
        return render(
            request,
            "flowise_questionnaire/module_routing_simulation.html",
            {
                "module": module,
                "simulation": None,
                "results": [],
                "error": str(exc),
            },
            status=500,
        )


@api_view(["GET"])
@renderer_classes([JSONRenderer])
def module_routing_simulation_json_view(request, module_id: UUID):
    """
    Return deterministic routing simulation as JSON only.
    """

    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
    )

    try:
        response_payload = _run_routing_simulation(module)
        return Response(response_payload, status=status.HTTP_200_OK)

    except Exception as exc:
        return Response(
            {
                "error": "routing_simulation_failed",
                "detail": str(exc),
                "module": {
                    "id": str(module.id),
                    "name": module.name,
                    "version": module.version,
                },
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )