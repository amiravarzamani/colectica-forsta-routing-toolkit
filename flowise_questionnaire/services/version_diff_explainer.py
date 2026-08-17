from __future__ import annotations

from dataclasses import dataclass

from flowise_questionnaire.models import RoutingEdge, VersionRoutingDiscrepancy
from flowise_questionnaire.services.condition_evaluator import strip_condition_wrapper


@dataclass
class VersionEdgeExplanation:
    origin_module: str
    source_question: str
    target_question: str
    condition_text: str


@dataclass
class VersionDiscrepancyExplanation:
    summary: str
    edge: VersionEdgeExplanation | None
    meaning: str


def explain_version_discrepancy(discrepancy: VersionRoutingDiscrepancy) -> VersionDiscrepancyExplanation:
    """
    Turn a VersionRoutingDiscrepancy into plain-language text, mirroring
    routing_diff_explainer.explain_discrepancy for the base/compare
    Colectica-vs-Colectica pipeline:

    - MISSING_IN_COMPARE: a conditional edge out of the base question
      (base_edge) has no edge with the same target on the compare side.
    - MISSING_IN_BASE: the mirror image -- a compare edge (compare_edge) has
      no base counterpart.
    - CONDITION_MISMATCH: the target exists on both sides (so neither of the
      above applies), but the two edges' condition text differs after
      normalize_condition_text() -- see VersionRoutingComparator's docstring
      for why this check is only meaningful here (same grammar both sides).
    """

    match = discrepancy.question_match
    base_name = match.base_question.name
    compare_question = match.compare_question
    compare_name = compare_question.name if compare_question else None
    same_name = (
        compare_name is not None
        and base_name.strip().casefold() == compare_name.strip().casefold()
    )

    if compare_name:
        if same_name:
            matched_phrase = f"Question {base_name} matched in both modules"
        else:
            matched_phrase = (
                f"Base question {base_name} matched compare question {compare_name}"
            )
    else:
        matched_phrase = f"Question {base_name}"

    discrepancy_type = discrepancy.discrepancy_type
    DiscrepancyType = VersionRoutingDiscrepancy.DiscrepancyType

    if discrepancy_type == DiscrepancyType.MISSING_IN_COMPARE:
        edge = discrepancy.base_edge
        edge_explanation = _build_edge_explanation(edge, "base")
        summary = (
            f"{matched_phrase}, but the routing edge to {edge.target_question}"
            f"{_condition_fragment(edge)} has no equivalent in the compare module."
            if edge is not None
            else f"{matched_phrase}, but a base-module routing edge referenced here no longer exists."
        )
    elif discrepancy_type == DiscrepancyType.MISSING_IN_BASE:
        edge = discrepancy.compare_edge
        edge_explanation = _build_edge_explanation(edge, "compare")
        summary = (
            f"{matched_phrase}, but the compare module has a routing edge to "
            f"{edge.target_question}{_condition_fragment(edge)} that has no equivalent in the base module."
            if edge is not None
            else f"{matched_phrase}, but a compare-module routing edge referenced here no longer exists."
        )
    elif discrepancy_type == DiscrepancyType.CONDITION_MISMATCH:
        edge = discrepancy.base_edge
        edge_explanation = _build_edge_explanation(edge, "base")
        target = edge.target_question if edge is not None else "the same target"
        base_condition = _bare_condition_text(discrepancy.base_edge)
        compare_condition = _bare_condition_text(discrepancy.compare_edge)
        summary = (
            f"{matched_phrase}. Both modules route to {target}, but the gating condition text "
            f"differs: base fires when {base_condition}; compare fires when {compare_condition}."
        )
    else:
        edge = discrepancy.base_edge or discrepancy.compare_edge
        origin_module = "base" if discrepancy.base_edge else "compare"
        edge_explanation = _build_edge_explanation(edge, origin_module)
        summary = f"{matched_phrase}. The routing condition differs between the two modules."

    meaning = _meaning_for_type(discrepancy_type, edge_explanation)

    return VersionDiscrepancyExplanation(summary=summary, edge=edge_explanation, meaning=meaning)


def _meaning_for_type(discrepancy_type: str, edge: VersionEdgeExplanation | None) -> str:
    """
    Same "lead for human review, not a confirmed bug" framing as
    routing_diff_explainer._meaning_for_type -- this is still a structural
    diff only (see VersionRoutingComparator's docstring): it checks whether
    an edge with the same source/target exists on both sides, never what
    the condition means or whether an alternate path reaches the same
    target.
    """

    DiscrepancyType = VersionRoutingDiscrepancy.DiscrepancyType
    target = edge.target_question if edge is not None else "the target question"

    if discrepancy_type == DiscrepancyType.MISSING_IN_COMPARE:
        return (
            "This is a structural comparison only: it checks whether an edge with the same "
            "source and target exists in both modules, not what the condition means or whether "
            f"an alternate path exists. This flag means either (a) the compare module genuinely "
            f"never routes to {target} from here -- a real routing change worth reviewing, or "
            f"(b) the compare module reaches {target} some other way (a different source "
            "question, different condition logic, a default fallthrough) that this structural "
            "check can't recognize -- a false positive. Severity \"Warning\" means this needs a "
            "human to check, not that it's a confirmed bug."
        )

    if discrepancy_type == DiscrepancyType.MISSING_IN_BASE:
        return (
            "This is a structural comparison only: it checks whether an edge with the same "
            f"source and target exists in both modules. This flag means the compare module has "
            f"a branch to {target} that the base module has no equivalent for -- either an "
            "intentional addition in the newer/compare version, or something the base module's "
            "extraction missed. Severity \"Warning\" means this needs a human to check, not that "
            "it's a confirmed bug."
        )

    if discrepancy_type == DiscrepancyType.CONDITION_MISMATCH:
        return (
            "This is a textual comparison of the two condition expressions after normalizing "
            "whitespace and casing -- it does not evaluate whether the conditions are logically "
            "equivalent. Two differently-worded conditions can still mean exactly the same thing "
            "(and would still be reported here as different), just as two identically-normalized "
            "conditions aren't guaranteed to behave identically in every runtime edge case. Both "
            f"modules agree that {target} is a valid route from here -- what changed is *when* it "
            "fires. Treat this as a lead: worth checking whether the gating logic change was "
            "intentional (e.g. a design refinement between waves) or a routing regression."
        )

    return (
        "This is a structural comparison only: it checks whether the same source/target edge "
        "exists in both modules, not what the condition means. Treat this as a lead for manual "
        "review, not a confirmed bug."
    )


def _bare_condition_text(edge: RoutingEdge | None) -> str:
    if edge is None or not edge.condition_text:
        return "(no condition text)"

    condition = strip_condition_wrapper(edge.condition_text)
    return condition or "(no condition text)"


def _condition_fragment(edge: RoutingEdge | None) -> str:
    if edge is None or not edge.condition_text:
        return ""

    condition = strip_condition_wrapper(edge.condition_text)
    if not condition:
        return ""

    return f" (fires when {condition})"


def _build_edge_explanation(edge: RoutingEdge | None, origin_module: str) -> VersionEdgeExplanation | None:
    if edge is None:
        return None

    return VersionEdgeExplanation(
        origin_module=origin_module,
        source_question=edge.source_question,
        target_question=edge.target_question,
        condition_text=strip_condition_wrapper(edge.condition_text) if edge.condition_text else "",
    )
