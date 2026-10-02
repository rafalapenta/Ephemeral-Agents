"""Router module — stateless agent routing via FastMCP."""

from typing import Any


class AgentRouter:
    """
    Stateless router that delegates to registered agents.
    No accumulated session state — decisions are per-request.
    """

    def __init__(self):
        self._agents: dict[str, Any] = {}

    def register(self, agent_id: str, agent: Any) -> None:
        self._agents[agent_id] = agent

    def route(self, query: str, threshold: float = 0.25) -> dict:
        """
        Route a query to the best-matching agent.
        Returns agent_id, prompt, and tool_ids — minimal context.
        """
        # Placeholder: in production, this would use embedding similarity
        # or a lightweight classifier against agent profiles.
        return {
            "agent_id": "default",
            "query": query,
            "threshold": threshold,
            "tool_ids": [],
            "note": "Router is stateless — no context bloat",
        }

    def list_agents(self) -> list[str]:
        return list(self._agents.keys())


# Global singleton
router = AgentRouter()
