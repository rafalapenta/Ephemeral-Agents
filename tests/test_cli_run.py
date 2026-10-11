"""Tests for `aggency run` CLI command and free-tier execution."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.cli.run import run_cli
from src.database.models import SkillUsage
from src.database.usage import ensure_tables


def test_dry_run_end_to_end_creates_card_usage_and_run_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """aggency run --dry-run completes end-to-end: creates card, records SkillUsage, and saves run file."""
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    runs_dir = tmp_path / "runs"

    task_text = "Execução autônoma de suíte de testes unitários com pytest runner"
    exit_code = run_cli(
        [task_text, "--dry-run", "--director", "cto"], runs_dir=runs_dir
    )
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "AGency Run — Resumo da Execução" in captured.out
    assert "SUCCESS" in captured.out
    assert "vulcan" in captured.out

    # Verify run markdown file was generated
    run_files = list(runs_dir.glob("*.md"))
    assert len(run_files) == 1
    run_file = run_files[0]
    content = run_file.read_text(encoding="utf-8")
    assert 'task: "' in content
    assert 'director: "vulcan"' in content
    assert 'outcome: "SUCCESS"' in content
    assert 'cost_source: "free_tier"' in content
    assert "card_id: " in content

    card_id = run_file.stem
    # Verify SkillUsage record exists in DB
    engine = create_engine("sqlite:///./agency_agents.db")
    ensure_tables(engine)
    with Session(engine) as session:
        usage = session.scalars(
            select(SkillUsage).where(SkillUsage.task_id == card_id)
        ).first()
        assert usage is not None
        assert usage.director_id == "vulcan"
        assert usage.outcome == "SUCCESS"


def test_dry_run_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """aggency run --dry-run --json outputs structured valid JSON with all required fields."""
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    runs_dir = tmp_path / "runs"
    exit_code = run_cli(
        [
            "Execução autônoma de suíte de testes unitários com pytest runner",
            "--dry-run",
            "--json",
            "--director",
            "cto",
        ],
        runs_dir=runs_dir,
    )
    assert exit_code == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "card_id" in data
    assert data["director"] in ["vulcan", "vesper", "sterling", "lyra", "aura", "atlas"]
    assert "director_score" in data
    assert "skill" in data
    assert "ephemerals_created" in data
    assert data["outcome"] == "SUCCESS"
    assert "result" in data
    assert "tokens" in data
    assert data["cost_usd"] == 0.0
    assert data["cost_source"] == "free_tier"
    assert "duration_seconds" in data
    assert "run_file" in data


def test_gateway_offline_shows_clear_message_and_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """Gateway offline returns clear troubleshooting message without stack trace and exit code 1."""
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with patch(
        "src.cli.run.check_gateway_health",
        return_value=(False, "Connection refused: [Errno 111]", 0.0),
    ):
        exit_code = run_cli(["Tarefa com gateway offline"])
        assert exit_code == 1

        captured = capsys.readouterr()
        assert "[ERRO] LLM Gateway indisponível" in captured.err
        assert "Como resolver:" in captured.err
        assert "Traceback (most recent call last)" not in captured.err


def test_max_cost_zero_blocks_paid_model(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """--max-cost 0 blocks paid model when AGENCY_ALLOW_PAID=0."""
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.setenv("AGENCY_DEFAULT_MODEL", "openai/gpt-4o")
    monkeypatch.setenv("AGENCY_ALLOW_PAID", "0")

    with patch("src.cli.run.check_gateway_health", return_value=(True, "OK", 0.05)):
        exit_code = run_cli(["Tarefa com modelo pago", "--max-cost", "0.00"])
        assert exit_code == 1

        captured = capsys.readouterr()
        assert "[ERRO] Modelo não-gratuito recusado" in captured.err
        assert "Como resolver:" in captured.err
        assert "Traceback (most recent call last)" not in captured.err


def test_rate_limit_429_retries_fallbacks_and_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """429 rate limit triggers retry, fallback candidate, and transitions card to ESCALATED if still failing."""
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.setenv("AGENCY_RETRY_BACKOFF_SECONDS", "0.01")
    monkeypatch.setenv("AGENCY_ALLOW_PAID", "1")
    runs_dir = tmp_path / "runs"

    # Mock gateway health check as healthy
    with patch("src.cli.run.check_gateway_health", return_value=(True, "OK", 0.05)):
        # Mock litellm completion to raise 429 RateLimitError
        rate_err = Exception("Rate limit exceeded: 429 Too Many Requests")
        with patch("src.macro_agents.handoff.litellm.completion", side_effect=rate_err):
            exit_code = run_cli(
                [
                    "Execução autônoma de suíte de testes unitários com pytest runner",
                    "--director",
                    "cto",
                ],
                runs_dir=runs_dir,
            )
            assert exit_code == 1

            captured = capsys.readouterr()
            assert "ESCALATED" in captured.out or "ESCALATED" in captured.err

            run_files = list(runs_dir.glob("*.md"))
            assert len(run_files) == 1
            content = run_files[0].read_text(encoding="utf-8")
            assert 'outcome: "ESCALATED"' in content


def test_marketing_task_cannot_resolve_to_agentmail_or_pytest_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """Marketing task must not force-match agentmail or pytest-runner; result must be MISSING_SKILL."""
    from src.macro_agents.orchestrator import MacroOrchestrator
    from src.orchestration.kanban import MISSING_SKILL

    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    runs_dir = tmp_path / "runs"

    task_prompt = "3 ganchos de post para lançar um serviço de agentes para PMEs"

    # 1. Orchestrator unit evaluation
    orchestrator = MacroOrchestrator(dry_run=True)
    res = orchestrator.orchestrate(title=task_prompt, body=task_prompt, assignee="cmo")

    assert res.status == "blocked"
    assert res.error == MISSING_SKILL
    assert res.resolved_skill_id is None
    assert res.resolved_skill_id != "agentmail"
    assert res.resolved_skill_id != "pytest-runner"
    assert res.skill_match is not None
    assert res.skill_match.matched is False

    # 2. CLI execution evaluation
    exit_code = run_cli(
        [task_prompt, "--director", "cmo", "--dry-run", "--json"],
        runs_dir=runs_dir,
    )
    assert exit_code == 1

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["outcome"] == "MISSING_SKILL"
    assert data["skill"] is None
    assert data["skill"] != "agentmail"
    assert data["skill"] != "pytest-runner"

    # 3. Output markdown verification
    run_files = list(runs_dir.glob("*.md"))
    assert len(run_files) >= 1
    content = run_files[-1].read_text(encoding="utf-8")
    assert 'outcome: "MISSING_SKILL"' in content
    assert 'skill: "none"' in content
