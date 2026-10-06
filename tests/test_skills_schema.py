"""Test suite for relational skills catalog models, schemas, and seeding."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from scripts.seed_skills_catalog import seed_skills_catalog
from src.database.models import Agent, Base, DirectorSkill, SkillCatalog
from src.database.schemas import DirectorSkillLink, SkillCreate, SkillRead


@pytest.fixture
def db_session() -> Session:
    """Provide a fresh in-memory SQLite database session with foreign keys enabled."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        yield session


def _create_agent(session: Session, agent_id: str, name: str, domain: str) -> Agent:
    agent = Agent(
        agent_id=agent_id,
        macro_domain=domain,
        name=name,
        system_prompt_path=f"src/bots_config/{agent_id}/SOUL.md",
        trigger_hooks=[f"{agent_id} trigger"],
        is_active=True,
    )
    session.add(agent)
    session.flush()
    return agent


def test_skill_catalog_and_director_skill_insertion_and_defaults(db_session: Session) -> None:
    """Test inserting SkillCatalog and DirectorSkill and verify default values and relationships."""
    agent = _create_agent(db_session, "vulcan", "Vulcan", "engineering")

    skill = SkillCatalog(
        id="pytest-runner",
        name="Pytest Runner",
        description="Executa testes unitários com pytest.",
        content_md="---\nname: pytest-runner\n---\n# Pytest Runner",
        token_budget=600,
    )
    db_session.add(skill)
    db_session.flush()

    link = DirectorSkill(
        director_id=agent.agent_id,
        skill_id=skill.id,
        is_core=True,
        load_priority=1,
    )
    db_session.add(link)
    db_session.commit()

    stored_skill = db_session.scalar(select(SkillCatalog).where(SkillCatalog.id == "pytest-runner"))
    assert stored_skill is not None
    assert stored_skill.name == "Pytest Runner"
    assert stored_skill.description == "Executa testes unitários com pytest."
    assert stored_skill.source == "local"
    assert stored_skill.token_budget == 600
    assert isinstance(stored_skill.created_at, datetime)

    # Relationships
    assert len(stored_skill.director_links) == 1
    assert stored_skill.director_links[0].director_id == "vulcan"
    assert stored_skill.director_links[0].is_core is True
    assert stored_skill.director_links[0].load_priority == 1

    stored_agent = db_session.scalar(select(Agent).where(Agent.agent_id == "vulcan"))
    assert stored_agent is not None
    assert len(stored_agent.skill_links) == 1
    assert stored_agent.skill_links[0].skill.id == "pytest-runner"


def test_filtered_query_by_director_id(db_session: Session) -> None:
    """Test querying skills filtered by specific director_id."""
    _create_agent(db_session, "vulcan", "Vulcan", "engineering")
    _create_agent(db_session, "aura", "Aura", "product")
    _create_agent(db_session, "atlas", "Atlas", "governance")

    skills = [
        SkillCatalog(
            id="pytest-runner",
            name="Pytest Runner",
            description="Testes com pytest",
            content_md="# Pytest",
        ),
        SkillCatalog(
            id="clean-architecture",
            name="Clean Architecture",
            description="Camadas arquiteturais",
            content_md="# Clean Architecture",
        ),
        SkillCatalog(
            id="pm-product-strategy",
            name="PM Product Strategy",
            description="Estratégia de produto",
            content_md="# PM Strategy",
        ),
        SkillCatalog(
            id="wayfinder",
            name="Wayfinder",
            description="Planejamento de trajetórias",
            content_md="# Wayfinder",
        ),
    ]
    db_session.add_all(skills)
    db_session.flush()

    links = [
        DirectorSkill(director_id="vulcan", skill_id="pytest-runner", is_core=True, load_priority=1),
        DirectorSkill(director_id="vulcan", skill_id="clean-architecture", is_core=True, load_priority=2),
        DirectorSkill(director_id="aura", skill_id="pm-product-strategy", is_core=True, load_priority=1),
        DirectorSkill(director_id="atlas", skill_id="wayfinder", is_core=True, load_priority=1),
    ]
    db_session.add_all(links)
    db_session.commit()

    # Query skills for vulcan
    vulcan_skills = db_session.scalars(
        select(SkillCatalog)
        .join(DirectorSkill, SkillCatalog.id == DirectorSkill.skill_id)
        .where(DirectorSkill.director_id == "vulcan")
        .order_by(DirectorSkill.load_priority)
    ).all()

    assert len(vulcan_skills) == 2
    assert [s.id for s in vulcan_skills] == ["pytest-runner", "clean-architecture"]

    # Query skills for aura
    aura_skills = db_session.scalars(
        select(SkillCatalog)
        .join(DirectorSkill, SkillCatalog.id == DirectorSkill.skill_id)
        .where(DirectorSkill.director_id == "aura")
    ).all()
    assert len(aura_skills) == 1
    assert aura_skills[0].id == "pm-product-strategy"

    # Query non-existent director
    empty_skills = db_session.scalars(
        select(SkillCatalog)
        .join(DirectorSkill, SkillCatalog.id == DirectorSkill.skill_id)
        .where(DirectorSkill.director_id == "unknown_director")
    ).all()
    assert len(empty_skills) == 0


