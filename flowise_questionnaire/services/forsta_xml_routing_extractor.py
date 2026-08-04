from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

from django.db import transaction

from flowise_questionnaire.models import NormalizedQuestion, QuestionnaireModule, RoutingEdge
from flowise_questionnaire.services.forsta_xml_schema_extractor import QUESTION_TAGS
from flowise_questionnaire.services.routing_extraction_contracts import ExtractedRoutingEdge

_VARIABLE_PATTERN = re.compile(r"f\(\s*'([^']+)'\s*\)")


class ForstaXmlRoutingExtractor:
    """
    Deterministic routing extractor for Forsta+ (Confirmit Horizons) XML.

    Extracts:
    - conditional branch edges (TrueNodes, and FalseNodes as a second edge with
      a negated condition -- see forsta_xml_routing_validation_plan.md §6.2)
    - loop container -> loop member question edges
    - sequential (document-order) edges

    Mirrors ColecticaRoutingExtractor's output shape (ExtractedRoutingEdge).
    """

    def __init__(
            self,
            raw_xml: str,
            canonical_question_names: set[str] | None = None,
            ordered_question_names: list[str] | None = None,
    ):
        self.raw_xml = raw_xml
        self.root = ET.fromstring(raw_xml)
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

    def _find_routing_nodes(self) -> ET.Element | None:
        questionnaire = self.root.find(".//Questionnaire")
        if questionnaire is None:
            return None

        routing = questionnaire.find("Routing")
        if routing is None:
            return None

        return routing.find("Nodes")

    # ------------------------------------------------------------------
    # Conditional branch extraction
    # ------------------------------------------------------------------

    def _extract_conditional_edges(self) -> list[ExtractedRoutingEdge]:
        edges: list[ExtractedRoutingEdge] = []

        routing_nodes = self._find_routing_nodes()
        if routing_nodes is None:
            return edges

        for condition in routing_nodes.iter("Condition"):
            expression = self._extract_expression(condition)

            if not expression:
                continue

            source_question = self._infer_source_questions_from_expression(expression)

            true_nodes = condition.find("TrueNodes")
            if true_nodes is not None:
                for target in self._extract_direct_question_names(true_nodes):
                    self._sequence_index += 1
                    edges.append(
                        ExtractedRoutingEdge(
                            source_question=source_question,
                            target_question=self._canonicalize_question_name(target),
                            condition_text=expression,
                            edge_type=RoutingEdge.EdgeType.CONDITIONAL,
                            sequence_index=self._sequence_index,
                        )
                    )

            false_nodes = condition.find("FalseNodes")
            if false_nodes is not None and len(list(false_nodes)) > 0:
                negated_expression = f"NOT ({expression})"

                for target in self._extract_direct_question_names(false_nodes):
                    self._sequence_index += 1
                    edges.append(
                        ExtractedRoutingEdge(
                            source_question=source_question,
                            target_question=self._canonicalize_question_name(target),
                            condition_text=negated_expression,
                            edge_type=RoutingEdge.EdgeType.CONDITIONAL,
                            sequence_index=self._sequence_index,
                        )
                    )

        return edges

    def _extract_expression(self, condition: ET.Element) -> str:
        expression_element = condition.find("Expression")

        if expression_element is None or not expression_element.text:
            return ""

        return expression_element.text.strip()

    def _extract_direct_question_names(self, container: ET.Element) -> list[str]:
        """
        Direct question elements inside this container, recursing through pure
        grouping containers (Nodes/Page/Folder) but NOT into nested
        Condition/Loop branches -- those get their own edges from their own
        Condition/Loop element. Mirrors the Colectica extractor's "direct
        target questions from branch" concept.
        """

        targets: list[str] = []
        self._collect_direct_question_names(container, targets)
        return targets

    def _collect_direct_question_names(self, element: ET.Element, targets: list[str]) -> None:
        for child in element:
            if child.tag in QUESTION_TAGS:
                name = self._extract_question_name(child)
                if name and name not in targets:
                    targets.append(name)
            elif child.tag in {"Condition", "Loop"}:
                continue
            else:
                # Any other container (Folder/Page/Nodes/Script wrapper/etc.)
                # is treated as pure grouping for "direct target" purposes.
                self._collect_direct_question_names(child, targets)

    def _extract_question_name(self, element: ET.Element) -> str:
        name_element = element.find("Name")
        if name_element is None or not name_element.text:
            return ""
        return name_element.text.strip()

    # ------------------------------------------------------------------
    # Loop extraction
    # ------------------------------------------------------------------

    def _extract_loop_edges(self) -> list[ExtractedRoutingEdge]:
        edges: list[ExtractedRoutingEdge] = []

        routing_nodes = self._find_routing_nodes()
        if routing_nodes is None:
            return edges

        for loop in routing_nodes.iter("Loop"):
            loop_name = self._extract_loop_name(loop)

            nodes = loop.find("Nodes")
            if nodes is None:
                continue

            target_questions: list[str] = []
            seen: set[str] = set()

            for element in nodes.iter():
                if element.tag not in QUESTION_TAGS:
                    continue

                name = self._extract_question_name(element)
                if name and name not in seen:
                    seen.add(name)
                    target_questions.append(name)

            for target in target_questions:
                self._sequence_index += 1
                edges.append(
                    ExtractedRoutingEdge(
                        source_question=loop_name,
                        target_question=self._canonicalize_question_name(target),
                        condition_text="",
                        edge_type=RoutingEdge.EdgeType.LOOP,
                        sequence_index=self._sequence_index,
                    )
                )

        return edges

    def _extract_loop_name(self, loop: ET.Element) -> str:
        entity_id = loop.get("EntityId", "")
        return f"Loop_{entity_id}" if entity_id else "Loop"

    # ------------------------------------------------------------------
    # Sequential flow extraction
    # ------------------------------------------------------------------

    def _extract_sequential_edges(self) -> list[ExtractedRoutingEdge]:
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

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _infer_source_questions_from_expression(self, expression: str) -> str:
        variables: list[str] = []

        for match in _VARIABLE_PATTERN.finditer(expression):
            variable = match.group(1)
            canonical = self._canonicalize_question_name(variable)

            if canonical not in variables:
                variables.append(canonical)

        return ", ".join(variables)

    def _canonicalize_question_name(self, name: str) -> str:
        if not name:
            return ""

        stripped = name.strip()

        if stripped in self.canonical_question_names:
            return stripped

        return self._canonical_lookup.get(stripped.lower(), stripped)


@transaction.atomic
def extract_routing_for_forsta_module(module: QuestionnaireModule) -> dict[str, Any]:
    if not module.raw_xml:
        raise ValueError("Module has no raw_xml stored.")

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

    extractor = ForstaXmlRoutingExtractor(
        raw_xml=module.raw_xml,
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
