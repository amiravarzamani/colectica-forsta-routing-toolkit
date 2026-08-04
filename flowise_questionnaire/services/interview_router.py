from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from flowise_questionnaire.models import (
    InterviewSimulatorSession,
    QuestionnaireModule,
)
from flowise_questionnaire.services.condition_evaluator import evaluate_condition
from flowise_questionnaire.services.interview_simulator_contracts import (
    GraphHighlight,
    SimulatorDebugInfo,
)


class InterviewRouterError(Exception):
    """
    Raised when the interview router cannot advance deterministically.
    """


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalise_name(value: Any) -> str:
    return _as_text(value).upper()


def _merge_answers(
    existing_answers: dict[str, Any],
    coded_answer: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing_answers or {})

    for key, value in (coded_answer or {}).items():
        clean_key = _as_text(key)

        if clean_key:
            merged[clean_key] = value

    return merged


def _answers_for_condition_evaluator(answers: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}

    for key, value in (answers or {}).items():
        key_text = _as_text(key)

        if not key_text:
            continue

        context[key_text] = value
        context[key_text.upper()] = value
        context[key_text.lower()] = value

    return context


def _question_name_set(module: QuestionnaireModule) -> set[str]:
    return {
        _normalise_name(question.name)
        for question in module.normalized_questions.all()
    }


def _get_question_by_name(module: QuestionnaireModule, question_name: str):
    return (
        module.normalized_questions
        .filter(name__iexact=question_name)
        .order_by("sequence_index")
        .first()
    )


def _get_ordered_questions_after(
    module: QuestionnaireModule,
    current_question_name: str,
):
    current_question = _get_question_by_name(module, current_question_name)

    if current_question is None:
        return []

    return list(
        module.normalized_questions
        .filter(sequence_index__gt=current_question.sequence_index)
        .order_by("sequence_index")
    )


def _condition_text_from_edge(edge) -> str:
    return _as_text(getattr(edge, "condition_text", ""))


def _edge_source_name(edge) -> str:
    return _as_text(getattr(edge, "source_question", ""))


def _edge_target_name(edge) -> str:
    return _as_text(getattr(edge, "target_question", ""))


def _edge_type(edge) -> str:
    return _as_text(getattr(edge, "edge_type", ""))


def _edge_identifier(edge) -> str:
    if edge is None:
        return ""

    source = _edge_source_name(edge)
    target = _edge_target_name(edge)
    condition = _condition_text_from_edge(edge)

    if condition:
        return f"{source}->{target}::{condition}"

    return f"{source}->{target}"


def _source_matches_current_question(edge, current_question_name: str) -> bool:
    source = _edge_source_name(edge)

    if not source:
        return False

    current = _normalise_name(current_question_name)

    parts = [
        _normalise_name(part)
        for part in source.split(",")
        if _as_text(part)
    ]

    return current in parts


def _condition_status_is_true(result: dict[str, Any]) -> bool:
    return _as_text(result.get("status")).lower() == "true"


def _condition_status_is_false(result: dict[str, Any]) -> bool:
    return _as_text(result.get("status")).lower() == "false"


def _evaluate_conditional_edge(
    edge,
    answers: dict[str, Any],
) -> dict[str, Any]:
    condition_text = _condition_text_from_edge(edge)

    if not condition_text:
        return {
            "edge_id": str(getattr(edge, "id", "")),
            "source": _edge_source_name(edge),
            "target": _edge_target_name(edge),
            "condition_text": condition_text,
            "status": "unknown",
            "missing_inputs": [],
            "used_inputs": {},
            "warnings": ["Conditional edge has no condition text."],
        }

    result = evaluate_condition(
        condition_text,
        _answers_for_condition_evaluator(answers),
    )

    return {
        "edge_id": str(getattr(edge, "id", "")),
        "source": _edge_source_name(edge),
        "target": _edge_target_name(edge),
        "condition_text": condition_text,
        "status": result.get("status"),
        "missing_inputs": result.get("missing_inputs") or [],
        "used_inputs": result.get("used_inputs") or {},
        "normalized_expression": result.get("normalized_expression") or "",
        "warnings": result.get("warnings") or [],
    }


def _candidate_edges_from_current_question(
    module: QuestionnaireModule,
    current_question_name: str,
):
    return [
        edge
        for edge in module.routing_edges.all().order_by("sequence_index")
        if _source_matches_current_question(edge, current_question_name)
    ]


def _incoming_conditional_edges_for_question(
    module: QuestionnaireModule,
    question_name: str,
):
    return list(
        module.routing_edges
        .filter(
            target_question__iexact=question_name,
            edge_type="conditional",
        )
        .order_by("sequence_index")
    )


