from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.prompts import Prompt

from mcp_server import prompts, tools

mcp = MCPServer(
    name="flowise-questionnaire",
    instructions=(
        "Read-only access to parsed questionnaire data (questions and routing "
        "edges) extracted by the flowise-questionnaire-system Django app, plus "
        "two independent routing-diff comparisons: structural (and, for the "
        "version-diff pair, condition-text) discrepancies between a matched "
        "Colectica/Forsta+ module pair, and between two Colectica-format "
        "modules (e.g. two waves/versions of the same questionnaire). "
        "Deterministic parsing owns these facts -- this server never "
        "summarizes or paraphrases them; do your own interpretation on the "
        "client side. Most tools are Colectica-only and reject Forsta+ "
        "modules with a clear error; tools whose docstring names Forsta+ "
        "explicitly (get_module_graph, evaluate_edge_condition, and the "
        "Colectica/Forsta+ routing-diff tools) support both. The version-diff "
        "tools (get_version_diff_report, get_version_diff_discrepancy_detail, "
        "list_version_diff_affected_questions) require both modules to be "
        "Colectica JSON."
    ),
)

mcp.add_tool(tools.list_modules)
mcp.add_tool(tools.get_module_summary)
mcp.add_tool(tools.list_questions)
mcp.add_tool(tools.get_question)
mcp.add_tool(tools.get_routing_edges)
mcp.add_tool(tools.trace_variable)
mcp.add_tool(tools.get_module_graph)
mcp.add_tool(tools.evaluate_edge_condition)
mcp.add_tool(tools.get_routing_diff_report)
mcp.add_tool(tools.get_routing_discrepancy_detail)
mcp.add_tool(tools.get_routing_simulation)
mcp.add_tool(tools.get_version_diff_report)
mcp.add_tool(tools.get_version_diff_discrepancy_detail)
mcp.add_tool(tools.list_version_diff_affected_questions)

mcp.add_prompt(Prompt.from_function(prompts.list_modules, name="listModules"))
mcp.add_prompt(Prompt.from_function(prompts.show_module_summary, name="showModuleSummary"))
mcp.add_prompt(Prompt.from_function(prompts.list_questions, name="listQuestions"))
mcp.add_prompt(Prompt.from_function(prompts.show_question, name="showQuestion"))
mcp.add_prompt(Prompt.from_function(prompts.list_routing_edges, name="listRoutingEdges"))
mcp.add_prompt(Prompt.from_function(prompts.trace_variable, name="traceVariable"))
mcp.add_prompt(Prompt.from_function(prompts.show_module_graph, name="showModuleGraph"))
mcp.add_prompt(Prompt.from_function(prompts.evaluate_condition, name="evaluateCondition"))
mcp.add_prompt(Prompt.from_function(prompts.show_routing_diff_report, name="showRoutingDiffReport"))
mcp.add_prompt(
    Prompt.from_function(
        prompts.show_colectica_forsta_discrepancy, name="showColecticaForstaDiscrepancy"
    )
)
mcp.add_prompt(Prompt.from_function(prompts.show_routing_simulation, name="showRoutingSimulation"))
mcp.add_prompt(Prompt.from_function(prompts.show_version_diff_report, name="showVersionDiffReport"))
mcp.add_prompt(
    Prompt.from_function(
        prompts.show_version_diff_discrepancy, name="showVersionDiffDiscrepancy"
    )
)
mcp.add_prompt(
    Prompt.from_function(
        prompts.list_version_diff_affected_questions, name="listVersionDiffAffectedQuestions"
    )
)
