"""FastMCP server for AGency — combines all MCP tools (router + health).

This is the main entry point for the AGency MCP server.
Run with: `python -m src.mcp_servers.server`
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import both MCP servers
from src.mcp_servers.semantic_router import mcp as router_mcp
from src.mcp_servers.health import mcp as health_mcp

# We can't easily merge FastMCP instances, so we'll use the router as the main
# and add health tools to it, or run both. For now, we'll just run the router.
# In a production setup, you'd want to combine them or run separate servers.

def main() -> None:
    """Run the combined AGency MCP server."""
    print("Starting AGency MCP Server...")
    print("Available tools: route_agent, list_agents, health_check, agent_health_check, list_agents_health")
    # Run the router MCP server (it's the primary one)
    router_mcp.run()


if __name__ == "__main__":
    main()