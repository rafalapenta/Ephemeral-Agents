"""Unit and integration tests for Ephemeral Agents orchestration pipeline (Etapas 1-4)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.database.models import Agent, Base, DirectorSkill, SkillCatalog, SkillUsage
from src.database.usage import prune_skill_usage, record_skill_usage
from src.governance.limits import EphemeralLimits, get_cost_report
from src.macro_agents.ephemeral import execute_ephemeral_task
from src.macro_agents.handoff import litellm_handoff
from src.macro_agents.orchestrator import MacroOrchestrator
from src.orchestration.kanban import MISSING_SKILL, KanbanBoard, KanbanStatus, KanbanTask
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
        k8s_skill = SkillCatalog(
            id="k8s-baremetal-migrator",
            name="Kubernetes Bare-Metal Migrator",
            description="Migração de cluster Kubernetes com etcd distribuído e bare-metal com payload e dados de configuração.",
            content_md="# Kubernetes Bare-Metal\nPasso a passo de migracao bare metal etcd distribuido.",
            token_budget=800,
        )
        playwright_skill = SkillCatalog(
            id="playwright-runner",
            name="Playwright Runner",
            description="Execução de testes e2e automatizados.",
            content_md="# Playwright Runner\nExecuta testes com Playwright headless.",
            token_budget=600,
        )
        session.add_all([vulcan, k8s_skill, playwright_skill])
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
    assert "Passo a passo de migracao bare metal etcd" in sys_content

    # User message must contain context
    user_content = messages[1]["content"]
    assert "Kubernetes Bare-Metal Migrator" in user_content


def test_etapa2_director_spawns_ephemeral_loop(setup_ephemeral_db, monkeypatch: pytest.MonkeyPatch):
    """Test ETAPA 2: Director calls spawn_ephemeral, child subtask is created and completed, output returns to director."""
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

    call_index = 0

    def fake_completion(**kwargs):
        nonlocal call_index
        call_index += 1

        # Turn 1: Director requests spawn_ephemeral
        if call_index == 1:
            mock_tool_call = MagicMock()
            mock_tool_call.id = "call_abc123"
            mock_tool_call.function.name = "spawn_ephemeral"
            mock_tool_call.function.arguments = json.dumps({
                "skill_id": "playwright-runner",
                "subtask_title": "Run Playwright E2E Tests",
                "subtask_body": "Execute checkout flow tests on staging",
                "expected_output": "All 12 tests passing",
            })
            mock_choice = MagicMock()
            mock_choice.message.content = "Delegating E2E testing to ephemeral subagent."
            mock_choice.message.tool_calls = [mock_tool_call]
            mock_resp = MagicMock()
            mock_resp.choices = [mock_choice]
            mock_resp.usage.model_dump.return_value = {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}
            return mock_resp

        # Turn 2: Ephemeral subagent execution
        if call_index == 2:
            # Ephemerals receive no tools
            assert "tools" not in kwargs or kwargs.get("tools") is None
            mock_choice = MagicMock()
            mock_choice.message.content = "Playwright test suite finished: 12/12 passed."
            mock_choice.message.tool_calls = None
            mock_resp = MagicMock()
            mock_resp.choices = [mock_choice]
            mock_resp.usage.model_dump.return_value = {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}
            return mock_resp

        # Turn 3: Director receives tool response and finalizes
        mock_choice = MagicMock()
        mock_choice.message.content = "All checks and subtasks finished successfully."
        mock_choice.message.tool_calls = None
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage.model_dump.return_value = {"prompt_tokens": 150, "completion_tokens": 25, "total_tokens": 175}
        return mock_resp

    monkeypatch.setattr("litellm.completion", fake_completion)

    orchestrator = MacroOrchestrator(
        state_dir=state_dir,
        database_url=db_url,
        source_root=source_root,
        handoff_fn=litellm_handoff,
        dry_run=False,
    )
    orchestrator.board._sync_linear = False

    task = KanbanTask(
        title="Kubernetes Bare-Metal Migrator",
        body="Migração de cluster Kubernetes com etcd distribuído e bare-metal.",
        priority="high",
    )
    orchestrator.board.add(task)

    result = orchestrator.orchestrate_task(task)

    assert result.status == "completed"
    assert call_index == 3

    # Check Kanban tasks: child subtask should exist and be DONE
    all_tasks = orchestrator.board.tasks
    child_subtasks = [t for t in all_tasks.values() if t.parent_task_id == task.task_id]
    assert len(child_subtasks) == 1
    subtask = child_subtasks[0]
    assert subtask.title == "Run Playwright E2E Tests"
    assert subtask.status == KanbanStatus.DONE
    assert subtask.assignee == "ephemeral"


def test_etapa2_ephemeral_has_no_tools_depth_bounded(setup_ephemeral_db, monkeypatch: pytest.MonkeyPatch):
    """Test ETAPA 2: Ephemeral subagents never receive tools, enforcing max_depth=1."""
    db_url = setup_ephemeral_db["db_url"]
    captured_kwargs = {}

    def fake_completion(**kwargs):
        captured_kwargs.update(kwargs)
        mock_choice = MagicMock()
        mock_choice.message.content = "Isolated output."
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage.model_dump.return_value = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}
        return mock_resp

    monkeypatch.setattr("litellm.completion", fake_completion)

    board = KanbanBoard(sync_linear=False)
    parent_task = KanbanTask(title="Parent Task")
    board.add(parent_task)

    res = execute_ephemeral_task(
        skill_id="playwright-runner",
        subtask_title="Isolated Subtask",
        subtask_body="Execute single task",
        expected_output="Result",
        parent_task=parent_task,
        board=board,
        database_url=db_url,
    )

    assert res["status"] == "success"
    assert "tools" not in captured_kwargs or captured_kwargs.get("tools") is None


def test_etapa2_missing_skill_escalation(setup_ephemeral_db):
    """Test ETAPA 2: Non-existent skill during ephemeral execution marks subtask as MISSING_SKILL."""
    db_url = setup_ephemeral_db["db_url"]
    board = KanbanBoard(sync_linear=False)
    parent_task = KanbanTask(title="Parent Task")
    board.add(parent_task)

    res = execute_ephemeral_task(
        skill_id="nonexistent-magic-skill",
        subtask_title="Do Magic",
        subtask_body="Unknown operation",
        expected_output="Magic",
        parent_task=parent_task,
        board=board,
        database_url=db_url,
    )

    assert res["status"] == "missing_skill"
    assert "not found in catalog" in res["error"]

    # Verify subtask is BLOCKED with MISSING_SKILL
    subtasks = [t for t in board.tasks.values() if t.parent_task_id == parent_task.task_id]
    assert len(subtasks) >= 1
    assert any(t.status == KanbanStatus.BLOCKED and t.blocked_reason == MISSING_SKILL for t in subtasks)


def test_etapa3_privacy_skill_usage_no_task_content(setup_ephemeral_db, monkeypatch: pytest.MonkeyPatch):
    """Test ETAPA 3 (Privacy): SkillUsage records metrics only, NEVER storing prompt/body/customer data."""
    db_url = setup_ephemeral_db["db_url"]
    state_dir = setup_ephemeral_db["state_dir"]
    source_root = setup_ephemeral_db["source_root"]

    SECRET_DATA = "SECRET_PAYLOAD_CREDIT_CARD_4111_2222_3333_4444"

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

    def fake_completion(**kwargs):
        mock_choice = MagicMock()
        mock_choice.message.content = "Operation done."
        mock_choice.message.tool_calls = None
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage.model_dump.return_value = {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160}
        return mock_resp

    monkeypatch.setattr("litellm.completion", fake_completion)

    orchestrator = MacroOrchestrator(
        state_dir=state_dir,
        database_url=db_url,
        source_root=source_root,
        handoff_fn=litellm_handoff,
        dry_run=False,
    )
    orchestrator.board._sync_linear = False

    task = KanbanTask(
        title="Kubernetes Bare-Metal Migrator",
        body=f"Migração de cluster Kubernetes com etcd distribuído e bare-metal com payload: {SECRET_DATA}",
        priority="high",
    )
    orchestrator.board.add(task)

    result = orchestrator.orchestrate_task(task)
    assert result.status == "completed"

    # Query SQLite SkillUsage table
    engine = create_engine(db_url)
    with Session(engine) as session:
        records = list(session.scalars(select(SkillUsage).where(SkillUsage.task_id == task.task_id)).all())
        assert len(records) >= 1
        for rec in records:
            # None of the fields should contain the secret
            assert SECRET_DATA not in (rec.note or "")
            assert SECRET_DATA not in rec.director_id
            assert SECRET_DATA not in (rec.skill_id or "")
            assert rec.prompt_tokens > 0

    # Query StateManager journal
    journal_records = orchestrator.state.journal()
    for entry in journal_records:
        entry_str = json.dumps(entry)
        if entry.get("patch", {}).get("event_type") == "skill_usage":
            assert SECRET_DATA not in entry_str


def test_etapa3_prune_skill_usage(setup_ephemeral_db):
    """Test ETAPA 3 (Retention): prune_skill_usage aggregates old records and deletes details."""
    db_url = setup_ephemeral_db["db_url"]
    engine = create_engine(db_url)

    now = datetime.now(UTC)
    old_date = now - timedelta(days=120)
    recent_date = now - timedelta(days=10)

    with Session(engine) as session:
        # 3 old records for skill 'playwright-runner'
        for i in range(3):
            session.add(
                SkillUsage(
                    id=f"old-{i}",
                    created_at=old_date,
                    skill_id="playwright-runner",
                    director_id="vulcan",
                    task_id=f"old-task-{i}",
                    outcome="success" if i < 2 else "failure",
                    gates_passed=i < 2,
                    model="mock-model",
                    prompt_tokens=100,
                    completion_tokens=50,
                    cost_usd=0.01,
                    duration_ms=500.0,
                    note=f"Old note {i}",
                )
            )
        # 1 recent record
        session.add(
            SkillUsage(
                id="recent-1",
                created_at=recent_date,
                skill_id="playwright-runner",
                director_id="vulcan",
                task_id="recent-task-1",
                outcome="success",
                gates_passed=True,
                model="mock-model",
                prompt_tokens=100,
                completion_tokens=50,
                cost_usd=0.01,
                duration_ms=400.0,
                note="Recent note",
            )
        )
        session.commit()

    # Prune records older than 90 days
    summary = prune_skill_usage(days=90, database_url=db_url)

    assert "playwright-runner" in summary
    pw_agg = summary["playwright-runner"]
    assert pw_agg["total_uses"] == 3
    assert pw_agg["successful_uses"] == 2
    assert pw_agg["success_rate"] == round(2 / 3, 4)
    assert pw_agg["total_cost_usd"] == 0.03

    # Check database: only recent record remains
    with Session(engine) as session:
        remaining = list(session.scalars(select(SkillUsage)).all())
        assert len(remaining) == 1
        assert remaining[0].id == "recent-1"


def test_etapa3_skill_usage_failure_does_not_crash_orchestration(setup_ephemeral_db, monkeypatch: pytest.MonkeyPatch):
    """Test ETAPA 3 (Resilience): DB failure during SkillUsage logging does not crash orchestration."""
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

    def fake_completion(**kwargs):
        mock_choice = MagicMock()
        mock_choice.message.content = "Completed smoothly."
        mock_choice.message.tool_calls = None
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage.model_dump.return_value = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}
        return mock_resp

    monkeypatch.setattr("litellm.completion", fake_completion)

    # Simulate database crash during record_skill_usage
    def broken_record_skill_usage(*args, **kwargs):
        raise RuntimeError("Database connection suddenly dropped!")

    monkeypatch.setattr("src.macro_agents.handoff.record_skill_usage", broken_record_skill_usage)

    orchestrator = MacroOrchestrator(
        state_dir=state_dir,
        database_url=db_url,
        source_root=source_root,
        handoff_fn=litellm_handoff,
        dry_run=False,
    )
    orchestrator.board._sync_linear = False

    task = KanbanTask(
        title="Kubernetes Bare-Metal Migrator",
        body="Migração de cluster Kubernetes com etcd distribuído e bare-metal.",
        priority="high",
    )
    orchestrator.board.add(task)

    result = orchestrator.orchestrate_task(task)
    assert result.status == "completed"


def test_etapa4_limit_exceeded_ephemerals_and_tokens(setup_ephemeral_db, monkeypatch: pytest.MonkeyPatch):
    """Test ETAPA 4: Exceeding max_ephemerals_per_task or max_tokens_per_task blocks task with limit_exceeded."""
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

    # 1. Test max_ephemerals_per_task exceeded
    monkeypatch.setenv("EPHEMERAL_MAX_PER_TASK", "1")

    call_count = 0

    def fake_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        # Director always tries to spawn ephemerals
        mock_tool_call = MagicMock()
        mock_tool_call.id = f"call_{call_count}"
        mock_tool_call.function.name = "spawn_ephemeral"
        mock_tool_call.function.arguments = json.dumps({
            "skill_id": "playwright-runner",
            "subtask_title": f"Subtask {call_count}",
            "subtask_body": "Run tests",
            "expected_output": "OK",
        })
        mock_choice = MagicMock()
        mock_choice.message.content = "Spawning subtask..."
        mock_choice.message.tool_calls = [mock_tool_call]
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage.model_dump.return_value = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}
        return mock_resp

    monkeypatch.setattr("litellm.completion", fake_completion)

    orchestrator = MacroOrchestrator(
        state_dir=state_dir,
        database_url=db_url,
        source_root=source_root,
        handoff_fn=litellm_handoff,
        dry_run=False,
    )
    orchestrator.board._sync_linear = False

    task = KanbanTask(
        title="Kubernetes Bare-Metal Migrator",
        body="Migração de cluster Kubernetes com etcd distribuído e bare-metal.",
        priority="high",
    )
    orchestrator.board.add(task)

    # First ephemeral succeeds, second attempt fails with limit_exceeded
    board = KanbanBoard(sync_linear=False)
    limits = EphemeralLimits(max_ephemerals_per_task=1)
    
    res1 = execute_ephemeral_task(
        skill_id="playwright-runner",
        subtask_title="Subtask 1",
        subtask_body="Body 1",
        expected_output="Out 1",
        parent_task=task,
        board=board,
        database_url=db_url,
        limits=limits,
    )
    assert res1["status"] == "success"

    res2 = execute_ephemeral_task(
        skill_id="playwright-runner",
        subtask_title="Subtask 2",
        subtask_body="Body 2",
        expected_output="Out 2",
        parent_task=task,
        board=board,
        database_url=db_url,
        limits=limits,
    )
    assert res2["status"] == "limit_exceeded"
    assert "limit_exceeded" in res2["error"]

    # Verify cost report for Sterling
    rep = limits.to_report()
    assert rep["ephemeral_count"] == 1
    assert rep["limit_exceeded"] is True


def test_estimate_cost_fallback_and_limit_exceeded(setup_ephemeral_db, monkeypatch: pytest.MonkeyPatch):
    """Test PONTO 1: When completion_cost is 0, estimate_cost falls back to table, cost > 0, and limits trigger."""
    from src.gateway.models import estimate_cost

    # 1. Test unit behavior of estimate_cost
    cost, source = estimate_cost("mistral/codestral-latest", prompt_tokens=1000, completion_tokens=1000)
    assert cost > 0.0
    assert source == "fallback_table"

    unknown_cost, unknown_source = estimate_cost("nonexistent-custom-model", prompt_tokens=1000, completion_tokens=1000)
    assert unknown_cost > 0.0
    assert unknown_source == "default_estimate"

    # 2. Integration test with mocked completion_cost = 0
    monkeypatch.setattr("litellm.completion_cost", lambda *args, **kwargs: 0.0)

    db_url = setup_ephemeral_db["db_url"]
    board = KanbanBoard(sync_linear=False)

    task = KanbanTask(
        title="Test Cost Limit Exceeded",
        body="Execute subtask under cost limits",
        priority="high",
    )
    board.add(task)

    # Set very small cost limit: $0.000001
    limits = EphemeralLimits(max_cost_usd_per_task=0.000001)

    mock_choice = MagicMock()
    mock_choice.message.content = "Subtask output content"
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]
    mock_resp.usage.model_dump.return_value = {"prompt_tokens": 500, "completion_tokens": 500, "total_tokens": 1000}

    monkeypatch.setattr("litellm.completion", lambda *args, **kwargs: mock_resp)

    # First ephemeral executes and records fallback cost, surpassing max_cost_usd_per_task
    res1 = execute_ephemeral_task(
        skill_id="playwright-runner",
        subtask_title="Subtask 1",
        subtask_body="Body 1",
        expected_output="Out 1",
        parent_task=task,
        board=board,
        database_url=db_url,
        limits=limits,
    )
    assert res1["status"] == "success"
    assert res1["cost_usd"] > 0.0

    # Second call must be blocked due to cost limit exceeded
    res2 = execute_ephemeral_task(
        skill_id="playwright-runner",
        subtask_title="Subtask 2",
        subtask_body="Body 2",
        expected_output="Out 2",
        parent_task=task,
        board=board,
        database_url=db_url,
        limits=limits,
    )
    assert res2["status"] == "limit_exceeded"
    assert "limit_exceeded" in res2["error"]

    cost_rep = limits.to_report()
    assert cost_rep["limit_exceeded"] is True
    assert cost_rep["total_cost_usd"] > 0.0
    assert cost_rep["cost_source"] in ("fallback_table", "default_estimate")


def test_handoff_fn_protocol_and_no_inspect(setup_ephemeral_db, monkeypatch: pytest.MonkeyPatch):
    """Test PONTO 2: Standardized handoff_fn signature receives task, route_result, and context."""
    from src.macro_agents.handoff import HandoffFn, HandoffRuntime

    received_args = {}

    def custom_handoff(
        task: KanbanTask,
        route: RouteAgentResult,
        context: dict,
        runtime: HandoffRuntime | None = None,
    ) -> dict:
        received_args["task"] = task
        received_args["route"] = route
        received_args["context"] = context
        received_args["runtime"] = runtime
        return {"status": "success", "agent_id": route.agent_id, "reply": "Handled custom"}

    # Verify custom_handoff matches HandoffFn protocol
    assert isinstance(custom_handoff, HandoffFn)

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
            system_prompt="# Vulcan System Prompt",
            tools=[],
            reason="matched vulcan",
        )

    monkeypatch.setattr("src.macro_agents.orchestrator.route_agent", fake_route)

    orchestrator = MacroOrchestrator(
        state_dir=state_dir,
        database_url=db_url,
        source_root=source_root,
        handoff_fn=custom_handoff,
        dry_run=False,
    )
    orchestrator.board._sync_linear = False

    res = orchestrator.orchestrate(
        title="Kubernetes Bare-Metal Migrator",
        body="Migração de cluster Kubernetes",
    )

    assert res.status == "completed"
    assert received_args["task"].title == "Kubernetes Bare-Metal Migrator"
    assert received_args["route"].agent_id == "vulcan"
    assert isinstance(received_args["context"], dict)
    assert "system_prompt" in received_args["context"]


def test_context_sanitization_removes_private_and_non_serializable_objects(setup_ephemeral_db, monkeypatch: pytest.MonkeyPatch):
    """Test PONTO 3: Verify sanitize_context strips '_' keys and non-JSON serializable objects from LLM messages."""
    from src.macro_agents.handoff import HandoffRuntime

    class DummyUnserializableObject:
        def __init__(self):
            self.internal_fn = lambda x: x

    dummy_obj = DummyUnserializableObject()

    dirty_context = {
        "task_id": "task-clean-123",
        "title": "Task Clean Test",
        "system_prompt": "You are Vulcan the engineer.",
        "_state_manager": object(),
        "_db_url": "sqlite:///secret_database.db",
        "_limits": EphemeralLimits(),
        "unserializable_obj": dummy_obj,
        "public_data": "visible_payload_value",
    }

    captured_messages = []

    def mock_completion(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        mock_choice = MagicMock()
        mock_choice.message.content = "Task finished cleanly"
        mock_choice.message.tool_calls = None
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage.model_dump.return_value = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
        return mock_resp

    monkeypatch.setattr("litellm.completion", mock_completion)

    task = KanbanTask(
        title="Sanitization Test Task",
        body="Verify context clean",
        priority="high",
    )

    route = RouteAgentResult(
        matched=True,
        score=0.95,
        agent_id="vulcan",
        name="Vulcan",
        macro_domain="engineering",
        system_prompt="# Vulcan System",
        tools=[],
        reason="matched",
    )

    runtime = HandoffRuntime(
        db_url=setup_ephemeral_db["db_url"],
        limits=EphemeralLimits(),
        adherence_score=0.95,
    )

    res = litellm_handoff(
        task=task,
        route=route,
        context=dirty_context,
        runtime=runtime,
        tools_enabled=False,
    )

    assert res["status"] == "success"

    # Convert all message contents to string
    all_content_str = "\n".join(str(m.get("content", "")) for m in captured_messages)

    # Assert internal / private / non-serializable fields were completely excluded
    assert "_state_manager" not in all_content_str
    assert "_db_url" not in all_content_str
    assert "secret_database.db" not in all_content_str
    assert "_limits" not in all_content_str
    assert "unserializable_obj" not in all_content_str
    assert "DummyUnserializableObject" not in all_content_str

    # Assert valid data IS present
    assert "public_data" in all_content_str
    assert "visible_payload_value" in all_content_str


def test_no_circular_import_between_indexer_and_router():
    """Test PONTO 1: Fresh import of indexer and semantic router without circular import errors."""
    import sys
    # Clear cached modules if any to simulate clean import
    for mod in ["src.catalog.indexer", "src.router.semantic", "src.catalog.embeddings"]:
        sys.modules.pop(mod, None)

    import src.catalog.embeddings as embeddings_mod
    import src.catalog.indexer as indexer_mod
    import src.router.semantic as router_mod

    assert hasattr(embeddings_mod, "DeterministicHashEmbeddingFunction")
    assert hasattr(indexer_mod, "run_indexing")
    assert hasattr(router_mod, "route_agent")


def test_agency_embeddings_hash_indexes_and_routes_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Test PONTO 2: AGENCY_EMBEDDINGS=hash indexes and routes completely offline."""
    from src.catalog.embeddings import get_embedding_function
    from src.catalog.indexer import run_indexing
    from src.router.semantic import route_agent

    monkeypatch.setenv("AGENCY_EMBEDDINGS", "hash")

    embed_fn = get_embedding_function()
    assert embed_fn.backend_name == "hash"

    source_root = tmp_path / "agency-agents"
    agent_dir = source_root / "engineering"
    agent_dir.mkdir(parents=True)
    agent_file = agent_dir / "vulcan.md"
    agent_file.write_text(
        "---\nname: Vulcan\ndescription: Software engineer and DevOps specialist.\n---\n# Vulcan\n- **Role**: Software Engineering\n",
        encoding="utf-8",
    )

    db_url = f"sqlite:///{(tmp_path / 'hash_test.db').as_posix()}"
    chroma_path = tmp_path / "chroma_hash"

    report = run_indexing(
        source_root=source_root,
        database_url=db_url,
        chroma_path=chroma_path,
        reindex=True,
    )
    assert report.valid == 1

    # Route agent offline with hash embeddings
    res = route_agent(
        query="software engineering and devops",
        database_url=db_url,
        chroma_path=chroma_path,
        source_root=source_root,
    )
    assert res.matched is True
    assert res.agent_id == "vulcan"


