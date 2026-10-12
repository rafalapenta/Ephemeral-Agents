"""Tests for skills loader, YAML frontmatter validation, quarantine, and semantic resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.catalog.loader import (
    SkillFrontmatter,
    load_skills_from_dir,
    parse_skill_markdown,
)
from src.catalog.skills_engine import (
    DirectorSkillsEngine,
    SkillMatchResult,
    _cosine_similarity,
)
from src.database.models import Agent, Base, DirectorSkill, SkillCatalog


def test_parse_skill_markdown_valid() -> None:
    """Ensure valid frontmatter is properly parsed into SkillFrontmatter."""
    content = """---
name: risk-assessment
description: "Avaliação sistemática de riscos técnicos de infraestrutura e software."
director: vulcan
source: "internal"
license: MIT
status: active
version: "1.0.0"
token_budget: 900
---

# Title
Corpo da skill.
"""
    fm, body = parse_skill_markdown(content)
    assert fm.name == "risk-assessment"
    assert fm.director == "vulcan"
    assert fm.status == "active"
    assert fm.license == "MIT"
    assert fm.token_budget == 900
    assert "Corpo da skill." in body


def test_parse_skill_markdown_missing_delimiters() -> None:
    """Ensure ValueError is raised when frontmatter delimiters are missing."""
    content = """name: no-delimiters
description: "Invalid"
"""
    with pytest.raises(ValueError, match="Missing or malformed YAML frontmatter"):
        parse_skill_markdown(content)


def test_parse_skill_markdown_invalid_yaml() -> None:
    """Ensure ValueError is raised on syntax errors in YAML."""
    content = """---
