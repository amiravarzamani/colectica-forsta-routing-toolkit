from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from flowise_questionnaire.models import (
    InterviewSimulatorSession,
    QuestionnaireModule,
)
from flowise_questionnaire.services.interview_simulator_service import (
    InterviewSimulatorServiceError,
    abandon_interview_simulator_session,
    get_interview_simulator_session_state,
    start_interview_simulator_session,
    submit_interview_simulator_answer,
)
from flowise_questionnaire.services.question_presentation import (
    QuestionPresentationError,
)


def _json_body(request):
    if not request.body:
        return {}

    try:
        return json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        raise InterviewSimulatorServiceError("Invalid JSON request body.")


def _error_response(message, *, status=400, details=None):
    payload = {
        "error": message,
    }

    if details is not None:
        payload["details"] = details

    return JsonResponse(payload, status=status)


@login_required
@require_GET
def interview_simulator_page_view(request, module_id):
    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
        user=request.user,
    )

    graph = getattr(module, "graph", None)

    if graph is None:
        graph_nodes_json = []
        graph_edges_json = []
    else:
        graph_nodes_json = getattr(graph, "nodes_json", None) or []
        graph_edges_json = getattr(graph, "edges_json", None) or []

    return render(
        request,
        "flowise_questionnaire/interview_simulator.html",
        {
            "module": module,
            "graph_nodes_json": graph_nodes_json,
            "graph_edges_json": graph_edges_json,
        },
    )


@login_required
@require_POST
def interview_simulator_start_view(request, module_id):
    module = get_object_or_404(
        QuestionnaireModule,
        id=module_id,
        user=request.user,
    )

    try:
        state = start_interview_simulator_session(
            module=module,
            user=request.user,
        )
    except (InterviewSimulatorServiceError, QuestionPresentationError) as exc:
        return _error_response(str(exc), status=400)

    return JsonResponse(
        state,
        json_dumps_params={"indent": 2},
    )


@login_required
@require_GET
def interview_simulator_state_view(request, session_id):
    session = get_object_or_404(
        InterviewSimulatorSession.objects.select_related("module"),
        id=session_id,
        user=request.user,
    )

    try:
        state = get_interview_simulator_session_state(
            session=session,
        )
    except (InterviewSimulatorServiceError, QuestionPresentationError) as exc:
        return _error_response(str(exc), status=400)

    return JsonResponse(
        state,
        json_dumps_params={"indent": 2},
    )


@login_required
@require_POST
def interview_simulator_answer_view(request, session_id):
    session = get_object_or_404(
        InterviewSimulatorSession.objects.select_related("module"),
        id=session_id,
        user=request.user,
    )

    try:
        body = _json_body(request)
        user_answer_text = body.get("answer", "")
        state = submit_interview_simulator_answer(
            session=session,
            user_answer_text=user_answer_text,
        )
    except (InterviewSimulatorServiceError, QuestionPresentationError) as exc:
        return _error_response(str(exc), status=400)

    return JsonResponse(
        state,
        json_dumps_params={"indent": 2},
    )


@login_required
@require_POST
def interview_simulator_abandon_view(request, session_id):
    session = get_object_or_404(
        InterviewSimulatorSession,
        id=session_id,
        user=request.user,
    )

    state = abandon_interview_simulator_session(
        session=session,
    )

    return JsonResponse(
        state,
        json_dumps_params={"indent": 2},
    )