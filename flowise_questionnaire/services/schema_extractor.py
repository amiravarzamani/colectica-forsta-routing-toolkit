from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction

from flowise_questionnaire.models import NormalizedQuestion, QuestionnaireModule


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


class ColecticaSchemaExtractor:
    """
    Deterministic extractor for Colectica-style questionnaire JSON.

    This class must stay generic:
    - no module-specific question names
    - no benefits-specific assumptions
    - no hardcoded routing rules
    """

    def __init__(self, raw_json: dict[str, Any]):
        self.raw_json = raw_json
        self._sequence_index = 0
        self._seen_question_names: set[str] = set()

    def extract_questions(self) -> list[ExtractedQuestion]:
        questions: list[ExtractedQuestion] = []

        for activity in self._walk_question_activities(self.raw_json):
            extracted = self._extract_question_from_activity(activity)

            if extracted is None:
                continue

            if extracted.name in self._seen_question_names:
                continue

            self._seen_question_names.add(extracted.name)
            questions.append(extracted)

        return questions

    def _walk_question_activities(self, value: Any):
        """
        Recursively finds activity objects that contain a Question object.
        This supports nested branches and child sequences.
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

    def _extract_question_from_activity(self, activity: dict[str, Any]) -> ExtractedQuestion | None:
        question = activity.get("Question")

        if not isinstance(question, dict):
            return None

        name = self._multilingual_text(question.get("ItemName"))
        if not name:
            name = self._multilingual_text(activity.get("ItemName"))

        if not name:
            return None

        label = self._multilingual_text(question.get("Label"))
        text = self._multilingual_text(question.get("QuestionText"))
        if not text:
            text = self._multilingual_text(question.get("Summary"))

        response_domains = question.get("ResponseDomains") or []
        question_type = self._detect_question_type(response_domains)
        options = self._extract_options(response_domains)

        help_text = self._extract_custom_field(question, "Help")
        interviewer_instruction = self._extract_custom_field(
            question,
            "Post-text Interviewer Instruction",
        )

        if not interviewer_instruction:
            interviewer_instruction = self._multilingual_text(
                question.get("InterviewerInstructions")
            )

        source_identifier = question.get("Identifier") or ""
        source_composite_id = question.get("CompositeId")

        self._sequence_index += 1

        return ExtractedQuestion(
            name=name,
            label=label,
            text=text,
            question_type=question_type,
            options=options,
            help_text=help_text,
            interviewer_instruction=interviewer_instruction,
            source_identifier=source_identifier,
            source_composite_id=source_composite_id,
            sequence_index=self._sequence_index,
        )

    def _detect_question_type(self, response_domains: list[dict[str, Any]]) -> str:
        if not response_domains:
            return NormalizedQuestion.QuestionType.UNKNOWN

        # Numeric domains can appear together with a second coded domain that only
        # contains missing values such as Don't know / Refused / Inapplicable.
        # Therefore numeric detection must happen before choice detection and must
        # inspect all response domains, not only the first one.
        for domain in response_domains:
            if not isinstance(domain, dict):
                continue

            if domain.get("NumericType"):
                return NormalizedQuestion.QuestionType.NUMBER

            representation_type = domain.get("RepresentationType")

            if representation_type in [1, 6, "number", "numeric"]:
                return NormalizedQuestion.QuestionType.NUMBER

        first_domain = response_domains[0]

        if "Codes" in first_domain:
            multiple_choice_type = first_domain.get("MultipleChoiceType")

            if multiple_choice_type == 1:
                return NormalizedQuestion.QuestionType.MULTI_CHOICE

            if multiple_choice_type == 0:
                return NormalizedQuestion.QuestionType.SINGLE_CHOICE

            return NormalizedQuestion.QuestionType.SINGLE_CHOICE

        representation_type = first_domain.get("RepresentationType")

        if representation_type in [1, 6, "number", "numeric"]:
            return NormalizedQuestion.QuestionType.NUMBER

        if representation_type in [2, 4, "date"]:
            return NormalizedQuestion.QuestionType.DATE

        if representation_type in [0, 7, "text", "string"]:
            return NormalizedQuestion.QuestionType.TEXT

        return NormalizedQuestion.QuestionType.UNKNOWN

    def _extract_options(self, response_domains: list[dict[str, Any]]) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []

        for domain in response_domains:
            codes_container = domain.get("Codes") or {}
            codes = codes_container.get("Codes") or []

            scripting_notes = self._extract_custom_field_from_container(
                domain,
                "Scripting Notes",
            )

            for code in codes:
                value = str(code.get("Value", ""))
                category = code.get("Category") or {}

                label = self._multilingual_text(category.get("Label"))
                if not label:
                    label = category.get("DisplayLabel") or ""

                is_missing = bool(category.get("IsMissing", False))

                option = {
                    "code": value,
                    "label": label,
                    "is_missing": is_missing,
                    "exclusive": self._is_probably_exclusive(value, label, scripting_notes),
                    "category_identifier": category.get("Identifier") or "",
                }

                options.append(option)

        return options

    def _is_probably_exclusive(self, code: str, label: str, scripting_notes: str) -> bool:
        label_lower = label.lower()
        notes_lower = scripting_notes.lower()

        if "exclusive" in notes_lower and code in {"96", "97", "98"}:
            return True

        if label_lower in {"none", "none of these", "none of the above"}:
            return True

        return False

    def _extract_custom_field(self, question: dict[str, Any], title: str) -> str:
        return self._extract_custom_field_from_container(question, title)

    def _extract_custom_field_from_container(self, container: dict[str, Any], title: str) -> str:
        custom_fields = container.get("CustomFields") or []

        for field in custom_fields:
            field_title = self._multilingual_text(field.get("Title"))

            if field_title == title:
                return field.get("StringValue") or ""

        return ""

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
def extract_schema_for_module(module: QuestionnaireModule) -> dict[str, Any]:
    if module.source_format == QuestionnaireModule.SourceFormat.FORSTA_XML:
        from flowise_questionnaire.services.forsta_xml_schema_extractor import (
            extract_schema_for_forsta_module,
        )

        return extract_schema_for_forsta_module(module)

    if not module.raw_json:
        raise ValueError("Module has no raw_json stored.")

    module.processing_status = QuestionnaireModule.ProcessingStatus.EXTRACTING
    module.error_message = ""
    module.save(update_fields=["processing_status", "error_message", "updated_at"])

    NormalizedQuestion.objects.filter(module=module).delete()

    extractor = ColecticaSchemaExtractor(module.raw_json)
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