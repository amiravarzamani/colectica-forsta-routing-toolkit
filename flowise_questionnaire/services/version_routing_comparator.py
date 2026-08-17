from __future__ import annotations

import re
from collections import defaultdict

from django.db import transaction
from django.db.models import QuerySet

from flowise_questionnaire.models import (
    QuestionnaireModule,
    QuestionnaireVersionMatch,
    RoutingEdge,
    VersionRoutingDiscrepancy,
)
from flowise_questionnaire.services.condition_evaluator import strip_condition_wrapper

_WHITESPACE_PATTERN = re.compile(r"\s+")
_OPERATOR_SPACING_PATTERN = re.compile(r"\s*(<>|>=|<=|=|>|<|\|)\s*")


def normalize_condition_text(condition_text: str) -> str:
    """
    Loose textual normalization for comparing two Colectica-syntax
    conditions, valid here specifically because both the base and compare
    module use the identical "if [...]" grammar (unlike Colectica vs
    Forsta+, see VersionRoutingComparator's docstring). Strips the "if
    [...]" wrapper, uppercases, and collapses whitespace (including around
    comparison/pipe operators) so purely cosmetic differences like
    "NAMEPERM =1" vs "NAMEPERM=1" don't count as a mismatch.

    NOT a proof of logical equivalence: two differently-worded conditions
    can still mean the same thing (reported as different here), and two
    identically-normalized conditions are not guaranteed to behave
    identically in every runtime edge case (variable ordering inside a
    membership list, for instance). This is a textual difference signal for
    human review, not a semantic evaluator -- see version_diff_explainer's
    explanation text for the same caveat surfaced in the GUI.
    """

    expression = strip_condition_wrapper(condition_text or "")
    if not expression:
        return ""

    expression = expression.upper()
    expression = _OPERATOR_SPACING_PATTERN.sub(r"\1", expression)
    expression = _WHITESPACE_PATTERN.sub(" ", expression).strip()
    return expression


