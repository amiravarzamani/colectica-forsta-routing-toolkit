from __future__ import annotations

import re
from typing import Any

from django.db import transaction

from flowise_questionnaire.models import NormalizedQuestion, QuestionnaireModule, RoutingEdge
from flowise_questionnaire.services.forsta_xml_routing_extractor import (
    extract_routing_for_forsta_module,
)
from flowise_questionnaire.services.routing_extraction_contracts import ExtractedRoutingEdge


class ColecticaRoutingExtractor:
    """
    Deterministic routing extractor for Colectica-style questionnaire JSON.

    Extracts:
    - conditional branch edges
    - loop container -> loop member question edges

    This stays generic:
    - no module-specific question names
    - no hardcoded questionnaire logic
    """

    _RESERVED_WORDS = {
        "if",
        "and",
        "or",
        "not",
        "in",
        "true",
        "false",
        "null",
    }

    def __init__(
            self,
            raw_json: dict[str, Any],
            canonical_question_names: set[str] | None = None,
            ordered_question_names: list[str] | None = None,
    ):
        self.raw_json = raw_json
        self._sequence_index = 0
        self.canonical_question_names = canonical_question_names or set()
        self.ordered_question_names = ordered_question_names or []
        self._canonical_lookup = {
            name.lower(): name
            for name in self.canonical_question_names
        }

    def extract_edges(self) -> list[ExtractedRoutingEdge]:
        edges: list[ExtractedRoutingEdge] = []

        edges.extend(self._extract_conditional_edges())
        edges.extend(self._extract_loop_edges())
        edges.extend(self._extract_sequential_edges())

        return edges

    # ---------------------------------------------------------------------
    # Conditional branch extraction
    # ---------------------------------------------------------------------

    def _extract_conditional_edges(self) -> list[ExtractedRoutingEdge]:
        edges: list[ExtractedRoutingEdge] = []

        for branch in self._walk_branch_containers(self.raw_json):
            condition_text = self._extract_condition_text(branch)

            if not condition_text:
                continue

            source_question = self._infer_source_questions_from_condition(condition_text)
            target_questions = self._extract_direct_target_questions_from_branch(branch)

            for target_question in target_questions:
                self._sequence_index += 1

                edges.append(
                    ExtractedRoutingEdge(
                        source_question=source_question,
                        target_question=self._canonicalize_question_name(target_question),
                        condition_text=condition_text,
                        edge_type=RoutingEdge.EdgeType.CONDITIONAL,
                        sequence_index=self._sequence_index,
                    )
                )

        return edges

    def _walk_branch_containers(self, value: Any):
        if isinstance(value, dict):
            branches = value.get("Branches")

            if isinstance(branches, list):
                for branch in branches:
                    if isinstance(branch, dict):
                        yield branch

            for child_value in value.values():
                yield from self._walk_branch_containers(child_value)

        elif isinstance(value, list):
            for item in value:
                yield from self._walk_branch_containers(item)

    def _extract_condition_text(self, branch: dict[str, Any]) -> str:
        condition = branch.get("Condition") or {}
        expressions = condition.get("SourceCodeExpressions") or []

        for expression in expressions:
            code = expression.get("Code")
            if isinstance(code, str) and code.strip():
                return code.strip()

        header = branch.get("Header")
        if isinstance(header, str) and header.strip().lower().startswith("if "):
            return header.strip()

        display_label = branch.get("DisplayLabel")
        if isinstance(display_label, str) and display_label.strip().lower().startswith("if "):
            return display_label.strip()

        return ""

    def _extract_direct_target_questions_from_branch(self, branch: dict[str, Any]) -> list[str]:
        """
        Extracts direct question activities from the branch ChildSequence.

        It intentionally does not recursively collect questions inside nested
        conditional branches. Those nested questions are handled by their own
        branch conditions.
        """

        child_sequence = branch.get("ChildSequence") or {}
        activities = child_sequence.get("Activities") or []

        targets: list[str] = []

        for activity in activities:
            if not isinstance(activity, dict):
                continue

            question = activity.get("Question")
            if not isinstance(question, dict):
                continue

            question_name = self._extract_question_name(activity)
            question_name = self._canonicalize_question_name(question_name)

            if question_name and question_name not in targets:
                targets.append(question_name)

        return targets

    # ---------------------------------------------------------------------
    # Loop extraction
    # ---------------------------------------------------------------------

    def _extract_loop_edges(self) -> list[ExtractedRoutingEdge]:
        edges: list[ExtractedRoutingEdge] = []

        for loop_activity in self._walk_loop_activities(self.raw_json):
            loop_name = self._extract_loop_name(loop_activity)

            if not loop_name:
                continue

            target_questions = self._extract_direct_questions_from_loop(loop_activity)

            for target_question in target_questions:
                self._sequence_index += 1

                edges.append(
                    ExtractedRoutingEdge(
                        source_question=loop_name,
                        target_question=self._canonicalize_question_name(target_question),
                        condition_text=self._extract_loop_condition_text(loop_activity),
                        edge_type=RoutingEdge.EdgeType.LOOP,
                        sequence_index=self._sequence_index,
                    )
                )

        return edges

    def _walk_loop_activities(self, value: Any):
        """
        Finds likely loop/iterator activities.

        Colectica exports can vary, so this looks for generic loop indicators:
        - ItemName/Header/DisplayLabel containing 'loop'
        - activity objects with loop-like child sequences
        """

        if isinstance(value, dict):
            if self._is_loop_activity(value):
                yield value

            for child_value in value.values():
                yield from self._walk_loop_activities(child_value)

        elif isinstance(value, list):
            for item in value:
                yield from self._walk_loop_activities(item)

    def _is_loop_activity(self, activity: dict[str, Any]) -> bool:
        if not isinstance(activity, dict):
            return False

        if activity.get("Question"):
            return False

        candidate_texts = [
            self._multilingual_text(activity.get("ItemName")),
            self._multilingual_text(activity.get("Label")),
            activity.get("Header") if isinstance(activity.get("Header"), str) else "",
            activity.get("DisplayLabel") if isinstance(activity.get("DisplayLabel"), str) else "",
        ]

        joined = " ".join(text for text in candidate_texts if text).lower()

        if "loop" not in joined:
            return False

        # Avoid classifying the entire root module as a loop just because
        # nested labels mention loops somewhere else.
        has_direct_sequence = isinstance(activity.get("Loop") or activity.get("LoopWhile") or activity.get("ChildSequence"), dict)
        has_activities = isinstance(activity.get("Activities"), list)

        return has_direct_sequence or has_activities or "Activities" in activity

    def _extract_loop_name(self, loop_activity: dict[str, Any]) -> str:
        for value in (
            loop_activity.get("ItemName"),
            loop_activity.get("Header"),
            loop_activity.get("DisplayLabel"),
            loop_activity.get("Label"),
        ):
            name = self._multilingual_text(value)
            if name:
                return self._normalize_loop_name(name)

        return ""

    def _normalize_loop_name(self, name: str) -> str:
        cleaned = re.sub(r"\s+", "", name.strip())

        # Keep already explicit loop-like names stable.
        if cleaned.lower().endswith("loop"):
            return cleaned

        return f"{cleaned}Loop"

    def _extract_direct_questions_from_loop(self, loop_activity: dict[str, Any]) -> list[str]:
        """
        Extract all question members inside a loop container.

        For loop representation, we want structural containment:
        Loop node -> every question contained in the loop

        This is intentionally recursive because loop bodies may contain:
        - direct question activities
        - conditional branches
        - nested child sequences
        """

        targets: list[str] = []

        candidate_containers: list[dict[str, Any]] = []

        if isinstance(loop_activity.get("Activities"), list):
            candidate_containers.append(loop_activity)

        for key in ("ChildSequence", "Loop", "LoopWhile", "LoopUntil"):
            value = loop_activity.get(key)
            if isinstance(value, dict):
                candidate_containers.append(value)

        for container in candidate_containers:
            for activity in self._walk_question_activities(container):
                question_name = self._extract_question_name(activity)
                question_name = self._canonicalize_question_name(question_name)

                if question_name and question_name not in targets:
                    targets.append(question_name)

        return targets

    def _walk_question_activities(self, value: Any):
        """
        Recursively finds question activities.
        Used for loop containment extraction.
        """

        if isinstance(value, dict):
            question = value.get("Question")

            if isinstance(question, dict):
                yield value

            for child_value in value.values():
                yield from self._walk_question_activities(child_value)

        elif isinstance(value, list):
            for item in value:
                yield from self._walk_question_activities(item)

    def _extract_loop_condition_text(self, loop_activity: dict[str, Any]) -> str:
        """
        Capture useful loop condition text if present.
        First version keeps it generic.
        """

        for key in ("LoopWhile", "LoopUntil", "Loop"):
            value = loop_activity.get(key)

            if not isinstance(value, dict):
                continue

            condition_text = self._extract_condition_text(value)
            if condition_text:
                return condition_text

            condition = value.get("Condition")
            if isinstance(condition, dict):
                expressions = condition.get("SourceCodeExpressions") or []
                for expression in expressions:
                    code = expression.get("Code")
                    if isinstance(code, str) and code.strip():
                        return code.strip()

        return ""

    # ---------------------------------------------------------------------
    # Sequential flow extraction
    # ---------------------------------------------------------------------

    def _extract_sequential_edges(self) -> list[ExtractedRoutingEdge]:
        """
        Extracts simple questionnaire-order edges from NormalizedQuestion.sequence_index.

        This is a visual flow layer, not routing authority.
        Conditional and loop edges still represent actual routing dependencies.
        """

        edges: list[ExtractedRoutingEdge] = []

        if len(self.ordered_question_names) < 2:
            return edges

        for index in range(len(self.ordered_question_names) - 1):
            source_question = self.ordered_question_names[index]
            target_question = self.ordered_question_names[index + 1]

            if not source_question or not target_question:
                continue

            self._sequence_index += 1

            edges.append(
                ExtractedRoutingEdge(
                    source_question=source_question,
                    target_question=target_question,
                    condition_text="",
                    edge_type=RoutingEdge.EdgeType.SEQUENTIAL,
                    sequence_index=self._sequence_index,
                )
            )

        return edges


    # ---------------------------------------------------------------------
    # Shared helpers
    # ---------------------------------------------------------------------

    def _extract_question_name(self, activity: dict[str, Any]) -> str:
        question = activity.get("Question") or {}

        name = self._multilingual_text(question.get("ItemName"))
        if name:
            return name

        return self._multilingual_text(activity.get("ItemName"))

    def _infer_source_questions_from_condition(self, condition_text: str) -> str:
        """
        Extract likely source variables from routing condition text.

        Rules:
        - Keep dotted external variables as one token:
          HHGRID.HHSIZE
          DEMOGRAPHICS.JBSTAT
          EDUCATIONALASPIRATIONS.EDTYPE

        - Normalize known questionnaire variables to canonical names:
          AidHH  -> AIDHH
          AidXHH -> AIDXHH

        - Ignore natural-language words that appear inside expressions:
          Number
          of
          absent
          hhold
          members

        - Do not treat qsrx keywords as variables:
          if, and, or, in, true, false, null
        """

        expression = self._extract_bracket_expression(condition_text)

        if not expression:
            expression = condition_text

        variables: list[str] = []

        # 1. Extract dotted variables first, for example:
        #    HHGRID.HHSIZE
        #    DEMOGRAPHICS.JBSTAT
        #    EDUCATIONALASPIRATIONS.EDTYPE
        dotted_variables = re.findall(
            r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b",
            expression,
        )

        for variable in dotted_variables:
            normalized = variable.strip()

            if normalized and normalized not in variables:
                variables.append(normalized)

        # 2. Remove dotted variables before scanning standalone tokens.
        #    This prevents HHGRID.HHSIZE from becoming HHGRID + HHSIZE.
        expression_without_dotted = re.sub(
            r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b",
            " ",
            expression,
        )

        # 3. Extract standalone tokens, but only keep tokens that match
        #    known canonical questionnaire names.
        candidates = re.findall(
            r"\b[A-Za-z_][A-Za-z0-9_]*\b",
            expression_without_dotted,
        )

        for candidate in candidates:
            token = candidate.strip()

            if not token:
                continue

            if token.lower() in self._RESERVED_WORDS:
                continue

            if token.isdigit():
                continue

            canonical_name = self._canonicalize_question_name(token)

            # Critical rule:
            # Keep standalone variables only if they are known questionnaire names.
            # This filters out natural-language words like Number/of/absent/hhold/members.
            if canonical_name not in self.canonical_question_names:
                continue

            if canonical_name not in variables:
                variables.append(canonical_name)

        return ", ".join(variables)

    def _extract_bracket_expression(self, condition_text: str) -> str:
        start = condition_text.find("[")
        end = condition_text.rfind("]")

        if start == -1 or end == -1 or end <= start:
            return ""

        return condition_text[start + 1:end].strip()

    def _canonicalize_question_name(self, name: str) -> str:
        if not name:
            return ""

        stripped = name.strip()

        if stripped in self.canonical_question_names:
            return stripped

        return self._canonical_lookup.get(stripped.lower(), stripped)

    def _multilingual_text(self, value: Any) -> str:
        if value is None:
            return ""

        if isinstance(value, str):
            return value.strip()

        if isinstance(value, dict):
            for language_code in ("en-GB", "en-US", "en"):
                text = value.get(language_code)
                if isinstance(text, str) and text.strip():
                    return text.strip()

            for text in value.values():
                if isinstance(text, str) and text.strip():
                    return text.strip()

        return ""


