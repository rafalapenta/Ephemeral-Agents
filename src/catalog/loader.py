"""Skills catalog loader with YAML frontmatter validation and Chroma indexing."""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from src.database.models import Agent, Base, DirectorSkill, SkillCatalog

DIRECTOR_ALIASES: dict[str, str] = {
    "cto": "vulcan",
    "cmo": "vesper",
    "cfo": "sterling",
    "cpo": "aura",
    "cos": "atlas",
    "ceo": "atlas",
    "cro": "lyra",
    "research": "lyra",
}

DEFAULT_DIRECTORS: dict[str, dict[str, Any]] = {
    "atlas": {
        "name": "Atlas",
        "macro_domain": "governance",
        "system_prompt_path": "src/bots_config/atlas/SOUL.md",
        "trigger_hooks": ["governance", "orchestration", "strategy", "ceo"],
    },
    "vulcan": {
        "name": "Vulcan",
        "macro_domain": "engineering",
        "system_prompt_path": "src/bots_config/vulcan/SOUL.md",
        "trigger_hooks": [
            "software engineering",
            "devops",
            "infrastructure",
            "security",
        ],
    },
    "aura": {
        "name": "Aura",
        "macro_domain": "product",
        "system_prompt_path": "src/bots_config/aura/SOUL.md",
        "trigger_hooks": ["product", "design", "ux", "ui"],
    },
    "vesper": {
        "name": "Vesper",
        "macro_domain": "growth",
        "system_prompt_path": "src/bots_config/vesper/SOUL.md",
        "trigger_hooks": ["growth", "sales", "marketing", "market intelligence"],
    },
    "sterling": {
        "name": "Sterling",
        "macro_domain": "business",
        "system_prompt_path": "src/bots_config/sterling/SOUL.md",
        "trigger_hooks": ["finance", "unit economics", "contracts", "operations"],
    },
    "lyra": {
        "name": "Lyra",
        "macro_domain": "research",
        "system_prompt_path": "src/bots_config/lyra/SOUL.md",
        "trigger_hooks": ["research", "spatial data", "analysis", "experiments"],
    },
}


class SkillFrontmatter(BaseModel):
    """Pydantic schema enforcing required frontmatter on SKILL.md files."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=5)
    director: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1)
    license: str = Field(..., min_length=1)
    status: Literal["active", "quarantined"] = "active"
    version: str = Field(default="1.0.0", min_length=1)
    token_budget: int = Field(default=800, ge=100)
    notes: str | None = None


def normalize_director_id(director: str) -> str:
    """Normalize director name or alias to canonical director agent id."""
    raw = director.strip().lower()
    return DIRECTOR_ALIASES.get(raw, raw)


def parse_skill_markdown(content: str) -> tuple[SkillFrontmatter, str]:
    """Parse frontmatter and markdown body from SKILL.md content string.

    Raises:
        ValueError: If frontmatter is missing or invalid.
        ValidationError: If frontmatter fails Pydantic validation.
    """
    pattern = r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$"
    match = re.match(pattern, content.strip(), re.DOTALL)
    if not match:
        raise ValueError(
            "Missing or malformed YAML frontmatter (expected --- delimiters)"
        )

    raw_yaml, body = match.group(1), match.group(2)
    try:
        data = yaml.safe_load(raw_yaml)
    except Exception as exc:
        raise ValueError(f"Invalid YAML in frontmatter: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Frontmatter must be a valid YAML mapping")

    frontmatter = SkillFrontmatter.model_validate(data)
    return frontmatter, body.strip()


def parse_skill_file(file_path: Path | str) -> tuple[SkillFrontmatter, str]:
    """Read and parse a SKILL.md file."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Skill file not found: {path}")
    raw_content = path.read_text(encoding="utf-8")
    return parse_skill_markdown(raw_content)


