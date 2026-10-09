"""Deterministic regression evaluation suite for self-improvement mechanisms.

Verifies:
1. SOUL.md hot-patches preserve YAML frontmatter schema integrity.
2. Guardrails under 'Limites Invioláveis' are appended idempotently without corrupting workflows.
3. Non-restriction human feedback is ignored gracefully.
4. Task-to-skill distillation produces valid SKILL.md specs and persists with source='distilled'.
5. Post-task telemetry outputs valid audit records in journal.jsonl.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.database.models import Base, SkillCatalog
from src.evolution.distiller import distill_task_to_skill
from src.evolution.hotpatch import (
    apply_human_correction,
    is_rule_or_permission_restriction,
)
from src.macro_agents.orchestrator import _post_task_telemetry
from src.orchestration.kanban import KanbanTask


@pytest.fixture
def temp_soul_setup(tmp_path: Path) -> tuple[Path, str]:
    """Setup a mock bot directory with a valid SOUL.md."""
    director_dir = tmp_path / "atlas"
    director_dir.mkdir(parents=True)
    soul_file = director_dir / "SOUL.md"

    initial_content = """---
name: atlas
role: Chief Executive Officer & Strategic Orchestrator
domain: Executive Strategy, Resource Allocation
model_preference: claude-3-5-sonnet
description: Atlas - Strategic Orchestrator
---
# Identidade & Missão
Você é Atlas, o Diretor Executivo da AGency.

# Postura & Tom
- Sintético e focado em alavancagem.

# Limites Invioláveis (Hard Guardrails)
- NUNCA execute tarefas especializadas de um diretor se ele estiver disponível.
- NUNCA repasse contexto bruto: comprima sempre antes do handoff.

