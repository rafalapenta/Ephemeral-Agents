"""Autonomous task-to-skill distillation engine.

Distills execution logs of successful complex tasks into standard SKILL.md
specifications and registers them in the SQLite `skills_catalog` with
`source="distilled"`.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.database.models import Agent, Base, DirectorSkill, SkillCatalog
from src.router.semantic import DEFAULT_DB_URL

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Generate a clean URL-friendly slug."""
    text = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return slug or "distilled-skill"


def distill_task_to_skill(
    task_log: dict[str, Any],
    *,
    db_url: str | None = None,
    auto_link_director: bool = True,
) -> str:
    """Convert a successful task's log into an Agent Skills standard SKILL.md.

    Persists the distilled skill into the database table `skills_catalog` with
    `source="distilled"` and returns the complete Markdown content string.

    Parameters
    ----------
    task_log:
        Execution log dict containing task metadata, steps, and resolution.
    db_url:
        Database connection URL (defaults to DEFAULT_DB_URL).
    auto_link_director:
        If True and a director is specified, links the skill in director_skills.

    Returns
    -------
    The generated SKILL.md formatted content.
    """
    title = (
        task_log.get("title")
        or task_log.get("task_id")
        or "Procedimento Operacional Destilado"
    )
    desc = (
        task_log.get("description")
        or task_log.get("body")
        or f"Skill gerada automaticamente a partir da tarefa: {title}"
    )
    director = task_log.get("director") or task_log.get("agent_id") or ""
    token_budget = int(task_log.get("token_budget") or 800)

    # Slug ID
    slug_base = _slugify(title)
    if director:
        slug = f"{_slugify(director)}-{slug_base}"[:64]
    else:
        slug = slug_base[:64]

    # Triggers
    triggers = task_log.get("triggers") or []
    if not triggers:
        # Synthesize triggers from title keywords
        words = [w for w in re.findall(r"\w+", title.lower()) if len(w) > 3]
        triggers = words[:4] or [title.lower()]

    # Prerequisites
    prereqs = task_log.get("prerequisites") or task_log.get("deps") or []
    if isinstance(prereqs, str):
        prereqs = [prereqs]
    if not prereqs:
        prereqs = [
            f"Ambiente operacional do Diretor {director or 'responsável'} configurado",
            "Credenciais e permissões de execução ativas",
        ]

    # Steps / Procedure
    raw_steps = (
        task_log.get("steps")
        or task_log.get("actions")
        or task_log.get("solution")
        or task_log.get("reply")
        or []
    )
    steps: list[str] = []
    if isinstance(raw_steps, list):
        for item in raw_steps:
            if isinstance(item, dict):
                step_str = item.get("action") or item.get("step") or str(item)
            else:
                step_str = str(item)
            steps.append(step_str)
    elif isinstance(raw_steps, str):
        steps = [line.strip("- ") for line in raw_steps.splitlines() if line.strip()]

    if not steps:
        steps = [
            f"Analisar o contexto da demanda '{title}'.",
            "Executar as instruções homologadas com validação incremental.",
            "Consolidar a resposta e registrar logs de conformidade.",
        ]

    # Verification criteria
    verification = (
        task_log.get("verification")
        or task_log.get("criteria")
        or task_log.get("tests")
        or []
    )
    if isinstance(verification, str):
        verification = [verification]
    if not verification:
        verification = [
            "Conferir conformidade dos artefatos produzidos",
            "Garantir ausência de erros nos logs de execução",
        ]

    # Assemble Frontmatter
    frontmatter_dict = {
        "name": slug,
        "description": desc,
        "triggers": triggers,
    }
    frontmatter_yaml = yaml.dump(
        frontmatter_dict, sort_keys=False, allow_unicode=True
    ).strip()

    # Assemble Body
    prereqs_md = "\n".join(f"- {p}" for p in prereqs)
    steps_md = "\n".join(f"{idx}. {s}" for idx, s in enumerate(steps, 1))
    verification_md = "\n".join(f"- {v}" for v in verification)

    content_md = (
        f"---\n{frontmatter_yaml}\n---\n"
        f"# {title}\n\n"
        f"## Pré-requisitos\n{prereqs_md}\n\n"
        f"## Procedimento\n{steps_md}\n\n"
        f"## Verificação\n{verification_md}\n"
    )

    # Persist in SQLite skills_catalog with source='distilled'
    db_target = db_url or DEFAULT_DB_URL
    engine = create_engine(db_target)
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as session:
        existing = session.scalar(select(SkillCatalog).where(SkillCatalog.id == slug))
        if existing:
            existing.name = title
            existing.description = desc
            existing.content_md = content_md
            existing.token_budget = token_budget
            existing.source = "distilled"
        else:
            new_skill = SkillCatalog(
                id=slug,
                name=title,
                description=desc,
                content_md=content_md,
                token_budget=token_budget,
                source="distilled",
            )
            session.add(new_skill)
        session.flush()

        if auto_link_director and director:
            agent = session.scalar(select(Agent).where(Agent.agent_id == director))
            if agent:
                existing_link = session.scalar(
                    select(DirectorSkill).where(
                        DirectorSkill.director_id == director,
                        DirectorSkill.skill_id == slug,
                    )
                )
                if not existing_link:
                    session.add(
                        DirectorSkill(
                            director_id=director,
                            skill_id=slug,
                            is_core=False,
                            load_priority=1,
                        )
                    )

        session.commit()

    return content_md
