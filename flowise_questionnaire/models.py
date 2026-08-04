import uuid

from django.conf import settings
from django.db import models


class QuestionnaireModule(models.Model):
    class ProcessingStatus(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        EXTRACTING = "extracting", "Extracting"
        EXTRACTED = "extracted", "Extracted"
        FAILED = "failed", "Failed"

    class SourceFormat(models.TextChoices):
        COLECTICA_JSON = "colectica_json", "Colectica JSON"
        FORSTA_XML = "forsta_xml", "Forsta+ XML"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="flowise_questionnaire_modules",
    )

    name = models.CharField(max_length=255)
    version = models.CharField(max_length=100, blank=True)

    uploaded_file = models.FileField(upload_to="flowise_questionnaires/raw_json/")
    raw_json = models.JSONField(null=True, blank=True)

    source_format = models.CharField(
        max_length=30,
        choices=SourceFormat.choices,
        default=SourceFormat.COLECTICA_JSON,
    )
    raw_xml = models.TextField(null=True, blank=True)

    processing_status = models.CharField(
        max_length=30,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.UPLOADED,
    )

    error_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "flowise_questionnaire_module"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.version})" if self.version else self.name


class NormalizedQuestion(models.Model):
    class QuestionType(models.TextChoices):
        SINGLE_CHOICE = "single_choice", "Single choice"
        MULTI_CHOICE = "multi_choice", "Multi choice"
        TEXT = "text", "Text"
        NUMBER = "number", "Number"
        DATE = "date", "Date"
        GRID_MATRIX = "grid_matrix", "Grid / Matrix"
        UNKNOWN = "unknown", "Unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    module = models.ForeignKey(
        QuestionnaireModule,
        on_delete=models.CASCADE,
        related_name="normalized_questions",
    )

    name = models.CharField(max_length=255)
    label = models.TextField(blank=True)
    text = models.TextField(blank=True)

    question_type = models.CharField(
        max_length=50,
        choices=QuestionType.choices,
        default=QuestionType.UNKNOWN,
    )

    options_json = models.JSONField(default=list, blank=True)
    help_text = models.TextField(blank=True)
    interviewer_instruction = models.TextField(blank=True)

    source_identifier = models.CharField(max_length=255, blank=True)
    source_composite_id = models.JSONField(null=True, blank=True)

    sequence_index = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "flowise_normalized_question"
        ordering = ["module", "sequence_index", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["module", "name"],
                name="flowise_unique_question_per_module",
            )
        ]

    def __str__(self):
        return f"{self.module.name} :: {self.name}"


class RoutingEdge(models.Model):
    class EdgeType(models.TextChoices):
        SEQUENTIAL = "sequential", "Sequential"
        CONDITIONAL = "conditional", "Conditional"
        LOOP = "loop", "Loop"
        UNKNOWN = "unknown", "Unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    module = models.ForeignKey(
        QuestionnaireModule,
        on_delete=models.CASCADE,
        related_name="routing_edges",
    )

    source_question = models.TextField(blank=True)
    target_question = models.CharField(max_length=255)

    condition_text = models.TextField(blank=True)
    condition_ast = models.JSONField(null=True, blank=True)

    edge_type = models.CharField(
        max_length=30,
        choices=EdgeType.choices,
        default=EdgeType.UNKNOWN,
    )

    sequence_index = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "flowise_routing_edge"
        ordering = ["module", "sequence_index"]

    def __str__(self):
        if self.condition_text:
            return f"{self.source_question} -> {self.target_question} [{self.condition_text}]"
        return f"{self.source_question} -> {self.target_question}"


class QuestionnaireGraph(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    module = models.OneToOneField(
        QuestionnaireModule,
        on_delete=models.CASCADE,
        related_name="graph",
    )

    nodes_json = models.JSONField(default=list, blank=True)
    edges_json = models.JSONField(default=list, blank=True)
    mermaid_text = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "flowise_questionnaire_graph"

    def __str__(self):
        return f"Graph for {self.module.name}"


class AgentRun(models.Model):
    class TaskType(models.TextChoices):
        SCHEMA_REVIEW = "schema_review", "Schema review"
        ROUTING_ANALYSIS = "routing_analysis", "Routing analysis"
        TEST_PLANNING = "test_planning", "Test planning"
        SYNTHETIC_PROFILE_GENERATION = "synthetic_profile_generation", "Synthetic profile generation"
        SEMANTIC_VALIDATION = "semantic_validation", "Semantic validation"
        REPORT_EXPLANATION = "report_explanation", "Report explanation"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    module = models.ForeignKey(
        QuestionnaireModule,
        on_delete=models.CASCADE,
        related_name="agent_runs",
    )

    flowise_flow_id = models.CharField(max_length=255, blank=True)
    task_type = models.CharField(max_length=80, choices=TaskType.choices)

    input_json = models.JSONField(default=dict, blank=True)
    output_json = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.PENDING,
    )

    error_message = models.TextField(blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "flowise_agent_run"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.task_type} :: {self.status}"


class SyntheticProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    module = models.ForeignKey(
        QuestionnaireModule,
        on_delete=models.CASCADE,
        related_name="synthetic_profiles",
    )

    agent_run = models.ForeignKey(
        AgentRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="synthetic_profiles",
    )

    profile_json = models.JSONField(default=dict, blank=True)
    seed_answers_json = models.JSONField(default=dict, blank=True)
    target_routes_json = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "flowise_synthetic_profile"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Synthetic profile {self.id}"


class SimulationRun(models.Model):
    class RunType(models.TextChoices):
        RANDOM = "random", "Random"
        TARGET_QUESTION = "target_question", "Target question"
        FULL_COVERAGE = "full_coverage", "Full coverage"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    module = models.ForeignKey(
        QuestionnaireModule,
        on_delete=models.CASCADE,
        related_name="simulation_runs",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="flowise_simulation_runs",
    )

    run_type = models.CharField(max_length=50, choices=RunType.choices)
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.PENDING,
    )

    requested_config_json = models.JSONField(default=dict, blank=True)
    coverage_json = models.JSONField(default=dict, blank=True)
    summary_json = models.JSONField(default=dict, blank=True)

    error_message = models.TextField(blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "flowise_simulation_run"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.module.name} :: {self.run_type} :: {self.status}"


class SimulationCase(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    simulation_run = models.ForeignKey(
        SimulationRun,
        on_delete=models.CASCADE,
        related_name="cases",
    )

    profile = models.ForeignKey(
        SyntheticProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="simulation_cases",
    )

    answers_json = models.JSONField(default=dict, blank=True)
    active_path_json = models.JSONField(default=list, blank=True)
    skipped_questions_json = models.JSONField(default=list, blank=True)
    validation_result_json = models.JSONField(default=dict, blank=True)

    passed = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "flowise_simulation_case"
        ordering = ["simulation_run", "created_at"]

    def __str__(self):
        return f"Case {self.id} :: passed={self.passed}"


class ValidationIssue(models.Model):
    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    simulation_case = models.ForeignKey(
        SimulationCase,
        on_delete=models.CASCADE,
        related_name="validation_issues",
    )

    severity = models.CharField(max_length=30, choices=Severity.choices)
    issue_type = models.CharField(max_length=100)
    question_name = models.CharField(max_length=255, blank=True)
    message = models.TextField()

    details_json = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "flowise_validation_issue"
        ordering = ["simulation_case", "severity", "question_name"]

    def __str__(self):
        return f"{self.severity} :: {self.issue_type} :: {self.question_name}"

class ModuleAIReview(models.Model):
    class Status(models.TextChoices):
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"
        ERROR = "error", "Error"

    module = models.ForeignKey(
        QuestionnaireModule,
        on_delete=models.CASCADE,
        related_name="ai_reviews",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ERROR,
    )

    review_json = models.JSONField(
        default=dict,
        blank=True,
    )

    post_validation_json = models.JSONField(
        default=dict,
        blank=True,
    )

    request_payload_json = models.JSONField(
        default=dict,
        blank=True,
    )

    raw_response_json = models.JSONField(
        default=dict,
        blank=True,
    )

    error_message = models.TextField(
        blank=True,
        default="",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.module.name} AI review {self.status} at {self.created_at}"


class InterviewSimulatorSession(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        ABANDONED = "abandoned", "Abandoned"
        ERROR = "error", "Error"

    module = models.ForeignKey(
        QuestionnaireModule,
        on_delete=models.CASCADE,
        related_name="interview_simulator_sessions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="interview_simulator_sessions",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )

    current_question_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
    )

    previous_question_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
    )

    answers_json = models.JSONField(
        default=dict,
        blank=True,
        help_text="Internal coded answers keyed by questionnaire variable name.",
    )

    routing_trace_json = models.JSONField(
        default=list,
        blank=True,
        help_text="Deterministic routing trace for debugging and replay.",
    )

    graph_highlight_json = models.JSONField(
        default=dict,
        blank=True,
        help_text="Latest graph highlight payload for frontend synchronization.",
    )

    debug_json = models.JSONField(
        default=dict,
        blank=True,
        help_text="Latest designer/debug information for this simulator session.",
    )

    error_message = models.TextField(
        blank=True,
        default="",
    )

    started_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["module", "status"]),
            models.Index(fields=["user", "status"]),
            models.Index(fields=["current_question_name"]),
        ]

    def __str__(self):
        return (
            f"Interview simulator session {self.id} "
            f"for {self.module.name} ({self.status})"
        )