def ensure_sqlite_schema(engine: Any) -> None:
    """Apply backwards-compatible column additions for SQLite."""
    with engine.connect() as conn:
        try:
            columns_info = conn.execute(
                text("PRAGMA table_info(skills_catalog)")
            ).fetchall()
            col_names = {row[1] for row in columns_info}
            if "status" not in col_names:
                conn.execute(
                    text(
                        "ALTER TABLE skills_catalog ADD COLUMN status VARCHAR DEFAULT 'active'"
                    )
                )
            if "license" not in col_names:
                conn.execute(
                    text(
                        "ALTER TABLE skills_catalog ADD COLUMN license VARCHAR DEFAULT NULL"
                    )
                )
            if "version" not in col_names:
                conn.execute(
                    text(
                        "ALTER TABLE skills_catalog ADD COLUMN version VARCHAR DEFAULT '1.0.0'"
                    )
                )
            conn.commit()
        except Exception:
            pass


def ensure_directors_exist(session: Session) -> None:
    """Ensure standard C-Level directors exist in the agents table."""
    for agent_id, meta in DEFAULT_DIRECTORS.items():
        existing = session.scalar(select(Agent).where(Agent.agent_id == agent_id))
        if not existing:
            new_agent = Agent(
                agent_id=agent_id,
                macro_domain=meta["macro_domain"],
                name=meta["name"],
                system_prompt_path=meta["system_prompt_path"],
                trigger_hooks=meta["trigger_hooks"],
                is_active=True,
            )
            session.add(new_agent)
    session.flush()


