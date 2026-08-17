from __future__ import annotations

from collections import defaultdict

from django.db import transaction
from django.db.models import QuerySet

from flowise_questionnaire.models import (
    QuestionMatch,
    QuestionnaireModule,
    RoutingDiscrepancy,
    RoutingEdge,
)


def _conditional_edges_by_source(module: QuestionnaireModule) -> dict[str, list[RoutingEdge]]:
    """
    Prefetch every conditional edge for a module once, grouped by
    case-folded source question name.

    Doing this per module (2 queries total) instead of per matched question
    pair avoids O(question_matches) unindexed `source_question__iexact`
    table scans, which is prohibitively slow/memory-heavy on real-sized
    questionnaires (thousands of questions x thousands of edges).

    A multi-variable condition stores a comma-joined source_question (e.g.
    "NAMEPERM, NAME" for a condition referencing both) -- same convention as
    graph_builder.py/interview_router.py/coverage_intent_builder.py. Each
    edge is indexed under every individual variable name, not just the raw
    joined string, so a lookup for either "NAMEPERM" or "NAME" alone finds
    it. Without this split, an edge whose source_question happens to be a
    multi-variable string was only ever discoverable under that exact
    combined key -- invisible to a lookup by either variable's own name,
    which made a question's real edge look "missing" on this side and
    produced a false-positive discrepancy against the other system.
    """

    edges_by_source: dict[str, list[RoutingEdge]] = defaultdict(list)

    edges = RoutingEdge.objects.filter(
        module=module,
        edge_type=RoutingEdge.EdgeType.CONDITIONAL,
    )

    for edge in edges:
        for source_name in edge.source_question.split(","):
            source_name = source_name.strip().casefold()
            if source_name:
                edges_by_source[source_name].append(edge)

    return edges_by_source


