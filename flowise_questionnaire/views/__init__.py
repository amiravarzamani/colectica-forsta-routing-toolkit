from .auth_views import questionnaire_login_view, questionnaire_logout_view
from .module_views import (
    module_agentflow_payload_view,
    module_agentflow_compact_payload_view,
    module_build_all_view,
    module_build_graph_view,
    module_detail_view,
    module_extract_routing_view,
    module_extract_schema_view,
    module_graph_view,
    module_list_view,
    module_upload_view,
    module_run_flowise_review_view,
    module_ai_review_history_view,
    module_ai_review_detail_view,
)
from .routing_simulation_views import (
    module_routing_simulation_view,
    module_routing_simulation_json_view,
)
from .interview_simulator_views import (
    interview_simulator_abandon_view,
    interview_simulator_answer_view,
    interview_simulator_page_view,
    interview_simulator_start_view,
    interview_simulator_state_view,
)
from .routing_diff_views import (
    routing_diff_discrepancy_detail_view,
    routing_diff_report_view,
    routing_diff_run_view,
)

__all__ = [
    "questionnaire_login_view",
    "questionnaire_logout_view",

    "module_build_all_view",
    "module_build_graph_view",
    "module_detail_view",
    "module_extract_routing_view",
    "module_extract_schema_view",
    "module_graph_view",
    "module_list_view",
    "module_upload_view",

    "module_agentflow_payload_view",
    "module_agentflow_compact_payload_view",
    "module_run_flowise_review_view",
    "module_ai_review_history_view",
    "module_ai_review_detail_view",

    "module_routing_simulation_view",
    "module_routing_simulation_json_view",

    "interview_simulator_page_view",
    "interview_simulator_start_view",
    "interview_simulator_state_view",
    "interview_simulator_answer_view",
    "interview_simulator_abandon_view",

    "routing_diff_discrepancy_detail_view",
    "routing_diff_report_view",
    "routing_diff_run_view",
]