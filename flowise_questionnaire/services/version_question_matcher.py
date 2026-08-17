from __future__ import annotations

import difflib
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q

from flowise_questionnaire.models import QuestionnaireModule, QuestionnaireVersionMatch
from flowise_questionnaire.services.question_matcher import (
    DEFAULT_FUZZY_THRESHOLD,
    _significant_tokens,
    normalize_question_text,
)


@dataclass
class VersionMatchCandidate:
    base_question_id: str
    compare_question_id: str | None
    score: float
    method: str


class VersionQuestionMatcher:
    """
    Matches NormalizedQuestion rows between two Colectica-format modules
    (e.g. two waves/versions of the same questionnaire), one match per base
    question, ordered name-first rather than text-first.

    Unlike QuestionMatcher (Colectica vs Forsta+, where the two sides use
    unrelated naming schemes and text is the only reliable signal), both
    sides here are Colectica JSON: the variable name is usually the stable
    identifier across versions of the same questionnaire, while wording is
    what tends to change (rewording, added instructions). So name is tried
    first here, with exact-then-fuzzy text matching as the fallback for
    anything renamed -- the reverse priority of QuestionMatcher, which is
    why this is a separate class rather than a reused/parameterized one.
    """

    def __init__(
            self,
            base_module: QuestionnaireModule,
            compare_module: QuestionnaireModule,
            fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    ):
        self.base_module = base_module
        self.compare_module = compare_module
        self.fuzzy_threshold = fuzzy_threshold

    @staticmethod
    def _similarity(normalized_a: str, normalized_b: str) -> float:
        if not normalized_a or not normalized_b:
            return 0.0

        return difflib.SequenceMatcher(None, normalized_a, normalized_b).ratio()

    def build_matches(self) -> list[VersionMatchCandidate]:
        base_questions = list(self.base_module.normalized_questions.all())
        compare_questions = list(self.compare_module.normalized_questions.all())

        base_normalized = {
            question.id: normalize_question_text(question.text or question.label)
            for question in base_questions
        }
        compare_normalized = {
            question.id: normalize_question_text(question.text or question.label)
            for question in compare_questions
        }

        used_compare_ids: set = set()
        candidates: list[VersionMatchCandidate] = []

        compare_ids_by_name: dict[str, str] = {}
        for question in compare_questions:
            compare_ids_by_name.setdefault(question.name.strip().casefold(), question.id)

        # Name pass -- tried first (see class docstring). One base question
        # can only claim a same-named compare question once; ties go to
        # whichever base question is processed first (module ordering).
        for question in base_questions:
            compare_id = compare_ids_by_name.get(question.name.strip().casefold())
            if compare_id is None or compare_id in used_compare_ids:
                continue

            used_compare_ids.add(compare_id)
            candidates.append(
                VersionMatchCandidate(
                    base_question_id=question.id,
                    compare_question_id=compare_id,
                    score=1.0,
                    method=QuestionnaireVersionMatch.MatchMethod.NAME,
                )
            )

        matched_base_ids = {candidate.base_question_id for candidate in candidates}

        compare_ids_by_text: dict[str, list] = {}
        for question in compare_questions:
            normalized = compare_normalized[question.id]
            if normalized:
                compare_ids_by_text.setdefault(normalized, []).append(question.id)

        # Exact-text pass for anything not resolved by name (e.g. a renamed
        # variable that kept its wording).
        for question in base_questions:
            if question.id in matched_base_ids:
                continue

            normalized = base_normalized[question.id]
            if not normalized:
                continue

            available_ids = [
                compare_id
                for compare_id in compare_ids_by_text.get(normalized, [])
                if compare_id not in used_compare_ids
            ]
            if not available_ids:
                continue

            compare_id = available_ids[0]
            used_compare_ids.add(compare_id)
            candidates.append(
                VersionMatchCandidate(
                    base_question_id=question.id,
                    compare_question_id=compare_id,
                    score=1.0,
                    method=QuestionnaireVersionMatch.MatchMethod.EXACT_TEXT,
                )
            )

        matched_base_ids = {candidate.base_question_id for candidate in candidates}

        compare_ids_by_token: dict[str, list] = {}
        for question in compare_questions:
            tokens = _significant_tokens(compare_normalized[question.id])
            for token in tokens:
                compare_ids_by_token.setdefault(token, []).append(question.id)

        # Fuzzy-text pass for whatever neither name nor exact text resolved.
        for question in base_questions:
            if question.id in matched_base_ids:
                continue

            normalized = base_normalized[question.id]
            best_score = 0.0
            best_compare_id = None

            if normalized:
                candidate_compare_ids: set = set()
                tokens = _significant_tokens(normalized)
                for token in tokens:
                    candidate_compare_ids.update(compare_ids_by_token.get(token, ()))

                for compare_id in candidate_compare_ids:
                    if compare_id in used_compare_ids:
                        continue

                    compare_text = compare_normalized[compare_id]
                    if not compare_text:
                        continue

                    score = self._similarity(normalized, compare_text)
                    if score > best_score:
                        best_score = score
                        best_compare_id = compare_id

            if best_compare_id is not None and best_score >= self.fuzzy_threshold:
                used_compare_ids.add(best_compare_id)
                candidates.append(
                    VersionMatchCandidate(
                        base_question_id=question.id,
                        compare_question_id=best_compare_id,
                        score=best_score,
                        method=QuestionnaireVersionMatch.MatchMethod.FUZZY_TEXT,
                    )
                )
            else:
                candidates.append(
                    VersionMatchCandidate(
                        base_question_id=question.id,
                        compare_question_id=None,
                        score=best_score,
                        method=QuestionnaireVersionMatch.MatchMethod.FUZZY_TEXT,
                    )
                )

        return candidates


@transaction.atomic
def build_version_matches(
        base_module: QuestionnaireModule,
        compare_module: QuestionnaireModule,
) -> dict[str, int]:
    # Same rationale as question_matcher.build_question_matches: scope
    # cleanup to this exact (base_module, compare_module) pair, sweeping up
    # any pre-existing rows that couldn't be attributed to a specific pair.
    QuestionnaireVersionMatch.objects.filter(
        base_question__module=base_module,
    ).filter(
        Q(compare_module=compare_module) | Q(compare_module__isnull=True)
    ).delete()

    matcher = VersionQuestionMatcher(base_module, compare_module)
    candidates = matcher.build_matches()

    matches = [
        QuestionnaireVersionMatch(
            base_question_id=candidate.base_question_id,
            compare_question_id=candidate.compare_question_id,
            compare_module=compare_module,
            match_score=candidate.score,
            match_method=candidate.method,
        )
        for candidate in candidates
    ]
    QuestionnaireVersionMatch.objects.bulk_create(matches)

    return {
        "total": len(matches),
        "name_matched": sum(
            1 for c in candidates if c.method == QuestionnaireVersionMatch.MatchMethod.NAME
        ),
        "exact_text_matched": sum(
            1 for c in candidates if c.method == QuestionnaireVersionMatch.MatchMethod.EXACT_TEXT
        ),
        "fuzzy_matched": sum(
            1 for c in candidates
            if c.method == QuestionnaireVersionMatch.MatchMethod.FUZZY_TEXT
            and c.compare_question_id is not None
        ),
        "unmatched": sum(1 for c in candidates if c.compare_question_id is None),
    }
