"""Unit tests for DirectorSkillsEngine department isolation and adherence scoring."""

from __future__ import annotations

import chromadb
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.catalog.skills_engine import DirectorSkillsEngine, SkillMatchResult
from src.database.models import Agent, Base, DirectorSkill, SkillCatalog


@pytest.fixture
def db_session() -> Session:
    """Create an in-memory SQLite database populated with Vulcan and Sterling test data."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        # Create Directors
        vulcan = Agent(
            agent_id="vulcan",
            macro_domain="engineering",
            name="Vulcan",
            system_prompt_path="src/bots_config/vulcan/SOUL.md",
            trigger_hooks=["software engineering", "devops", "testing"],
            is_active=True,
        )
        sterling = Agent(
            agent_id="sterling",
            macro_domain="business",
            name="Sterling",
            system_prompt_path="src/bots_config/sterling/SOUL.md",
            trigger_hooks=["finance", "costs", "contracts"],
            is_active=True,
        )
        session.add_all([vulcan, sterling])
        session.flush()

        # Vulcan Skills (Engineering)
        pytest_skill = SkillCatalog(
            id="pytest-runner",
            name="Pytest Runner",
            description="Execução autônoma de suíte de testes unitários e de integração com pytest.",
            content_md="# Pytest Runner\nExecuta testes unitários com pytest e cobertura de código.",
            token_budget=600,
        )
        arch_skill = SkillCatalog(
            id="clean-architecture",
            name="Clean Architecture Validator",
            description="Estruturação por camadas, dependency rule e desacoplamento de módulos.",
            content_md="# Clean Architecture\nValidação de dependências e regras de arquitetura limpa.",
            token_budget=600,
        )

        # Sterling Skills (Finance / Business)
        runway_skill = SkillCatalog(
            id="runway-forecasting",
            name="Runway Forecasting",
            description="Projeção de fluxo de caixa, queima de capital e simulações de runway financeiro.",
            content_md="# Runway Forecasting\nModelagem de queima financeira e runway em semanas.",
            token_budget=500,
        )
        token_cost_skill = SkillCatalog(
            id="token-cost-calculator",
            name="Token Cost Calculator",
            description="Rastreamento do consumo de tokens e cálculo de custo unitário por invocação.",
            content_md="# Token Cost Calculator\nCálculo de custo em USD por chamada de modelo LLM.",
            token_budget=450,
        )

        session.add_all([pytest_skill, arch_skill, runway_skill, token_cost_skill])
        session.flush()

        # Associations
        session.add_all([
            DirectorSkill(director_id="vulcan", skill_id="pytest-runner", is_core=True, load_priority=1),
            DirectorSkill(director_id="vulcan", skill_id="clean-architecture", is_core=True, load_priority=2),
            DirectorSkill(director_id="sterling", skill_id="runway-forecasting", is_core=True, load_priority=1),
            DirectorSkill(director_id="sterling", skill_id="token-cost-calculator", is_core=True, load_priority=2),
        ])
        session.commit()

        yield session


def test_get_authorized_skills_isolation(db_session: Session) -> None:
    """Verify that get_authorized_skills returns only skills linked to the specific director."""
    engine = DirectorSkillsEngine(db_session=db_session, threshold=0.72)

    vulcan_skills = engine.get_authorized_skills("vulcan")
    assert len(vulcan_skills) == 2
    assert [s.id for s in vulcan_skills] == ["pytest-runner", "clean-architecture"]

    sterling_skills = engine.get_authorized_skills("sterling")
    assert len(sterling_skills) == 2
    assert [s.id for s in sterling_skills] == ["runway-forecasting", "token-cost-calculator"]

    unknown_skills = engine.get_authorized_skills("unknown")
    assert unknown_skills == []


def test_success_technical_command_vulcan_matches_above_threshold(db_session: Session) -> None:
    """Test success scenario: Vulcan's technical command with high similarity (> 0.72) returns match."""
    engine = DirectorSkillsEngine(db_session=db_session, threshold=0.72)

    prompt = "Executar suíte de testes unitários e de integração com pytest"
    result: SkillMatchResult = engine.resolve_skill("vulcan", prompt)

    assert result.matched is True
    assert result.score >= 0.72
    assert result.skill is not None
    assert result.skill.id == "pytest-runner"
    assert result.candidate_id == "pytest-runner"
    assert "Matched authorized skill 'pytest-runner'" in result.reason


def test_department_isolation_vulcan_cannot_match_sterling_skills(db_session: Session) -> None:
    """Test department scoping: Vulcan cannot retrieve a skill exclusive to Sterling even with financial terms."""
    engine = DirectorSkillsEngine(db_session=db_session, threshold=0.72)

    financial_prompt = "Projeção de fluxo de caixa, queima financeira e simulações de runway"

    # Querying as Vulcan
    vulcan_result: SkillMatchResult = engine.resolve_skill("vulcan", financial_prompt)

    # Vulcan must NOT match
    assert vulcan_result.matched is False
    assert vulcan_result.skill is None
    # Candidate must NOT be any of Sterling's skills
    assert vulcan_result.candidate_id != "runway-forecasting"
    assert vulcan_result.candidate_id != "token-cost-calculator"

    # Querying the exact same prompt as Sterling MUST succeed
    sterling_result: SkillMatchResult = engine.resolve_skill("sterling", financial_prompt)
    assert sterling_result.matched is True
    assert sterling_result.score >= 0.72
    assert sterling_result.skill is not None
    assert sterling_result.skill.id == "runway-forecasting"


def test_sub_adherence_vague_or_unknown_prompt_returns_not_matched(db_session: Session) -> None:
    """Test sub-adherence scenario: vague or unknown prompt returns matched=False with candidate details."""
    engine = DirectorSkillsEngine(db_session=db_session, threshold=0.72)

    vague_prompt = "Olá, tudo bem? Alguma novidade ou atualização hoje?"
    result: SkillMatchResult = engine.resolve_skill("vulcan", vague_prompt)

    assert result.matched is False
    assert result.skill is None
    assert result.candidate_id is not None  # Highest scoring candidate recorded
    assert result.score < 0.72
    assert f"below threshold ({engine.threshold:.2f})" in result.reason


def test_empty_prompt_and_unknown_director(db_session: Session) -> None:
    """Test edge cases with empty prompt and unknown director."""
    engine = DirectorSkillsEngine(db_session=db_session, threshold=0.72)

    empty_res = engine.resolve_skill("vulcan", "   ")
    assert empty_res.matched is False
    assert empty_res.score == 0.0
    assert empty_res.skill is None

    unknown_res = engine.resolve_skill("unknown_director", "Executar pytest")
    assert unknown_res.matched is False
    assert unknown_res.score == 0.0
    assert unknown_res.skill is None
    assert "No authorized skills found" in unknown_res.reason


def test_chromadb_client_integration(db_session: Session) -> None:
    """Test DirectorSkillsEngine operating with an in-memory ChromaDB client."""
    chroma_client = chromadb.EphemeralClient()
    engine = DirectorSkillsEngine(
        db_session=db_session,
        chroma_client=chroma_client,
        threshold=0.72,
        collection_name="test_director_skills",
    )

    prompt = "Executar testes unitários com pytest"
    result = engine.resolve_skill("vulcan", prompt)

    assert result.matched is True
    assert result.score >= 0.72
    assert result.skill is not None
    assert result.skill.id == "pytest-runner"
