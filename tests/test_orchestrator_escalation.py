"""End-to-end integration tests for MacroOrchestrator Fast-Path vs Slow-Path skill escalation."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.database.models import Agent, Base, DirectorSkill, SkillCatalog
from src.macro_agents.orchestrator import MacroOrchestrator
from src.orchestration.context import inject_ephemeral_skill
from src.orchestration.kanban import MISSING_SKILL, KanbanStatus
from src.router.semantic import RouteAgentResult


@pytest.fixture
def test_env(tmp_path: Path):
    """Set up an isolated database and state directory for escalation tests."""
    db_path = tmp_path / "test_escalation.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    state_dir = tmp_path / "state_data"
    source_root = tmp_path / "bots_config"
    vulcan_dir = source_root / "vulcan"
    vulcan_dir.mkdir(parents=True)
    soul_file = vulcan_dir / "SOUL.md"
    soul_file.write_text(
        "---\nname: vulcan\nrole: CTO\n---\n# Vulcan CTO\n- **Role**: Software Engineering and DevOps\n",
        encoding="utf-8",
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
            trigger_hooks=["software engineering", "devops", "testes unitarios", "arquitetura", "playwright"],
            is_active=True,
        )
        lyra = Agent(
            agent_id="lyra",
            macro_domain="research",
            name="Lyra",
            system_prompt_path="lyra/SOUL.md",
            trigger_hooks=["research", "skills discovery"],
            is_active=True,
        )
        arch_skill = SkillCatalog(
            id="clean-architecture",
            name="Clean Architecture Validator",
            description="Estruturação por camadas e desacoplamento de módulos.",
            content_md="# Clean Architecture\nValidação de dependências.",
            token_budget=600,
        )
        session.add_all([vulcan, lyra, arch_skill])
        session.flush()

        session.add(
            DirectorSkill(
                director_id="vulcan",
                skill_id="clean-architecture",
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


def test_end_to_end_skill_escalation_and_unblocking(test_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test full cycle: Task blocked due to missing skill -> Lyra subtask -> on_skill_discovered -> unblock to READY -> Fast Path."""
    db_url = test_env["db_url"]
    state_dir = test_env["state_dir"]
    source_root = test_env["source_root"]

    # Mock route_agent to route to Vulcan deterministically
    def fake_route(query, **kwargs):
        return RouteAgentResult(
            matched=True,
            score=0.95,
            agent_id="vulcan",
            name="Vulcan",
            macro_domain="engineering",
            system_prompt="# Vulcan System Prompt",
            tools=[],
            reason="matched vulcan",
        )

    monkeypatch.setattr("src.macro_agents.orchestrator.route_agent", fake_route)

    orchestrator = MacroOrchestrator(
        state_dir=state_dir,
        database_url=db_url,
        source_root=source_root,
        handoff_fn=None,  # simulated handoff
    )

    task_title = "Automação de testes end-to-end com Playwright"
    task_body = "Execução autônoma de testes de interface com Playwright."

    # 1. Execute task with missing skill (Vulcan only has clean-architecture)
    res = orchestrator.orchestrate(
        title=task_title,
        body=task_body,
        query="Playwright e2e testing",
    )

    assert res.status == "blocked"
    assert res.error == MISSING_SKILL

    # Check Kanban board state
    main_task = orchestrator.board.get(res.task_id)
    assert main_task is not None
    assert main_task.status == KanbanStatus.BLOCKED
    assert main_task.blocked_reason == MISSING_SKILL

    # Verify that a sub-task for Lyra was created
    assert res.subtask_id is not None
    lyra_subtask = orchestrator.board.get(res.subtask_id)
    assert lyra_subtask is not None
    assert lyra_subtask.assignee == "lyra"
    assert lyra_subtask.macro_domain == "research"
    assert lyra_subtask.parent_task_id == main_task.task_id
    assert "Pesquisar no skills.sh especificação homologada para: Automação de testes end-to-end com Playwright" in lyra_subtask.title

    # 2. Simulate Lyra discovering the skill from skills.sh
    discovered_skill_md = """---
name: playwright-runner
description: Execução autônoma de testes de interface com Playwright.
---
# Playwright Runner
Automação e execução autônoma de testes de interface com Playwright.
"""

    discovered_skill = orchestrator.on_skill_discovered(
        director_id="vulcan",
        skill_md=discovered_skill_md,
        task_id=main_task.task_id,
        skill_id="playwright-runner",
        token_budget=600,
    )
    assert discovered_skill.id == "playwright-runner"

    # Verify skill is persisted in the database and linked to Vulcan
    engine = create_engine(db_url)
    with Session(engine) as session:
        stored_skill = session.scalar(select(SkillCatalog).where(SkillCatalog.id == "playwright-runner"))
        assert stored_skill is not None
        assert stored_skill.source == "skills.sh"

        link = session.scalar(
            select(DirectorSkill).where(
                DirectorSkill.director_id == "vulcan",
                DirectorSkill.skill_id == "playwright-runner",
            )
        )
        assert link is not None

    # Verify original task is unblocked and moved to READY
    assert main_task.status == KanbanStatus.READY
    assert main_task.blocked_reason is None

    # 3. Resume / re-orchestrate the unblocked task (Fast Path)
    fast_res = orchestrator.orchestrate_task(main_task, query="Playwright e2e testing")
    assert fast_res.status == "completed"
    assert fast_res.skill_match is not None
    assert fast_res.skill_match.matched is True
    assert fast_res.skill_match.skill.id == "playwright-runner"
    assert main_task.status == KanbanStatus.DONE


