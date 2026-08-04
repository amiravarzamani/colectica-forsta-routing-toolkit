from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q

from flowise_questionnaire.models import QuestionMatch, QuestionnaireModule

DEFAULT_FUZZY_THRESHOLD = 0.75

_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_CARET_PIPING_PATTERN = re.compile(r"\^[^^]*\^")
_BRACE_PIPING_PATTERN = re.compile(r"\{[^{}]*\}")
_NON_ALNUM_PATTERN = re.compile(r"[^a-z0-9\s]")
_WHITESPACE_PATTERN = re.compile(r"\s+")

# Common words that appear in most survey question text and so carry no
# discriminative value for the token-based candidate pre-filter below.
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "do", "does", "did",
    "have", "has", "had", "you", "your", "yours", "this", "that", "these",
    "those", "with", "for", "and", "or", "of", "to", "in", "on", "at",
    "please", "any", "all", "not", "if", "would", "will", "can", "about",
})


def _significant_tokens(
        normalized_text: str,
        min_token_length: int = 3,
        stopwords: frozenset = _STOPWORDS,
) -> set[str]:
    """
    Tokens used to pre-filter fuzzy-match candidate pairs before the
    expensive difflib comparison. Only words of `min_token_length`+
    characters that aren't in `stopwords` count, so two questions must
    share at least one meaningfully distinctive word to be considered as a
    fuzzy match candidate at all.

    `min_token_length`/`stopwords` are overridable (see QuestionMatcher)
    purely for offline tuning experiments -- the shipped default matches
    the original hardcoded behavior exactly.
    """

    return {
        token
        for token in normalized_text.split()
        if len(token) >= min_token_length and token not in stopwords
    }


def normalize_question_text(text: str) -> str:
    """
    Normalize question text for cross-format comparison.

    Strips both piping syntaxes seen in this codebase (Colectica's
    `{if HHGrid.NAMEPERM = 1}` and Forsta+'s `^f('var').get()^`), plus any
    inline HTML, then lowercases and collapses whitespace/punctuation so
    superficial formatting differences don't block a match.
    """

    if not text:
        return ""

    cleaned = _CARET_PIPING_PATTERN.sub("", text)
    cleaned = _BRACE_PIPING_PATTERN.sub("", cleaned)
    cleaned = _HTML_TAG_PATTERN.sub(" ", cleaned)
    cleaned = cleaned.lower()
    cleaned = _NON_ALNUM_PATTERN.sub(" ", cleaned)
    cleaned = _WHITESPACE_PATTERN.sub(" ", cleaned).strip()

    return cleaned


@dataclass
class MatchCandidate:
    colectica_question_id: str
    forsta_question_id: str | None
    score: float
    method: str


