from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from django.db import transaction

from flowise_questionnaire.models import NormalizedQuestion, QuestionnaireModule

QUESTION_TAGS = {"Single", "Multi", "Open", "Scale", "Grid"}

QUESTION_TYPE_BY_TAG = {
    "Single": NormalizedQuestion.QuestionType.SINGLE_CHOICE,
    "Multi": NormalizedQuestion.QuestionType.MULTI_CHOICE,
    "Open": NormalizedQuestion.QuestionType.TEXT,
    "Scale": NormalizedQuestion.QuestionType.NUMBER,
    "Grid": NormalizedQuestion.QuestionType.GRID_MATRIX,
}

_HIDDEN_VARIABLE_TYPES = {"Hidden", "Background"}

_PIPING_PATTERN = re.compile(r"\^[^^]*\^")
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_ENGLISH_LANGUAGE = "9"


@dataclass
class ExtractedQuestion:
    name: str
    label: str
    text: str
    question_type: str
    options: list[dict[str, Any]]
    help_text: str
    interviewer_instruction: str
    source_identifier: str
    source_composite_id: dict[str, Any] | None
    sequence_index: int


class ForstaXmlSchemaExtractor:
    """
    Deterministic extractor for Forsta+ (Confirmit Horizons) questionnaire XML.

    Mirrors ColecticaSchemaExtractor's output shape (ExtractedQuestion) so
    downstream code (extract_schema_for_module, matching, etc.) does not need
    to know which source format produced a given NormalizedQuestion.

    Respondent-visible questions only: elements carrying
    VariableType="Hidden"/"Background" are internal/system variables and are
    intentionally excluded here, rather than stored and filtered later, so
    every NormalizedQuestion produced by this extractor is a candidate for
    text-matching against the Colectica side.
    """

    def __init__(self, raw_xml: str):
        self.raw_xml = raw_xml
        self.root = ET.fromstring(raw_xml)
        self._sequence_index = 0
        self._seen_question_names: set[str] = set()
        self._predefined_lists = self._build_predefined_list_lookup()

    def extract_questions(self) -> list[ExtractedQuestion]:
        questions: list[ExtractedQuestion] = []

        routing_nodes = self._find_routing_nodes()
        if routing_nodes is None:
            return questions

        for element in routing_nodes.iter():
            if element.tag not in QUESTION_TAGS:
                continue

            if element.get("VariableType") in _HIDDEN_VARIABLE_TYPES:
                continue

            extracted = self._extract_question_from_element(element)

            if extracted is None:
                continue

            if extracted.name in self._seen_question_names:
                continue

            self._seen_question_names.add(extracted.name)
            questions.append(extracted)

        return questions

    def _find_routing_nodes(self) -> ET.Element | None:
        questionnaire = self.root.find(".//Questionnaire")
        if questionnaire is None:
            return None

        routing = questionnaire.find("Routing")
        if routing is None:
            return None

        return routing.find("Nodes")

    def _extract_question_from_element(self, element: ET.Element) -> ExtractedQuestion | None:
        name_element = element.find("Name")
        name = self._element_text(name_element)

        if not name:
            return None

        text, title = self._extract_form_text(element)
        display_text = text or title

        question_type = QUESTION_TYPE_BY_TAG.get(element.tag, NormalizedQuestion.QuestionType.UNKNOWN)
        options = self._extract_options(element)

        self._sequence_index += 1

        return ExtractedQuestion(
            name=name,
            label=title,
            text=display_text,
            question_type=question_type,
            options=options,
            help_text="",
            interviewer_instruction="",
            source_identifier=element.get("EntityId", ""),
            source_composite_id=None,
            sequence_index=self._sequence_index,
        )

    def _extract_form_text(self, element: ET.Element) -> tuple[str, str]:
        form_texts = element.find("FormTexts")
        if form_texts is None:
            return "", ""

        for form_text in form_texts.findall("FormText"):
            if form_text.get("Language") == _ENGLISH_LANGUAGE:
                text = self._clean_text(self._element_text(form_text.find("Text")))
                title = self._clean_text(self._element_text(form_text.find("Title")))
                return text, title

        return "", ""

    def _extract_options(self, element: ET.Element) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []
        seen_codes: set[str] = set()

        answers_container = None
        for child in element:
            if child.tag.endswith("Answers"):
                answers_container = child
                break

        if answers_container is None:
            return options

        for answer in answers_container.findall("Answer"):
            option = self._extract_answer_option(answer)
            if option["code"] not in seen_codes:
                seen_codes.add(option["code"])
                options.append(option)

        predefined = answers_container.find("Predefined")
        if predefined is not None:
            list_source = predefined.get("ListSource") or predefined.get("ReferencedEntityId")
            for option in self._predefined_lists.get(list_source, []):
                if option["code"] not in seen_codes:
                    seen_codes.add(option["code"])
                    options.append(option)

        return options

    def _extract_answer_option(self, answer: ET.Element) -> dict[str, Any]:
        return {
            "code": answer.get("Precode", ""),
            "label": self._extract_answer_label(answer),
            "is_missing": False,
            "exclusive": False,
            "category_identifier": "",
        }

    def _extract_answer_label(self, answer: ET.Element) -> str:
        texts = answer.find("Texts")
        if texts is None:
            return ""

        for text in texts.findall("Text"):
            if text.get("Language") == _ENGLISH_LANGUAGE:
                return self._clean_text(self._element_text(text))

        return ""

    def _build_predefined_list_lookup(self) -> dict[str, list[dict[str, Any]]]:
        lookup: dict[str, list[dict[str, Any]]] = {}

        for predefined_list in self.root.iter("PredefinedList"):
            entity_id = predefined_list.get("EntityId")
            if not entity_id:
                continue

            options: list[dict[str, Any]] = []
            answers_container = predefined_list.find("PredefinedListAnswers")

            if answers_container is not None:
                for answer in answers_container.findall("Answer"):
                    options.append(self._extract_answer_option(answer))

            lookup[entity_id] = options

        return lookup

    def _element_text(self, element: ET.Element | None) -> str:
        if element is None or element.text is None:
            return ""
        return element.text.strip()

    def _clean_text(self, value: str) -> str:
        if not value:
            return ""

        cleaned = _PIPING_PATTERN.sub("", value)
        cleaned = _HTML_TAG_PATTERN.sub(" ", cleaned)
        cleaned = html.unescape(cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        return cleaned


@transaction.atomic
def extract_schema_for_forsta_module(module: QuestionnaireModule) -> dict[str, Any]:
    if not module.raw_xml:
        raise ValueError("Module has no raw_xml stored.")

    module.processing_status = QuestionnaireModule.ProcessingStatus.EXTRACTING
    module.error_message = ""
    module.save(update_fields=["processing_status", "error_message", "updated_at"])

    NormalizedQuestion.objects.filter(module=module).delete()

    extractor = ForstaXmlSchemaExtractor(module.raw_xml)
    extracted_questions = extractor.extract_questions()

    normalized_questions = [
        NormalizedQuestion(
            module=module,
            name=question.name,
            label=question.label,
            text=question.text,
            question_type=question.question_type,
            options_json=question.options,
            help_text=question.help_text,
            interviewer_instruction=question.interviewer_instruction,
            source_identifier=question.source_identifier,
            source_composite_id=question.source_composite_id,
            sequence_index=question.sequence_index,
        )
        for question in extracted_questions
    ]

    NormalizedQuestion.objects.bulk_create(normalized_questions)

    module.processing_status = QuestionnaireModule.ProcessingStatus.EXTRACTED
    module.save(update_fields=["processing_status", "updated_at"])

    return {
        "module_id": str(module.id),
        "question_count": len(normalized_questions),
        "status": module.processing_status,
    }