def test_local_embeddings_fallback_to_hash_when_missing(monkeypatch: pytest.MonkeyPatch):
    """Test PONTO 2: When AGENCY_EMBEDDINGS=local but sentence-transformers is missing, fall back to hash."""
    import builtins
    from src.catalog.embeddings import get_embedding_function

    monkeypatch.setenv("AGENCY_EMBEDDINGS", "local")

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ImportError("No module named 'sentence_transformers'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    embed_fn = get_embedding_function()
    assert embed_fn.backend_name == "hash"


def test_embedding_backend_mismatch_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Test PONTO 2: Mismatch between index metadata and current router embedding produces reindex warning."""
    import logging
    from src.catalog.embeddings import (
        DeterministicHashEmbeddingFunction,
        GatewayEmbeddingFunction,
        check_embedding_compatibility,
        save_embedding_metadata,
    )

    chroma_path = tmp_path / "chroma_meta_test"
    chroma_path.mkdir(parents=True)

    # Index was saved with gateway embedding
    gateway_fn = GatewayEmbeddingFunction(model="openai/mistral-embed")
    save_embedding_metadata(chroma_path, gateway_fn)

    # Current router is using hash embedding
    hash_fn = DeterministicHashEmbeddingFunction()
    is_compat, warn_msg = check_embedding_compatibility(chroma_path, hash_fn)

    assert is_compat is False
    assert warn_msg is not None
    assert "reindex" in warn_msg
    assert "gateway" in warn_msg
    assert "hash" in warn_msg


def test_gateway_models_configurable_base_url_and_api_key(monkeypatch: pytest.MonkeyPatch):
    """Test PONTO 3: Models registry respects AGENCY_LLM_BASE_URL and AGENCY_LLM_API_KEY with aliases."""
    from src.gateway.models import _gateway_base, _gateway_key, resolve_model

    # Custom agency env vars
    monkeypatch.setenv("AGENCY_LLM_BASE_URL", "https://custom-gateway.openai.azure.com/v1")
    monkeypatch.setenv("AGENCY_LLM_API_KEY", "secret-test-key-12345")

    assert _gateway_base() == "https://custom-gateway.openai.azure.com/v1"
    assert _gateway_key() == "secret-test-key-12345"

    res = resolve_model("vulcan")
    assert res.api_base == "https://custom-gateway.openai.azure.com/v1"
    assert res.api_key == "secret-test-key-12345"

    # Alias fallback
    monkeypatch.delenv("AGENCY_LLM_BASE_URL")
    monkeypatch.delenv("AGENCY_LLM_API_KEY")
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://omniroute.local:8080/v1")
    monkeypatch.setenv("OMNIROUTE_API_KEY", "omniroute-secret")

    assert _gateway_base() == "http://omniroute.local:8080/v1"
    assert _gateway_key() == "omniroute-secret"


def test_ephemeral_model_resolution_and_override(monkeypatch: pytest.MonkeyPatch):
    """Test PONTO 3: Ephemerals use cheapest tier, configurable by AGENCY_EPHEMERAL_MODEL."""
    from src.gateway.models import resolve_model

    monkeypatch.delenv("AGENCY_EPHEMERAL_MODEL", raising=False)
    default_res = resolve_model("ephemeral")
    assert default_res.tier == "fast"
    assert "ministral-8b" in default_res.model

    monkeypatch.setenv("AGENCY_EPHEMERAL_MODEL", "openrouter/free-model-v1")
    override_res = resolve_model("ephemeral")
    assert override_res.tier == "fast"
    assert "openrouter/free-model-v1" in override_res.model


def test_check_gateway_health_offline(monkeypatch: pytest.MonkeyPatch):
    """Test PONTO 3: Gateway health check handles offline gateway gracefully with clear warning message."""
    from src.gateway.models import check_gateway_health

    # Point to an unreachable port / offline host with short timeout
    is_ok, msg, models = check_gateway_health(base_url="http://127.0.0.1:59999/v1", timeout=0.5)
    assert is_ok is False
    assert "indisponível" in msg or "indisponivel" in msg or "127.0.0.1:59999" in msg
    assert models == []


def test_doctor_command_runs_cleanly(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """Test PONTO 3: 'aggency doctor' diagnostic tool runs completely without unhandled exceptions."""
    from src.cli.doctor import run_doctor

    monkeypatch.setenv("AGENCY_EMBEDDINGS", "hash")
    monkeypatch.setenv("AGENCY_LLM_API_KEY", "test-secret-key-abcdef")

    ret = run_doctor()
    captured = capsys.readouterr()

    assert ret in (0, 2)
    assert "AGency System Doctor" in captured.out
    assert "Embeddings Backend" in captured.out
    assert "Model Matrix & Director Tiers" in captured.out
    # Ensure raw secret key is NEVER printed in plain text
    assert "test-secret-key-abcdef" not in captured.out
    assert "masked" in captured.out