class QuestionMatcher:
    """
    Matches Colectica NormalizedQuestion rows to Forsta+ NormalizedQuestion
    rows by normalized text: exact match first, then fuzzy match (difflib
    ratio) above a confidence threshold for the remainder. Every Colectica
    question produces exactly one MatchCandidate, even when unmatched
    (forsta_question_id=None), so the caller can surface it for manual review.
    """

    def __init__(
            self,
            colectica_module: QuestionnaireModule,
            forsta_module: QuestionnaireModule,
            fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
            min_token_length: int = 3,
            stopwords: frozenset = _STOPWORDS,
    ):
        self.colectica_module = colectica_module
        self.forsta_module = forsta_module
        self.fuzzy_threshold = fuzzy_threshold
        self.min_token_length = min_token_length
        self.stopwords = stopwords

    @staticmethod
    def _similarity(normalized_a: str, normalized_b: str) -> float:
        """
        Shared text-similarity scoring used by both the fuzzy pass and the
        name-tiebreak reconciliation step below, so both apply the exact
        same notion of "close enough" -- identical strings already score
        1.0 via SequenceMatcher, so no separate exact-match branch is
        needed here.
        """

        if not normalized_a or not normalized_b:
            return 0.0

        return difflib.SequenceMatcher(None, normalized_a, normalized_b).ratio()

    def build_matches(self) -> list[MatchCandidate]:
        colectica_questions = list(self.colectica_module.normalized_questions.all())
        forsta_questions = list(self.forsta_module.normalized_questions.all())

        colectica_normalized = {
            question.id: normalize_question_text(question.text or question.label)
            for question in colectica_questions
        }
        forsta_normalized = {
            question.id: normalize_question_text(question.text or question.label)
            for question in forsta_questions
        }

        used_forsta_ids: set = set()
        candidates: list[MatchCandidate] = []

        forsta_ids_by_text: dict[str, list] = {}
        for question in forsta_questions:
            normalized = forsta_normalized[question.id]
            if normalized:
                forsta_ids_by_text.setdefault(normalized, []).append(question.id)

        # Exact pass.
        for question in colectica_questions:
            normalized = colectica_normalized[question.id]
            if not normalized:
                continue

            available_ids = [
                forsta_id
                for forsta_id in forsta_ids_by_text.get(normalized, [])
                if forsta_id not in used_forsta_ids
            ]
            if not available_ids:
                continue

            forsta_id = available_ids[0]
            used_forsta_ids.add(forsta_id)
            candidates.append(
                MatchCandidate(
                    colectica_question_id=question.id,
                    forsta_question_id=forsta_id,
                    score=1.0,
                    method=QuestionMatch.MatchMethod.EXACT,
                )
            )

        matched_colectica_ids = {candidate.colectica_question_id for candidate in candidates}

        # Inverted token index so the fuzzy pass below only runs the
        # expensive difflib comparison against Forsta+ questions that share
        # at least one meaningful word with the Colectica question, instead
        # of every remaining Forsta+ question. Full O(n*m) comparison is not
        # viable at real questionnaire sizes (thousands of questions per
        # side) -- see forsta_xml_routing_validation_plan.md perf notes.
        forsta_ids_by_token: dict[str, list] = {}
        for question in forsta_questions:
            tokens = _significant_tokens(
                forsta_normalized[question.id], self.min_token_length, self.stopwords
            )
            for token in tokens:
                forsta_ids_by_token.setdefault(token, []).append(question.id)

        # Fuzzy pass for everything the exact pass didn't resolve.
        for question in colectica_questions:
            if question.id in matched_colectica_ids:
                continue

            normalized = colectica_normalized[question.id]
            best_score = 0.0
            best_forsta_id = None

            if normalized:
                candidate_forsta_ids: set = set()
                tokens = _significant_tokens(normalized, self.min_token_length, self.stopwords)
                for token in tokens:
                    candidate_forsta_ids.update(forsta_ids_by_token.get(token, ()))

                for forsta_id in candidate_forsta_ids:
                    if forsta_id in used_forsta_ids:
                        continue

                    forsta_text = forsta_normalized[forsta_id]
                    if not forsta_text:
                        continue

                    score = self._similarity(normalized, forsta_text)
                    if score > best_score:
                        best_score = score
                        best_forsta_id = forsta_id

            if best_forsta_id is not None and best_score >= self.fuzzy_threshold:
                used_forsta_ids.add(best_forsta_id)
                candidates.append(
                    MatchCandidate(
                        colectica_question_id=question.id,
                        forsta_question_id=best_forsta_id,
                        score=best_score,
                        method=QuestionMatch.MatchMethod.FUZZY,
                    )
                )
            else:
                candidates.append(
                    MatchCandidate(
                        colectica_question_id=question.id,
                        forsta_question_id=None,
                        score=best_score,
                        method=QuestionMatch.MatchMethod.FUZZY,
                    )
                )

        colectica_by_id = {question.id: question for question in colectica_questions}
        forsta_by_name: dict[str, str] = {}
        for question in forsta_questions:
            key = question.name.strip().casefold()
            forsta_by_name.setdefault(key, question.id)

        self._apply_name_tiebreak(
            candidates, colectica_by_id, colectica_normalized, forsta_normalized, forsta_by_name
        )

        return candidates

    def _apply_name_tiebreak(
            self,
            candidates: list[MatchCandidate],
            colectica_by_id: dict,
            colectica_normalized: dict[str, str],
            forsta_normalized: dict[str, str],
            forsta_by_name: dict[str, str],
    ) -> None:
        """
        Name is a tiebreak, never an override (see question_matcher's module
        docstring / SKILL.md): a same-named Forsta+ question only replaces
        the text-based winner when it is *also* independently a legitimate
        match on wording grounds (clears `fuzzy_threshold`). A true
        false-friend -- same name, unrelated content -- never triggers a
        reassignment, so it's left exactly as the text-only passes decided.

        Mutates `candidates` in place. Runs after both the exact and fuzzy
        passes, so it sees their final picture, including questions that
        ended up unmatched (`forsta_question_id=None`).
        """

        used_forsta_ids = {
            candidate.forsta_question_id
            for candidate in candidates
            if candidate.forsta_question_id is not None
        }

        for index, candidate in enumerate(candidates):
            colectica_question = colectica_by_id[candidate.colectica_question_id]
            same_name_id = forsta_by_name.get(colectica_question.name.strip().casefold())

            if same_name_id is None:
                continue
            if same_name_id == candidate.forsta_question_id:
                continue
            if same_name_id in used_forsta_ids:
                continue

            colectica_text = colectica_normalized.get(candidate.colectica_question_id, "")
            forsta_text = forsta_normalized.get(same_name_id, "")
            same_name_score = self._similarity(colectica_text, forsta_text)

            # Forsta+ source XML often writes interviewer instructions
            # inline in the question text ("What is your email address?
            # CODE HERE AND ENTER EMAIL ADDRESS AT NEXT SCREEN"), where
            # Colectica keeps them in a separate field -- this can drag the
            # plain character-similarity ratio well below the threshold
            # even though it's clearly the same question. A clean
            # containment relationship (one normalized text is fully a
            # prefix/substring of the other) is treated as a match on its
            # own merits, since it's exactly this "same wording plus trailing
            # notes" shape, not two unrelated questions coincidentally
            # scoring high.
            is_contained = bool(colectica_text) and bool(forsta_text) and (
                colectica_text in forsta_text or forsta_text in colectica_text
            )

            if same_name_score < self.fuzzy_threshold and not is_contained:
                continue
            if is_contained:
                same_name_score = max(same_name_score, self.fuzzy_threshold)

            if candidate.forsta_question_id is not None:
                used_forsta_ids.discard(candidate.forsta_question_id)

            used_forsta_ids.add(same_name_id)
            candidates[index] = MatchCandidate(
                colectica_question_id=candidate.colectica_question_id,
                forsta_question_id=same_name_id,
                score=same_name_score,
                method=QuestionMatch.MatchMethod.NAME,
            )