name: [unclosed list
---
Body
"""
    with pytest.raises(ValueError, match="Invalid YAML"):
        parse_skill_markdown(content)


def test_parse_skill_markdown_missing_required_fields() -> None:
    """Ensure ValidationError is raised when required frontmatter fields are missing."""
    content = """---
name: missing-director
description: "Has name and description but lacks director and license"
---
Body
"""
    with pytest.raises(ValidationError):
        parse_skill_markdown(content)


def test_parse_skill_markdown_invalid_status() -> None:
    """Ensure ValidationError is raised when status is neither active nor quarantined."""
    content = """---
name: bad-status
description: "Testing status validation"
director: vulcan
source: "internal"
license: MIT
status: archived
---
Body
"""
    with pytest.raises(ValidationError):
        parse_skill_markdown(content)


def test_load_skills_from_dir_idempotent(tmp_path: Path) -> None:
    """Ensure loader reads skills, persists to database, and remains idempotent."""
    skills_root = tmp_path / "skills"
    skill1_dir = skills_root / "engineering" / "active-test"
    skill1_dir.mkdir(parents=True)
    (skill1_dir / "SKILL.md").write_text(
        """---
name: active-test
description: "Active test skill description."
director: cto
source: "internal"
license: MIT
status: active
version: "1.0.0"
---
# Active Test
Content
""",
        encoding="utf-8",
    )

    skill2_dir = skills_root / "engineering" / "quarantined-test"
    skill2_dir.mkdir(parents=True)
    (skill2_dir / "SKILL.md").write_text(
        """---
name: quarantined-test
description: "Quarantined test skill description."
director: vulcan
source: "external"
license: MIT
status: quarantined
version: "1.0.0"
---
# Quarantined Test
Content
""",
        encoding="utf-8",
    )

    db_path = tmp_path / "test_catalog.db"
    db_url = f"sqlite:///{db_path}"

    class FakeEmbedder:
        def name(self) -> str:
            return "fake"

        def __call__(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 32 for _ in texts]

    # First load
    res1 = load_skills_from_dir(
        skills_dir=skills_root,
        db_url=db_url,
        chroma_path=tmp_path / "chroma",
        embedder=FakeEmbedder(),
    )
    assert res1["total_discovered"] == 2
    assert res1["new_skills"] == 2

    # Verify DB persistence
    engine = create_engine(db_url)
    with Session(engine) as session:
        s1 = session.scalar(
            select(SkillCatalog).where(SkillCatalog.id == "active-test")
        )
        assert s1 is not None
        assert s1.status == "active"

        s2 = session.scalar(
            select(SkillCatalog).where(SkillCatalog.id == "quarantined-test")
        )
        assert s2 is not None
        assert s2.status == "quarantined"

    # Second load (idempotence verification)
    res2 = load_skills_from_dir(
        skills_dir=skills_root,
        db_url=db_url,
        chroma_path=tmp_path / "chroma",
        embedder=FakeEmbedder(),
    )
    assert res2["new_skills"] == 0
    assert res2["updated_skills"] == 2


def test_quarantined_skill_never_resolved(tmp_path: Path) -> None:
    """Verify that quarantined skills are NEVER retrieved or resolved by the router."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        vulcan = Agent(
            agent_id="vulcan",
            macro_domain="engineering",
            name="Vulcan",
            system_prompt_path="src/bots_config/vulcan/SOUL.md",
            trigger_hooks=["engineering"],
            is_active=True,
        )
        session.add(vulcan)
        session.flush()

        active_skill = SkillCatalog(
            id="active-runner",
            name="Active Runner",
            description="Execução de rotinas ativas de compilação e teste.",
            content_md="Corpo da skill ativa",
            status="active",
            source="internal",
        )
        quarantined_skill = SkillCatalog(
            id="dangerous-tool",
            name="Dangerous Tool",
            description="Ferramenta em quarentena com potencial de risco à segurança.",
            content_md="Corpo perigoso",
            status="quarantined",
            source="external",
        )
        session.add_all([active_skill, quarantined_skill])
        session.flush()

        session.add_all(
            [
                DirectorSkill(director_id="vulcan", skill_id="active-runner"),
                DirectorSkill(director_id="vulcan", skill_id="dangerous-tool"),
            ]
        )
        session.commit()

        # Deterministic embedder that would return 1.0 similarity for the dangerous prompt
        class MockDangerousEmbedder:
            def __call__(self, texts: list[str]) -> list[list[float]]:
                results = []
                for t in texts:
                    if "perigoso" in t.lower() or "dangerous" in t.lower():
                        results.append([1.0, 0.0])
                    else:
                        results.append([0.0, 1.0])
                return results

        skills_engine = DirectorSkillsEngine(
            db_session=session,
            embedder=MockDangerousEmbedder(),
            threshold=0.50,
        )

        # 1. Authorized skills must strictly omit dangerous-tool
        authorized = skills_engine.get_authorized_skills("vulcan")
        auth_ids = [s.id for s in authorized]
        assert "dangerous-tool" not in auth_ids
        assert "active-runner" in auth_ids

        # 2. Resolving with prompt targeting the quarantined skill must never resolve it
        match_result = skills_engine.resolve_skill(
            "vulcan", "executar comando perigoso da dangerous tool"
        )
        assert match_result.matched is False
        assert match_result.skill is None
        assert match_result.candidate_id != "dangerous-tool"


try:
    import sentence_transformers  # noqa: F401

    HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    HAS_SENTENCE_TRANSFORMERS = False


@pytest.fixture
def seeded_skills_db(tmp_path: Path) -> dict[str, Any]:
    """Provide a fresh isolated SQLite DB with real skills seeded from skills/."""
    project_root = Path(__file__).resolve().parent.parent
    db_path = tmp_path / "test_three_tasks.db"
    db_url = f"sqlite:///{db_path}"

    engine = create_engine(db_url)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)

    # Load real skills catalog from skills/ into isolated database
    load_skills_from_dir(
        skills_dir=project_root / "skills",
        db_url=db_url,
        chroma_path=tmp_path / "chroma",
        engine=engine,
    )
    return {"engine": engine, "db_url": db_url, "chroma_path": tmp_path / "chroma"}