class InterviewSimulatorTurn(models.Model):
    session = models.ForeignKey(
        InterviewSimulatorSession,
        on_delete=models.CASCADE,
        related_name="turns",
    )

    turn_index = models.PositiveIntegerField(
        db_index=True,
    )

    question_name = models.CharField(
        max_length=255,
        db_index=True,
    )

    question_text = models.TextField(
        blank=True,
        default="",
    )

    selection_mode = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="single or multiple.",
    )

    shown_options_json = models.JSONField(
        default=list,
        blank=True,
        help_text="A/B/C respondent-facing options shown at this turn.",
    )

    assistant_message = models.TextField(
        blank=True,
        default="",
        help_text="Question message shown to respondent.",
    )

    user_answer_text = models.TextField(
        blank=True,
        default="",
        help_text="Raw respondent input, usually A/B/C letters.",
    )

    user_answer_letters_json = models.JSONField(
        default=list,
        blank=True,
    )

    coded_answer_json = models.JSONField(
        default=dict,
        blank=True,
        help_text="Validated internal answer code, e.g. {'BENBASE': [3]}.",
    )

    validation_json = models.JSONField(
        default=dict,
        blank=True,
    )

    routing_result_json = models.JSONField(
        default=dict,
        blank=True,
    )

    graph_highlight_json = models.JSONField(
        default=dict,
        blank=True,
    )

    flowise_request_json = models.JSONField(
        default=dict,
        blank=True,
    )

    flowise_response_json = models.JSONField(
        default=dict,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["session", "turn_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "turn_index"],
                name="unique_interview_simulator_turn_per_session",
            )
        ]
        indexes = [
            models.Index(fields=["session", "turn_index"]),
            models.Index(fields=["question_name"]),
        ]

    def __str__(self):
        return (
            f"Turn {self.turn_index} "
            f"({self.question_name}) "
            f"for session {self.session_id}"
        )


class QuestionMatch(models.Model):
    """
    Links one Colectica NormalizedQuestion to its matched Forsta+ counterpart
    (or to nothing, when unmatched) for routing-validation comparison.
    """

    class MatchMethod(models.TextChoices):
        EXACT = "exact", "Exact"
        FUZZY = "fuzzy", "Fuzzy"
        NAME = "name", "Name"
        MANUAL = "manual", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    colectica_question = models.ForeignKey(
        NormalizedQuestion,
        on_delete=models.CASCADE,
        related_name="colectica_matches",
    )

    forsta_question = models.ForeignKey(
        NormalizedQuestion,
        on_delete=models.CASCADE,
        related_name="forsta_matches",
        null=True,
        blank=True,
    )

    # Which Forsta+ module this match attempt was run against -- set even
    # when forsta_question is None (unmatched), since that's the only way to
    # scope a re-run's cleanup to one (colectica_module, forsta_module) pair
    # instead of wiping out every pair sharing the same Colectica module.
    # Nullable so pre-existing rows/test fixtures created before this field
    # existed don't need a backfill.
    forsta_module = models.ForeignKey(
        QuestionnaireModule,
        on_delete=models.CASCADE,
        related_name="+",
        null=True,
        blank=True,
    )

    match_score = models.FloatField(default=0.0)

    match_method = models.CharField(
        max_length=20,
        choices=MatchMethod.choices,
        default=MatchMethod.FUZZY,
    )

    confirmed = models.BooleanField(default=False)

    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "flowise_question_match"
        ordering = ["-match_score"]

    def __str__(self):
        forsta_name = self.forsta_question.name if self.forsta_question_id else "(unmatched)"
        return f"{self.colectica_question.name} <-> {forsta_name} ({self.match_method})"


class RoutingDiscrepancy(models.Model):
    """
    One structural difference between a matched question's Colectica routing
    and its Forsta+ routing.
    """

    class DiscrepancyType(models.TextChoices):
        # Display labels are worded around "edge" deliberately: every
        # discrepancy row already implies a matched question pair (see
        # RoutingComparator._compare_match), so the thing that's actually
        # missing/mismatched is always a specific routing edge, never the
        # question itself. The label must say so on its own, since the
        # sentence explaining that lives in a separate column.
        MISSING_IN_FORSTA = "missing_in_forsta", "Edge missing in Forsta+"
        MISSING_IN_COLECTICA = "missing_in_colectica", "Edge missing in Colectica"
        CONDITION_MISMATCH = "condition_mismatch", "Edge condition mismatch"
        ELSE_BRANCH_ONLY_IN_FORSTA = "else_branch_only_in_forsta", "Else-branch edge only in Forsta+"

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    question_match = models.ForeignKey(
        QuestionMatch,
        on_delete=models.CASCADE,
        related_name="discrepancies",
    )

    discrepancy_type = models.CharField(
        max_length=40,
        choices=DiscrepancyType.choices,
    )

    colectica_edge = models.ForeignKey(
        RoutingEdge,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    forsta_edge = models.ForeignKey(
        RoutingEdge,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    details_json = models.JSONField(default=dict, blank=True)

    severity = models.CharField(
        max_length=20,
        choices=Severity.choices,
        default=Severity.WARNING,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "flowise_routing_discrepancy"
        ordering = ["-severity", "created_at"]

    def __str__(self):
        return f"{self.discrepancy_type} :: {self.question_match}"