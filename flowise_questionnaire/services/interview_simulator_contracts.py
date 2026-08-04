from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


SelectionMode = Literal["single", "multiple", "number", "text"]
SimulatorSessionStatus = Literal["active", "completed", "abandoned", "error"]


@dataclass(frozen=True)
class SimulatorOption:
    """
    Respondent-facing option.

    `letter` is what the respondent sees and submits.
    `code` is the real questionnaire answer code used internally for routing.
    """

    letter: str
    code: Any
    label: str
    exclusive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "letter": self.letter,
            "code": self.code,
            "label": self.label,
            "exclusive": self.exclusive,
        }


@dataclass(frozen=True)
class SimulatorQuestion:
    """
    Respondent-facing question payload.

    The respondent must see `text` and, for closed questions, `options`.
    `name` is kept for backend/debug/graph use only.
    """

    name: str
    text: str
    selection_mode: SelectionMode
    options: list[SimulatorOption]
    help_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "text": self.text,
            "selection_mode": self.selection_mode,
            "help_text": self.help_text,
            "options": [option.to_dict() for option in self.options],
        }


@dataclass(frozen=True)
class GraphHighlight:
    """
    Frontend graph synchronization payload.
    """

    active_node: str | None = None
    previous_node: str | None = None
    active_edge: str | None = None
    candidate_edges: list[str] = field(default_factory=list)
    rejected_edges: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_node": self.active_node,
            "previous_node": self.previous_node,
            "active_edge": self.active_edge,
            "candidate_edges": self.candidate_edges,
            "rejected_edges": self.rejected_edges,
        }


@dataclass(frozen=True)
class SimulatorDebugInfo:
    """
    Designer-facing debug payload.

    This is not respondent-facing chat text.
    """

    variable_name: str | None = None
    coded_answer: dict[str, Any] | None = None
    routing_reason: str = ""
    condition_evaluations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "variable_name": self.variable_name,
            "coded_answer": self.coded_answer,
            "routing_reason": self.routing_reason,
            "condition_evaluations": self.condition_evaluations,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class FlowiseQuestionRequest:
    """
    Payload Django sends to Flowise.

    Flowise may format wording, but must not invent options,
    expose variable names, validate answers, or choose routing.
    """

    module_name: str
    module_version: str
    question: SimulatorQuestion
    previous_answers: dict[str, Any] = field(default_factory=dict)
    routing_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        rules = [
            "Ask only the real question text.",
            "Do not show internal variable names to the respondent.",
            "Do not invent answer options.",
            "Do not decide the next question.",
            "Do not perform routing.",
        ]

        if self.question.selection_mode == "multiple":
            rules.append("Use only the provided A/B/C option letters.")
            rules.append("Tell the respondent they may choose all that apply.")
        elif self.question.selection_mode == "single":
            rules.append("Use only the provided A/B/C option letters.")
            rules.append("Tell the respondent to choose one option only.")
        elif self.question.selection_mode == "number":
            rules.append("Do not create A/B/C options.")
            rules.append("Tell the respondent to enter a number.")
        else:
            rules.append("Do not create A/B/C options.")
            rules.append("Tell the respondent to type their answer.")

        return {
            "module": {
                "name": self.module_name,
                "version": self.module_version,
            },
            "question": self.question.to_dict(),
            "previous_answers": self.previous_answers,
            "routing_context": self.routing_context,
            "strict_rules": rules,
        }


@dataclass(frozen=True)
class FlowiseQuestionResponse:
    """
    Normalized response expected back from Flowise.
    """

    assistant_message: str
    raw_response: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "assistant_message": self.assistant_message,
            "raw_response": self.raw_response,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class AnswerValidationResult:
    """
    Result of validating respondent input.

    For option questions, `user_answer_letters` contains A/B/C selections.
    For raw-entry questions, it is empty and `coded_answer` stores the typed value.
    """

    accepted: bool
    user_answer_letters: list[str] = field(default_factory=list)
    coded_answer: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "user_answer_letters": self.user_answer_letters,
            "coded_answer": self.coded_answer,
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class SimulatorStateResponse:
    """
    Main API response contract used by the simulator UI.
    """

    session_id: str
    status: SimulatorSessionStatus
    assistant_message: str
    current_question: SimulatorQuestion | None
    graph_highlight: GraphHighlight
    debug: SimulatorDebugInfo
    validation: AnswerValidationResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "assistant_message": self.assistant_message,
            "current_question": (
                self.current_question.to_dict()
                if self.current_question
                else None
            ),
            "graph_highlight": self.graph_highlight.to_dict(),
            "debug": self.debug.to_dict(),
            "validation": (
                self.validation.to_dict()
                if self.validation
                else None
            ),
        }


def _instruction_for_question(question: SimulatorQuestion) -> str:
    if question.selection_mode == "multiple":
        return "Choose all that apply. Answer using letters only, for example: A, C."

    if question.selection_mode == "number":
        return "Enter a number, then press Submit."

    if question.selection_mode == "text":
        return "Type your answer, then press Submit."

    return "Choose one option only. Answer using one letter only, for example: A."


def build_fallback_assistant_message(question: SimulatorQuestion) -> str:
    """
    Deterministic fallback message if Flowise is unavailable.

    This keeps the simulator usable even when the AI agent fails.
    """

    option_lines = [
        f"{option.letter}. {option.label}"
        for option in question.options
    ]

    parts = [question.text.strip()]

    if option_lines:
        parts.extend(["", *option_lines])

    parts.extend(["", _instruction_for_question(question)])

    return "\n".join(parts).strip()