def test_referential_integrity_foreign_key_enforcement(db_session: Session) -> None:
    """Test foreign key constraints and duplicate key prevention."""
    _create_agent(db_session, "vulcan", "Vulcan", "engineering")
    skill = SkillCatalog(
        id="pytest-runner",
        name="Pytest Runner",
        description="Testes",
        content_md="# Pytest",
    )
    db_session.add(skill)
    db_session.commit()

    # Invalid director_id FK failure
    with pytest.raises(IntegrityError):
        db_session.add(
            DirectorSkill(
                director_id="non_existent_director",
                skill_id="pytest-runner",
            )
        )
        db_session.commit()
    db_session.rollback()

    # Invalid skill_id FK failure
    with pytest.raises(IntegrityError):
        db_session.add(
            DirectorSkill(
                director_id="vulcan",
                skill_id="non_existent_skill",
            )
        )
        db_session.commit()
    db_session.rollback()

    # Duplicate primary key failure
    db_session.add(DirectorSkill(director_id="vulcan", skill_id="pytest-runner"))
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.add(DirectorSkill(director_id="vulcan", skill_id="pytest-runner"))
        db_session.commit()
    db_session.rollback()


def test_referential_integrity_cascade_deletion(db_session: Session) -> None:
    """Test CASCADE deletion when deleting SkillCatalog or Agent."""
    agent = _create_agent(db_session, "vulcan", "Vulcan", "engineering")
    skill = SkillCatalog(
        id="pytest-runner",
        name="Pytest Runner",
        description="Testes",
        content_md="# Pytest",
    )
    db_session.add(skill)
    db_session.flush()

    db_session.add(DirectorSkill(director_id="vulcan", skill_id="pytest-runner"))
    db_session.commit()

    # Verify link exists
    assert db_session.scalar(select(DirectorSkill)) is not None

    # Deleting the skill cascades to remove DirectorSkill
    db_session.delete(skill)
    db_session.commit()
    assert db_session.scalar(select(DirectorSkill)) is None

    # Test cascade when Agent is deleted
    skill2 = SkillCatalog(
        id="clean-code",
        name="Clean Code",
        description="Refatoração",
        content_md="# Clean Code",
    )
    db_session.add(skill2)
    db_session.flush()
    db_session.add(DirectorSkill(director_id="vulcan", skill_id="clean-code"))
    db_session.commit()

    assert db_session.scalar(select(DirectorSkill)) is not None
    db_session.delete(agent)
    db_session.commit()
    assert db_session.scalar(select(DirectorSkill)) is None