def _is_question_eligible_by_incoming_conditions(
    *,
    module: QuestionnaireModule,
    question_name: str,
    answers: dict[str, Any],
) -> dict[str, Any]:
    """
    Decide whether a question can be reached during sequence fallback.

    Rule:
    - If the question has no incoming conditional routes, it is eligible.
    - If at least one incoming conditional route evaluates true, it is eligible.
    - If incoming conditional routes exist but none is true, it is not eligible yet.
    """
    incoming_edges = _incoming_conditional_edges_for_question(
        module,
        question_name,
    )

    if not incoming_edges:
        return {
            "eligible": True,
            "selected_edge": None,
            "condition_evaluations": [],
            "reason": "Question has no incoming conditional gate.",
            "warnings": [],
        }

    evaluations = []
    warnings = []

    for edge in incoming_edges:
        evaluation = _evaluate_conditional_edge(edge, answers)
        evaluations.append(evaluation)

        for warning in evaluation.get("warnings") or []:
            warnings.append(warning)

        if _condition_status_is_true(evaluation):
            return {
                "eligible": True,
                "selected_edge": edge,
                "condition_evaluations": evaluations,
                "reason": (
                    "Question is eligible because an incoming condition "
                    f"evaluated true: {_condition_text_from_edge(edge)}"
                ),
                "warnings": warnings,
            }

    return {
        "eligible": False,
        "selected_edge": None,
        "condition_evaluations": evaluations,
        "reason": "Question skipped because no incoming conditional route evaluated true.",
        "warnings": warnings,
    }


def _find_next_eligible_question_by_sequence(
    *,
    module: QuestionnaireModule,
    current_question_name: str,
    answers: dict[str, Any],
) -> dict[str, Any]:
    """
    Scan forward by sequence and skip conditionally gated questions whose
    incoming route conditions are not true.
    """
    skipped_questions = []
    condition_evaluations = []
    rejected_edges = []
    warnings = []

    for question in _get_ordered_questions_after(module, current_question_name):
        eligibility = _is_question_eligible_by_incoming_conditions(
            module=module,
            question_name=question.name,
            answers=answers,
        )

        condition_evaluations.extend(eligibility["condition_evaluations"])
        warnings.extend(eligibility["warnings"])

        if eligibility["eligible"]:
            selected_edge = eligibility.get("selected_edge")

            return {
                "next_question_name": question.name,
                "selected_edge": selected_edge,
                "skipped_questions": skipped_questions,
                "condition_evaluations": condition_evaluations,
                "rejected_edges": rejected_edges,
                "routing_reason": (
                    f"Advanced by sequence to the next eligible question: {question.name}. "
                    f"{eligibility['reason']}"
                ),
                "warnings": warnings,
            }

        skipped_questions.append(
            {
                "question_name": question.name,
                "reason": eligibility["reason"],
            }
        )

        for evaluation in eligibility["condition_evaluations"]:
            rejected_edges.append(
                f"{evaluation['source']}->{evaluation['target']}::{evaluation['condition_text']}"
            )

    return {
        "next_question_name": "",
        "selected_edge": None,
        "skipped_questions": skipped_questions,
        "condition_evaluations": condition_evaluations,
        "rejected_edges": rejected_edges,
        "routing_reason": "No next eligible question found; interview completed.",
        "warnings": warnings,
    }


def _choose_next_question(
    *,
    module: QuestionnaireModule,
    current_question_name: str,
    answers: dict[str, Any],
) -> dict[str, Any]:
    question_names = _question_name_set(module)
    candidate_edges = _candidate_edges_from_current_question(
        module,
        current_question_name,
    )

    condition_evaluations: list[dict[str, Any]] = []
    candidate_edge_ids: list[str] = []
    rejected_edge_ids: list[str] = []
    warnings: list[str] = []

    sequential_edges = []
    true_conditional_edges = []

    for edge in candidate_edges:
        edge_type = _edge_type(edge)
        edge_id = _edge_identifier(edge)
        target = _edge_target_name(edge)

        candidate_edge_ids.append(edge_id)

        if edge_type == "conditional":
            evaluation = _evaluate_conditional_edge(edge, answers)
            condition_evaluations.append(evaluation)

            if _condition_status_is_true(evaluation):
                if _normalise_name(target) in question_names:
                    true_conditional_edges.append(edge)
                else:
                    rejected_edge_ids.append(edge_id)
                    warnings.append(
                        f"Conditional edge to unknown target ignored: {target}"
                    )
            else:
                rejected_edge_ids.append(edge_id)

        elif edge_type == "sequential":
            sequential_edges.append(edge)

        elif edge_type == "loop":
            rejected_edge_ids.append(edge_id)
            warnings.append(f"Loop edge ignored by Step 29 router: {edge_id}")

        else:
            rejected_edge_ids.append(edge_id)
            warnings.append(f"Unknown edge type ignored: {edge_type}")

    selected_edge = None
    next_question_name = ""
    skipped_questions = []

    if true_conditional_edges:
        selected_edge = true_conditional_edges[0]
        next_question_name = _edge_target_name(selected_edge)
        routing_reason = (
            "Conditional route selected because condition evaluated true: "
            f"{_condition_text_from_edge(selected_edge)}"
        )
    elif sequential_edges:
        sequence_result = _find_next_eligible_question_by_sequence(
            module=module,
            current_question_name=current_question_name,
            answers=answers,
        )

        selected_edge = sequence_result.get("selected_edge")
        next_question_name = sequence_result["next_question_name"]
        skipped_questions = sequence_result["skipped_questions"]
        condition_evaluations.extend(sequence_result["condition_evaluations"])
        rejected_edge_ids.extend(sequence_result["rejected_edges"])
        warnings.extend(sequence_result["warnings"])
        routing_reason = sequence_result["routing_reason"]
    else:
        sequence_result = _find_next_eligible_question_by_sequence(
            module=module,
            current_question_name=current_question_name,
            answers=answers,
        )

        selected_edge = sequence_result.get("selected_edge")
        next_question_name = sequence_result["next_question_name"]
        skipped_questions = sequence_result["skipped_questions"]
        condition_evaluations.extend(sequence_result["condition_evaluations"])
        rejected_edge_ids.extend(sequence_result["rejected_edges"])
        warnings.extend(sequence_result["warnings"])
        routing_reason = sequence_result["routing_reason"]

    if next_question_name and _normalise_name(next_question_name) not in question_names:
        warnings.append(
            f"Selected next question is not in normalized questions: {next_question_name}"
        )
        next_question_name = ""

    active_edge_id = _edge_identifier(selected_edge) if selected_edge else None

    return {
        "next_question_name": next_question_name,
        "selected_edge": {
            "id": active_edge_id,
            "source": _edge_source_name(selected_edge) if selected_edge else "",
            "target": _edge_target_name(selected_edge) if selected_edge else "",
            "type": _edge_type(selected_edge) if selected_edge else "",
            "condition_text": (
                _condition_text_from_edge(selected_edge)
                if selected_edge
                else ""
            ),
        } if selected_edge else None,
        "candidate_edges": candidate_edge_ids,
        "rejected_edges": rejected_edge_ids,
        "skipped_questions": skipped_questions,
        "condition_evaluations": condition_evaluations,
        "routing_reason": routing_reason,
        "warnings": warnings,
    }


