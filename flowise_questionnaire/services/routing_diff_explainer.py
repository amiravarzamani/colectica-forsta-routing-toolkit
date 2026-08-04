from __future__ import annotations

from dataclasses import dataclass

from flowise_questionnaire.models import RoutingDiscrepancy, RoutingEdge
from flowise_questionnaire.services.condition_evaluator import strip_condition_wrapper


@dataclass
class EdgeExplanation:
    """
    Structural breakdown of a single RoutingEdge for display: which system it
    came from, which question it originates at, which question it leads to,
    and the raw gating expression that must hold for it to fire. This is a
    labeling of RoutingEdge's own fields (source_question / target_question /
    condition_text) -- it does not evaluate or translate the condition.
    """

    origin_system: str
    source_question: str
    target_question: str
    condition_text: str


@dataclass
class DiscrepancyExplanation:
    summary: str
    edge: EdgeExplanation | None
    meaning: str


def explain_discrepancy(discrepancy: RoutingDiscrepancy) -> DiscrepancyExplanation:
    """
    Turn a RoutingDiscrepancy into plain-language text for the routing diff
    GUI, per routing_comparator.RoutingComparator._compare_match:

    - MISSING_IN_FORSTA: the matched question pair is fine: a specific
      *conditional edge* out of the Colectica question (colectica_edge) has
      no edge with the same target on the Forsta+ side.
    - MISSING_IN_COLECTICA: the mirror image -- a Forsta+ edge (forsta_edge)
      has no Colectica counterpart.
    - ELSE_BRANCH_ONLY_IN_FORSTA: a Forsta+ edge whose condition_text is a
      `NOT (...)` wrapper, i.e. an explicit FalseNodes/"else" branch that
      Colectica's format has no construct for -- expected, not a real gap.
    """

    match = discrepancy.question_match
    colectica_name = match.colectica_question.name
    forsta_question = match.forsta_question
    forsta_name = forsta_question.name if forsta_question else None
    same_name = (
        forsta_name is not None
        and colectica_name.strip().casefold() == forsta_name.strip().casefold()
    )

    if forsta_name:
        if same_name:
            matched_phrase = f"Question {colectica_name} matched in both systems"
        else:
            matched_phrase = (
                f"Colectica question {colectica_name} matched Forsta+ question {forsta_name}"
            )
    else:
        matched_phrase = f"Question {colectica_name}"

    discrepancy_type = discrepancy.discrepancy_type
    DiscrepancyType = RoutingDiscrepancy.DiscrepancyType

    if discrepancy_type == DiscrepancyType.MISSING_IN_FORSTA:
        edge = discrepancy.colectica_edge
        edge_explanation = _build_edge_explanation(edge, "Colectica")
        summary = (
            f"{matched_phrase}, but the routing edge to {edge.target_question}"
            f"{_condition_fragment(edge)} has no equivalent in Forsta+."
            if edge is not None
            else f"{matched_phrase}, but a Colectica routing edge referenced here no longer exists."
        )
    elif discrepancy_type == DiscrepancyType.MISSING_IN_COLECTICA:
        edge = discrepancy.forsta_edge
        edge_explanation = _build_edge_explanation(edge, "Forsta+")
        summary = (
            f"{matched_phrase}, but Forsta+ has a routing edge to {edge.target_question}"
            f"{_condition_fragment(edge)} that has no equivalent in Colectica."
            if edge is not None
            else f"{matched_phrase}, but a Forsta+ routing edge referenced here no longer exists."
        )
    elif discrepancy_type == DiscrepancyType.ELSE_BRANCH_ONLY_IN_FORSTA:
        edge = discrepancy.forsta_edge
        edge_explanation = _build_edge_explanation(edge, "Forsta+")
        summary = (
            f"{matched_phrase}. Forsta+ adds an explicit 'else' branch to "
            f"{edge.target_question}{_condition_fragment(edge)} for respondents who don't "
            "meet the main routing condition. Colectica's format has no direct equivalent "
            "for else branches, so this is expected rather than a gap to fix."
            if edge is not None
            else f"{matched_phrase}. A Forsta+ else-branch edge referenced here no longer exists."
        )
    else:
        edge = discrepancy.colectica_edge or discrepancy.forsta_edge
        origin_system = "Colectica" if discrepancy.colectica_edge else "Forsta+"
        edge_explanation = _build_edge_explanation(edge, origin_system)
        summary = f"{matched_phrase}. The routing condition differs between Colectica and Forsta+."

    meaning = _meaning_for_type(discrepancy_type, edge_explanation)

    return DiscrepancyExplanation(summary=summary, edge=edge_explanation, meaning=meaning)


def _meaning_for_type(discrepancy_type: str, edge: EdgeExplanation | None) -> str:
    """
    The deeper "what does this actually imply" explanation, kept separate
    from `summary` because it's about the comparison methodology, not this
    one row's facts. routing_comparator.RoutingComparator is a *structural*
    diff only (see its docstring): it checks whether an edge with the same
    source/target exists on both sides, and never evaluates what a condition
    means or traces alternate paths to the same target. That's why a flagged
    edge is a lead for a human to check, not proof of a bug.
    """

    DiscrepancyType = RoutingDiscrepancy.DiscrepancyType
    target = edge.target_question if edge is not None else "the target question"

    if discrepancy_type == DiscrepancyType.MISSING_IN_FORSTA:
        return (
            "This is a structural comparison only: it checks whether an edge with the same "
            "source and target exists on both sides, not what the condition means or whether "
            f"an alternate path exists. This flag means either (a) Forsta+ genuinely never "
            f"routes to {target} from here -- a real gap worth fixing, or (b) Forsta+ reaches "
            f"{target} some other way (a different source question, different condition logic, "
            "a default fallthrough) that this structural check can't recognize -- a false "
            "positive. Severity \"Warning\" means this needs a human to check, not that it's a "
            "confirmed bug."
        )

    if discrepancy_type == DiscrepancyType.MISSING_IN_COLECTICA:
        return (
            "This is a structural comparison only: it checks whether an edge with the same "
            f"source and target exists on both sides. This flag means Forsta+ has a branch to "
            f"{target} that Colectica's extracted routing has no equivalent for -- either an "
            "intentional Forsta+-only addition, or something Colectica's extraction missed. "
            "Severity \"Info\" reflects that this is lower-priority than a missing-in-Forsta+ "
            "gap, but it's still worth a human check."
        )

    if discrepancy_type == DiscrepancyType.ELSE_BRANCH_ONLY_IN_FORSTA:
        return (
            "Forsta+'s format has an explicit \"else\" construct (a FalseNodes branch) for "
            "\"anything not covered by the main condition.\" Colectica's extracted format has "
            "no equivalent construct, so this discrepancy is expected by design, not a gap -- "
            "there is nothing to fix here."
        )

    return (
        "This is a structural comparison only: it checks whether the same source/target edge "
        "exists on both sides, not what the condition means. Treat this as a lead for manual "
        "review, not a confirmed bug."
    )


def _condition_fragment(edge: RoutingEdge | None) -> str:
    if edge is None or not edge.condition_text:
        return ""

    condition = strip_condition_wrapper(edge.condition_text)
    if not condition:
        return ""

    return f" (fires when {condition})"


def _build_edge_explanation(edge: RoutingEdge | None, origin_system: str) -> EdgeExplanation | None:
    if edge is None:
        return None

    return EdgeExplanation(
        origin_system=origin_system,
        source_question=edge.source_question,
        target_question=edge.target_question,
        condition_text=strip_condition_wrapper(edge.condition_text) if edge.condition_text else "",
    )