def test_fast_path_when_adherence_is_high(test_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test Fast-Path execution when skill adherence is >= 0.72 from the start."""
    db_url = test_env["db_url"]
    state_dir = test_env["state_dir"]
    source_root = test_env["source_root"]

    def fake_route(query, **kwargs):
        return RouteAgentResult(
            matched=True,
            score=0.90,
            agent_id="vulcan",
            name="Vulcan",
            macro_domain="engineering",
            system_prompt="# Vulcan System Prompt",
            tools=[],
            reason="matched vulcan",
        )

    monkeypatch.setattr("src.macro_agents.orchestrator.route_agent", fake_route)

    orchestrator = MacroOrchestrator(
        state_dir=state_dir,
        database_url=db_url,
        source_root=source_root,
        handoff_fn=None,
    )

    # Prompt directly matching clean-architecture (which Vulcan already has)
    res = orchestrator.orchestrate(
        title="Validar regras de Clean Architecture",
        body="Estruturação por camadas e desacoplamento de módulos.",
        query="Clean Architecture desacoplamento de módulos",
    )

    assert res.status == "completed"
    assert res.skill_match is not None
    assert res.skill_match.matched is True
    assert res.skill_match.skill.id == "clean-architecture"
    assert res.subtask_id is None

    task = orchestrator.board.get(res.task_id)
    assert task is not None
    assert task.status == KanbanStatus.DONE


def test_inject_ephemeral_skill_budget_truncation() -> None:
    """Verify that inject_ephemeral_skill limits markdown according to token_budget."""
    skill = SkillCatalog(
        id="massive-skill",
        name="Massive Skill",
        description="A skill with a very long manual.",
        content_md="A" * 5000,
        token_budget=100,  # 100 tokens ~ 400 chars
    )

    base_payload = {
        "task_id": "123",
        "system_prompt": "You are Vulcan.",
    }

    injected = inject_ephemeral_skill(base_payload, skill)
    assert "ephemeral_skill" in injected
    assert injected["ephemeral_skill"]["id"] == "massive-skill"
    assert len(injected["ephemeral_skill"]["content_md"]) <= 450
    assert "[truncated to fit token_budget]" in injected["ephemeral_skill"]["content_md"]
    assert "## Ephemeral Skill: Massive Skill" in injected["system_prompt"]