# Workflows Operacionais
## Decomposição Estratégica:
- Entrada: Demanda do usuário.
- Saída: Cartões estruturados no Kanban.
"""
    soul_file.write_text(initial_content, encoding="utf-8")
    return tmp_path, "atlas"


def test_soul_hotpatch_preserves_yaml_frontmatter(temp_soul_setup: tuple[Path, str]) -> None:
    """Ensure hot-patching SOUL.md does not corrupt or modify the YAML frontmatter schema."""
    config_dir, director = temp_soul_setup
    soul_path = config_dir / director / "SOUL.md"

    correction = "NUNCA aprovar deploys sem validação humana e revisão dupla de segurança"
    result = apply_human_correction(
        correction_text=correction,
        target_director=director,
        config_dir=config_dir,
        auto_commit=False,
    )

    assert result["patched"] is True
    assert result["director"] == "atlas"

    # Verify YAML frontmatter can be cleanly parsed
    updated_content = soul_path.read_text(encoding="utf-8")
    assert updated_content.startswith("---")
    parts = updated_content.split("---", 2)
    assert len(parts) >= 3

    meta = yaml.safe_load(parts[1])
    assert meta["name"] == "atlas"
    assert meta["role"] == "Chief Executive Officer & Strategic Orchestrator"
    assert meta["domain"] == "Executive Strategy, Resource Allocation"
    assert meta["model_preference"] == "claude-3-5-sonnet"

    # Verify rule was added to Limites Invioláveis
    assert correction in updated_content
    assert "# Limites Invioláveis" in updated_content
    # Verify downstream sections are preserved intact
    assert "# Workflows Operacionais" in updated_content
    assert "Decomposição Estratégica:" in updated_content


def test_soul_hotpatch_idempotency(temp_soul_setup: tuple[Path, str]) -> None:
    """Ensure applying the same rule multiple times does not duplicate entries."""
    config_dir, director = temp_soul_setup
    soul_path = config_dir / director / "SOUL.md"

    correction = "NUNCA permitir execução direta de comandos SQL destrutivos sem aprovação"

    first_res = apply_human_correction(
        correction_text=correction,
        target_director=director,
        config_dir=config_dir,
        auto_commit=False,
    )
    assert first_res["patched"] is True

    # Second application should detect existing rule
    second_res = apply_human_correction(
        correction_text=correction,
        target_director=director,
        config_dir=config_dir,
        auto_commit=False,
    )
    assert second_res["patched"] is False
    assert "already present" in second_res["reason"].lower()

    # Rule must appear exactly once in the file
    content = soul_path.read_text(encoding="utf-8")
    assert content.count(correction) == 1


def test_soul_hotpatch_rejects_non_restrictions(temp_soul_setup: tuple[Path, str]) -> None:
    """Verify informative feedback without rule restrictions is not patched into SOUL.md."""
    config_dir, director = temp_soul_setup
    soul_path = config_dir / director / "SOUL.md"
    content_before = soul_path.read_text(encoding="utf-8")

    positive_feedback = "Parabéns, o relatório gerado na sprint passada foi muito claro e objetivo."
    res = apply_human_correction(
        correction_text=positive_feedback,
        target_director=director,
        config_dir=config_dir,
        auto_commit=False,
    )

    assert res["patched"] is False
    assert "not represent a rule" in res["reason"].lower()
    # Content must remain identical
    assert soul_path.read_text(encoding="utf-8") == content_before


def test_restriction_keyword_detector() -> None:
    """Test the rule restriction detector on various phrasing samples."""
    assert is_rule_or_permission_restriction("Nunca execute comandos de drop database") is True
    assert is_rule_or_permission_restriction("É proibido acessar variáveis de ambiente sem token") is True
    assert is_rule_or_permission_restriction("Must not execute shell commands without review") is True
    assert is_rule_or_permission_restriction("Limite de requisições por segundo deve ser respeitado") is True
    assert is_rule_or_permission_restriction("Apenas use a porta 443 para conexões externas") is True
    assert is_rule_or_permission_restriction("Excelente trabalho na documentação da API") is False


def test_distill_task_to_skill_contract(tmp_path: Path) -> None:
    """Verify task-to-skill distillation produces a valid SKILL.md and persists to DB."""
    test_db = f"sqlite:///{tmp_path / 'skills_eval.db'}"

    task_log = {
        "task_id": "task-ci-gcp-setup",
        "title": "GCP Cloud Run Deploy Workflow",
        "description": "Procedimento automatizado para build e deploy contínuo no Google Cloud Run.",
        "director": "vulcan",
        "triggers": ["deploy", "cloud run", "gcp ci/cd"],
        "prerequisites": [
            "gcloud CLI autenticado",
            "Artifact Registry configurado",
        ],
        "steps": [
            "Gerar imagem Docker com multi-stage build.",
            "Efetuar push para o Artifact Registry regional.",
            "Acionar gcloud run deploy com variáveis de ambiente injetadas.",
        ],
        "verification": [
            "Checar healthcheck HTTP 200 no endpoint de produção.",
            "Validar logs de inicialização sem erros críticos.",
        ],
        "token_budget": 950,
    }

    markdown_output = distill_task_to_skill(task_log, db_url=test_db, auto_link_director=False)

    # 1. Frontmatter contract validation
    assert markdown_output.startswith("---")
    parts = markdown_output.split("---", 2)
    assert len(parts) >= 3

    meta = yaml.safe_load(parts[1])
    assert meta["name"] == "vulcan-gcp-cloud-run-deploy-workflow"
    assert "deploy" in meta["triggers"]
    assert meta["description"] == task_log["description"]

    # 2. Markdown sections validation
    body = parts[2]
    assert "## Pré-requisitos" in body
    assert "gcloud CLI autenticado" in body
    assert "## Procedimento" in body
    assert "1. Gerar imagem Docker com multi-stage build." in body
    assert "## Verificação" in body
    assert "Checar healthcheck HTTP 200" in body

    # 3. Database persistence validation
    engine = create_engine(test_db)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        skill = session.scalar(
            select(SkillCatalog).where(SkillCatalog.id == "vulcan-gcp-cloud-run-deploy-workflow")
        )
        assert skill is not None
        assert skill.name == "GCP Cloud Run Deploy Workflow"
        assert skill.source == "distilled"
        assert skill.token_budget == 950
        assert skill.content_md == markdown_output


def test_post_task_telemetry_audit_logging(tmp_path: Path) -> None:
    """Verify that post-task telemetry writes valid, structured audit records."""
    journal_file = tmp_path / "journal.jsonl"

    mock_task = KanbanTask(
        title="Executar auditoria de segurança em segredos",
        body="Verificar vazamentos no git history",
        assignee="vulcan",
        macro_domain="engineering",
    )

    class MockRoute:
        agent_id = "vulcan"

    class MockResult:
        task_id = mock_task.task_id
        route = MockRoute()
        tokens = 450

    record = _post_task_telemetry(
        mock_task,
        MockResult(),
        latency_ms=125.4,
        journal_path=journal_file,
    )

    # Validate output dictionary
    assert record["task_id"] == mock_task.task_id
    assert record["director"] == "vulcan"
    assert record["tokens"] == 450
    assert record["latency_ms"] == 125.4
    assert record["human_corrected"] is False
    assert isinstance(record["timestamp"], float)

    # Validate physical journal file
    assert journal_file.exists()
    lines = journal_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    stored = json.loads(lines[0])
    assert stored["task_id"] == mock_task.task_id
    assert stored["director"] == "vulcan"
    assert stored["tokens"] == 450
    assert stored["latency_ms"] == 125.4
    assert stored["human_corrected"] is False
