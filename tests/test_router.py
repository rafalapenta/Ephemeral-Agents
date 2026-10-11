from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.catalog.indexer import run_indexing
from src.database.models import AgentTool, Tool
from src.mcp_servers.semantic_router import create_mcp
from src.router.semantic import RouteAgentResult, route_agent


def _agent(name: str, description: str, role: str) -> str:
    return f"""---
name: {name}
description: {description}
vibe: Precise specialist.
---
# {name}
- **Role**: {role}
"""


def _build_catalog(tmp_path: Path) -> tuple[Path, str, Path]:
    source_root = tmp_path / "agency-agents"
    engineering = source_root / "engineering" / "engineering-frontend-developer.md"
    finance = source_root / "finance" / "finance-risk-analyst.md"
    engineering.parent.mkdir(parents=True)
    finance.parent.mkdir(parents=True)
    engineering.write_text(
        _agent(
            "Frontend Developer",
            "Expert React frontend accessibility and web performance engineer.",
            "Modern UI implementation specialist",
        ),
        encoding="utf-8",
    )
    finance.write_text(
        _agent(
            "Risk Analyst",
            "Credit risk, fixed income, and financial covenant analyst.",
            "Financial risk specialist",
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "catalog.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    chroma_path = tmp_path / "chroma"
    report = run_indexing(
        source_root=source_root,
        database_url=database_url,
        chroma_path=chroma_path,
        dry_run=False,
        reindex=True,
    )
    assert report.written == 2

    engine = create_engine(database_url)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                Tool(
                    tool_id="repo-search",
                    name="Repository Search",
                    tool_type="fastmcp",
                    connection_config={},
                    input_schema={},
                    output_schema={},
                ),
                Tool(
                    tool_id="finance-ledger",
                    name="Finance Ledger",
                    tool_type="fastmcp",
                    connection_config={},
                    input_schema={},
                    output_schema={},
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                AgentTool(
                    agent_id="engineering-frontend-developer",
                    tool_id="repo-search",
                ),
                AgentTool(
                    agent_id="finance-risk-analyst",
                    tool_id="finance-ledger",
                ),
            ]
        )
    return source_root, database_url, chroma_path


def test_route_agent_returns_prompt_and_only_authorized_tools(tmp_path: Path) -> None:
    source_root, database_url, chroma_path = _build_catalog(tmp_path)

    result = route_agent(
        query="React frontend accessibility web performance",
        threshold=0.2,
        database_url=database_url,
        chroma_path=chroma_path,
        source_root=source_root,
    )

    assert result.matched is True
    assert result.agent_id == "engineering-frontend-developer"
    assert result.system_prompt is not None
    assert "Frontend Developer" in result.system_prompt
    assert [tool.tool_id for tool in result.tools] == ["repo-search"]
    assert all(tool.tool_id != "finance-ledger" for tool in result.tools)

    no_match = route_agent(
        query="quantum botany telescope",
        threshold=0.99,
        database_url=database_url,
        chroma_path=chroma_path,
        source_root=source_root,
    )
    assert no_match.matched is False
    assert no_match.agent_id is None
    assert no_match.tools == []


@pytest.mark.asyncio
async def test_fastmcp_route_agent_tool_runs_in_memory() -> None:
    captured: dict[str, object] = {}

    def fake_router(query: str, threshold: float) -> RouteAgentResult:
        captured.update(query=query, threshold=threshold)
        return RouteAgentResult(
            matched=True,
            score=0.9,
            agent_id="test-agent",
            name="Test Agent",
            macro_domain="testing",
            system_prompt="test prompt",
            tools=[],
            reason="matched",
        )

    server = create_mcp(router=fake_router)
    async with Client(server) as client:
        response = await client.call_tool(
            "route_agent",
            {"query": "test routing", "threshold": 0.4},
        )

    assert response.data["agent_id"] == "test-agent"
    assert captured == {"query": "test routing", "threshold": 0.4}
