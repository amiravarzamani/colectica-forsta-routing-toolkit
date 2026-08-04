"""
MCP prompts -- slash-command-style shortcuts (e.g. Claude Desktop's "/") that
pre-fill a message telling the assistant which mcp_server.tools function to
call and how to use it. One prompt per active tool; registered in server.py
via Prompt.from_function(..., name="<camelCase>"). Kept as plain functions
(not the @mcp.prompt() decorator) so they're testable in isolation, matching
tools.py's convention of plain functions + explicit registration in server.py.
"""

from __future__ import annotations


def list_modules() -> str:
    """List every Colectica questionnaire module."""

    return (
        "Using the list_modules tool, list every Colectica questionnaire "
        "module available, with its status, question count, and routing-edge "
        "count."
    )


def show_module_summary(module_id_or_name: str) -> str:
    """Full status summary for one module."""

    return (
        f"Using the get_module_summary tool, give me the full status summary "
        f"for module '{module_id_or_name}' (question/edge counts by type, "
        f"whether a graph has been built, and any error message)."
    )


def list_questions(module_id_or_name: str) -> str:
    """List every question in a module."""

    return (
        f"Using the list_questions tool, list every question in module "
        f"'{module_id_or_name}' with its type and options."
    )


def show_question(module_id_or_name: str, question_name: str) -> str:
    """Full detail for one question."""

    return (
        f"Using the get_question tool, show me the full detail for question "
        f"'{question_name}' in module '{module_id_or_name}'."
    )


def list_routing_edges(module_id_or_name: str) -> str:
    """List every routing edge in a module."""

    return (
        f"Using the get_routing_edges tool, list every routing edge in "
        f"module '{module_id_or_name}' (source, target, condition, edge "
        f"type)."
    )


def trace_variable(variable_name: str, module_id_or_name: str | None = None) -> str:
    """Find every routing edge referencing a variable."""

    scope = f"module '{module_id_or_name}'" if module_id_or_name else "all Colectica modules"
    return (
        f"Using the trace_variable tool, find every routing edge referencing "
        f"variable '{variable_name}' across {scope}, and tell me which "
        f"references are external variables."
    )


def show_module_graph(module_id_or_name: str, question_name: str | None = None) -> str:
    """Show the routing graph for a module, or a question's neighborhood."""

    if question_name:
        return (
            f"Using the get_module_graph tool, show me the routing graph "
            f"around question '{question_name}' in module "
            f"'{module_id_or_name}' (its prerequisites, incoming/outgoing "
            f"routes, and start semantics). Render the returned \"mermaid\" "
            f"text as a ```mermaid fenced code block so I can see the "
            f"diagram, not just the text description."
        )

    return (
        f"Using the get_module_graph tool, show me the routing graph for "
        f"module '{module_id_or_name}'. If it's too large to return in "
        f"full, summarize graph_start_summary and the node/edge counts, "
        f"then ask me which question to zoom into. If a \"mermaid\" field "
        f"is returned, render it as a ```mermaid fenced code block."
    )


def evaluate_condition(module_id_or_name: str, condition_text: str, inputs: dict) -> str:
    """Evaluate a hypothetical routing condition against hypothetical answers."""

    return (
        f"Using the evaluate_edge_condition tool, evaluate the condition "
        f"'{condition_text}' for module '{module_id_or_name}' against these "
        f"hypothetical answers: {inputs}. Tell me whether it fires (true/"
        f"false/unknown/unsupported) and which inputs were used or missing."
    )


def show_routing_diff_report(
    colectica_module_id_or_name: str,
    forsta_module_id_or_name: str,
) -> str:
    """Show the routing-diff report for a Colectica/Forsta+ module pair."""

    return (
        f"Using the get_routing_diff_report tool, show me the routing-diff "
        f"report between Colectica module '{colectica_module_id_or_name}' "
        f"and Forsta+ module '{forsta_module_id_or_name}'. If has_run is "
        f"false, tell me matching/comparison hasn't been run for this pair "
        f"yet instead of guessing. Otherwise summarize the match_summary "
        f"and group the discrepancies by type and severity."
    )


def show_colectica_forsta_discrepancy(
    colectica_module_id_or_name: str,
    forsta_module_id_or_name: str,
    discrepancy_id: str,
) -> str:
    """Show one routing discrepancy in full detail, both systems' graphs included."""

    return (
        f"Using the get_routing_discrepancy_detail tool, show me full detail "
        f"for discrepancy '{discrepancy_id}' between Colectica module "
        f"'{colectica_module_id_or_name}' and Forsta+ module "
        f"'{forsta_module_id_or_name}' -- the summary, the meaning "
        f"(why this is a lead not a confirmed bug), and describe the "
        f"routing around the focus question on both the Colectica graph "
        f"and the Forsta+ graph."
    )


def show_routing_simulation(module_id_or_name: str) -> str:
    """Show coverage-intent simulation results for a module."""

    return (
        f"Using the get_routing_simulation tool, show me the routing "
        f"coverage simulation for module '{module_id_or_name}'. Summarize "
        f"status_counts and call out any intents that are not_covered or "
        f"blocked_missing_inputs."
    )