class RoutingComparator:
    """
    Structural diff only (per forsta_xml_routing_validation_plan.md §6.3):
    compares conditional-edge target/condition presence for each matched
    question pair. Does not evaluate condition semantics -- that is Step 8
    (ForstaConditionEvaluator), used separately for simulated-path comparison.

    Both the *source* and *target* side of an edge are compared by matched
    identity (via QuestionMatch), not by raw name string: a target question
    with no QuestionMatch of its own falls back to its own raw (casefolded)
    name, so a genuinely unmatched target behaves as before. A target that
    IS matched but named differently across systems (case, a Forsta+ "q"
    suffix, etc.) is resolved to its counterpart's name before comparing --
    without this, edges that are structurally identical on both sides were
    reported as missing purely because of target-naming differences.

    Operates on every QuestionMatch with a forsta_question assigned,
    regardless of its `confirmed` flag -- confirmation tracks human review
    state, it is not a precondition for computing the structural diff.
    """

    def __init__(
            self,
            question_matches: QuerySet[QuestionMatch] | list[QuestionMatch],
            colectica_module: QuestionnaireModule,
            forsta_module: QuestionnaireModule,
    ):
        self.question_matches = list(question_matches)
        self._colectica_edges_by_source = _conditional_edges_by_source(colectica_module)
        self._forsta_edges_by_source = _conditional_edges_by_source(forsta_module)

        # Cross-system name resolution for *target* questions, built once
        # from the same QuestionMatch rows the source side already relies
        # on -- so an edge's target is compared against the other system's
        # targets by matched identity, not by raw name string, the same way
        # source questions already are. Without this, a target that's
        # legitimately present on both sides but named differently
        # (case, a Forsta+ "q" suffix, etc.) is reported as missing.
        self._colectica_to_forsta_name: dict[str, str] = {}
        self._forsta_to_colectica_name: dict[str, str] = {}
        for match in self.question_matches:
            if match.forsta_question_id is None:
                continue
            colectica_name = match.colectica_question.name.strip().casefold()
            forsta_name = match.forsta_question.name.strip().casefold()
            self._colectica_to_forsta_name[colectica_name] = forsta_name
            self._forsta_to_colectica_name[forsta_name] = colectica_name

    def compare(self) -> list[RoutingDiscrepancy]:
        discrepancies: list[RoutingDiscrepancy] = []

        for match in self.question_matches:
            if match.forsta_question_id is None:
                continue

            discrepancies.extend(self._compare_match(match))

        return discrepancies

    def _compare_match(self, match: QuestionMatch) -> list[RoutingDiscrepancy]:
        colectica_name = match.colectica_question.name.strip().casefold()
        forsta_name = match.forsta_question.name.strip().casefold()

        colectica_edges = self._colectica_edges_by_source.get(colectica_name, [])
        forsta_edges = self._forsta_edges_by_source.get(forsta_name, [])

        colectica_targets = {edge.target_question.strip().casefold() for edge in colectica_edges}
        forsta_targets = {edge.target_question.strip().casefold() for edge in forsta_edges}

        discrepancies: list[RoutingDiscrepancy] = []

        for edge in colectica_edges:
            target_cf = edge.target_question.strip().casefold()
            # Fall back to the raw (casefolded) name when the target has no
            # cross-system match of its own -- same behavior as before for
            # a genuinely-unmatched target, just case-insensitive now too.
            resolved_forsta_target = self._colectica_to_forsta_name.get(target_cf, target_cf)
            if resolved_forsta_target in forsta_targets:
                continue

            discrepancies.append(
                RoutingDiscrepancy(
                    question_match=match,
                    discrepancy_type=RoutingDiscrepancy.DiscrepancyType.MISSING_IN_FORSTA,
                    colectica_edge=edge,
                    forsta_edge=None,
                    details_json={
                        "target_question": edge.target_question,
                        "condition_text": edge.condition_text,
                    },
                    severity=RoutingDiscrepancy.Severity.WARNING,
                )
            )

        for edge in forsta_edges:
            target_cf = edge.target_question.strip().casefold()
            resolved_colectica_target = self._forsta_to_colectica_name.get(target_cf, target_cf)
            if resolved_colectica_target in colectica_targets:
                continue

            # An explicit Forsta+ FalseNodes branch (see decision §6.2) is
            # stored as a second conditional edge whose condition_text is
            # wrapped as "NOT (...)". Colectica has no equivalent construct,
            # so this is a distinct, lower-severity discrepancy type rather
            # than an ordinary "missing in Colectica" gap.
            is_else_branch = edge.condition_text.strip().startswith("NOT (")

            discrepancies.append(
                RoutingDiscrepancy(
                    question_match=match,
                    discrepancy_type=(
                        RoutingDiscrepancy.DiscrepancyType.ELSE_BRANCH_ONLY_IN_FORSTA
                        if is_else_branch
                        else RoutingDiscrepancy.DiscrepancyType.MISSING_IN_COLECTICA
                    ),
                    colectica_edge=None,
                    forsta_edge=edge,
                    details_json={
                        "target_question": edge.target_question,
                        "condition_text": edge.condition_text,
                    },
                    severity=(
                        RoutingDiscrepancy.Severity.WARNING
                        if is_else_branch
                        else RoutingDiscrepancy.Severity.INFO
                    ),
                )
            )

        return discrepancies


@transaction.atomic
def compare_routing_for_modules(
        colectica_module: QuestionnaireModule,
        forsta_module: QuestionnaireModule,
) -> dict[str, int]:
    matches = QuestionMatch.objects.filter(
        colectica_question__module=colectica_module,
        forsta_question__module=forsta_module,
    ).select_related("colectica_question", "forsta_question")

    RoutingDiscrepancy.objects.filter(question_match__in=matches).delete()

    comparator = RoutingComparator(matches, colectica_module, forsta_module)
    discrepancies = comparator.compare()

    RoutingDiscrepancy.objects.bulk_create(discrepancies)

    return {
        "total": len(discrepancies),
        "missing_in_forsta": sum(
            1 for d in discrepancies
            if d.discrepancy_type == RoutingDiscrepancy.DiscrepancyType.MISSING_IN_FORSTA
        ),
        "missing_in_colectica": sum(
            1 for d in discrepancies
            if d.discrepancy_type == RoutingDiscrepancy.DiscrepancyType.MISSING_IN_COLECTICA
        ),
        "else_branch_only_in_forsta": sum(
            1 for d in discrepancies
            if d.discrepancy_type == RoutingDiscrepancy.DiscrepancyType.ELSE_BRANCH_ONLY_IN_FORSTA
        ),
    }
