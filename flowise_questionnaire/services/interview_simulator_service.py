from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from flowise_questionnaire.models import (
    InterviewSimulatorSession,
    InterviewSimulatorTurn,
    QuestionnaireModule,
)
from flowise_questionnaire.services.answer_validation import validate_answer_letters
from flowise_questionnaire.services.flowise_interview_wording import (
    build_interview_assistant_message,
)
from flowise_questionnaire.services.interview_router import advance_interview_session
from flowise_questionnaire.services.interview_simulator_contracts import (
    AnswerValidationResult,
    GraphHighlight,
    SimulatorDebugInfo,
    SimulatorStateResponse,
)
from flowise_questionnaire.services.question_presentation import (
    QuestionPresentationError,
    build_simulator_question_for_module,
)


class InterviewSimulatorServiceError(Exception):
    pass


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _get_first_question_name(module: QuestionnaireModule) -> str:
    """
    Step 30 start rule.

    For now we start with the first normalized question by sequence.
    Later we can enhance this with graph_start_summary and external entry context.
    """
    question = module.normalized_questions.order_by("sequence_index").first()

    if question is None:
        raise InterviewSimulatorServiceError(
            f"Module {module.name} has no normalized questions."
        )

    return question.name


def _next_turn_index(session: InterviewSimulatorSession) -> int:
    last_turn = session.turns.order_by("-turn_index").first()

    if last_turn is None:
        return 1

    return last_turn.turn_index + 1


def _build_state_response(
    *,
    session: InterviewSimulatorSession,
    validation: AnswerValidationResult | None = None,
) -> dict[str, Any]:
    """
    Build the main response consumed by the simulator frontend.

    Step 31:
    Assistant wording is built through build_interview_assistant_message().
    If Flowise wording is disabled or fails, that function returns a deterministic
    fallback message and records a warning.
    """
    if session.status == InterviewSimulatorSession.Status.COMPLETED:
        response = SimulatorStateResponse(
            session_id=str(session.id),
            status=session.status,
            assistant_message="Interview completed.",
            current_question=None,
            graph_highlight=GraphHighlight(
                active_node=None,
                previous_node=session.previous_question_name or None,
                active_edge=None,
            ),
            debug=SimulatorDebugInfo(
                variable_name=session.previous_question_name or None,
                routing_reason="Interview completed.",
            ),
            validation=validation,
        )
        return response.to_dict()

    current_question_name = _as_text(session.current_question_name)

    if not current_question_name:
        raise InterviewSimulatorServiceError(
            "Active session has no current_question_name."
        )

    simulator_question = build_simulator_question_for_module(
        session.module,
        current_question_name,
    )

    wording_response = build_interview_assistant_message(
        module_name=session.module.name,
        module_version=session.module.version or "",
        question=simulator_question,
        previous_answers=session.answers_json or {},
        routing_context={
            "previous_question_name": session.previous_question_name or None,
            "current_question_name": session.current_question_name or None,
            "graph_highlight": session.graph_highlight_json or {},
            "debug": session.debug_json or {},
        },
    )

    assistant_message = wording_response.assistant_message

    graph_highlight = session.graph_highlight_json or {
        "active_node": current_question_name,
        "previous_node": session.previous_question_name or None,
        "active_edge": None,
        "candidate_edges": [],
        "rejected_edges": [],
    }

    debug_json = session.debug_json or {
        "variable_name": current_question_name,
        "routing_reason": "Current active question.",
        "condition_evaluations": [],
        "warnings": [],
    }

    response = SimulatorStateResponse(
        session_id=str(session.id),
        status=session.status,
        assistant_message=assistant_message,
        current_question=simulator_question,
        graph_highlight=GraphHighlight(
            active_node=graph_highlight.get("active_node"),
            previous_node=graph_highlight.get("previous_node"),
            active_edge=graph_highlight.get("active_edge"),
            candidate_edges=graph_highlight.get("candidate_edges") or [],
            rejected_edges=graph_highlight.get("rejected_edges") or [],
        ),
        debug=SimulatorDebugInfo(
            variable_name=debug_json.get("variable_name") or current_question_name,
            coded_answer=debug_json.get("coded_answer"),
            routing_reason=debug_json.get("routing_reason") or "",
            condition_evaluations=debug_json.get("condition_evaluations") or [],
            warnings=[
                *(debug_json.get("warnings") or []),
                *wording_response.warnings,
            ],
        ),
        validation=validation,
    )

    return response.to_dict()