@transaction.atomic
def advance_interview_session(
    *,
    session: InterviewSimulatorSession,
    coded_answer: dict[str, Any],
) -> dict[str, Any]:
    session = (
        InterviewSimulatorSession.objects
        .select_for_update()
        .select_related("module")
        .get(pk=session.pk)
    )

    if session.status != InterviewSimulatorSession.Status.ACTIVE:
        raise InterviewRouterError(
            f"Cannot advance simulator session with status {session.status}."
        )

    current_question_name = _as_text(session.current_question_name)

    if not current_question_name:
        raise InterviewRouterError("Session has no current_question_name.")

    answers = _merge_answers(session.answers_json, coded_answer)

    routing_result = _choose_next_question(
        module=session.module,
        current_question_name=current_question_name,
        answers=answers,
    )

    next_question_name = routing_result["next_question_name"]

    graph_highlight = GraphHighlight(
        active_node=next_question_name or None,
        previous_node=current_question_name,
        active_edge=(
            routing_result["selected_edge"]["id"]
            if routing_result.get("selected_edge")
            else None
        ),
        candidate_edges=routing_result["candidate_edges"],
        rejected_edges=routing_result["rejected_edges"],
    )

    debug = SimulatorDebugInfo(
        variable_name=current_question_name,
        coded_answer=coded_answer,
        routing_reason=routing_result["routing_reason"],
        condition_evaluations=routing_result["condition_evaluations"],
        warnings=routing_result["warnings"],
    )

    routing_trace = list(session.routing_trace_json or [])
    routing_trace.append(
        {
            "from_question": current_question_name,
            "coded_answer": coded_answer,
            "to_question": next_question_name,
            "selected_edge": routing_result.get("selected_edge"),
            "candidate_edges": routing_result["candidate_edges"],
            "rejected_edges": routing_result["rejected_edges"],
            "skipped_questions": routing_result.get("skipped_questions") or [],
            "condition_evaluations": routing_result["condition_evaluations"],
            "routing_reason": routing_result["routing_reason"],
            "warnings": routing_result["warnings"],
            "timestamp": timezone.now().isoformat(),
        }
    )

    session.answers_json = answers
    session.previous_question_name = current_question_name
    session.current_question_name = next_question_name
    session.routing_trace_json = routing_trace
    session.graph_highlight_json = graph_highlight.to_dict()
    session.debug_json = debug.to_dict()

    if not next_question_name:
        session.status = InterviewSimulatorSession.Status.COMPLETED
        session.completed_at = timezone.now()

    session.save(
        update_fields=[
            "answers_json",
            "previous_question_name",
            "current_question_name",
            "routing_trace_json",
            "graph_highlight_json",
            "debug_json",
            "status",
            "completed_at",
            "updated_at",
        ]
    )

    return {
        "session_id": str(session.id),
        "status": session.status,
        "previous_question_name": current_question_name,
        "current_question_name": next_question_name,
        "answers": session.answers_json,
        "routing_result": routing_result,
        "graph_highlight": graph_highlight.to_dict(),
        "debug": debug.to_dict(),
    }