def test_pydantic_schemas_validation(db_session: Session) -> None:
    """Test Pydantic schemas SkillCreate, SkillRead, and DirectorSkillLink."""
    # SkillCreate valid
    create_schema = SkillCreate(
        id="pytest-runner",
        name="Pytest Runner",
        description="Executa testes",
        content_md="# Pytest",
        token_budget=500,
    )
    assert create_schema.id == "pytest-runner"
    assert create_schema.source == "local"
    assert create_schema.token_budget == 500

    # SkillCreate validation failure on empty id
    with pytest.raises(ValidationError):
        SkillCreate(
            id="",
            name="Pytest Runner",
            content_md="# Content",
        )

    # SkillCreate validation failure on non-positive token_budget
    with pytest.raises(ValidationError):
        SkillCreate(
            id="test-skill",
            name="Test",
            content_md="# Content",
            token_budget=0,
        )

    # DirectorSkillLink valid
    link_schema = DirectorSkillLink(
        director_id="vulcan",
        skill_id="pytest-runner",
        is_core=True,
        load_priority=1,
    )
    assert link_schema.director_id == "vulcan"
    assert link_schema.is_core is True

    with pytest.raises(ValidationError):
        DirectorSkillLink(director_id="", skill_id="pytest-runner")

    # SkillRead from ORM
    _create_agent(db_session, "vulcan", "Vulcan", "engineering")
    now = datetime.now(UTC)
    skill_orm = SkillCatalog(
        id="pytest-runner",
        name="Pytest Runner",
        description="Executa testes",
        content_md="# Pytest",
        token_budget=500,
        source="local",
        created_at=now,
    )
    db_session.add(skill_orm)
    db_session.commit()

    read_schema = SkillRead.model_validate(skill_orm)
    assert read_schema.id == "pytest-runner"
    assert read_schema.name == "Pytest Runner"
    assert isinstance(read_schema.created_at, datetime)
    assert read_schema.created_at.replace(tzinfo=UTC) == now

    # DirectorSkillLink from ORM
    link_orm = DirectorSkill(
        director_id="vulcan",
        skill_id="pytest-runner",
        is_core=True,
        load_priority=1,
    )
    db_session.add(link_orm)
    db_session.commit()

    link_from_orm = DirectorSkillLink.model_validate(link_orm)
    assert link_from_orm.director_id == "vulcan"
    assert link_from_orm.skill_id == "pytest-runner"
    assert link_from_orm.is_core is True


def test_seed_skills_catalog_populates_all_six_directors() -> None:
    """Test that seed_skills_catalog populates skills and associations for all 6 directors."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    result = seed_skills_catalog(engine=engine)
    assert result["total_catalog_items"] >= 30
    assert result["skills_seeded"] == result["total_catalog_items"]
    assert result["links_seeded"] == result["total_catalog_items"]

    with Session(engine) as session:
        directors = ["vulcan", "sterling", "aura", "vesper", "lyra", "atlas"]
        for director_id in directors:
            agent = session.scalar(select(Agent).where(Agent.agent_id == director_id))
            assert agent is not None, f"Director {director_id} not found in agents table"

            links = session.scalars(
                select(DirectorSkill).where(DirectorSkill.director_id == director_id)
            ).all()
            assert len(links) >= 5, f"Director {director_id} should have at least 5 skills"

        # Verify idempotency
        second_run = seed_skills_catalog(engine=engine)
        assert second_run["skills_seeded"] == 0
        assert second_run["links_seeded"] == 0


def test_seed_skills_catalog_with_custom_json(tmp_path: Path) -> None:
    """Test seed_skills_catalog loading ephemeral tools from a custom JSON file."""
    custom_json = tmp_path / "ephemeral_tools.json"
    custom_json.write_text(
        json.dumps({
            "version": "1.0.0",
            "ephemeral_tools": [
                {
                    "tool_id": "custom-vulcan-profiler",
                    "name": "Custom Vulcan Profiler",
                    "description": "Profilagem avançada de CPU e memória.",
                    "target_director": "vulcan",
                    "trigger_keywords": ["profiler", "cpu", "memory"],
                    "budget_tokens": 420,
                }
            ],
        }),
        encoding="utf-8",
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    result = seed_skills_catalog(engine=engine, catalog_path=custom_json)
    assert result["skills_seeded"] > 0

    with Session(engine) as session:
        custom_skill = session.scalar(
            select(SkillCatalog).where(SkillCatalog.id == "custom-vulcan-profiler")
        )
        assert custom_skill is not None
        assert custom_skill.name == "Custom Vulcan Profiler"
        assert custom_skill.token_budget == 420
        assert custom_skill.source == "catalog_json"

        link = session.scalar(
            select(DirectorSkill).where(
                DirectorSkill.director_id == "vulcan",
                DirectorSkill.skill_id == "custom-vulcan-profiler",
            )
        )
        assert link is not None
        assert link.is_core is False