@transaction.atomic
def start_interview_simulator_session(
    *,
    module: QuestionnaireModule,
    user,
) -> dict[str, Any]:
    first_question_name = _get_first_question_name(module)

    session = InterviewSimulatorSession.objects.create(
        module=module,
        user=user,
        status=InterviewSimulatorSession.Status.ACTIVE,
        current_question_name=first_question_name,
        previous_question_name="",
        answers_json={},
        routing_trace_json=[],
        graph_highlight_json={
            "active_node": first_question_name,
            "previous_node": None,
            "active_edge": None,
            "candidate_edges": [],
            "rejected_edges": [],
        },
        debug_json={
            "variable_name": first_question_name,
            "routing_reason": "First question selected for simulator session.",
            "condition_evaluations": [],
            "warnings": [],
        },
    )

    state = _build_state_response(session=session)

    current_question = state.get("current_question") or {}

    InterviewSimulatorTurn.objects.create(
        session=session,
        turn_index=1,
        question_name=current_question.get("name") or first_question_name,
        question_text=current_question.get("text") or "",
        selection_mode=current_question.get("selection_mode") or "",
        shown_options_json=current_question.get("options") or [],
        assistant_message=state.get("assistant_message") or "",
        graph_highlight_json=state.get("graph_highlight") or {},
    )

    return state


@transaction.atomic
def submit_interview_simulator_answer(
    *,
    session: InterviewSimulatorSession,
    user_answer_text: str,
) -> dict[str, Any]:
    session = (
        InterviewSimulatorSession.objects
        .select_for_update()
        .select_related("module")
        .get(pk=session.pk)
    )

    if session.status != InterviewSimulatorSession.Status.ACTIVE:
        raise InterviewSimulatorServiceError(
            f"Session is not active. Current status: {session.status}."
        )

    current_question_name = _as_text(session.current_question_name)

    if not current_question_name:
        raise InterviewSimulatorServiceError(
            "Session has no current question."
        )

    simulator_question = build_simulator_question_for_module(
        session.module,
        current_question_name,
    )

    validation = validate_answer_letters(
        simulator_question,
        user_answer_text,
    )

    turn_index = _next_turn_index(session)

    if not validation.accepted:
        state = _build_state_response(
            session=session,
            validation=validation,
        )

        InterviewSimulatorTurn.objects.create(
            session=session,
            turn_index=turn_index,
            question_name=current_question_name,
            question_text=simulator_question.text,
            selection_mode=simulator_question.selection_mode,
            shown_options_json=[
                option.to_dict()
                for option in simulator_question.options
            ],
            assistant_message=state.get("assistant_message") or "",
            user_answer_text=user_answer_text,
            user_answer_letters_json=validation.user_answer_letters,
            coded_answer_json={},
            validation_json=validation.to_dict(),
            graph_highlight_json=state.get("graph_highlight") or {},
        )

        return state

    routing_result = advance_interview_session(
        session=session,
        coded_answer=validation.coded_answer or {},
    )

    refreshed_session = (
        InterviewSimulatorSession.objects
        .select_related("module")
        .get(pk=session.pk)
    )

    state = _build_state_response(
        session=refreshed_session,
        validation=validation,
    )

    InterviewSimulatorTurn.objects.create(
        session=refreshed_session,
        turn_index=turn_index,
        question_name=current_question_name,
        question_text=simulator_question.text,
        selection_mode=simulator_question.selection_mode,
        shown_options_json=[
            option.to_dict()
            for option in simulator_question.options
        ],
        assistant_message=state.get("assistant_message") or "",
        user_answer_text=user_answer_text,
        user_answer_letters_json=validation.user_answer_letters,
        coded_answer_json=validation.coded_answer or {},
        validation_json=validation.to_dict(),
        routing_result_json=routing_result,
        graph_highlight_json=state.get("graph_highlight") or {},
    )

    return state


def get_interview_simulator_session_state(
    *,
    session: InterviewSimulatorSession,
) -> dict[str, Any]:
    return _build_state_response(session=session)


@transaction.atomic
def abandon_interview_simulator_session(
    *,
    session: InterviewSimulatorSession,
) -> dict[str, Any]:
    session = (
        InterviewSimulatorSession.objects
        .select_for_update()
        .get(pk=session.pk)
    )

    session.status = InterviewSimulatorSession.Status.ABANDONED
    session.completed_at = timezone.now()
    session.save(
        update_fields=[
            "status",
            "completed_at",
            "updated_at",
        ]
    )

    return {
        "session_id": str(session.id),
        "status": session.status,
    }