@transaction.atomic
def build_question_matches(
        colectica_module: QuestionnaireModule,
        forsta_module: QuestionnaireModule,
) -> dict[str, int]:
    # Scoped to this exact pair -- a Colectica module compared against more
    # than one Forsta+ module over time must keep each pair's matches
    # independent, not wipe out every other pair sharing this Colectica
    # module. Rows with forsta_module still NULL (pre-existing legacy rows
    # from before this field existed, or any other code path that didn't
    # set it) can't be attributed to a specific pair either way, so they're
    # always swept up here rather than left to accumulate as duplicates.
    QuestionMatch.objects.filter(
        colectica_question__module=colectica_module,
    ).filter(
        Q(forsta_module=forsta_module) | Q(forsta_module__isnull=True)
    ).delete()

    matcher = QuestionMatcher(colectica_module, forsta_module)
    candidates = matcher.build_matches()

    matches = [
        QuestionMatch(
            colectica_question_id=candidate.colectica_question_id,
            forsta_question_id=candidate.forsta_question_id,
            forsta_module=forsta_module,
            match_score=candidate.score,
            match_method=candidate.method,
        )
        for candidate in candidates
    ]
    QuestionMatch.objects.bulk_create(matches)

    return {
        "total": len(matches),
        "exact": sum(1 for c in candidates if c.method == QuestionMatch.MatchMethod.EXACT),
        "fuzzy_matched": sum(
            1 for c in candidates
            if c.method == QuestionMatch.MatchMethod.FUZZY and c.forsta_question_id is not None
        ),
        "unmatched": sum(1 for c in candidates if c.forsta_question_id is None),
    }