def _conditional_edges_by_source(module: QuestionnaireModule) -> dict[str, list[RoutingEdge]]:
    """
    Same prefetch-once-per-module approach as
    routing_comparator._conditional_edges_by_source, for the same reason:
    avoids O(question_matches) unindexed source_question__iexact scans.

    Also splits a comma-joined multi-variable source_question (e.g.
    "NAMEPERM, NAME") and indexes the edge under each individual variable
    name -- see routing_comparator._conditional_edges_by_source's docstring
    for why this matters: without it, an edge whose source_question is a
    multi-variable string is invisible to a lookup by any single variable's
    own name, producing false-positive discrepancies.
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


class VersionRoutingComparator:
    """
    Primarily a structural diff, same approach as RoutingComparator (see its
    docstring): compares conditional-edge target presence for each matched
    question pair between a "base" and "compare" Colectica module.

    Unlike RoutingComparator, this one additionally flags CONDITION_MISMATCH
    when a target is present on both sides but the two edges' condition
    text differs after normalize_condition_text() -- valid here specifically
    because both modules use the same Colectica grammar (RoutingComparator
    can't do this: Colectica vs Forsta+ are different grammars entirely, so
    a text comparison there would flag every single edge). This is still a
    textual signal, not a semantic evaluator -- see normalize_condition_text
    and version_diff_explainer for the caveat.

    No else-branch special case here -- unlike Forsta+'s FalseNodes
    construct, both sides use Colectica's own "if [...]" grammar, which has
    no equivalent, so every one-sided edge is either MISSING_IN_COMPARE or
    MISSING_IN_BASE.

    Both source and target sides of an edge are resolved by matched
    identity (via QuestionnaireVersionMatch), not raw name string, same
    rationale as RoutingComparator: a target with no match of its own falls
    back to its own casefolded name.
    """

    def __init__(
            self,
            question_matches: QuerySet[QuestionnaireVersionMatch] | list[QuestionnaireVersionMatch],
            base_module: QuestionnaireModule,
            compare_module: QuestionnaireModule,
    ):
        self.question_matches = list(question_matches)
        self._base_edges_by_source = _conditional_edges_by_source(base_module)
        self._compare_edges_by_source = _conditional_edges_by_source(compare_module)

        self._base_to_compare_name: dict[str, str] = {}
        self._compare_to_base_name: dict[str, str] = {}
        for match in self.question_matches:
            if match.compare_question_id is None:
                continue
            base_name = match.base_question.name.strip().casefold()
            compare_name = match.compare_question.name.strip().casefold()
            self._base_to_compare_name[base_name] = compare_name
            self._compare_to_base_name[compare_name] = base_name

    def compare(self) -> list[VersionRoutingDiscrepancy]:
        discrepancies: list[VersionRoutingDiscrepancy] = []

        for match in self.question_matches:
            if match.compare_question_id is None:
                continue

            discrepancies.extend(self._compare_match(match))

        return discrepancies

    def _compare_match(self, match: QuestionnaireVersionMatch) -> list[VersionRoutingDiscrepancy]:
        base_name = match.base_question.name.strip().casefold()
        compare_name = match.compare_question.name.strip().casefold()

        base_edges = self._base_edges_by_source.get(base_name, [])
        compare_edges = self._compare_edges_by_source.get(compare_name, [])

        base_targets = {edge.target_question.strip().casefold() for edge in base_edges}
        compare_targets = {edge.target_question.strip().casefold() for edge in compare_edges}

        compare_edges_by_target: dict[str, list[RoutingEdge]] = defaultdict(list)
        for edge in compare_edges:
            compare_edges_by_target[edge.target_question.strip().casefold()].append(edge)

        discrepancies: list[VersionRoutingDiscrepancy] = []

        for edge in base_edges:
            target_cf = edge.target_question.strip().casefold()
            resolved_compare_target = self._base_to_compare_name.get(target_cf, target_cf)

            if resolved_compare_target not in compare_targets:
                discrepancies.append(
                    VersionRoutingDiscrepancy(
                        question_match=match,
                        discrepancy_type=VersionRoutingDiscrepancy.DiscrepancyType.MISSING_IN_COMPARE,
                        base_edge=edge,
                        compare_edge=None,
                        details_json={
                            "target_question": edge.target_question,
                            "condition_text": edge.condition_text,
                        },
                        severity=VersionRoutingDiscrepancy.Severity.WARNING,
                    )
                )
                continue

            # Target present on both sides -- still check whether the
            # gating condition text agrees. If the base edge's normalized
            # condition matches ANY compare edge to this same target, that's
            # not a mismatch (handles the rare case of more than one path to
            # the same target with different conditions on either side).
            matching_compare_edges = compare_edges_by_target.get(resolved_compare_target, [])
            base_normalized = normalize_condition_text(edge.condition_text)
            compare_normalized_texts = {
                normalize_condition_text(candidate.condition_text) for candidate in matching_compare_edges
            }

            if base_normalized not in compare_normalized_texts:
                discrepancies.append(
                    VersionRoutingDiscrepancy(
                        question_match=match,
                        discrepancy_type=VersionRoutingDiscrepancy.DiscrepancyType.CONDITION_MISMATCH,
                        base_edge=edge,
                        compare_edge=matching_compare_edges[0],
                        details_json={
                            "target_question": edge.target_question,
                            "base_condition_text": edge.condition_text,
                            "compare_condition_text": matching_compare_edges[0].condition_text,
                        },
                        severity=VersionRoutingDiscrepancy.Severity.WARNING,
                    )
                )

        for edge in compare_edges:
            target_cf = edge.target_question.strip().casefold()
            resolved_base_target = self._compare_to_base_name.get(target_cf, target_cf)
            if resolved_base_target in base_targets:
                continue

            discrepancies.append(
                VersionRoutingDiscrepancy(
                    question_match=match,
                    discrepancy_type=VersionRoutingDiscrepancy.DiscrepancyType.MISSING_IN_BASE,
                    base_edge=None,
                    compare_edge=edge,
                    details_json={
                        "target_question": edge.target_question,
                        "condition_text": edge.condition_text,
                    },
                    severity=VersionRoutingDiscrepancy.Severity.WARNING,
                )
            )

        return discrepancies


@transaction.atomic
def compare_versions_for_modules(
        base_module: QuestionnaireModule,
        compare_module: QuestionnaireModule,
) -> dict[str, int]:
    matches = QuestionnaireVersionMatch.objects.filter(
        base_question__module=base_module,
        compare_question__module=compare_module,
    ).select_related("base_question", "compare_question")

    VersionRoutingDiscrepancy.objects.filter(question_match__in=matches).delete()

    comparator = VersionRoutingComparator(matches, base_module, compare_module)
    discrepancies = comparator.compare()

    VersionRoutingDiscrepancy.objects.bulk_create(discrepancies)

    return {
        "total": len(discrepancies),
        "missing_in_compare": sum(
            1 for d in discrepancies
            if d.discrepancy_type == VersionRoutingDiscrepancy.DiscrepancyType.MISSING_IN_COMPARE
        ),
        "missing_in_base": sum(
            1 for d in discrepancies
            if d.discrepancy_type == VersionRoutingDiscrepancy.DiscrepancyType.MISSING_IN_BASE
        ),
        "condition_mismatch": sum(
            1 for d in discrepancies
            if d.discrepancy_type == VersionRoutingDiscrepancy.DiscrepancyType.CONDITION_MISMATCH
        ),
    }