@transaction.atomic
def extract_routing_for_module(module: QuestionnaireModule) -> dict[str, Any]:
    if module.source_format == QuestionnaireModule.SourceFormat.FORSTA_XML:
        return extract_routing_for_forsta_module(module)

    if not module.raw_json:
        raise ValueError("Module has no raw_json stored.")

    normalized_questions = list(
        NormalizedQuestion.objects.filter(module=module).order_by("sequence_index")
    )

    canonical_question_names = {
        question.name
        for question in normalized_questions
    }

    ordered_question_names = [
        question.name
        for question in normalized_questions
    ]

    RoutingEdge.objects.filter(module=module).delete()

    extractor = ColecticaRoutingExtractor(
        raw_json=module.raw_json,
        canonical_question_names=canonical_question_names,
        ordered_question_names=ordered_question_names,
    )
    extracted_edges = extractor.extract_edges()

    routing_edges = [
        RoutingEdge(
            module=module,
            source_question=edge.source_question,
            target_question=edge.target_question,
            condition_text=edge.condition_text,
            edge_type=edge.edge_type,
            sequence_index=edge.sequence_index,
        )
        for edge in extracted_edges
    ]

    RoutingEdge.objects.bulk_create(routing_edges)

    return {
        "module_id": str(module.id),
        "routing_edge_count": len(routing_edges),
        "conditional_edge_count": sum(
            1 for edge in extracted_edges if edge.edge_type == RoutingEdge.EdgeType.CONDITIONAL
        ),
        "loop_edge_count": sum(
            1 for edge in extracted_edges if edge.edge_type == RoutingEdge.EdgeType.LOOP
        ),
        "sequential_edge_count": sum(
            1 for edge in extracted_edges if edge.edge_type == RoutingEdge.EdgeType.SEQUENTIAL
        ),
    }