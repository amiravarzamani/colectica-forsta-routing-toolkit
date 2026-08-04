from django.contrib import admin

from .models import (
    AgentRun,
    NormalizedQuestion,
    QuestionnaireGraph,
    QuestionnaireModule,
    RoutingEdge,
    SimulationCase,
    SimulationRun,
    SyntheticProfile,
    ValidationIssue,
)


@admin.register(QuestionnaireModule)
class QuestionnaireModuleAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "version",
        "user",
        "processing_status",
        "created_at",
        "updated_at",
    )
    list_filter = ("processing_status", "created_at")
    search_fields = ("name", "version", "user__username", "user__email")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(NormalizedQuestion)
class NormalizedQuestionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "module",
        "name",
        "question_type",
        "sequence_index",
        "created_at",
    )
    list_filter = ("question_type", "module")
    search_fields = ("name", "label", "text", "module__name")
    readonly_fields = ("id", "created_at")


@admin.register(RoutingEdge)
class RoutingEdgeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "module",
        "source_question",
        "target_question",
        "edge_type",
        "sequence_index",
        "created_at",
    )
    list_filter = ("edge_type", "module")
    search_fields = (
        "source_question",
        "target_question",
        "condition_text",
        "module__name",
    )
    readonly_fields = ("id", "created_at")


@admin.register(QuestionnaireGraph)
class QuestionnaireGraphAdmin(admin.ModelAdmin):
    list_display = ("id", "module", "created_at", "updated_at")
    search_fields = ("module__name",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "module",
        "task_type",
        "status",
        "flowise_flow_id",
        "created_at",
    )
    list_filter = ("task_type", "status", "module")
    search_fields = ("module__name", "flowise_flow_id", "error_message")
    readonly_fields = ("id", "created_at")


@admin.register(SyntheticProfile)
class SyntheticProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "module", "agent_run", "created_at")
    list_filter = ("module",)
    search_fields = ("module__name",)
    readonly_fields = ("id", "created_at")


@admin.register(SimulationRun)
class SimulationRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "module",
        "user",
        "run_type",
        "status",
        "created_at",
    )
    list_filter = ("run_type", "status", "module")
    search_fields = ("module__name", "user__username", "user__email")
    readonly_fields = ("id", "created_at")


@admin.register(SimulationCase)
class SimulationCaseAdmin(admin.ModelAdmin):
    list_display = ("id", "simulation_run", "profile", "passed", "created_at")
    list_filter = ("passed",)
    search_fields = ("simulation_run__module__name",)
    readonly_fields = ("id", "created_at")


@admin.register(ValidationIssue)
class ValidationIssueAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "simulation_case",
        "severity",
        "issue_type",
        "question_name",
        "created_at",
    )
    list_filter = ("severity", "issue_type")
    search_fields = ("question_name", "message", "issue_type")
    readonly_fields = ("id", "created_at")