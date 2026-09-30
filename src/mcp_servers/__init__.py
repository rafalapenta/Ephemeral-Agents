"""FastMCP server adapters for AGency."""
from .health import mcp as health_mcp
from .semantic_router import mcp as router_mcp

__all__ = ["health_mcp", "router_mcp"]
