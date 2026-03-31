"""Agent definitions for the UI testing swarm."""

from claude_agent_sdk import AgentDefinition


def make_explorer_agent() -> AgentDefinition:
    """Agent that explores and maps a web application."""
    return AgentDefinition(
        description=(
            "Website explorer specialist. Use this agent to crawl and map "
            "a web application — it discovers pages, forms, navigation, "
            "and user flows by browsing like a real human."
        ),
        prompt=(
            "You are an expert web application explorer. "
            "Use the Playwright browser tools to navigate, click, type, "
            "and take screenshots. Be thorough and systematic."
        ),
        tools=["mcp__playwright__*"],
    )


def make_tester_agent() -> AgentDefinition:
    """Agent that executes test scenarios in the browser."""
    return AgentDefinition(
        description=(
            "Website tester specialist. Use this agent to execute specific "
            "test cases against a web application — it interacts with the "
            "site like a real user and validates expected behavior."
        ),
        prompt=(
            "You are an expert QA tester. "
            "Use the Playwright browser tools to execute test steps precisely. "
            "Document everything — take screenshots, note what works and what doesn't."
        ),
        tools=["mcp__playwright__*"],
    )


SWARM_AGENTS = {
    "explorer": make_explorer_agent(),
    "tester": make_tester_agent(),
}
