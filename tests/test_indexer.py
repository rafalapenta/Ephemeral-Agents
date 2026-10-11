from __future__ import annotations

from pathlib import Path

import chromadb
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from src.catalog.indexer import parse_agent_markdown, run_indexing
from src.database.models import Agent


AGENT_MARKDOWN = """---
name: Frontend Developer
description: Expert frontend developer for React, Vue, accessibility, and performance.
color: cyan
emoji: desktop
vibe: Builds responsive web apps with pixel-perfect precision.
---

# Frontend Developer Agent Personality

## Your Identity & Memory
- **Role**: Modern web application and UI implementation specialist
- **Personality**: Detail-oriented and performance-focused

## Your Core Mission
- Build responsive, accessible web applications.
"""


def _write_agent(source_root: Path) -> Path:
    agent_path = source_root / "engineering" / "engineering-frontend-developer.md"
    agent_path.parent.mkdir(parents=True)
    agent_path.write_text(AGENT_MARKDOWN, encoding="utf-8")
    return agent_path


def test_parse_real_agency_agent_shape_and_dry_run_does_not_write(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "agency-agents"
    agent_path = _write_agent(source_root)

    agent = parse_agent_markdown(agent_path, source_root)

    assert agent.agent_id == "engineering-frontend-developer"
    assert agent.macro_domain == "engineering"
    assert agent.squad is None
    assert agent.name == "Frontend Developer"
    assert agent.system_prompt_path == "engineering/engineering-frontend-developer.md"
    assert agent.trigger_hooks == [
        "Expert frontend developer for React, Vue, accessibility, and performance.",
        "Builds responsive web apps with pixel-perfect precision.",
        "Modern web application and UI implementation specialist",
    ]

    database_path = tmp_path / "catalog.db"
    chroma_path = tmp_path / "chroma"
    report = run_indexing(
        source_root=source_root,
        database_url=f"sqlite:///{database_path.as_posix()}",
        chroma_path=chroma_path,
        dry_run=True,
        reindex=False,
    )

    assert report.discovered == 1
    assert report.valid == 1
    assert report.invalid == 0
    assert report.written == 0
    assert not database_path.exists()
    assert not chroma_path.exists()


def test_reindex_writes_sqlite_and_cosine_chroma_idempotently(tmp_path: Path) -> None:
    source_root = tmp_path / "agency-agents"
    _write_agent(source_root)
    database_path = tmp_path / "catalog.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    chroma_path = tmp_path / "chroma"

    first = run_indexing(
        source_root=source_root,
        database_url=database_url,
        chroma_path=chroma_path,
        dry_run=False,
        reindex=True,
    )

    assert first.written == 1
    assert database_path.exists()
    assert chroma_path.exists()

    engine = create_engine(database_url)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Agent)) == 1
        stored = session.get(Agent, "engineering-frontend-developer")
        assert stored is not None
        assert stored.trigger_hooks[0].startswith("Expert frontend developer")

    client = chromadb.PersistentClient(path=str(chroma_path))
    collection = client.get_collection("agency_agents")
    assert collection.count() == 1
    assert collection.metadata["hnsw:space"] == "cosine"

    second = run_indexing(
        source_root=source_root,
        database_url=database_url,
        chroma_path=chroma_path,
        dry_run=False,
        reindex=True,
    )
    assert second.written == 1
    assert client.get_collection("agency_agents").count() == 1