def test_three_tasks_resolve_to_correct_skills_above_threshold(
    seeded_skills_db: dict[str, Any],
) -> None:
    """Verify that the 3 tasks in examples/tasks.md resolve to the correct skills > 0.72 using deterministic embedder."""
    engine = seeded_skills_db["engine"]

    class DeterministicThreeTasksEmbedder:
        backend_name = "test_fake"
        model_name = "three-tasks-unit"

        def __call__(self, texts: list[str]) -> list[list[float]]:
            results = []
            for t in texts:
                tl = t.lower()
                if any(
                    k in tl
                    for k in ("risco", "whatsapp", "imobiliária", "risk-assessment")
                ):
                    results.append([1.0, 0.0, 0.0])
                elif any(k in tl for k in ("gancho", "post", "pmes", "post-hooks")):
                    results.append([0.0, 1.0, 0.0])
                elif any(
                    k in tl
                    for k in (
                        "estimativa",
                        "custo",
                        "token",
                        "modelos",
                        "token-cost-estimate",
                    )
                ):
                    results.append([0.0, 0.0, 1.0])
                else:
                    results.append([0.0, 0.0, 0.0])
            return results

    with Session(engine) as session:
        # Check that the 3 skills exist in isolated DB
        s1 = session.scalar(
            select(SkillCatalog).where(SkillCatalog.id == "risk-assessment")
        )
        s2 = session.scalar(select(SkillCatalog).where(SkillCatalog.id == "post-hooks"))
        s3 = session.scalar(
            select(SkillCatalog).where(SkillCatalog.id == "token-cost-estimate")
        )
        assert s1 is not None, "risk-assessment must exist in catalog"
        assert s2 is not None, "post-hooks must exist in catalog"
        assert s3 is not None, "token-cost-estimate must exist in catalog"

        skills_engine = DirectorSkillsEngine(
            db_session=session,
            embedder=DeterministicThreeTasksEmbedder(),
            threshold=0.72,
        )

        task1 = "liste 3 riscos técnicos de um bot de WhatsApp para imobiliária"
        res1: SkillMatchResult = skills_engine.resolve_skill("vulcan", task1)
        assert res1.matched is True
        assert res1.skill is not None
        assert res1.skill.id == "risk-assessment"
        assert res1.score >= 0.72

        task2 = "3 ganchos de post para lançar um serviço de agentes para PMEs"
        res2: SkillMatchResult = skills_engine.resolve_skill("vesper", task2)
        assert res2.matched is True
        assert res2.skill is not None
        assert res2.skill.id == "post-hooks"
        assert res2.score >= 0.72

        task3 = "estimativa de custo mensal para 1000 tarefas com modelos grátis"
        res3: SkillMatchResult = skills_engine.resolve_skill("sterling", task3)
        assert res3.matched is True
        assert res3.skill is not None
        assert res3.skill.id == "token-cost-estimate"
        assert res3.score >= 0.72


@pytest.mark.skipif(
    not HAS_SENTENCE_TRANSFORMERS,
    reason="sentence-transformers not installed (local embeddings test)",
)
def test_three_tasks_resolve_with_sentence_transformers(tmp_path: Path) -> None:
    """Verify that the 3 tasks resolve with sentence-transformers if installed in isolated DB."""
    from src.catalog.embeddings import LocalEmbeddingFunction

    project_root = Path(__file__).resolve().parent.parent
    db_path = tmp_path / "test_local_embed.db"
    db_url = f"sqlite:///{db_path}"

    engine = create_engine(db_url)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)

    embedder = LocalEmbeddingFunction()
    load_skills_from_dir(
        skills_dir=project_root / "skills",
        db_url=db_url,
        chroma_path=tmp_path / "chroma",
        embedder=embedder,
        engine=engine,
    )

    with Session(engine) as session:
        skills_engine = DirectorSkillsEngine(
            db_session=session,
            embedder=embedder,
            threshold=0.72,
        )

        task1 = "liste 3 riscos técnicos de um bot de WhatsApp para imobiliária"
        res1 = skills_engine.resolve_skill("vulcan", task1)
        assert res1.matched is True
        assert res1.skill is not None
        assert res1.skill.id == "risk-assessment"
        assert res1.score >= 0.72

        task2 = "3 ganchos de post para lançar um serviço de agentes para PMEs"
        res2 = skills_engine.resolve_skill("vesper", task2)
        assert res2.matched is True
        assert res2.skill is not None
        assert res2.skill.id == "post-hooks"
        assert res2.score >= 0.72

        task3 = "estimativa de custo mensal para 1000 tarefas com modelos grátis"
        res3 = skills_engine.resolve_skill("sterling", task3)
        assert res3.matched is True
        assert res3.skill is not None
        assert res3.skill.id == "token-cost-estimate"
        assert res3.score >= 0.72