def load_skills_from_dir(
    skills_dir: Path | str,
    db_url: str | None = None,
    chroma_path: Path | str | None = None,
    embedder: Any = None,
    engine: Any = None,
) -> dict[str, Any]:
    """Scan directory recursively for SKILL.md files, validate, persist in DB, and index in Chroma.

    Idempotent: updates existing skills and re-indexes embeddings.
    Quarantined skills are indexed into Chroma and stored in DB, but never resolved by the router.
    """
    root_path = Path(skills_dir).resolve()
    if not root_path.is_dir():
        raise FileNotFoundError(f"Skills directory does not exist: {root_path}")

    # 1. Discover all SKILL.md files
    skill_files = sorted(root_path.rglob("SKILL.md"))
    if not skill_files:
        skill_files = sorted(root_path.glob("*/SKILL.md"))

    # 2. Setup DB engine
    if engine is None:
        resolved_db_url = db_url or os.getenv(
            "DATABASE_URL", f"sqlite:///{Path.cwd() / 'agency_agents.db'}"
        )
        engine = create_engine(resolved_db_url)
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    ensure_sqlite_schema(engine)

    parsed_skills: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for file_path in skill_files:
        skill_id = file_path.parent.name
        try:
            fm, body = parse_skill_file(file_path)
            canonical_director = normalize_director_id(fm.director)
            parsed_skills.append(
                {
                    "id": skill_id,
                    "frontmatter": fm,
                    "body": body,
                    "director_id": canonical_director,
                    "file_path": str(file_path),
                }
            )
        except (ValueError, ValidationError) as exc:
            errors.append({"file": str(file_path), "error": str(exc)})

    if errors:
        raise ValueError(
            f"Failed to validate {len(errors)} skills:\n"
            + "\n".join(f"- {e['file']}: {e['error']}" for e in errors)
        )

    # 3. Persist in database
    loaded_count = 0
    updated_count = 0
    linked_count = 0
    with Session(engine) as session:
        ensure_directors_exist(session)

        for item in parsed_skills:
            s_id = item["id"]
            fm: SkillFrontmatter = item["frontmatter"]
            body: str = item["body"]
            director_id: str = item["director_id"]

            existing = session.scalar(
                select(SkillCatalog).where(SkillCatalog.id == s_id)
            )
            if existing:
                existing.name = fm.name
                existing.description = fm.description
                existing.content_md = body
                existing.token_budget = fm.token_budget
                existing.source = fm.source
                existing.status = fm.status
                existing.license = fm.license
                existing.version = fm.version
                updated_count += 1
            else:
                new_skill = SkillCatalog(
                    id=s_id,
                    name=fm.name,
                    description=fm.description,
                    content_md=body,
                    token_budget=fm.token_budget,
                    source=fm.source,
                    status=fm.status,
                    license=fm.license,
                    version=fm.version,
                    created_at=datetime.now(UTC),
                )
                session.add(new_skill)
                loaded_count += 1

            session.flush()

            # Ensure DirectorSkill link
            existing_link = session.scalar(
                select(DirectorSkill).where(
                    DirectorSkill.director_id == director_id,
                    DirectorSkill.skill_id == s_id,
                )
            )
            if not existing_link:
                new_link = DirectorSkill(
                    director_id=director_id,
                    skill_id=s_id,
                    is_core=True,
                    load_priority=1,
                )
                session.add(new_link)
                linked_count += 1

        session.commit()

    # 4. Index in Chroma
    chroma_indexed = 0
    try:
        import chromadb
        from src.catalog.embeddings import (
            get_embedding_function,
            save_embedding_metadata,
        )

        resolved_chroma_path = (
            Path(chroma_path) if chroma_path else Path.cwd() / "chroma"
        )
        resolved_chroma_path.mkdir(parents=True, exist_ok=True)

        embedding_fn = embedder or get_embedding_function()
        client = chromadb.PersistentClient(path=str(resolved_chroma_path))
        collection = client.get_or_create_collection(
            name="skills_catalog",
            metadata={"hnsw:space": "cosine"},
        )

        ids = [item["id"] for item in parsed_skills]
        documents = [
            f"{item['frontmatter'].name}. {item['frontmatter'].description}\n{item['body']}"
            for item in parsed_skills
        ]
        metadatas = [
            {
                "director_id": item["director_id"],
                "status": item["frontmatter"].status,
                "name": item["frontmatter"].name,
                "source": item["frontmatter"].source,
                "version": item["frontmatter"].version,
                "license": item["frontmatter"].license,
            }
            for item in parsed_skills
        ]

        if ids:
            embeddings = embedding_fn(documents)
            collection.upsert(
                ids=ids,
                documents=documents,
                embeddings=embeddings,
                metadatas=metadatas,
            )
            chroma_indexed = len(ids)
            try:
                save_embedding_metadata(resolved_chroma_path, embedding_fn)
            except Exception as meta_exc:
                print(f"Notice: embedding metadata save: {meta_exc}", file=sys.stderr)
    except Exception as exc:
        print(f"Warning: Chroma indexing failed ({exc})", file=sys.stderr)

    return {
        "total_discovered": len(skill_files),
        "new_skills": loaded_count,
        "updated_skills": updated_count,
        "new_links": linked_count,
        "chroma_indexed": chroma_indexed,
        "skills": [s["id"] for s in parsed_skills],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load SKILL.md catalog into DB and Chroma"
    )
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=Path("skills"),
        help="Path to root skills directory",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="Database URL",
    )
    parser.add_argument(
        "--chroma-path",
        default=None,
        help="Path to ChromaDB storage directory",
    )
    args = parser.parse_args()

    try:
        res = load_skills_from_dir(
            skills_dir=args.skills_dir,
            db_url=args.db_url,
            chroma_path=args.chroma_path,
        )
        print(
            f"Skills catalog loaded successfully:\n"
            f"  Discovered: {res['total_discovered']}\n"
            f"  Created: {res['new_skills']}\n"
            f"  Updated: {res['updated_skills']}\n"
            f"  Chroma Indexed: {res['chroma_indexed']}\n"
            f"  Skills: {', '.join(res['skills'])}"
        )
        return 0
    except Exception as err:
        print(f"Error loading skills catalog: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
