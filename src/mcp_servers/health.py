"""FastMCP server for AGency — health check tools.

This module defines the health check MCP tools and can be run as a standalone
MCP server via `python -m src.mcp_servers.health`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.health.health import check_agent_health, check_system_health, get_health_summary

mcp = FastMCP("agency-health")


@mcp.tool()
def health_check() -> dict[str, Any]:
    """Get overall system health status.

    Returns a summary of system health including:
    - Overall status (healthy/degraded/unhealthy)
    - Agent counts by status
    - Catalog, routing, and Kanban accessibility
    - Any errors encountered
    """
    return get_health_summary()


@mcp.tool()
def agent_health_check(agent_id: str) -> dict[str, Any]:
    """Check health of a specific agent by ID.

    Args:
        agent_id: The agent identifier (e.g., 'atlas', 'vulcan', 'aura')

    Returns:
        Detailed health report for the specified agent including:
        - Status (healthy/degraded/unhealthy/missing)
        - Individual checks (SOUL.md validity, routing accessibility)
        - Any errors encountered
    """
    report = check_agent_health(agent_id)
    return report.model_dump(mode="json")


@mcp.tool()
def list_agents_health() -> list[dict[str, Any]]:
    """List health status of all registered agents.

    Returns:
        List of agent health summaries including agent_id, name,
        macro_domain, status, and last check timestamp.
    """
    report = check_system_health()
    results = []
    for agent_id, agent_report in report.agent_reports.items():
        results.append({
            "agent_id": agent_id,
            "name": agent_report.name,
            "macro_domain": agent_report.macro_domain,
            "status": agent_report.status,
            "last_check": agent_report.last_check,
            "errors": agent_report.errors,
        })
    return results


def main() -> None:
    """Run the health check MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
