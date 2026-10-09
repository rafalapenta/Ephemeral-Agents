"""Unit and integration tests for Ephemeral Agents orchestration pipeline (Etapas 1-4)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.database.models import Agent, Base, DirectorSkill, SkillCatalog
from src.macro_agents.handoff import litellm_handoff
from src.macro_agents.orchestrator import MacroOrchestrator
from src.orchestration.kanban import KanbanStatus, KanbanTask
from src.router.semantic import RouteAgentResult


@pytest.fixture
def setup_ephemeral_db(tmp_path: Path):
    db_path = tmp_path / "ephemeral_test.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    state_dir = tmp_path / "state_data"
    source_root = tmp_path / "bots_config"
    vulcan_dir = source_root / "vulcan"
    vulcan_dir.mkdir(parents=True)
    (vulcan_dir / "SOUL.md").write_text(
        "---\nname: vulcan\nrole: CTO\n---\n# Vulcan CTO\n", encoding="utf-8"
    )

    engine = create_engine(db_url)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        vulcan = Agent(
            agent_id="vulcan",
            macro_domain="engineering",
            name="Vulcan",
            system_prompt_path="vulcan/SOUL.md",
            trigger_hooks=["software engineering", "k8s"],
            is_active=True,
        )
        skill = SkillCatalog(
            id="k8s-baremetal-migrator",
            name="Kubernetes Bare-Metal Migrator",
            description="Migração de cluster Kubernetes com etcd.",
            content_md="# Kubernetes Bare-Metal\nPasso a passo de migracao bare metal etcd.",
            token_budget=800,
        )
        session.add_all([vulcan, skill])
        session.flush()
        session.add(
            DirectorSkill(
                director_id="vulcan",
                skill_id="k8s-baremetal-migrator",
                is_core=True,
                load_priority=1,
            )
        )
        session.commit()

    return {
        "db_url": db_url,
        "state_dir": state_dir,
        "source_root": source_root,
    }


def test_etapa1_skill_injected_into_llm_messages(setup_ephemeral_db, monkeypatch: pytest.MonkeyPatch):
    """Test ETAPA 1: Verify that the chosen skill content reaches litellm.completion messages."""
    db_url = setup_ephemeral_db["db_url"]
    state_dir = setup_ephemeral_db["state_dir"]
    source_root = setup_ephemeral_db["source_root"]

    def fake_route(query, **kwargs):
        return RouteAgentResult(
            matched=True,
            score=0.95,
            agent_id="vulcan",
            name="Vulcan",
            macro_domain="engineering",
            system_prompt="# Base Vulcan System Prompt",
            tools=[],
            reason="matched vulcan",
        )

    monkeypatch.setattr("src.macro_agents.orchestrator.route_agent", fake_route)

    captured_kwargs = {}

    def fake_completion(**kwargs):
        captured_kwargs.update(kwargs)
        mock_choice = MagicMock()
        mock_choice.message.content = "Migration task completed successfully."
        mock_choice.message.tool_calls = None
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage.model_dump.return_value = {
            "prompt_tokens": 120,
            "completion_tokens": 45,
            "total_tokens": 165,
        }
        return mock_resp

    monkeypatch.setattr("litellm.completion", fake_completion)

    orchestrator = MacroOrchestrator(
        state_dir=state_dir,
        database_url=db_url,
        source_root=source_root,
        handoff_fn=litellm_handoff,
        dry_run=False,
    )
    # Disable Linear sync during tests
    orchestrator.board._sync_linear = False

    task = KanbanTask(
        title="Kubernetes Bare-Metal Migrator",
        body="Migração de cluster Kubernetes com etcd distribuído e bare-metal.",
        priority="high",
    )
    orchestrator.board.add(task)

    result = orchestrator.orchestrate_task(task)

    assert result.status == "completed"
    assert result.skill_match is not None
    assert result.skill_match.skill.id == "k8s-baremetal-migrator"

    # Verify messages passed to litellm.completion
    assert "messages" in captured_kwargs
    messages = captured_kwargs["messages"]
    assert len(messages) >= 2

    # System message must contain Ephemeral Skill content
    sys_content = messages[0]["content"]
    assert "Ephemeral Skill: Kubernetes Bare-Metal Migrator" in sys_content
    assert "Passo a passo de migracao bare metal etcd." in sys_content

    # User message must contain context
    user_content = messages[1]["content"]
    assert "Kubernetes Bare-Metal Migrator" in user_content
