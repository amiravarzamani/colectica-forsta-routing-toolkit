from django.urls import path

from flowise_questionnaire.views import (
    questionnaire_login_view,
    questionnaire_logout_view,

    module_build_all_view,
    module_build_graph_view,
    module_detail_view,
    module_extract_routing_view,
    module_extract_schema_view,
    module_graph_view,
    module_list_view,
    module_upload_view,

    module_agentflow_payload_view,
    module_agentflow_compact_payload_view,
    module_run_flowise_review_view,
    module_ai_review_history_view,
    module_ai_review_detail_view,

    module_routing_simulation_view,
    module_routing_simulation_json_view,

    interview_simulator_abandon_view,
    interview_simulator_answer_view,
    interview_simulator_page_view,
    interview_simulator_start_view,
    interview_simulator_state_view,

    routing_diff_discrepancy_detail_view,
    routing_diff_report_view,
    routing_diff_run_view,

    version_diff_discrepancy_detail_view,
    version_diff_report_view,
    version_diff_run_view,
)

app_name = "flowise_questionnaire"

urlpatterns = [
    path("login/", questionnaire_login_view, name="login"),
    path("logout/", questionnaire_logout_view, name="logout"),

    path("", module_list_view, name="module_list"),
    path("upload/", module_upload_view, name="module_upload"),

    path("modules/<uuid:module_id>/", module_detail_view, name="module_detail"),
    path("modules/<uuid:module_id>/graph/", module_graph_view, name="module_graph"),

    path(
        "modules/<uuid:module_id>/extract-schema/",
        module_extract_schema_view,
        name="module_extract_schema",
    ),
    path(
        "modules/<uuid:module_id>/extract-routing/",
        module_extract_routing_view,
        name="module_extract_routing",
    ),
    path(
        "modules/<uuid:module_id>/build-graph/",
        module_build_graph_view,
        name="module_build_graph",
    ),
    path(
        "modules/<uuid:module_id>/build-all/",
        module_build_all_view,
        name="module_build_all",
    ),

    path(
        "modules/<uuid:module_id>/agentflow-payload/",
        module_agentflow_payload_view,
        name="module_agentflow_payload",
    ),
    path(
        "modules/<uuid:module_id>/agentflow-payload/compact/",
        module_agentflow_compact_payload_view,
        name="module_agentflow_compact_payload",
    ),
    path(
        "modules/<uuid:module_id>/flowise-review/run/",
        module_run_flowise_review_view,
        name="module_run_flowise_review",
    ),

    path(
        "modules/<uuid:module_id>/ai-reviews/",
        module_ai_review_history_view,
        name="module_ai_review_history",
    ),
    path(
        "modules/<uuid:module_id>/ai-reviews/<int:review_id>/",
        module_ai_review_detail_view,
        name="module_ai_review_detail",
    ),

    path(
        "modules/<uuid:module_id>/routing-simulation/",
        module_routing_simulation_view,
        name="module_routing_simulation",
    ),
    path(
        "modules/<uuid:module_id>/routing-simulation/json/",
        module_routing_simulation_json_view,
        name="module_routing_simulation_json",
    ),

    path(
        "modules/<uuid:module_id>/interview-simulator/",
        interview_simulator_page_view,
        name="interview_simulator_page",
    ),
    path(
        "modules/<uuid:module_id>/simulator/start/",
        interview_simulator_start_view,
        name="interview_simulator_start",
    ),
    path(
        "simulator-sessions/<int:session_id>/",
        interview_simulator_state_view,
        name="interview_simulator_state",
    ),
    path(
        "simulator-sessions/<int:session_id>/answer/",
        interview_simulator_answer_view,
        name="interview_simulator_answer",
    ),
    path(
        "simulator-sessions/<int:session_id>/abandon/",
        interview_simulator_abandon_view,
        name="interview_simulator_abandon",
    ),

    path(
        "routing-diff/<uuid:colectica_module_id>/<uuid:forsta_module_id>/",
        routing_diff_report_view,
        name="routing_diff_report",
    ),
    path(
        "routing-diff/<uuid:colectica_module_id>/<uuid:forsta_module_id>/run/",
        routing_diff_run_view,
        name="routing_diff_run",
    ),
    path(
        "routing-diff/<uuid:colectica_module_id>/<uuid:forsta_module_id>/discrepancy/<uuid:discrepancy_id>/",
        routing_diff_discrepancy_detail_view,
        name="routing_diff_discrepancy_detail",
    ),

    path(
        "version-diff/<uuid:base_module_id>/<uuid:compare_module_id>/",
        version_diff_report_view,
        name="version_diff_report",
    ),
    path(
        "version-diff/<uuid:base_module_id>/<uuid:compare_module_id>/run/",
        version_diff_run_view,
        name="version_diff_run",
    ),
    path(
        "version-diff/<uuid:base_module_id>/<uuid:compare_module_id>/discrepancy/<uuid:discrepancy_id>/",
        version_diff_discrepancy_detail_view,
        name="version_diff_discrepancy_detail",
    ),
]